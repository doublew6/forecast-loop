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


class V2ForecastEvaluationRead(APIModel):
    actual_value: float
    actual_label: Literal["up", "neutral", "down"]
    brier_score: float
    baseline_brier_score: float
    brier_improvement: float
    direction_correct: bool
    evaluated_at: datetime


class V2ForecastRead(APIModel):
    id: str
    run_id: str
    target_id: str
    horizon: Literal["D1", "W1"]
    lane: Literal["formal", "shadow"]
    configured_lane: Literal["formal", "shadow"]
    anchor_date: date
    target_date: date
    probabilities: Probabilities
    baseline_probabilities: Probabilities
    neutral_threshold: float
    rationale: str
    counter_evidence: list[str]
    invalidation_conditions: list[str]
    created_at: datetime
    evaluation: V2ForecastEvaluationRead | None = None


class V2LatestForecastsResponse(APIModel):
    program_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    formal: V2ForecastRead | None = None
    shadow: V2ForecastRead | None = None


class V2RiskDiagnostics(APIModel):
    critique_count: int = Field(ge=0)
    counter_evidence_coverage_rate: float = Field(ge=0, le=1)
    invalidation_coverage_rate: float = Field(ge=0, le=1)
    risk_flag_rate: float = Field(ge=0, le=1)
    evaluated_system_errors: int = Field(ge=0)
    missed_risk_count: int = Field(ge=0)
    missed_risk_rate: float | None = Field(default=None, ge=0, le=1)


class V2ScorecardItem(APIModel):
    agent_id: str
    agent_name: str
    agent_version: str
    model_name: str
    prompt_version: str
    target_id: str
    signal_kind: str
    horizon: str
    sample_size: int = Field(ge=0)
    independent_episodes: int = Field(ge=0)
    average_brier: float | None = None
    baseline_brier: float | None = None
    brier_skill: float | None = None
    classwise_ece: dict[str, float] | None = None
    direction_accuracy: float | None = None
    reasoning_average: float | None = None
    ablation_brier_delta: float | None = None
    risk_diagnostics: V2RiskDiagnostics | None = None
    note: str = ""


class V2ScorecardSection(APIModel):
    axis: Literal[
        "final_system",
        "natural_horizon",
        "d1_impact",
        "reasoning",
        "incremental_value",
    ]
    title: str
    items: list[V2ScorecardItem]


class PremarketHistoryPointRead(APIModel):
    forecast_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    forecast_session: date
    target_session: date
    predicted_direction: Literal["up", "neutral", "down"]
    realized_return: float
    actual_label: Literal["up", "neutral", "down"]
    direction_correct: bool | None
    cumulative_sample_size: int = Field(ge=0)
    cumulative_hits: int = Field(ge=0)
    cumulative_win_rate: float | None = Field(default=None, ge=0, le=1)
    rolling_20_win_rate: float | None = Field(default=None, ge=0, le=1)
    long_only_period_return: float
    long_short_period_return: float
    long_only_cumulative_return: float
    long_short_cumulative_return: float


class V2AgentScorecardsResponse(APIModel):
    program_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    sections: list[V2ScorecardSection]
    premarket_history: list[PremarketHistoryPointRead] = Field(default_factory=list)


class V2ReasoningReviewRead(APIModel):
    id: str
    signal_id: str
    agent_id: str
    target_id: str
    signal_kind: str
    horizon: str
    status: Literal["not_required", "pending", "approved", "rejected"]
    total_score: int = Field(ge=0, le=10)
    human_review_required: bool
    human_review_status: Literal["not_required", "pending", "approved", "rejected"]
    deterministic_checks: dict[str, bool]
    rubric: dict[str, Any]
    created_at: datetime


class V2ReasoningReviewListResponse(APIModel):
    items: list[V2ReasoningReviewRead]
    next_cursor: str | None = None


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


class AgentTraceReferenceRead(APIModel):
    wiki_entry_id: str
    wiki_title: str
    wiki_version: str
    section: str
    content_hash: str
    evidence_item_id: str | None = None
    evidence_content_hash: str | None = None
    source_url: str | None = None
    published_at: datetime | None = None


class AgentTraceSpanRead(APIModel):
    span_id: str
    parent_span_id: str | None
    node_id: str
    name: str
    span_kind: Literal["workflow", "agent", "llm", "validator", "persistence", "external"]
    status: Literal["running", "completed", "failed"]
    started_at: datetime
    completed_at: datetime | None
    duration_ms: float | None
    agent_id: str | None
    agent_version: str | None
    model_name: str | None
    prompt_version: str | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    estimated_cost_usd: float | None
    input_digest: str | None
    output_digest: str | None
    tool_name: str | None = None
    input_summary: str | None = None
    output_summary: str | None = None
    summary: str | None
    error_code: str | None
    error_summary: str | None
    attributes: dict[str, Any]
    references: list[AgentTraceReferenceRead] = Field(default_factory=list)


class AgentTraceArtifactLinkRead(APIModel):
    id: str
    span_id: str | None
    artifact_kind: Literal[
        "signal", "forecast", "evaluation", "reasoning_review", "reflection", "bad_case"
    ]
    artifact_id: str
    relation: Literal["input", "output", "reused", "diagnostic"]
    content_hash: str | None
    created_at: datetime


