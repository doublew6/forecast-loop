"""Pydantic API and structured-output schemas."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator

from .agent_contracts import AgentSpec
from .domain import (
    AgentSourceType,
    AgentWorkflowRole,
    Direction,
    Horizon,
    predicted_direction,
)


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class Probabilities(APIModel):
    up: float = Field(ge=0, le=1)
    neutral: float = Field(ge=0, le=1)
    down: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_sum(self) -> Probabilities:
        if abs(self.up + self.neutral + self.down - 1.0) > 1e-6:
            raise ValueError("probabilities must sum to one")
        return self

    def as_dict(self) -> dict[str, float]:
        return self.model_dump()


class Citation(APIModel):
    wiki_entry_id: str
    wiki_title: str
    wiki_version: str
    section: str
    quote: str = ""
    wiki_quote: str | None = None
    content_hash: str
    source_urls: list[str] = Field(default_factory=list)
    evidence_item_id: str | None = None
    source_url: str | None = None
    evidence_content_hash: str | None = None
    event_time: datetime | None = None
    published_at: datetime | None = None
    ingested_at: datetime | None = None


class StrategyContext(APIModel):
    market_regime: Literal["risk_on", "balanced", "risk_off"]
    style_bias: Literal["large_cap", "mid_small_cap", "growth", "balanced"]
    relative_rank: int = Field(ge=1, le=100)
    rank_tied: bool
    allocation_score: float = Field(ge=-1, le=1)


class EvidenceItem(APIModel):
    id: str
    title: str
    summary: str
    quote: str = Field(min_length=1)
    source_url: str
    event_time: datetime
    published_at: datetime
    ingested_at: datetime
    entities: list[str] = Field(default_factory=list)
    event_type: str
    content_hash: str = Field(min_length=16)


class MarketDataProvenance(APIModel):
    """Immutable provenance for the market inputs used to derive volatility."""

    trade_date: date
    source_url: str
    source_hash: str = Field(min_length=16)
    observed_at: datetime
    ingested_at: datetime


class TradingCalendarProvenance(APIModel):
    """Source-bound exchange sessions used to freeze D1 and D2 targets."""

    sessions: list[date] = Field(min_length=3, max_length=3)
    source_url: str
    source_hash: str = Field(min_length=16)
    observed_at: datetime
    ingested_at: datetime


class FrozenEvidenceSnapshot(APIModel):
    as_of: datetime
    data_cutoff: datetime
    created_at: datetime
    base_session: date
    trading_calendar: TradingCalendarProvenance
    volatility_20d: dict[str, float]
    market_data: dict[str, MarketDataProvenance] = Field(default_factory=dict)
    target_sessions: list[date] = Field(default_factory=list)
    items: list[EvidenceItem]
    content_hash: str = Field(min_length=16)


class EvaluationRead(APIModel):
    actual_return: float
    label: Direction = Field(validation_alias="actual_label")
    correct: bool
    brier: float = Field(validation_alias="brier_score")
    evaluated_at: datetime
    price_source: str
    observed_at: datetime
    start_trade_date: date
    start_close: float
    start_source_url: str
    start_source_hash: str
    end_trade_date: date
    end_close: float
    end_source_url: str
    end_source_hash: str
    observation_hash: str


class ForecastRead(APIModel):
    id: str
    run_id: str
    index_code: str
    index_name: str
    horizon: Horizon
    base_trade_date: date
    target_date: date
    as_of: datetime
    data_cutoff: datetime
    direction: Direction
    probabilities: Probabilities
    threshold: float
    confidence: float
    rationale: str
    counter_evidence: list[str]
    invalidation_conditions: list[str]
    citations: list[Citation]
    abstain: bool
    model_name: str
    model_version: str
    wiki_version: str
    input_hash: str
    evaluation: EvaluationRead | None = None


class LatestForecastResponse(APIModel):
    run_id: str
    as_of: datetime
    data_cutoff: datetime
    forecasts: list[ForecastRead]


class PredictionPrepareAttemptRead(APIModel):
    attempt_id: str
    base_session: date
    attempted_at: datetime
    status: Literal[
        "prepared",
        "already_prepared",
        "no_open_session",
        "blocked_upstream",
        "failed",
    ]
    state: Literal[
        "pending",
        "stale",
        "holiday",
        "blocked",
        "awaiting",
        "overdue",
        "completed",
    ]
    run_id: str | None = None
    run_status: str | None = None
    snapshot_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error_code: str | None = None
    message: str | None = None
    receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class PredictionDailyStatusRead(APIModel):
    base_session: date
    state: Literal[
        "pending",
        "stale",
        "holiday",
        "blocked",
        "awaiting",
        "overdue",
        "completed",
    ]
    attempted_at: datetime | None = None
    attempt_status: str | None = None
    run_id: str | None = None
    run_status: str | None = None
    message: str


class PredictionStatusResponse(APIModel):
    today: PredictionDailyStatusRead
    latest_completed_run_id: str | None = None
    latest_completed_as_of: datetime | None = None
    latest_completed_data_cutoff: datetime | None = None
    history: list[PredictionPrepareAttemptRead] = Field(default_factory=list)


class AgentOpinionRead(APIModel):
    id: str
    run_id: str
    agent_id: str
    agent_name: str
    role: str
    agent_version: str
    model_name: str
    status: str
    index_code: str
    horizon: Horizon
    target_date: date
    direction: Direction
    probabilities: Probabilities
    summary: str
    evidence: list[str]
    counter_evidence: list[str]
    invalidation_conditions: list[str]
    citations: list[Citation]
    contribution: str
    weight: float
    strategy_context: StrategyContext | None = None


class WorkflowStep(APIModel):
    id: str
    label: str
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None


class WorkflowTaskRead(APIModel):
    id: str
    status: Literal["queued", "running", "retry_wait", "completed", "failed"]
    stage: str
    attempt_count: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    available_at: datetime
    attempt_started_at: datetime | None
    lease_expires_at: datetime | None
    last_error: str | None
    updated_at: datetime


class WorkflowRunRead(APIModel):
    id: str
    as_of: datetime
    data_cutoff: datetime
    status: str
    mode: str
    started_at: datetime
    completed_at: datetime | None
    duration_seconds: float | None
    error: str | None
    data_quality: dict[str, Any]
    workflow_steps: list[WorkflowStep]
    input_hash: str
    forecasts_count: int = Field(ge=0)
    task: WorkflowTaskRead | None = None


class MeetingRead(APIModel):
    run: WorkflowRunRead
    opinions: list[AgentOpinionRead]
    forecasts: list[ForecastRead]
    workflow_steps: list[WorkflowStep]


class AgentRead(APIModel):
    id: str
    name: str
    role: str
    kind: str
    workflow_role: AgentWorkflowRole
    source_type: AgentSourceType
    version: str
    weight: float
    status: str
    spec: AgentSpec = Field(
        description=(
            "Versioned capability and participation contract. Registered source "
            "type is not per-signal provenance, and capabilities do not grant "
            "formal decision authority."
        )
    )


class UserJudgmentCreate(APIModel):
    forecast_id: str = Field(min_length=1, max_length=64)
    direction: Literal["up", "down"]
    confidence: float = Field(ge=0.5, le=1.0, allow_inf_nan=False)
    rationale: str = Field(min_length=20, max_length=4000)
    counter_evidence: str = Field(min_length=10, max_length=2000)
    invalidation_condition: str = Field(min_length=10, max_length=2000)
    blind_attestation: bool = False

    @field_validator(
        "forecast_id",
        "rationale",
        "counter_evidence",
        "invalidation_condition",
        mode="before",
    )
    @classmethod
    def strip_user_judgment_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class UserJudgmentTargetRead(APIModel):
    forecast_id: str
    run_id: str
    mode: Literal["demo", "live"]
    index_code: str
    index_name: str
    horizon: Horizon
    base_trade_date: date
    target_date: date
    as_of: datetime
    data_cutoff: datetime
    submission_deadline: datetime | None
    submission_open: bool
    submission_note: str
    score_eligible_if_blind: bool
    existing_judgment_id: str | None = None


class UserJudgmentEvaluationRead(APIModel):
    actual_return: float
    actual_label: Direction
    sign_correct: bool | None
    material_direction_correct: bool | None
    observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str
    evaluated_at: datetime
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class UserJudgmentRead(APIModel):
    id: str
    actor_id: str
    agent_id: str
    agent_version: str
    forecast_id: str
    run_id: str
    mode: Literal["demo", "live"]
    index_code: str
    index_name: str
    horizon: Horizon
    target_date: date
    direction: Literal["up", "down"]
    confidence: float
    rationale: str
    counter_evidence: str
    invalidation_condition: str
    blind_attestation: bool
    submitted_at: datetime
    submission_deadline: datetime | None
    formal_score_eligible: bool
    run_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    forecast_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    wiki_path: str
    wiki_artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    wiki_url: str
    committee_direction: Direction
    committee_agreement: bool
    evaluation: UserJudgmentEvaluationRead | None = None


class DirectionMetrics(APIModel):
    label: Direction
    predicted: int
    actual: int
    true_positive: int
    precision: float | None
    recall: float | None


class CalibrationPoint(APIModel):
    bucket: str
    predicted: float
    observed: float
    count: int


class ScorecardRead(APIModel):
    agent_id: str
    index_code: str | None = None
    horizon: Horizon | None = None
    sample_size: int
    sample_sufficient: bool
    accuracy: float | None
    sign_sample_size: int = 0
    sign_correct: int = 0
    sign_accuracy: float | None = None
    material_sample_size: int = 0
    material_correct: int = 0
    material_direction_accuracy: float | None = None
    average_brier: float | None
    direction_metrics: list[DirectionMetrics]
    calibration: list[CalibrationPoint] = Field(default_factory=list)
    expected_calibration_error: float | None = None
    agent_version: str
    model_name: str | None = None
    note: str


class WikiSection(APIModel):
    slug: str
    title: str
    excerpt: str


class WikiEntryRead(APIModel):
    id: str
    title: str
    version: str
    updated_at: date | None
    published_at: datetime | None = None
    status: str
    owners: list[str]
    tags: list[str]
    source_urls: list[str]
    sections: list[WikiSection]
    content_hash: str
    referenced_by_count: int = 0
    body: str | None = None


class RunCreate(APIModel):
    as_of: datetime | None = None


class PricePointInput(APIModel):
    trade_date: date
    close: float = Field(gt=0, allow_inf_nan=False)
    source_url: AnyHttpUrl
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class EvaluationInput(APIModel):
    forecast_id: str
    price_source: str = Field(min_length=1, max_length=120)
    observed_at: datetime
    start: PricePointInput
    end: PricePointInput

    @field_validator("observed_at")
    @classmethod
    def observation_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value


class EvaluationRunRequest(APIModel):
    observations: list[EvaluationInput] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def forecast_ids_must_be_unique(self) -> EvaluationRunRequest:
        forecast_ids = [item.forecast_id for item in self.observations]
        if len(forecast_ids) != len(set(forecast_ids)):
            raise ValueError("forecast_id must be unique within an evaluation batch")
        return self


class EvaluationRunResponse(APIModel):
    evaluated: int
    results: list[EvaluationRead]


class MarketSessionSnapshotRead(APIModel):
    id: str
    batch_id: str
    index_code: str
    index_name: str
    target_date: date
    base_trade_date: date
    base_close: float
    target_close: float
    actual_return: float
    amount: float | None
    advancers: int | None
    decliners: int | None
    unchanged: int | None
    limit_down_count: int | None
    breadth_down_ratio: float | None
    sector_contributions: list[dict[str, Any]]
    weight_contributions: list[dict[str, Any]]
    historical_abs_return_percentile: float | None
    history_sample_size: int
    source_url: str
    source_hash: str
    captured_at: datetime
    content_hash: str


class ForecastDiagnosticRead(APIModel):
    id: str
    forecast_id: str
    evaluation_result_id: str
    index_code: str
    index_name: str
    horizon: Horizon
    target_date: date
    predicted_direction: Direction
    actual_return: float
    actual_label: Direction
    threshold: float
    signed_sigma: float
    severity: Literal["noise", "directional", "large", "extreme"]
    systemic_extreme_down: bool
    historical_abs_return_percentile: float | None
    history_sample_size: int
    data_incomplete: bool
    sign_correct: bool | None
    material_direction_correct: bool | None
    brier_score: float
    policy_version: str
    created_at: datetime


class ReflectionOutcomeRead(APIModel):
    forecast_id: str
    index_code: str
    index_name: str
    horizon: Horizon
    target_date: date
    predicted_direction: Direction
    probabilities: Probabilities
    threshold: float
    actual_return: float
    actual_label: Direction
    diagnostic: ForecastDiagnosticRead
    market_snapshot: MarketSessionSnapshotRead | None = None


class ReflectionFindingRead(APIModel):
    id: str
    reflection_run_id: str
    scope_type: Literal["agent", "committee", "market_event"]
    subject_id: str
    index_code: str | None
    horizon: Horizon
    direction_correct: bool | None
    verdict: Literal[
        "right_reason",
        "lucky_correct",
        "wrong",
        "wrong_noise",
        "right_but_noise",
        "not_applicable",
        "unresolved",
    ]
    primary_error_type: Literal[
        "data_coverage_failure",
        "attention_omission",
        "reasoning_or_weighting_failure",
        "transmission_mapping",
        "horizon_timing",
        "post_cutoff_shock",
        "risk_plan_failure",
        "market_noise",
        "unresolved",
    ]
    secondary_error_types: list[str]
    evidence_ids: list[str]
    what_was_right: list[str]
    what_was_wrong: list[str]
    original_evidence_item_ids: list[str]
    missed_evidence_item_ids: list[str]
    source_ids: list[str]
    invalidation_conditions_triggered: list[str]
    availability_class: Literal[
        "available_used",
        "available_missed",
        "coverage_gap_pre_cutoff",
        "post_cutoff_event",
        "after_close_explanation",
        "unresolved",
    ]
    causal_status: Literal["verified", "supported", "hypothesis", "unresolved"]
    counterfactual: dict[str, Any]
    remediation: list[str]
    confidence: float = Field(ge=0, le=1)
    summary: str
    created_at: datetime


class LessonLifecycleEventRead(APIModel):
    id: str
    sequence_number: int
    event_type: Literal[
        "replay_recorded",
        "approved",
        "revalidated",
        "challenged",
        "retired",
        "superseded",
    ]
    from_status: Literal[
        "candidate", "active", "challenged", "retired", "superseded"
    ]
    to_status: Literal[
        "candidate", "active", "challenged", "retired", "superseded"
    ]
    actor: str
    reason: str
    payload_hash: str
    occurred_at: datetime


class LessonProposalRead(APIModel):
    id: str
    reflection_run_id: str
    episode_key: str
    cluster_key: str
    title: str
    summary: str
    status: Literal["candidate", "active", "challenged", "retired", "superseded"]
    proposal_type: str
    evidence_finding_ids: list[str]
    independent_episode_count: int
    replay_target_dates: int
    replay_metrics: dict[str, Any]
    half_life_sessions: int
    created_at: datetime
    reviewed_at: datetime | None
    supersedes_id: str | None
    superseded_by_id: str | None
    replay_batch_count: int
    latest_replay_hash: str | None
    revalidation_due: bool
    revalidation_due_reasons: list[str]
    lifecycle_history: list[LessonLifecycleEventRead]


class ReflectionMetricsRead(APIModel):
    outcome_count: int
    sign_sample_size: int
    sign_correct: int
    sign_accuracy: float | None
    material_sample_size: int
    material_correct: int
    material_direction_accuracy: float | None
    average_brier: float | None


class ReflectionSourceRead(APIModel):
    id: str
    title: str
    summary: str
    source_url: str
    event_time: datetime
    published_at: datetime
    ingested_at: datetime
    source_kind: str
    related_index_codes: list[str]
    time_class: Literal[
        "published_before_cutoff_not_frozen",
        "post_cutoff_preclose",
        "post_close_explanation",
    ]
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReflectionRunRead(APIModel):
    id: str
    source_run_id: str
    source_batch_id: str
    horizon: Horizon
    target_date: date
    schema_version: str
    evaluation_set_hash: str
    status: Literal[
        "awaiting_sources",
        "awaiting_analysis",
        "completed",
        "failed",
        "blocked_upstream",
    ]
    supersedes_id: str | None
    created_at: datetime
    completed_at: datetime | None
    error: str | None
    input_hash: str
    source_snapshot_hash: str | None
    output_hash: str | None
    receipt_hash: str | None
    prediction_cutoff: datetime
    reflection_cutoff: datetime | None
    data_quality: dict[str, Any]
    summary: str
    finding_count: int
    lesson_candidate_count: int
    overall_severity: Literal[
        "noise",
        "directional",
        "large",
        "extreme",
        "systemic_extreme_down",
        "unknown",
    ]
    metrics: ReflectionMetricsRead


class ReflectionDetailRead(ReflectionRunRead):
    outcomes: list[ReflectionOutcomeRead]
    diagnostics: list[ForecastDiagnosticRead]
    findings: list[ReflectionFindingRead]
    decision_chain: list[ReflectionFindingRead]
    source_timeline: list[ReflectionSourceRead]
    lesson_proposals: list[LessonProposalRead]


class ReflectionListResponse(APIModel):
    items: list[ReflectionRunRead]


class LessonListResponse(APIModel):
    items: list[LessonProposalRead]


class ErrorResponse(APIModel):
    detail: str


class AgentDraft(APIModel):
    """Schema used with LangChain ``with_structured_output``."""

    direction: Direction
    probabilities: Probabilities
    summary: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    counter_evidence: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    evidence_item_ids: list[str] = Field(
        default_factory=list,
        description=("Exact frozen evidence IDs used by this opinion. Do not infer or invent IDs."),
    )
    wiki_entry_id: str
    wiki_section: str

    @field_validator("evidence")
    @classmethod
    def no_empty_evidence(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("evidence items may not be empty")
        return value

    @model_validator(mode="after")
    def direction_matches_probabilities(self) -> AgentDraft:
        expected = predicted_direction(self.probabilities.as_dict())
        if self.direction is not expected:
            raise ValueError(
                f"direction must be the stronger up/down probability ({expected.value})"
            )
        if len(self.evidence_item_ids) != len(set(self.evidence_item_ids)):
            raise ValueError("evidence_item_ids must be unique")
        return self


class RunListResponse(APIModel):
    items: list[WorkflowRunRead]


class AgentListResponse(APIModel):
    items: list[AgentRead]


class UserJudgmentTargetListResponse(APIModel):
    items: list[UserJudgmentTargetRead]


class UserJudgmentListResponse(APIModel):
    items: list[UserJudgmentRead]


class WikiListResponse(APIModel):
    items: list[WikiEntryRead]


class HealthRead(APIModel):
    status: Literal["ok"]
    mode: str
    version: str