class AgentTraceRead(APIModel):
    id: str
    workflow_kind: Literal["prediction", "reflection", "agent_eval"]
    subject_id: str
    attempt_number: int
    target_id: str | None
    horizon: str | None
    mode: str
    status: Literal["running", "completed", "failed", "degraded"]
    started_at: datetime
    completed_at: datetime | None
    duration_ms: float | None
    input_hash: str | None
    trace_policy_version: str
    telemetry_complete: bool
    error_code: str | None
    error_summary: str | None
    attributes: dict[str, Any]
    span_count: int
    spans: list[AgentTraceSpanRead] = Field(default_factory=list)
    artifact_links: list[AgentTraceArtifactLinkRead] = Field(default_factory=list)
    external_url: str | None = None
    audit_url: str | None = None
    audit_label: str | None = None


class AgentTraceListResponse(APIModel):
    items: list[AgentTraceRead]
    next_cursor: str | None = None


class AgentObservabilitySummary(APIModel):
    window_hours: int
    total_traces: int
    running_traces: int
    completed_traces: int
    failed_traces: int
    degraded_traces: int
    telemetry_complete_rate: float | None
    completion_rate: float | None
    p95_duration_ms: float | None
    by_workflow_kind: dict[str, int]
    recent: list[AgentTraceRead]
    database_size_bytes: int | None = None
    trace_storage_bytes: int | None = None
    stored_span_count: int = 0
    stored_artifact_link_count: int = 0
    storage_warning_bytes: int
    storage_warning: bool


class AgentEvalV2JobRead(APIModel):
    id: str
    suite_id: str
    suite_version: str
    suite_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_target_id: str
    candidate_target_id: str
    status: Literal["awaiting_draft", "ready_to_finalize", "completed"]
    release_decision: Literal["pending", "pass", "fail", "insufficient_sample"]
    policy_version: str
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    report_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error: str | None = None
    summary: dict[str, Any]
    result_count: Literal[0] = 0
    results: list[Any] = Field(default_factory=list)


class AgentEvalV2JobListResponse(APIModel):
    items: list[AgentEvalV2JobRead]


class AgentEvalSuiteRead(APIModel):
    suite_id: str
    version: str
    title: str
    description: str
    synthetic: bool
    runner_kind: str
    case_count: int
    target_ids: list[str]
    arm_ids: list[str] = Field(default_factory=list)
    content_hash: str
    source: Literal["public", "private"]


class AgentEvalSuiteListResponse(APIModel):
    items: list[AgentEvalSuiteRead]


class AgentEvalExperimentCreate(APIModel):
    suite_id: str
    suite_version: str | None = None
    baseline_target_id: str
    candidate_target_id: str
    source: Literal["public", "private"] = "public"


class AgentEvalResultRead(APIModel):
    id: str
    arm: Literal["baseline", "candidate"]
    case_id: str
    evaluator_id: str
    evaluator_version: str
    metric_kind: str
    score: float | None
    passed: bool | None
    status: Literal["passed", "failed", "not_applicable", "error"]
    label: str | None
    explanation: str
    output_hash: str
    trace_id: str | None
    created_at: datetime


class AgentEvalExperimentRead(APIModel):
    id: str
    suite_id: str
    suite_version: str
    suite_hash: str
    baseline_target_id: str
    baseline_target_hash: str
    candidate_target_id: str
    candidate_target_hash: str
    status: Literal["queued", "running", "completed", "failed"]
    release_decision: Literal["pending", "pass", "fail", "insufficient_sample"]
    policy_version: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    report_hash: str | None
    error: str | None
    summary: dict[str, Any]
    result_count: int
    results: list[AgentEvalResultRead] = Field(default_factory=list)


class AgentEvalExperimentListResponse(APIModel):
    items: list[AgentEvalExperimentRead]


class AgentBadCaseTransitionCreate(APIModel):
    to_status: Literal["triaged", "confirmed", "materialized", "resolved", "rejected"]
    actor: str = Field(min_length=1, max_length=120)
    notes: str = ""
    dataset_id: str | None = Field(default=None, max_length=120)
    dataset_version: str | None = Field(default=None, max_length=32)
    test_case: dict[str, Any] | None = None


class AgentBadCaseEventRead(APIModel):
    id: str
    sequence_number: int
    event_type: str
    from_status: str | None
    to_status: str
    idempotency_key: str
    actor: str
    notes: str
    payload: dict[str, Any]
    previous_event_hash: str | None
    content_hash: str
    occurred_at: datetime


class AgentBadCaseRead(APIModel):
    id: str
    trace_id: str
    span_id: str | None
    eval_result_id: str | None
    workflow_kind: str
    issue_type: str
    severity: Literal["low", "medium", "high", "critical"]
    status: Literal["detected", "triaged", "confirmed", "materialized", "resolved", "rejected"]
    title: str
    summary: str
    expected_behavior: str
    input_hash: str | None
    dedupe_hash: str
    dataset_id: str | None
    dataset_version: str | None
    created_at: datetime
    updated_at: datetime
    events: list[AgentBadCaseEventRead] = Field(default_factory=list)


class AgentBadCaseListResponse(APIModel):
    items: list[AgentBadCaseRead]
