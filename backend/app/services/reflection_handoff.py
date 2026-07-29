"""Audited, Codex-first daily reflection file handoff.

The reflection workflow is deliberately separate from prediction.  Deterministic
Python freezes the original run and realized outcome, Codex may propose source
URLs and structured findings through two draft files, and only the finalizer may
append validated reflection facts to the business database.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from ..config import Settings
from ..db import Database
from ..domain import AGENT_BY_ID, INDEXES, Direction, Horizon, multiclass_brier_score
from ..models import (
    AgentOpinion,
    EvaluationBatch,
    Forecast,
    WorkflowRun,
)
from ..serializers import forecast_read
from .reflection import (
    MarketSnapshotFact,
    create_reflection_run,
    due_live_forecasts,
    materialize_evaluation_batch,
    record_blocked_upstream_batch,
    validate_default_reflection_universe,
)
from .reflection_governance import (
    LESSON_HALF_LIFE_SESSIONS,
    LESSON_REVALIDATION_EPISODES,
    approved_reflection_review_count,
    assess_lesson_policy,
    completed_live_target_date_count,
)
from .schema_readiness import require_schema_current
from .snapshot import validate_trusted_source_url

LEGACY_REFLECTION_PROTOCOL_VERSION = "1.0.0"
REFLECTION_PROTOCOL_VERSION = "2.0.0"
REFLECTION_SCHEMA_VERSION = "1.0.0"
MARKET_SNAPSHOT_PROTOCOL_VERSION = "2.0.0"
LEGACY_REFLECTION_PROVIDER = "codex-reflection-file-v1"
REFLECTION_PROVIDER = "codex-reflection-file-v2"
ReflectionProtocolVersion = Literal["1.0.0", "2.0.0"]
ReflectionProviderName = Literal[
    "codex-reflection-file-v1",
    "codex-reflection-file-v2",
]
REFLECTION_WINDOW = timedelta(hours=24)
MAX_JSON_BYTES = 25 * 1024 * 1024
CRITIC_AGENT_ID = "risk_critic_agent"
CIO_AGENT_ID = "cio_agent"
EXPECTED_AGENT_IDS = (
    "macro_policy_agent",
    "market_news_agent",
    "ai_storage_industry_agent",
    "strategy_agent",
    CRITIC_AGENT_ID,
)
REQUIRED_PERSISTED_AGENT_IDS = (*EXPECTED_AGENT_IDS, CIO_AGENT_ID)
MAX_ASSIGNMENTS = 500


def _reflection_provider_for_protocol(
    protocol_version: ReflectionProtocolVersion,
) -> ReflectionProviderName:
    if protocol_version == LEGACY_REFLECTION_PROTOCOL_VERSION:
        return LEGACY_REFLECTION_PROVIDER
    if protocol_version == REFLECTION_PROTOCOL_VERSION:
        return REFLECTION_PROVIDER
    raise ValueError(f"unsupported reflection protocol_version: {protocol_version}")

Severity = Literal[
    "noise",
    "directional",
    "large",
    "extreme",
    "systemic_extreme_down",
]
TimeClass = Literal[
    "published_before_cutoff_not_frozen",
    "post_cutoff_preclose",
    "post_close_explanation",
]
ScopeType = Literal["agent", "committee", "market_event"]
ErrorType = Literal[
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
CausalStatus = Literal["verified", "supported", "hypothesis", "unresolved"]
AgentVerdict = Literal[
    "right_reason",
    "lucky_correct",
    "wrong",
    "wrong_noise",
    "right_but_noise",
    "not_applicable",
    "unresolved",
]
CommitteeVerdict = Literal[
    "right_reason",
    "lucky_correct",
    "wrong",
    "wrong_noise",
    "right_but_noise",
    "not_applicable",
    "unresolved",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GeneratorIdentity(StrictModel):
    surface: Literal["codex"] = "codex"
    task_id: str | None = Field(default=None, max_length=200)
    model: str = Field(min_length=1, max_length=200)
    reasoning_effort: str = Field(min_length=1, max_length=100)


class OutcomeMetrics(StrictModel):
    forecast_id: UUID
    index_code: str
    horizon: Horizon
    target_date: date
    predicted_direction: Literal["up", "down"]
    actual_label: Direction
    actual_return: float = Field(allow_inf_nan=False)
    threshold: float = Field(gt=0, allow_inf_nan=False)
    horizon_sigma: float = Field(gt=0, allow_inf_nan=False)
    move_z: float = Field(ge=0, allow_inf_nan=False)
    severity: Literal["noise", "directional", "large", "extreme"]
    direction_result: Literal[
        "correct",
        "wrong",
        "right_but_noise",
        "wrong_noise",
        "zero_return",
    ]
    forecast_brier: float = Field(ge=0, allow_inf_nan=False)
    strategy_brier: float = Field(ge=0, allow_inf_nan=False)
    critic_haircut_brier_delta: float = Field(allow_inf_nan=False)
    observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class MarketSnapshotItemInput(StrictModel):
    index_code: str
    index_name: str
    target_date: date
    base_trade_date: date
    base_close: float = Field(gt=0, allow_inf_nan=False)
    target_close: float = Field(gt=0, allow_inf_nan=False)
    actual_return: float = Field(allow_inf_nan=False)
    source_url: AnyHttpUrl
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_source_url: AnyHttpUrl
    base_source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_source_url: AnyHttpUrl
    target_source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    captured_at: datetime
    amount: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    advancers: int | None = Field(default=None, ge=0)
    decliners: int | None = Field(default=None, ge=0)
    unchanged: int | None = Field(default=None, ge=0)
    limit_down_count: int | None = Field(default=None, ge=0)
    breadth_down_ratio: float | None = Field(default=None, ge=0, le=1)
    sector_contributions: list[dict[str, Any]] = Field(default_factory=list)
    weight_contributions: list[dict[str, Any]] = Field(default_factory=list)
    historical_abs_return_percentile: float | None = Field(
        default=None,
        ge=0,
        le=1,
    )
    history_sample_size: int = Field(default=0, ge=0, le=1250)

    @field_validator("captured_at")
    @classmethod
    def captured_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("market snapshot captured_at must be timezone-aware")
        return value


class MarketSnapshotDataQuality(StrictModel):
    status: Literal["passed"]
    source_id: str = Field(min_length=1, max_length=120)
    policy_version: str = Field(min_length=1, max_length=160)
    checked_at: datetime
    report_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    checks: dict[str, bool] = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def every_quality_check_passed(self) -> MarketSnapshotDataQuality:
        if self.checked_at.tzinfo is None or self.checked_at.utcoffset() is None:
            raise ValueError("data-quality checked_at must be timezone-aware")
        if not all(self.checks.values()):
            raise ValueError("every market snapshot data-quality check must pass")
        if self.report_hash == "0" * 64:
            raise ValueError("data-quality report hash cannot be a placeholder")
        return self


class TradingCalendarEvidenceInput(StrictModel):
    target_date: date
    calendar_id: str = Field(min_length=1, max_length=120)
    is_open: Literal[True]
    source_url: AnyHttpUrl
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("trading calendar observed_at must be timezone-aware")
        return value


class MarketSnapshotPublicationInput(StrictModel):
    source_id: str = Field(min_length=1, max_length=120)
    artifact_hashes: dict[str, str] = Field(min_length=1)

    @model_validator(mode="after")
    def publication_hashes_are_real(self) -> MarketSnapshotPublicationInput:
        values = list(self.artifact_hashes.values())
        if any(
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            or value == "0" * 64
            for value in values
        ):
            raise ValueError(
                "market snapshot publication hashes must be non-placeholder SHA-256"
            )
        if any(
            not artifact_id.strip()
            or "/" in artifact_id
            or "\\" in artifact_id
            or artifact_id in {".", ".."}
            for artifact_id in self.artifact_hashes
        ):
            raise ValueError(
                "publication artifact IDs must be non-empty opaque identifiers"
            )
        return self


class MarketSnapshotBundleInput(StrictModel):
    protocol_version: Literal["2.0.0"] = MARKET_SNAPSHOT_PROTOCOL_VERSION
    target_date: date
    horizon: Horizon
    captured_at: datetime
    data_quality: MarketSnapshotDataQuality
    trading_calendar: TradingCalendarEvidenceInput
    publication: MarketSnapshotPublicationInput
    items: list[MarketSnapshotItemInput] = Field(min_length=5, max_length=5)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("captured_at")
    @classmethod
    def bundle_captured_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("market snapshot bundle time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def market_items_are_complete(self) -> MarketSnapshotBundleInput:
        expected = {item.code for item in INDEXES}
        if {item.index_code for item in self.items} != expected:
            raise ValueError("market snapshot must contain the configured five indexes")
        if len({item.index_code for item in self.items}) != len(self.items):
            raise ValueError("market snapshot index codes must be unique")
        if any(item.target_date != self.target_date for item in self.items):
            raise ValueError("market snapshot item target dates must match the bundle")
        if self.trading_calendar.target_date != self.target_date:
            raise ValueError("trading calendar target date must match the bundle")
        if self.trading_calendar.observed_at > self.captured_at:
            raise ValueError("trading calendar must be observed before bundle capture")
        if self.data_quality.source_id != self.publication.source_id:
            raise ValueError("data-quality and publication source IDs must match")
        market_wide_fields = (
            "advancers",
            "decliners",
            "unchanged",
            "limit_down_count",
            "breadth_down_ratio",
        )
        for field_name in market_wide_fields:
            values = {getattr(item, field_name) for item in self.items}
            if len(values) != 1:
                raise ValueError(
                    f"market-wide statistic {field_name} must match across all five indexes"
                )
        return self


class ReflectionAssignment(StrictModel):
    finding_key: str = Field(min_length=1, max_length=200)
    scope_type: ScopeType
    opinion_id: UUID | None = None
    forecast_id: UUID | None = None
    agent_id: str | None = None
    index_code: str | None = None
    horizon: Horizon
    severity: Severity
    expected_direction_result: Literal[
        "correct",
        "wrong",
        "right_but_noise",
        "wrong_noise",
        "zero_return",
        "not_scored",
    ]
    original_evidence_item_ids: list[str] = Field(default_factory=list)
    eligible_missed_evidence_item_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def identity_matches_scope(self) -> ReflectionAssignment:
        if self.scope_type == "agent":
            if not all((self.opinion_id, self.forecast_id, self.agent_id, self.index_code)):
                raise ValueError("agent assignment requires opinion, forecast, agent and index")
        elif self.scope_type == "committee":
            if self.opinion_id is not None or not all((self.forecast_id, self.index_code)):
                raise ValueError("committee assignment requires forecast and index only")
        elif self.scope_type == "market_event" and any(
            value is not None
            for value in (self.opinion_id, self.forecast_id, self.agent_id, self.index_code)
        ):
            raise ValueError("market assignment must not bind an opinion, forecast or index")
        return self


class ReflectionRequest(StrictModel):
    protocol_version: ReflectionProtocolVersion = REFLECTION_PROTOCOL_VERSION
    reflection_id: UUID
    source_run_id: UUID
    source_batch_id: UUID
    schema_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", max_length=32)
    supersedes_id: UUID | None = None
    provider: ReflectionProviderName = REFLECTION_PROVIDER
    horizon: Horizon
    target_date: date
    prepared_at: datetime
    finalize_deadline: datetime
    source_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_run: dict[str, Any]
    original_input: dict[str, Any] | None
    outcomes: list[OutcomeMetrics] = Field(min_length=5, max_length=5)
    assignments: list[ReflectionAssignment] = Field(
        min_length=7,
        max_length=MAX_ASSIGNMENTS,
    )
    overall_severity: Severity
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("prepared_at", "finalize_deadline")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("reflection timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def request_is_coherent(self) -> ReflectionRequest:
        if self.finalize_deadline <= self.prepared_at:
            raise ValueError("finalize_deadline must be later than prepared_at")
        if {item.horizon for item in self.outcomes} != {self.horizon}:
            raise ValueError("all outcomes must use the requested horizon")
        if {item.target_date for item in self.outcomes} != {self.target_date}:
            raise ValueError("all outcomes must use the requested target date")
        keys = [item.finding_key for item in self.assignments]
        if len(keys) != len(set(keys)):
            raise ValueError("reflection assignment keys must be unique")
        if self.provider != _reflection_provider_for_protocol(self.protocol_version):
            raise ValueError(
                "reflection protocol_version and provider must use the same version"
            )
        agent_assignments = [
            item for item in self.assignments if item.scope_type == "agent"
        ]
        committee_assignments = [
            item for item in self.assignments if item.scope_type == "committee"
        ]
        market_assignments = [
            item for item in self.assignments if item.scope_type == "market_event"
        ]
        if len(market_assignments) != 1:
            raise ValueError("reflection requires one market assignment")
        if not agent_assignments:
            raise ValueError("reflection requires persisted agent assignments")
        outcome_by_index = {item.index_code: item for item in self.outcomes}
        if len(outcome_by_index) != len(self.outcomes):
            raise ValueError("reflection outcomes must use unique indexes")
        if len(committee_assignments) != len(self.outcomes):
            raise ValueError("reflection requires one committee assignment per outcome")
        if {
            (item.index_code, item.forecast_id)
            for item in committee_assignments
        } != {
            (item.index_code, item.forecast_id)
            for item in self.outcomes
        }:
            raise ValueError("committee assignments must match the outcome matrix")
        agent_rosters: dict[str, set[str]] = {
            index_code: set() for index_code in outcome_by_index
        }
        for assignment in agent_assignments:
            if assignment.index_code not in outcome_by_index:
                raise ValueError("agent assignment index is outside the outcome matrix")
            outcome = outcome_by_index[assignment.index_code]
            if assignment.forecast_id != outcome.forecast_id:
                raise ValueError("agent assignment forecast does not match its outcome")
            assert assignment.agent_id is not None
            if assignment.agent_id in agent_rosters[assignment.index_code]:
                raise ValueError("agent roster contains a duplicate index/agent pair")
            agent_rosters[assignment.index_code].add(assignment.agent_id)
        roster_values = list(agent_rosters.values())
        if any(roster != roster_values[0] for roster in roster_values[1:]):
            raise ValueError("persisted agent roster must be consistent across indexes")
        return self


class SourceCandidate(StrictModel):
    candidate_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")
    source_url: AnyHttpUrl
    source_kind: Literal[
        "official_market",
        "official_event",
        "company_disclosure",
        "reputable_news",
        "research_lead",
    ]
    related_index_codes: list[str] = Field(min_length=1, max_length=5)
    rationale: str = Field(min_length=1, max_length=1000)

    @field_validator("related_index_codes")
    @classmethod
    def index_codes_are_known(cls, value: list[str]) -> list[str]:
        allowed = {item.code for item in INDEXES}
        if not set(value).issubset(allowed) or len(value) != len(set(value)):
            raise ValueError("source candidate index codes must be unique configured indexes")
        return value


class SourceDiscoveryBundle(StrictModel):
    protocol_version: ReflectionProtocolVersion = REFLECTION_PROTOCOL_VERSION
    reflection_id: UUID
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    generated_by: GeneratorIdentity
    candidates: list[SourceCandidate] = Field(default_factory=list, max_length=50)

    @field_validator("generated_at")
    @classmethod
    def generated_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def candidates_are_unique(self) -> SourceDiscoveryBundle:
        ids = [item.candidate_id for item in self.candidates]
        urls = [str(item.source_url) for item in self.candidates]
        if len(ids) != len(set(ids)) or len(urls) != len(set(urls)):
            raise ValueError("source candidate IDs and URLs must be unique")
        return self


class CapturedSource(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=4000)
    quote: str = Field(min_length=1, max_length=2000)
    source_url: AnyHttpUrl
    event_time: datetime
    published_at: datetime
    ingested_at: datetime
    source_kind: Literal[
        "official_market",
        "official_event",
        "company_disclosure",
        "reputable_news",
        "research_lead",
    ]
    related_index_codes: list[str] = Field(min_length=1, max_length=5)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("event_time", "published_at", "ingested_at")
    @classmethod
    def source_timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("captured source timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def source_times_and_indexes_are_coherent(self) -> CapturedSource:
        if not self.event_time <= self.published_at <= self.ingested_at:
            raise ValueError("source times must satisfy event <= published <= ingested")
        allowed = {item.code for item in INDEXES}
        if (
            not set(self.related_index_codes).issubset(allowed)
            or len(self.related_index_codes) != len(set(self.related_index_codes))
        ):
            raise ValueError("captured source index codes must be configured and unique")
        return self


class CapturedSourceBundle(StrictModel):
    protocol_version: ReflectionProtocolVersion = REFLECTION_PROTOCOL_VERSION
    reflection_id: UUID
    captured_at: datetime
    items: list[CapturedSource] = Field(default_factory=list, max_length=100)

    @field_validator("captured_at")
    @classmethod
    def captured_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("captured_at must be timezone-aware")
        return value


class FrozenSource(StrictModel):
    id: str
    title: str
    summary: str
    quote: str
    source_url: str
    event_time: datetime
    published_at: datetime
    ingested_at: datetime
    source_kind: str
    related_index_codes: list[str]
    time_class: TimeClass
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class FrozenSourceSnapshot(StrictModel):
    protocol_version: ReflectionProtocolVersion = REFLECTION_PROTOCOL_VERSION
    reflection_id: UUID
    source_run_id: UUID
    frozen_at: datetime
    discovery_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    items: list[FrozenSource]
    unresolved_without_post_outcome_sources: bool
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class CounterfactualDraft(StrictModel):
    direction: Literal["up", "down", "unchanged", "not_applicable"]
    probabilities: dict[str, float] | None = None
    would_flip: bool | None = None
    basis: Literal[
        "original_frozen_evidence",
        "post_cutoff_oracle",
        "leave_one_input_out",
        "not_applicable",
    ]
    explanation: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def probabilities_are_valid_when_present(self) -> CounterfactualDraft:
        if self.probabilities is None:
            if self.direction in {"up", "down"}:
                raise ValueError("directional counterfactual requires probabilities")
            return self
        if set(self.probabilities) != {"up", "neutral", "down"}:
            raise ValueError("counterfactual probabilities require up/neutral/down")
        values = list(self.probabilities.values())
        if any(not math.isfinite(value) or value < 0 or value > 1 for value in values):
            raise ValueError("counterfactual probabilities must be finite values in [0, 1]")
        if not math.isclose(sum(values), 1.0, abs_tol=1e-6):
            raise ValueError("counterfactual probabilities must sum to one")
        if self.direction in {"up", "down"}:
            expected = (
                "up"
                if self.probabilities["up"] > self.probabilities["down"]
                else "down"
            )
            if math.isclose(
                self.probabilities["up"], self.probabilities["down"], abs_tol=1e-9
            ):
                raise ValueError("counterfactual up/down probabilities may not tie")
            if self.direction != expected:
                raise ValueError("counterfactual direction must match its probabilities")
        return self


class FindingDraftBase(StrictModel):
    finding_key: str
    scope_type: ScopeType
    severity: Severity
    primary_error_type: ErrorType
    secondary_error_types: list[ErrorType] = Field(default_factory=list, max_length=5)
    causal_status: CausalStatus
    summary: str = Field(min_length=1, max_length=4000)
    what_was_right: list[str] = Field(default_factory=list, max_length=10)
    what_was_wrong: list[str] = Field(default_factory=list, max_length=10)
    original_evidence_item_ids: list[str] = Field(default_factory=list)
    missed_evidence_item_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    invalidation_conditions_triggered: list[str] = Field(default_factory=list, max_length=10)
    remediation: list[str] = Field(default_factory=list, max_length=10)
    counterfactual: CounterfactualDraft
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def finding_lists_are_unique(self) -> FindingDraftBase:
        for field_name in (
            "secondary_error_types",
            "original_evidence_item_ids",
            "missed_evidence_item_ids",
            "source_ids",
        ):
            values = getattr(self, field_name)
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must be unique")
        if self.primary_error_type in self.secondary_error_types:
            raise ValueError("primary error type may not be duplicated as secondary")
        return self


class AgentFindingDraft(FindingDraftBase):
    scope_type: Literal["agent"] = "agent"
    opinion_id: UUID
    forecast_id: UUID
    agent_id: str
    index_code: str
    horizon: Horizon
    outcome_verdict: AgentVerdict


class CommitteeFindingDraft(FindingDraftBase):
    scope_type: Literal["committee"] = "committee"
    forecast_id: UUID
    index_code: str
    horizon: Horizon
    outcome_verdict: CommitteeVerdict


class MarketFindingDraft(FindingDraftBase):
    scope_type: Literal["market_event"] = "market_event"
    horizon: Horizon
    target_date: date
    outcome_verdict: Literal[
        "right_reason",
        "wrong",
        "wrong_noise",
        "right_but_noise",
        "not_applicable",
        "unresolved",
    ]


class LessonProposalDraft(StrictModel):
    lesson_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
    title: str = Field(min_length=1, max_length=300)
    lesson_type: Literal[
        "data_coverage",
        "reasoning",
        "risk_check",
        "index_mapping",
        "timing",
        "calibration",
        "workflow",
    ]
    summary: str = Field(min_length=1, max_length=4000)
    proposed_action: str = Field(min_length=1, max_length=4000)
    supporting_finding_keys: list[str] = Field(min_length=1, max_length=20)
    source_ids: list[str] = Field(default_factory=list, max_length=20)
    recurrence_key: str = Field(min_length=1, max_length=200)
    promotion_status: Literal["proposed"] = "proposed"


class AnalysisDraftBundle(StrictModel):
    protocol_version: ReflectionProtocolVersion = REFLECTION_PROTOCOL_VERSION
    reflection_id: UUID
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    generated_by: GeneratorIdentity
    agent_findings: list[AgentFindingDraft] = Field(
        min_length=1,
        max_length=MAX_ASSIGNMENTS,
    )
    committee_findings: list[CommitteeFindingDraft] = Field(
        min_length=1,
        max_length=MAX_ASSIGNMENTS,
    )
    market_finding: MarketFindingDraft
    lesson_proposals: list[LessonProposalDraft] = Field(default_factory=list, max_length=20)

    @field_validator("generated_at")
    @classmethod
    def generated_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value


class ReflectionReceipt(StrictModel):
    protocol_version: ReflectionProtocolVersion = REFLECTION_PROTOCOL_VERSION
    reflection_id: UUID
    source_run_id: UUID
    status: Literal["completed"]
    finalized_at: datetime
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    analysis_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    finding_count: int = Field(ge=0)
    lesson_proposal_count: int = Field(ge=0)
    receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def reflection_root(settings: Settings, override: Path | None = None) -> Path:
    """Return the configured private reflection root."""

    return override or settings.reflection_root


def prepare_reflection(
    settings: Settings,
    source_run_id: str,
    *,
    horizon: Horizon | str,
    market_snapshot_path: Path | None = None,
    schema_version: str = REFLECTION_SCHEMA_VERSION,
    protocol_version: ReflectionProtocolVersion = REFLECTION_PROTOCOL_VERSION,
    supersedes_id: str | None = None,
    now: datetime | None = None,
    output_root: Path | None = None,
) -> Path:
    """Freeze one completed live run's fully evaluated horizon for reflection."""

    require_schema_current(settings.database_url)
    zone = ZoneInfo(settings.timezone)
    prepared_at = _normalize_now(now, zone)
    requested_horizon = Horizon(horizon)
    _validate_schema_version(schema_version)
    root = _prepare_root(reflection_root(settings, output_root))
    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            source_run = session.scalar(
                select(WorkflowRun)
                .options(
                    selectinload(WorkflowRun.forecasts).selectinload(Forecast.evaluation),
                    selectinload(WorkflowRun.opinions),
                )
                .where(WorkflowRun.id == source_run_id)
            )
            if source_run is None:
                raise ValueError("source prediction run was not found")
            if source_run.mode != "live" or source_run.status != "completed":
                raise ValueError("reflection requires a completed live prediction run")
            validate_default_reflection_universe(source_run)
            forecasts = sorted(
                (
                    forecast
                    for forecast in source_run.forecasts
                    if forecast.horizon == requested_horizon.value
                ),
                key=lambda item: item.index_code,
            )
            _validate_mature_forecasts(forecasts, requested_horizon, prepared_at, zone)
            opinions = sorted(
                (
                    opinion
                    for opinion in source_run.opinions
                    if opinion.horizon == requested_horizon.value
                ),
                key=lambda item: (item.index_code, item.agent_id),
            )
            _validate_opinion_matrix(opinions, forecasts)
            target_date = forecasts[0].target_date
            try:
                source_batch = _materialize_source_batch(
                    session,
                    forecasts=forecasts,
                    horizon=requested_horizon,
                    target_date=target_date,
                    market_snapshot_path=market_snapshot_path,
                    market_snapshot_root=settings.market_snapshot_root,
                    timezone=settings.timezone,
                    prepared_at=prepared_at,
                )
            except Exception as exc:
                session.rollback()
                record_blocked_upstream_batch(
                    session,
                    target_date=target_date,
                    horizon=requested_horizon.value,
                    source_hash=_canonical_hash(
                        {
                            "target_date": target_date.isoformat(),
                            "horizon": requested_horizon.value,
                            "market_snapshot_path": (
                                str(market_snapshot_path)
                                if market_snapshot_path is not None
                                else None
                            ),
                        }
                    ),
                    now=prepared_at,
                    error=str(exc),
                    data_quality={
                        "market_snapshot_complete": False,
                        "blocked_stage": "reflection_prepare",
                    },
                )
                session.commit()
                raise ValueError(
                    f"reflection is blocked by its trusted market snapshot: {exc}"
                ) from exc

            original_input = _load_original_input(settings, source_run)
            source_payload = _source_run_payload(
                source_run,
                forecasts,
                opinions,
                source_batch=source_batch,
            )
            outcomes, overall_severity = _outcome_metrics(
                forecasts,
                opinions,
                source_batch=source_batch,
            )
            frozen_evidence_ids = _frozen_evidence_ids(original_input)
            assignments = _reflection_assignments(
                forecasts=forecasts,
                opinions=opinions,
                outcomes=outcomes,
                overall_severity=overall_severity,
                frozen_evidence_ids=frozen_evidence_ids,
            )
            supersedes = _load_superseded_reflection(
                session,
                supersedes_id=supersedes_id,
                source_run=source_run,
                source_batch=source_batch,
                schema_version=schema_version,
            )
            seed_hash = _canonical_hash(
                {
                    "source_run_id": source_run.id,
                    "source_batch_id": source_batch.id,
                    "horizon": requested_horizon.value,
                    "target_date": target_date.isoformat(),
                    "schema_version": schema_version,
                    "supersedes_id": supersedes.id if supersedes else None,
                    "source_input_hash": source_run.input_hash,
                    "evaluation_set_hash": source_batch.evaluation_set_hash,
                }
            )
            reflection_row = create_reflection_run(
                session,
                source_run=source_run,
                source_batch=source_batch,
                input_hash=seed_hash,
                now=prepared_at,
                schema_version=schema_version,
                supersedes=supersedes,
            )
            if reflection_row.supersedes_id != (
                supersedes.id if supersedes else None
            ):
                raise ValueError(
                    "reflection schema identity already exists with different lineage"
                )
            if reflection_row.status != "awaiting_sources":
                raise ValueError(
                    f"reflection already exists in terminal stage: {reflection_row.id}"
                )
            reflection_id = UUID(reflection_row.id)
            row_prepared_at = _aware(reflection_row.created_at)
            job_dir = root / str(reflection_id)
            if job_dir.is_symlink():
                raise ValueError("reflection job directory may not be a symlink")
            if job_dir.exists() and not job_dir.is_dir():
                raise ValueError("reflection job path must be a directory")
            if job_dir.is_dir():
                existing_raw, existing_payload = _secure_read_json(
                    job_dir / "input.json"
                )
                existing = _validate_request_file(
                    existing_raw,
                    existing_payload,
                    job_dir,
                )
                _load_reflection_row(
                    session,
                    existing,
                    expected_status="awaiting_sources",
                )
                return job_dir
            unsigned = ReflectionRequest(
                protocol_version=protocol_version,
                reflection_id=reflection_id,
                source_run_id=UUID(source_run.id),
                source_batch_id=UUID(source_batch.id),
                schema_version=schema_version,
                supersedes_id=UUID(supersedes.id) if supersedes else None,
                provider=_reflection_provider_for_protocol(protocol_version),
                horizon=requested_horizon,
                target_date=target_date,
                prepared_at=row_prepared_at,
                finalize_deadline=row_prepared_at + REFLECTION_WINDOW,
                source_input_hash=source_run.input_hash,
                evaluation_set_hash=source_batch.evaluation_set_hash,
                source_run=source_payload,
                original_input=original_input,
                outcomes=outcomes,
                assignments=assignments,
                overall_severity=overall_severity,
                request_hash="0" * 64,
            )
            request_hash = _canonical_hash(
                unsigned.model_dump(mode="json", exclude={"request_hash"})
            )
            request = unsigned.model_copy(update={"request_hash": request_hash})
            reflection_row.input_hash = request_hash
            session.commit()

        if job_dir.is_symlink():
            raise ValueError("reflection job directory may not be a symlink")
        if job_dir.exists() and not job_dir.is_dir():
            raise ValueError("reflection job path must be a directory")
        if job_dir.is_dir():
            existing_raw, existing_payload = _secure_read_json(job_dir / "input.json")
            existing = _validate_request_file(existing_raw, existing_payload, job_dir)
            if existing.request_hash != request.request_hash:
                raise ValueError("existing reflection package differs from database identity")
            return job_dir
        try:
            job_dir.mkdir(mode=0o700)
            os.chmod(job_dir, 0o700)
            discovery_dir = job_dir / "source-discovery"
            discovery_dir.mkdir(mode=0o700)
            _atomic_write(
                job_dir / "input.json",
                _json_bytes(request.model_dump(mode="json")),
                mode=0o400,
            )
            _atomic_write(
                discovery_dir / "INSTRUCTIONS.md",
                _source_discovery_instructions(request).encode("utf-8"),
                mode=0o400,
            )
            _atomic_write(
                discovery_dir / "drafts.template.json",
                _json_bytes(_source_discovery_template(request)),
                mode=0o600,
            )
        except Exception as exc:
            _mark_reflection_failed(
                database,
                str(reflection_id),
                f"reflection preparation failed: {exc}",
                row_prepared_at,
            )
            raise
        return job_dir
    finally:
        database.dispose()


def freeze_reflection_sources(
    settings: Settings,
    job_dir: str | Path,
    *,
    sources_path: Path | None = None,
    now: datetime | None = None,
    output_root: Path | None = None,
) -> FrozenSourceSnapshot:
    """Validate Codex URL leads and freeze trusted collector output deterministically."""

    require_schema_current(settings.database_url)
    zone = ZoneInfo(settings.timezone)
    frozen_at = _normalize_now(now, zone)
    directory = _resolve_job_dir(reflection_root(settings, output_root), job_dir)
    request_raw, request_payload = _secure_read_json(
        _job_file(directory, "input.json")
    )
    request = _validate_request_file(request_raw, request_payload, directory)
    _validate_deadline(request, frozen_at)
    _, discovery_payload = _secure_read_json(
        _job_file(directory, "source-discovery/drafts.json")
    )
    discovery = SourceDiscoveryBundle.model_validate(discovery_payload)
    _validate_discovery(discovery, request, frozen_at)
    discovery_hash = _canonical_hash(discovery.model_dump(mode="json"))

    captures = _load_source_captures(
        sources_path=sources_path,
        request=request,
        discovery=discovery,
        frozen_at=frozen_at,
    )
    frozen_items = [
        _freeze_source(item, request=request, frozen_at=frozen_at) for item in captures
    ]
    unsigned = FrozenSourceSnapshot(
        protocol_version=request.protocol_version,
        reflection_id=request.reflection_id,
        source_run_id=request.source_run_id,
        frozen_at=frozen_at,
        discovery_hash=discovery_hash,
        items=frozen_items,
        unresolved_without_post_outcome_sources=not frozen_items,
        content_hash="0" * 64,
    )
    snapshot = unsigned.model_copy(
        update={
            "content_hash": _canonical_hash(
                unsigned.model_dump(mode="json", exclude={"content_hash"})
            )
        }
    )

    analysis_dir = directory / "analysis"
    if analysis_dir.is_symlink():
        raise ValueError("reflection analysis directory may not be a symlink")
    analysis_dir_created = not analysis_dir.exists()
    analysis_dir.mkdir(mode=0o700, exist_ok=True)
    if not analysis_dir.is_dir() or analysis_dir.resolve().parent != directory:
        raise ValueError("invalid reflection analysis directory")
    os.chmod(analysis_dir, 0o700)
    file_specs = [
        (
            directory / "sources.json",
            _json_bytes(snapshot.model_dump(mode="json")),
            0o400,
        ),
        (
            analysis_dir / "INSTRUCTIONS.md",
            _analysis_instructions(request, snapshot).encode("utf-8"),
            0o400,
        ),
        (
            analysis_dir / "drafts.template.json",
            _json_bytes(_analysis_template(request, snapshot)),
            0o600,
        ),
    ]
    staged_files: list[tuple[Path, Path, bytes]] = []
    try:
        for destination, payload, mode in file_specs:
            staged_files.append(
                (_stage_file(destination, payload, mode=mode), destination, payload)
            )
    except Exception:
        for temporary, _, _ in staged_files:
            _unlink_if_exists(temporary)
        if analysis_dir_created:
            try:
                analysis_dir.rmdir()
            except OSError:
                pass
        raise
    published_paths: list[Path] = []
    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            row = _load_reflection_row(session, request, expected_status="awaiting_sources")
            _set_reflection_stage(
                row,
                status="awaiting_analysis",
                source_snapshot_hash=snapshot.content_hash,
            )
            try:
                for temporary, destination, payload in staged_files:
                    if _publish_staged_file(temporary, destination, payload):
                        published_paths.append(destination)
                session.commit()
            except Exception:
                session.rollback()
                _remove_published_files(published_paths)
                raise
        return snapshot
    finally:
        for temporary, _, _ in staged_files:
            _unlink_if_exists(temporary)
        if analysis_dir_created:
            try:
                analysis_dir.rmdir()
            except OSError:
                pass
        database.dispose()


def finalize_reflection(
    settings: Settings,
    job_dir: str | Path,
    *,
    now: datetime | None = None,
    output_root: Path | None = None,
) -> ReflectionReceipt:
    """Validate analysis drafts and append findings and lesson proposals once."""

    require_schema_current(settings.database_url)
    zone = ZoneInfo(settings.timezone)
    finalized_at = _normalize_now(now, zone)
    directory = _resolve_job_dir(reflection_root(settings, output_root), job_dir)
    request_raw, request_payload = _secure_read_json(
        _job_file(directory, "input.json")
    )
    request = _validate_request_file(request_raw, request_payload, directory)
    _validate_deadline(request, finalized_at)
    _, source_payload = _secure_read_json(_job_file(directory, "sources.json"))
    snapshot = FrozenSourceSnapshot.model_validate(source_payload)
    _validate_frozen_source_snapshot(snapshot, request)
    _, discovery_payload = _secure_read_json(
        _job_file(directory, "source-discovery/drafts.json")
    )
    discovery = SourceDiscoveryBundle.model_validate(discovery_payload)
    if _canonical_hash(discovery.model_dump(mode="json")) != snapshot.discovery_hash:
        raise ValueError("source discovery was changed after sources were frozen")
    analysis_raw, analysis_payload = _secure_read_json(
        _job_file(directory, "analysis/drafts.json")
    )
    analysis = AnalysisDraftBundle.model_validate(analysis_payload)
    _validate_analysis(analysis, request=request, snapshot=snapshot, finalized_at=finalized_at)
    if (
        analysis.lesson_proposals
        and not settings.reflection_lesson_proposals_enabled
    ):
        raise ValueError(
            "lesson proposals are disabled during the initial human-review gate"
        )
    analysis_hash = _canonical_hash(analysis.model_dump(mode="json"))
    analysis_raw_hash = _sha256(analysis_raw)
    findings = [
        *analysis.agent_findings,
        *analysis.committee_findings,
        analysis.market_finding,
    ]
    output_payload = {
        "reflection_id": str(request.reflection_id),
        "source_run_id": str(request.source_run_id),
        "request_hash": request.request_hash,
        "source_snapshot_hash": snapshot.content_hash,
        "analysis_hash": analysis_hash,
        "analysis_raw_hash": analysis_raw_hash,
        "findings": [item.model_dump(mode="json") for item in findings],
        "lesson_proposals": [
            item.model_dump(mode="json") for item in analysis.lesson_proposals
        ],
    }
    output_hash = _canonical_hash(output_payload)
    unsigned = ReflectionReceipt(
        protocol_version=request.protocol_version,
        reflection_id=request.reflection_id,
        source_run_id=request.source_run_id,
        status="completed",
        finalized_at=finalized_at,
        request_hash=request.request_hash,
        source_snapshot_hash=snapshot.content_hash,
        analysis_hash=analysis_hash,
        output_hash=output_hash,
        finding_count=len(findings),
        lesson_proposal_count=len(analysis.lesson_proposals),
        receipt_hash="0" * 64,
    )
    receipt = unsigned.model_copy(
        update={
            "receipt_hash": _canonical_hash(
                unsigned.model_dump(mode="json", exclude={"receipt_hash"})
            )
        }
    )
    receipt_path = directory / "receipt.json"
    receipt_payload = _json_bytes(receipt.model_dump(mode="json"))
    staged_receipt = _stage_file(receipt_path, receipt_payload, mode=0o400)
    receipt_published = False

    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            row = _load_reflection_row(
                session,
                request,
                expected_status="awaiting_analysis",
            )
            if row.source_snapshot_hash != snapshot.content_hash:
                raise ValueError("frozen sources do not match the database source seal")
            if analysis.lesson_proposals:
                approved_reviews = approved_reflection_review_count(
                    session,
                    cutoff=finalized_at,
                )
                if approved_reviews < settings.reflection_required_human_reviews:
                    raise ValueError(
                        "lesson proposals remain blocked until "
                        f"{settings.reflection_required_human_reviews} completed Live "
                        "reflections have immutable approved human reviews "
                        f"(currently {approved_reviews})"
                    )
            _persist_findings_and_lessons(
                session,
                request=request,
                findings=findings,
                lessons=analysis.lesson_proposals,
                source_snapshot=snapshot,
                finalized_at=finalized_at,
                required_shadow_target_dates=(
                    settings.reflection_shadow_target_dates
                ),
            )
            claimed = session.execute(
                update(type(row))
                .where(
                    type(row).id == str(request.reflection_id),
                    type(row).status == "awaiting_analysis",
                )
                .values(
                    status="completed",
                    completed_at=finalized_at,
                    output_hash=output_hash,
                    receipt_hash=receipt.receipt_hash,
                )
            )
            if claimed.rowcount != 1:
                session.rollback()
                raise RuntimeError("reflection was already claimed or finalized")
            try:
                os.chmod(directory / "analysis" / "drafts.json", 0o400)
                os.chmod(directory / "source-discovery" / "drafts.json", 0o400)
                receipt_published = _publish_staged_file(
                    staged_receipt,
                    receipt_path,
                    receipt_payload,
                )
                session.commit()
            except Exception:
                session.rollback()
                if receipt_published:
                    _remove_published_files([receipt_path])
                raise
        return receipt
    finally:
        _unlink_if_exists(staged_receipt)
        database.dispose()


def _materialize_source_batch(
    session,
    *,
    forecasts: list[Forecast],
    horizon: Horizon,
    target_date: date,
    market_snapshot_path: Path | None,
    market_snapshot_root: Path,
    timezone: str,
    prepared_at: datetime,
) -> EvaluationBatch:
    due_forecasts = due_live_forecasts(
        session,
        target_date=target_date,
        horizon=horizon.value,
    )
    if not {item.id for item in forecasts}.issubset(
        {item.id for item in due_forecasts}
    ):
        raise ValueError("source run forecasts are absent from the due evaluation set")
    evaluation_set_hash = _evaluation_set_hash(due_forecasts)
    existing = session.scalar(
        select(EvaluationBatch)
        .options(
            selectinload(EvaluationBatch.market_snapshots),
            selectinload(EvaluationBatch.diagnostics),
        )
        .where(
            EvaluationBatch.target_date == target_date,
            EvaluationBatch.horizon == horizon.value,
            EvaluationBatch.evaluation_set_hash == evaluation_set_hash,
            EvaluationBatch.status == "completed",
        )
    )
    if market_snapshot_path is None:
        raise ValueError("--market-snapshot is required for a formal reflection")
    bundle = _load_market_snapshot_bundle(
        market_snapshot_path,
        horizon=horizon,
        target_date=target_date,
        prepared_at=prepared_at,
        market_snapshot_root=market_snapshot_root,
        timezone=timezone,
    )
    facts = [_market_fact(item) for item in bundle.items]
    source_hash = bundle.content_hash
    data_quality = {
        "source": "trusted-market-snapshot",
        "market_snapshot_complete": True,
        "upstream": bundle.data_quality.model_dump(mode="json"),
        "trading_calendar": bundle.trading_calendar.model_dump(mode="json"),
    }
    if existing is not None:
        if existing.source_hash != source_hash:
            raise ValueError(
                "market snapshot differs from the immutable evaluation batch"
            )
        return existing
    batch = materialize_evaluation_batch(
        session,
        target_date=target_date,
        horizon=horizon.value,
        snapshots=facts,
        source_hash=source_hash,
        now=prepared_at,
        data_quality=data_quality,
    )
    session.flush()
    return batch


def _load_market_snapshot_bundle(
    path: Path,
    *,
    horizon: Horizon,
    target_date: date,
    prepared_at: datetime,
    market_snapshot_root: Path,
    timezone: str,
) -> MarketSnapshotBundleInput:
    from .market_outcome import load_market_snapshot

    bundle = load_market_snapshot(
        path,
        now=prepared_at,
        root=market_snapshot_root,
        timezone=timezone,
    )
    if bundle.horizon is not horizon or bundle.target_date != target_date:
        raise ValueError("market snapshot does not match the requested horizon/target")
    return bundle


def _market_fact(item: MarketSnapshotItemInput) -> MarketSnapshotFact:
    payload = item.model_dump(
        mode="python",
        exclude={
            "base_source_url",
            "base_source_hash",
            "target_source_url",
            "target_source_hash",
        },
    )
    payload["source_url"] = str(item.source_url)
    return MarketSnapshotFact(**payload)


def _evaluation_set_hash(forecasts: list[Forecast]) -> str:
    return _canonical_hash(
        [
            {
                "forecast_id": forecast.id,
                "evaluation_id": (
                    forecast.evaluation.id if forecast.evaluation else None
                ),
                "observation_hash": (
                    forecast.evaluation.observation_hash
                    if forecast.evaluation
                    else None
                ),
            }
            for forecast in sorted(forecasts, key=lambda item: item.id)
        ]
    )


def _validate_mature_forecasts(
    forecasts: list[Forecast],
    horizon: Horizon,
    prepared_at: datetime,
    zone: ZoneInfo,
) -> None:
    required_codes = {item.code for item in INDEXES}
    if len(forecasts) != len(INDEXES) or {item.index_code for item in forecasts} != required_codes:
        raise ValueError(f"reflection requires all five {horizon.value} forecasts")
    target_dates = {item.target_date for item in forecasts}
    if len(target_dates) != 1:
        raise ValueError("reflection horizon forecasts must share one target date")
    target_date = next(iter(target_dates))
    maturity = datetime.combine(target_date, time(15, 5), tzinfo=zone)
    if prepared_at < maturity:
        raise ValueError(f"reflection cannot start before target close: {maturity.isoformat()}")
    if any(item.evaluation is None for item in forecasts):
        raise ValueError("reflection requires all five forecasts to be evaluated")


def _validate_opinion_matrix(
    opinions: list[AgentOpinion],
    forecasts: list[Forecast],
) -> None:
    forecast_codes = {item.index_code for item in forecasts}
    if {item.index_code for item in opinions} != forecast_codes:
        raise ValueError("opinion matrix does not match evaluated forecasts")
    actual = {(item.index_code, item.agent_id) for item in opinions}
    if len(actual) != len(opinions):
        raise ValueError("opinion matrix contains duplicate index/agent pairs")
    unknown_agents = sorted(
        {item.agent_id for item in opinions if item.agent_id not in AGENT_BY_ID}
    )
    if unknown_agents:
        raise ValueError(
            "opinion matrix contains unregistered agents: "
            + ", ".join(unknown_agents)
        )
    unavailable_agents = sorted(
        {
            item.agent_id
            for item in opinions
            if AGENT_BY_ID[item.agent_id].status != "active"
            or AGENT_BY_ID[item.agent_id].kind == "placeholder"
        }
    )
    if unavailable_agents:
        raise ValueError(
            "unavailable placeholder opinions cannot enter formal reflection: "
            + ", ".join(unavailable_agents)
        )
    rosters = {
        index_code: {
            item.agent_id for item in opinions if item.index_code == index_code
        }
        for index_code in forecast_codes
    }
    roster_values = list(rosters.values())
    if not roster_values or any(
        roster != roster_values[0] for roster in roster_values[1:]
    ):
        raise ValueError("persisted opinion roster must be consistent across indexes")
    missing_required = sorted(
        set(REQUIRED_PERSISTED_AGENT_IDS) - roster_values[0]
    )
    if missing_required:
        raise ValueError(
            "formal reflection is missing required persisted agents: "
            + ", ".join(missing_required)
        )
    forecast_by_index = {item.index_code: item for item in forecasts}
    if any(
        item.target_date != forecast_by_index[item.index_code].target_date
        or item.horizon != forecast_by_index[item.index_code].horizon
        or item.status != "active"
        for item in opinions
    ):
        raise ValueError(
            "opinion matrix contains an immature or mismatched persisted opinion"
        )


def _source_run_payload(
    source_run: WorkflowRun,
    forecasts: list[Forecast],
    opinions: list[AgentOpinion],
    *,
    source_batch: EvaluationBatch,
) -> dict[str, Any]:
    return {
        "id": source_run.id,
        "as_of": _aware(source_run.as_of).isoformat(),
        "data_cutoff": _aware(source_run.data_cutoff).isoformat(),
        "mode": source_run.mode,
        "status": source_run.status,
        "input_hash": source_run.input_hash,
        "data_quality": source_run.data_quality or {},
        "forecasts": [
            forecast_read(item).model_dump(mode="json") for item in forecasts
        ],
        "opinions": [_opinion_payload(item) for item in opinions],
        "evaluation_batch": {
            "id": source_batch.id,
            "target_date": source_batch.target_date.isoformat(),
            "horizon": source_batch.horizon,
            "evaluation_set_hash": source_batch.evaluation_set_hash,
            "source_hash": source_batch.source_hash,
            "data_quality": source_batch.data_quality,
            "market_snapshots": [
                {
                    "id": item.id,
                    "index_code": item.index_code,
                    "index_name": item.index_name,
                    "target_date": item.target_date.isoformat(),
                    "base_trade_date": item.base_trade_date.isoformat(),
                    "base_close": item.base_close,
                    "target_close": item.target_close,
                    "actual_return": item.actual_return,
                    "amount": item.amount,
                    "advancers": item.advancers,
                    "decliners": item.decliners,
                    "unchanged": item.unchanged,
                    "limit_down_count": item.limit_down_count,
                    "breadth_down_ratio": item.breadth_down_ratio,
                    "sector_contributions": item.sector_contributions,
                    "weight_contributions": item.weight_contributions,
                    "historical_abs_return_percentile": (
                        item.historical_abs_return_percentile
                    ),
                    "history_sample_size": item.history_sample_size,
                    "source_url": item.source_url,
                    "source_hash": item.source_hash,
                    "captured_at": _aware(item.captured_at).isoformat(),
                    "content_hash": item.content_hash,
                }
                for item in sorted(
                    source_batch.market_snapshots,
                    key=lambda row: row.index_code,
                )
            ],
        },
    }


def _opinion_payload(item: AgentOpinion) -> dict[str, Any]:
    return {
        "id": item.id,
        "run_id": item.run_id,
        "agent_id": item.agent_id,
        "agent_name": item.agent_name,
        "role": item.role,
        "agent_version": item.agent_version,
        "model_name": item.model_name,
        "status": item.status,
        "index_code": item.index_code,
        "horizon": item.horizon,
        "target_date": item.target_date.isoformat(),
        "direction": item.direction,
        "probabilities": {
            "up": item.probability_up,
            "neutral": item.probability_neutral,
            "down": item.probability_down,
        },
        "summary": item.summary,
        "evidence": item.evidence,
        "counter_evidence": item.counter_evidence,
        "invalidation_conditions": item.invalidation_conditions,
        "citations": item.citations,
        "contribution": item.contribution,
        "weight": item.weight,
        "raw_response": item.raw_response,
    }


def _outcome_metrics(
    forecasts: list[Forecast],
    opinions: list[AgentOpinion],
    *,
    source_batch: EvaluationBatch,
) -> tuple[list[OutcomeMetrics], Severity]:
    strategy_by_index = {
        item.index_code: item for item in opinions if item.agent_id == "strategy_agent"
    }
    diagnostic_by_forecast = {
        item.forecast_id: item for item in source_batch.diagnostics
    }
    preliminary: list[OutcomeMetrics] = []
    for forecast in forecasts:
        assert forecast.evaluation is not None
        evaluation = forecast.evaluation
        horizon_sigma = forecast.threshold * 4.0
        diagnostic = diagnostic_by_forecast.get(forecast.id)
        if diagnostic is None:
            raise ValueError("evaluation batch is missing a forecast diagnostic")
        move_z = abs(diagnostic.signed_sigma)
        severity = diagnostic.severity
        actual = Direction(evaluation.actual_label)
        direction_result = _expected_direction_result(
            predicted_direction=forecast.direction,
            actual_label=actual,
            actual_return=evaluation.actual_return,
        )
        strategy = strategy_by_index[forecast.index_code]
        strategy_probabilities = {
            "up": strategy.probability_up,
            "neutral": strategy.probability_neutral,
            "down": strategy.probability_down,
        }
        final_probabilities = {
            "up": forecast.probability_up,
            "neutral": forecast.probability_neutral,
            "down": forecast.probability_down,
        }
        strategy_brier = multiclass_brier_score(strategy_probabilities, actual)
        forecast_brier = multiclass_brier_score(final_probabilities, actual)
        preliminary.append(
            OutcomeMetrics(
                forecast_id=UUID(forecast.id),
                index_code=forecast.index_code,
                horizon=Horizon(forecast.horizon),
                target_date=forecast.target_date,
                predicted_direction=forecast.direction,
                actual_label=actual,
                actual_return=evaluation.actual_return,
                threshold=forecast.threshold,
                horizon_sigma=horizon_sigma,
                move_z=move_z,
                severity=severity,
                direction_result=direction_result,
                forecast_brier=forecast_brier,
                strategy_brier=strategy_brier,
                critic_haircut_brier_delta=forecast_brier - strategy_brier,
                observation_hash=evaluation.observation_hash,
            )
        )
    if any(item.systemic_extreme_down for item in source_batch.diagnostics):
        overall: Severity = "systemic_extreme_down"
    else:
        severity_rank = {"noise": 0, "directional": 1, "large": 2, "extreme": 3}
        overall = max(
            (item.severity for item in preliminary),
            key=severity_rank.__getitem__,
        )
    return preliminary, overall


def _reflection_assignments(
    *,
    forecasts: list[Forecast],
    opinions: list[AgentOpinion],
    outcomes: list[OutcomeMetrics],
    overall_severity: Severity,
    frozen_evidence_ids: set[str],
) -> list[ReflectionAssignment]:
    forecast_by_index = {item.index_code: item for item in forecasts}
    outcome_by_index = {item.index_code: item for item in outcomes}
    used_by_index: dict[str, set[str]] = {item.code: set() for item in INDEXES}
    for opinion in opinions:
        used_by_index[opinion.index_code].update(
            (opinion.raw_response or {}).get("evidence_item_ids", [])
        )
    assignments: list[ReflectionAssignment] = []
    for opinion in opinions:
        if opinion.agent_id == CIO_AGENT_ID:
            continue
        forecast = forecast_by_index[opinion.index_code]
        outcome = outcome_by_index[opinion.index_code]
        raw_ids = set((opinion.raw_response or {}).get("evidence_item_ids", []))
        severity = _assignment_severity(outcome, overall_severity)
        if opinion.agent_id == CRITIC_AGENT_ID:
            expected_result = "not_scored"
        else:
            expected_result = _expected_direction_result(
                predicted_direction=opinion.direction,
                actual_label=outcome.actual_label,
                actual_return=outcome.actual_return,
            )
        assignments.append(
            ReflectionAssignment(
                finding_key=f"agent:{opinion.id}",
                scope_type="agent",
                opinion_id=UUID(opinion.id),
                forecast_id=UUID(forecast.id),
                agent_id=opinion.agent_id,
                index_code=opinion.index_code,
                horizon=Horizon(opinion.horizon),
                severity=severity,
                expected_direction_result=expected_result,
                original_evidence_item_ids=sorted(raw_ids),
                eligible_missed_evidence_item_ids=sorted(frozen_evidence_ids - raw_ids),
            )
        )
    for forecast in forecasts:
        outcome = outcome_by_index[forecast.index_code]
        assignments.append(
            ReflectionAssignment(
                finding_key=f"committee:{forecast.id}",
                scope_type="committee",
                forecast_id=UUID(forecast.id),
                index_code=forecast.index_code,
                horizon=Horizon(forecast.horizon),
                severity=_assignment_severity(outcome, overall_severity),
                expected_direction_result=outcome.direction_result,
                original_evidence_item_ids=sorted(
                    used_by_index[forecast.index_code]
                ),
                eligible_missed_evidence_item_ids=sorted(
                    frozen_evidence_ids - used_by_index[forecast.index_code]
                ),
            )
        )
    all_used = set().union(*used_by_index.values())
    assignments.append(
        ReflectionAssignment(
            finding_key=f"market:{forecasts[0].target_date}:{forecasts[0].horizon}",
            scope_type="market_event",
            horizon=Horizon(forecasts[0].horizon),
            severity=overall_severity,
            expected_direction_result="not_scored",
            original_evidence_item_ids=sorted(all_used),
            eligible_missed_evidence_item_ids=sorted(frozen_evidence_ids - all_used),
        )
    )
    return assignments


def _assignment_severity(outcome: OutcomeMetrics, overall: Severity) -> Severity:
    if overall == "systemic_extreme_down":
        return "systemic_extreme_down"
    return outcome.severity


def _expected_direction_result(
    *,
    predicted_direction: str,
    actual_label: Direction,
    actual_return: float,
) -> Literal[
    "correct",
    "wrong",
    "right_but_noise",
    "wrong_noise",
    "zero_return",
]:
    if actual_return == 0:
        return "zero_return"
    actual_direction = "up" if actual_return > 0 else "down"
    sign_correct = predicted_direction == actual_direction
    if actual_label is Direction.NEUTRAL:
        return "right_but_noise" if sign_correct else "wrong_noise"
    return "correct" if sign_correct else "wrong"


def _load_original_input(
    settings: Settings,
    source_run: WorkflowRun,
) -> dict[str, Any] | None:
    directory = settings.handoff_root / source_run.id
    path = directory / "input.json"
    if not path.exists():
        return None
    resolved_root = _prepare_root(settings.handoff_root)
    if directory.is_symlink() or directory.parent.resolve() != resolved_root:
        raise ValueError("prediction handoff directory escaped its configured root")
    raw, payload = _secure_read_json(path)
    if str(payload.get("run_id")) != source_run.id:
        raise ValueError("prediction handoff input belongs to another run")
    if payload.get("input_hash") != source_run.input_hash:
        raise ValueError("prediction handoff input_hash differs from its database run")
    handoff = (source_run.data_quality or {}).get("handoff", {})
    if handoff:
        if handoff.get("request_hash") != payload.get("request_hash"):
            raise ValueError("prediction handoff canonical seal is invalid")
        if handoff.get("request_raw_hash") != _sha256(raw):
            raise ValueError("prediction handoff raw seal is invalid")
    return payload


def _frozen_evidence_ids(original_input: dict[str, Any] | None) -> set[str]:
    if not original_input:
        return set()
    state = original_input.get("initial_state", {})
    snapshot = state.get("evidence_snapshot", {})
    return {
        str(item["id"])
        for item in snapshot.get("items", [])
        if isinstance(item, dict) and item.get("id")
    }


def _source_discovery_template(request: ReflectionRequest) -> dict[str, Any]:
    return {
        "protocol_version": request.protocol_version,
        "reflection_id": str(request.reflection_id),
        "request_hash": request.request_hash,
        "generated_at": "REPLACE_WITH_TIMEZONE_AWARE_ISO8601",
        "generated_by": {
            "surface": "codex",
            "task_id": None,
            "model": "REPLACE_WITH_MODEL_ID",
            "reasoning_effort": "REPLACE_WITH_REASONING_EFFORT",
        },
        "candidates": [],
    }


def _source_discovery_instructions(request: ReflectionRequest) -> str:
    brand = (
        "VeriCouncil"
        if request.protocol_version == LEGACY_REFLECTION_PROTOCOL_VERSION
        else "forecast-loop"
    )
    return f"""# {brand} 反省：来源发现

反省 ID：`{request.reflection_id}`

原预测 run：`{request.source_run_id}`

目标：`{request.horizon.value} / {request.target_date.isoformat()}`

1. 只把 URL 当作采集线索。不得自报正文、时间、哈希或声称某条新闻已证明因果。
2. 阅读上级目录 `input.json`，把本次真实走势可能相关的官方行情、公告、公司披露或
   可靠新闻 URL 填入 `drafts.json`。候选 URL 必须唯一并使用 HTTPS。
3. `related_index_codes` 只能取 input 中五个指数；`rationale` 说明为什么值得交给可信
   采集器核验，不写成已经验证的事实。
4. 若没有可靠候选，可提交空 candidates；后续分析将被强制标记为 unresolved。
5. generated_by 必须填写本次实际使用的 model 与 reasoning_effort；不得伪造或留空。
6. 不得修改 input、数据库、预测、评价、Wiki 或其他交接文件。

可信采集器会独立取得内容、时间与哈希；候选 URL 本身不能进入最终反省事实。
"""


def _validate_discovery(
    bundle: SourceDiscoveryBundle,
    request: ReflectionRequest,
    now: datetime,
) -> None:
    if bundle.protocol_version != request.protocol_version:
        raise ValueError("source discovery protocol_version does not match input")
    if bundle.reflection_id != request.reflection_id:
        raise ValueError("source discovery belongs to another reflection")
    if bundle.request_hash != request.request_hash:
        raise ValueError("source discovery request hash does not match input")
    if not request.prepared_at <= bundle.generated_at <= request.finalize_deadline:
        raise ValueError("source discovery generated_at is outside the reflection window")
    if bundle.generated_at > now + timedelta(minutes=5):
        raise ValueError("source discovery generated_at is implausibly in the future")
    for item in bundle.candidates:
        validate_trusted_source_url(str(item.source_url), label="reflection candidate")


def _load_source_captures(
    *,
    sources_path: Path | None,
    request: ReflectionRequest,
    discovery: SourceDiscoveryBundle,
    frozen_at: datetime,
) -> list[CapturedSource]:
    if sources_path is None:
        return []
    source_file = sources_path.expanduser()
    if source_file.is_symlink():
        raise ValueError("trusted source bundle may not be a symlink")
    _, payload = _secure_read_json(source_file.resolve(strict=True))
    captures = CapturedSourceBundle.model_validate(payload)
    if captures.protocol_version != request.protocol_version:
        raise ValueError("captured source protocol_version does not match input")
    if captures.reflection_id != request.reflection_id:
        raise ValueError("captured source bundle belongs to another reflection")
    if captures.captured_at > frozen_at + timedelta(minutes=5):
        raise ValueError("captured source bundle is implausibly in the future")
    candidates = {str(item.source_url): item for item in discovery.candidates}
    if not candidates and captures.items:
        raise ValueError("trusted captures require a matching Codex-discovered URL")
    if len({item.id for item in captures.items}) != len(captures.items):
        raise ValueError("captured source IDs must be unique")
    if len({str(item.source_url) for item in captures.items}) != len(captures.items):
        raise ValueError("captured source URLs must be unique")
    for item in captures.items:
        url = str(item.source_url)
        candidate = candidates.get(url)
        if candidate is None:
            raise ValueError(f"trusted capture URL was not discovered by Codex: {url}")
        if not set(item.related_index_codes).issubset(candidate.related_index_codes):
            raise ValueError("trusted capture expanded the candidate's index scope")
        validate_trusted_source_url(url, label="reflection source")
        expected = _canonical_hash(
            item.model_dump(mode="json", exclude={"content_hash"})
        )
        if expected != item.content_hash:
            raise ValueError(f"captured source {item.id} content_hash is invalid")
        if item.ingested_at > frozen_at + timedelta(minutes=5):
            raise ValueError(f"captured source {item.id} was ingested in the future")
    return captures.items


def _freeze_source(
    item: CapturedSource,
    *,
    request: ReflectionRequest,
    frozen_at: datetime,
) -> FrozenSource:
    cutoff = datetime.fromisoformat(str(request.source_run["data_cutoff"]))
    target_close = datetime.combine(
        request.target_date,
        time(15, 0),
        tzinfo=cutoff.tzinfo or ZoneInfo("Asia/Shanghai"),
    )
    if item.published_at <= cutoff:
        time_class: TimeClass = "published_before_cutoff_not_frozen"
    elif item.published_at <= target_close:
        time_class = "post_cutoff_preclose"
    else:
        time_class = "post_close_explanation"
    payload = item.model_dump(mode="python")
    payload["source_url"] = str(item.source_url)
    payload["time_class"] = time_class
    return FrozenSource.model_validate(payload)


def _analysis_template(
    request: ReflectionRequest,
    snapshot: FrozenSourceSnapshot,
) -> dict[str, Any]:
    common = {
        "severity": None,
        "primary_error_type": None,
        "secondary_error_types": [],
        "causal_status": None,
        "summary": None,
        "what_was_right": [],
        "what_was_wrong": [],
        "original_evidence_item_ids": [],
        "missed_evidence_item_ids": [],
        "source_ids": [],
        "invalidation_conditions_triggered": [],
        "remediation": [],
        "counterfactual": {
            "direction": "not_applicable",
            "probabilities": None,
            "would_flip": None,
            "basis": "not_applicable",
            "explanation": None,
        },
        "confidence": None,
    }
    agents = []
    committees = []
    market: dict[str, Any] | None = None
    for assignment in request.assignments:
        base = {
            **common,
            "finding_key": assignment.finding_key,
            "scope_type": assignment.scope_type,
            "severity": assignment.severity,
        }
        if assignment.scope_type == "agent":
            agents.append(
                {
                    **base,
                    "opinion_id": str(assignment.opinion_id),
                    "forecast_id": str(assignment.forecast_id),
                    "agent_id": assignment.agent_id,
                    "index_code": assignment.index_code,
                    "horizon": assignment.horizon.value,
                    "outcome_verdict": None,
                }
            )
        elif assignment.scope_type == "committee":
            committees.append(
                {
                    **base,
                    "forecast_id": str(assignment.forecast_id),
                    "index_code": assignment.index_code,
                    "horizon": assignment.horizon.value,
                    "outcome_verdict": None,
                }
            )
        else:
            market = {
                **base,
                "horizon": assignment.horizon.value,
                "target_date": request.target_date.isoformat(),
                "outcome_verdict": None,
            }
    assert market is not None
    return {
        "protocol_version": request.protocol_version,
        "reflection_id": str(request.reflection_id),
        "request_hash": request.request_hash,
        "source_snapshot_hash": snapshot.content_hash,
        "generated_at": "REPLACE_WITH_TIMEZONE_AWARE_ISO8601",
        "generated_by": {
            "surface": "codex",
            "task_id": None,
            "model": "REPLACE_WITH_MODEL_ID",
            "reasoning_effort": "REPLACE_WITH_REASONING_EFFORT",
        },
        "agent_findings": agents,
        "committee_findings": committees,
        "market_finding": market,
        "lesson_proposals": [],
    }


def _analysis_instructions(
    request: ReflectionRequest,
    snapshot: FrozenSourceSnapshot,
) -> str:
    brand = (
        "VeriCouncil"
        if request.protocol_version == LEGACY_REFLECTION_PROTOCOL_VERSION
        else "forecast-loop"
    )
    unresolved_note = (
        "本次没有冻结任何事后来源。所有原因归因必须使用 unresolved，不能补写市场故事。"
        if snapshot.unresolved_without_post_outcome_sources
        else "原因只可引用 sources.json 中已冻结的 source ID。"
    )
    agent_count = sum(
        item.scope_type == "agent" for item in request.assignments
    )
    committee_count = sum(
        item.scope_type == "committee" for item in request.assignments
    )
    return f"""# {brand} 反省：结构化分析

反省 ID：`{request.reflection_id}`

确定性严重度：`{request.overall_severity}`

{unresolved_note}

1. 完整填写模板的 {agent_count} 个 agent findings、{committee_count} 个
   committee findings 和 1 个 market finding；数量来自本次已持久化的成熟意见，
   不得增删或替换 finding_key、opinion_id、forecast_id、Agent、指数、周期和 severity。
   input 中的 expected_direction_result 同样是确定性的：wrong、right_but_noise、
   wrong_noise 和 zero_return 必须分别写成 wrong、right_but_noise、wrong_noise 和
   not_applicable；只有方向正确但理由尚无法判定时才可用 unresolved。
2. 只有原 input 中冻结但该 Agent 未采用的 evidence ID 才能写入 missed_evidence_item_ids。
   截止后来源不得写成“昨天应该知道”。
3. Risk Critic 不按涨跌命中率评分，只评其反证与失效条件是否覆盖实际风险。
4. 当前委员会方向完全由 Strategy 给出；Risk Critic 的 15% 对称 haircut 不改变方向。
   不得把三位研究员写成向 CIO 等权投票。
5. `market_noise` 只允许 severity=noise。large/extreme/systemic_extreme_down
   严禁归因于普通噪声。
6. counterfactual 是分析假设，不是历史事实；必须标明 basis。截止后事件只能使用
   post_cutoff_oracle，不能据此责备原判断。
7. Lesson 只能是 proposal，不能直接修改 Wiki；单日样本不得宣称已形成稳定规律。
8. generated_by 必须填写本次实际使用的 model 与 reasoning_effort；不得伪造或留空。
9. 不得修改数据库、input.json、sources.json、历史预测、评价或 Wiki。
10. 初始十份 completed Live 反省取得不可变 approved 人工复核前，即使操作员打开
   环境开关也会被拒绝；门禁未满足时 lesson_proposals 必须保持空数组。
"""


def _validate_analysis(
    analysis: AnalysisDraftBundle,
    *,
    request: ReflectionRequest,
    snapshot: FrozenSourceSnapshot,
    finalized_at: datetime,
) -> None:
    if analysis.protocol_version != request.protocol_version:
        raise ValueError("analysis protocol_version does not match input")
    if analysis.reflection_id != request.reflection_id:
        raise ValueError("analysis belongs to another reflection")
    if analysis.request_hash != request.request_hash:
        raise ValueError("analysis request hash does not match input")
    if analysis.source_snapshot_hash != snapshot.content_hash:
        raise ValueError("analysis source snapshot hash does not match frozen sources")
    if not request.prepared_at <= analysis.generated_at <= request.finalize_deadline:
        raise ValueError("analysis generated_at is outside the reflection window")
    if analysis.generated_at > finalized_at + timedelta(minutes=5):
        raise ValueError("analysis generated_at is implausibly in the future")

    assignments = {item.finding_key: item for item in request.assignments}
    findings: list[FindingDraftBase] = [
        *analysis.agent_findings,
        *analysis.committee_findings,
        analysis.market_finding,
    ]
    if {item.finding_key for item in findings} != set(assignments):
        raise ValueError("analysis finding identity matrix is incomplete or contains extras")
    source_by_id = {item.id: item for item in snapshot.items}
    frozen_evidence_ids = _frozen_evidence_ids(request.original_input)
    for finding in findings:
        assignment = assignments[finding.finding_key]
        _validate_finding_identity(finding, assignment)
        if finding.severity != assignment.severity:
            raise ValueError(f"{finding.finding_key} changed deterministic severity")
        all_errors = {finding.primary_error_type, *finding.secondary_error_types}
        if finding.severity != "noise" and "market_noise" in all_errors:
            raise ValueError("non-noise market moves may not be dismissed as market_noise")
        if finding.severity != "noise" and finding.model_dump(mode="python")[
            "outcome_verdict"
        ] in {"wrong_noise", "right_but_noise"}:
            raise ValueError("large or extreme outcomes may not use a noise verdict")
        if not set(finding.original_evidence_item_ids).issubset(
            assignment.original_evidence_item_ids
        ):
            raise ValueError(f"{finding.finding_key} invented original evidence IDs")
        if not set(finding.missed_evidence_item_ids).issubset(
            assignment.eligible_missed_evidence_item_ids
        ):
            raise ValueError(f"{finding.finding_key} invented or reused missed evidence IDs")
        if not set(finding.original_evidence_item_ids).issubset(frozen_evidence_ids):
            raise ValueError(f"{finding.finding_key} referenced evidence outside original input")
        if not set(finding.source_ids).issubset(source_by_id):
            raise ValueError(f"{finding.finding_key} referenced an unfrozen outcome source")
        source_time_classes = {
            source_by_id[source_id].time_class for source_id in finding.source_ids
        }
        if finding.missed_evidence_item_ids and any(
            time_class != "published_before_cutoff_not_frozen"
            for time_class in source_time_classes
        ):
            raise ValueError(
                "post-cutoff or post-close sources may not be mixed with "
                "missed pre-cutoff evidence in one finding"
            )
        verdict = finding.model_dump(mode="python")["outcome_verdict"]
        is_direction_scored = assignment.scope_type == "committee" or (
            assignment.scope_type == "agent"
            and assignment.agent_id != CRITIC_AGENT_ID
        )
        if snapshot.unresolved_without_post_outcome_sources:
            if (
                finding.primary_error_type != "unresolved"
                or finding.causal_status != "unresolved"
                or finding.source_ids
            ):
                raise ValueError("reflection without frozen outcome sources must remain unresolved")
        if is_direction_scored and verdict == "right_reason":
            if finding.causal_status not in {"verified", "supported"}:
                raise ValueError(
                    "right_reason requires supported or verified causal status"
                )
            if not finding.original_evidence_item_ids:
                raise ValueError("right_reason requires original frozen evidence")
            if not finding.source_ids:
                raise ValueError("right_reason requires a frozen outcome source")
        if is_direction_scored and verdict == "lucky_correct":
            if not finding.what_was_wrong:
                raise ValueError("lucky_correct requires an identified original error")
            if not finding.source_ids:
                raise ValueError("lucky_correct requires a frozen outcome source")
        if finding.primary_error_type == "attention_omission" and not (
            finding.missed_evidence_item_ids
        ):
            raise ValueError("attention_omission requires a missed frozen evidence ID")
        if finding.primary_error_type == "data_coverage_failure":
            if not any(
                source_by_id[source_id].time_class
                == "published_before_cutoff_not_frozen"
                for source_id in finding.source_ids
            ):
                raise ValueError(
                    "data_coverage_failure requires a pre-cutoff source absent from the snapshot"
                )
        if finding.primary_error_type == "post_cutoff_shock":
            if finding.missed_evidence_item_ids:
                raise ValueError(
                    "post_cutoff_shock may not claim missed pre-cutoff evidence"
                )
            if not any(
                source_by_id[source_id].time_class == "post_cutoff_preclose"
                for source_id in finding.source_ids
            ):
                raise ValueError("post_cutoff_shock requires a post-cutoff pre-close source")
        if finding.counterfactual.basis == "original_frozen_evidence":
            if not (
                finding.original_evidence_item_ids or finding.missed_evidence_item_ids
            ):
                raise ValueError("original-evidence counterfactual requires frozen evidence IDs")
        if finding.counterfactual.basis == "post_cutoff_oracle":
            if not any(
                source_by_id[source_id].time_class != "published_before_cutoff_not_frozen"
                for source_id in finding.source_ids
            ):
                raise ValueError("post-cutoff oracle requires a post-cutoff source")
        _validate_outcome_verdict(finding, assignment)

    finding_keys = set(assignments)
    source_ids = set(source_by_id)
    lesson_keys = [item.lesson_key for item in analysis.lesson_proposals]
    if len(lesson_keys) != len(set(lesson_keys)):
        raise ValueError("lesson proposal keys must be unique")
    recurrence_keys = [item.recurrence_key for item in analysis.lesson_proposals]
    if len(recurrence_keys) != len(set(recurrence_keys)):
        raise ValueError("lesson proposal recurrence keys must be unique per reflection")
    for lesson in analysis.lesson_proposals:
        if not set(lesson.supporting_finding_keys).issubset(finding_keys):
            raise ValueError("lesson proposal referenced an unknown finding")
        if not set(lesson.source_ids).issubset(source_ids):
            raise ValueError("lesson proposal referenced an unfrozen source")


def _validate_finding_identity(
    finding: FindingDraftBase,
    assignment: ReflectionAssignment,
) -> None:
    if finding.scope_type != assignment.scope_type:
        raise ValueError(f"{finding.finding_key} changed its scope")
    for field_name in ("opinion_id", "forecast_id", "agent_id", "index_code", "horizon"):
        if hasattr(finding, field_name):
            actual = getattr(finding, field_name)
            expected = getattr(assignment, field_name)
            if actual != expected:
                raise ValueError(f"{finding.finding_key} changed {field_name}")


def _validate_outcome_verdict(
    finding: FindingDraftBase,
    assignment: ReflectionAssignment,
) -> None:
    verdict = finding.model_dump(mode="python")["outcome_verdict"]
    expected = assignment.expected_direction_result
    if assignment.scope_type == "agent" and assignment.agent_id == CRITIC_AGENT_ID:
        if verdict not in {"not_applicable", "unresolved"}:
            raise ValueError("Risk Critic direction must remain not_applicable")
        return
    if assignment.scope_type == "market_event":
        if verdict not in {"not_applicable", "unresolved"}:
            raise ValueError("market-event findings do not receive a direction verdict")
        return
    if expected == "correct" and verdict not in {
        "right_reason",
        "lucky_correct",
        "unresolved",
    }:
        raise ValueError("correct direction must use a correct or unresolved verdict")
    if expected == "wrong" and verdict != "wrong":
        raise ValueError("wrong direction must use the deterministic wrong verdict")
    if expected == "right_but_noise" and verdict != "right_but_noise":
        raise ValueError(
            "matching noise-band sign must use right_but_noise"
        )
    if expected == "wrong_noise" and verdict != "wrong_noise":
        raise ValueError(
            "opposing noise-band sign must use wrong_noise"
        )
    if expected == "zero_return" and verdict != "not_applicable":
        raise ValueError("zero return has no direction verdict")


def _validate_frozen_source_snapshot(
    snapshot: FrozenSourceSnapshot,
    request: ReflectionRequest,
) -> None:
    if snapshot.protocol_version != request.protocol_version:
        raise ValueError("frozen source protocol_version does not match input")
    if (
        snapshot.reflection_id != request.reflection_id
        or snapshot.source_run_id != request.source_run_id
    ):
        raise ValueError("frozen sources belong to another reflection")
    expected = _canonical_hash(
        snapshot.model_dump(mode="json", exclude={"content_hash"})
    )
    if expected != snapshot.content_hash:
        raise ValueError("frozen source snapshot content_hash is invalid")
    if snapshot.unresolved_without_post_outcome_sources != (not snapshot.items):
        raise ValueError("frozen source unresolved flag does not match source count")
    if len({item.id for item in snapshot.items}) != len(snapshot.items):
        raise ValueError("frozen source IDs must be unique")
    for item in snapshot.items:
        validate_trusted_source_url(item.source_url, label="frozen reflection source")


def _validate_request_file(
    raw: bytes,
    payload: Any,
    directory: Path,
) -> ReflectionRequest:
    request = ReflectionRequest.model_validate(payload)
    if str(request.reflection_id) != directory.name:
        raise ValueError("reflection directory UUID does not match input.json")
    expected = _canonical_hash(
        request.model_dump(mode="json", exclude={"request_hash"})
    )
    if expected != request.request_hash:
        raise ValueError("reflection input canonical request hash is invalid")
    del raw
    return request


def _validate_deadline(request: ReflectionRequest, now: datetime) -> None:
    if now > request.finalize_deadline:
        raise ValueError("reflection handoff deadline has passed")


def _load_reflection_row(
    session,
    request: ReflectionRequest,
    *,
    expected_status: str,
):
    ReflectionRun, _, _ = _reflection_models()
    row = session.get(ReflectionRun, str(request.reflection_id))
    if row is None:
        raise RuntimeError("reflection run is missing from the database")
    if row.status != expected_status:
        raise RuntimeError(f"reflection cannot continue from status {row.status}")
    if row.source_run_id != str(request.source_run_id):
        raise ValueError("reflection database source run does not match input")
    if row.source_batch_id != str(request.source_batch_id):
        raise ValueError("reflection database source batch does not match input")
    if (
        row.horizon != request.horizon.value
        or row.target_date != request.target_date
        or row.evaluation_set_hash != request.evaluation_set_hash
    ):
        raise ValueError("reflection database identity does not match input")
    if row.input_hash != request.request_hash:
        raise ValueError("reflection input does not match the database seal")
    if (
        row.schema_version != request.schema_version
        or row.supersedes_id
        != (str(request.supersedes_id) if request.supersedes_id else None)
    ):
        raise ValueError("reflection version lineage does not match input")
    return row


def _load_superseded_reflection(
    session,
    *,
    supersedes_id: str | None,
    source_run: WorkflowRun,
    source_batch: EvaluationBatch,
    schema_version: str,
):
    if supersedes_id is None:
        return None
    try:
        normalized_id = str(UUID(supersedes_id))
    except ValueError as exc:
        raise ValueError("supersedes must be a reflection UUID") from exc
    ReflectionRun, _, _ = _reflection_models()
    supersedes = session.get(ReflectionRun, normalized_id)
    if supersedes is None:
        raise ValueError("superseded reflection was not found")
    if supersedes.status != "completed":
        raise ValueError("only a completed reflection may be superseded")
    if (
        supersedes.source_run_id != source_run.id
        or supersedes.horizon != source_batch.horizon
        or supersedes.target_date != source_batch.target_date
        or supersedes.evaluation_set_hash != source_batch.evaluation_set_hash
    ):
        raise ValueError(
            "superseded reflection must use the same run, horizon, target and evaluations"
        )
    if _schema_version_tuple(schema_version) <= _schema_version_tuple(
        supersedes.schema_version
    ):
        raise ValueError(
            "a corrected reflection requires a newer schema version"
        )
    return supersedes


def _validate_schema_version(value: str) -> None:
    if len(value) > 32:
        raise ValueError("reflection schema version is too long")
    _schema_version_tuple(value)


def _schema_version_tuple(value: str) -> tuple[int, int, int]:
    parts = value.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise ValueError("reflection schema version must use MAJOR.MINOR.PATCH")
    if any(len(part) > 1 and part.startswith("0") for part in parts):
        raise ValueError("reflection schema version components may not have leading zeroes")
    major, minor, patch = (int(part) for part in parts)
    return major, minor, patch


def _set_reflection_stage(row, **updates: Any) -> None:
    for key, value in updates.items():
        if not hasattr(row, key):
            raise RuntimeError(f"ReflectionRun model is missing required field: {key}")
        setattr(row, key, value)


def _persist_findings_and_lessons(
    session,
    *,
    request: ReflectionRequest,
    findings: list[FindingDraftBase],
    lessons: list[LessonProposalDraft],
    source_snapshot: FrozenSourceSnapshot,
    finalized_at: datetime,
    required_shadow_target_dates: int,
) -> None:
    _, ReflectionFinding, LessonProposal = _reflection_models()
    from ..models import LessonEpisode
    finding_ids: dict[str, str] = {}
    source_by_id = {item.id: item for item in source_snapshot.items}
    for finding in findings:
        content = finding.model_dump(mode="json")
        finding_id = str(uuid4())
        finding_ids[finding.finding_key] = finding_id
        source_time_classes = {
            source_by_id[source_id].time_class for source_id in finding.source_ids
        }
        if finding.missed_evidence_item_ids:
            availability_class = "available_missed"
        elif finding.primary_error_type == "data_coverage_failure":
            availability_class = "coverage_gap_pre_cutoff"
        elif (
            finding.primary_error_type == "post_cutoff_shock"
            or "post_cutoff_preclose" in source_time_classes
        ):
            availability_class = "post_cutoff_event"
        elif source_time_classes == {"post_close_explanation"}:
            availability_class = "after_close_explanation"
        elif finding.original_evidence_item_ids:
            availability_class = "available_used"
        else:
            availability_class = "unresolved"
        summary = finding.summary
        if finding.what_was_right:
            summary += "\n\n判断中有效：" + "；".join(finding.what_was_right)
        if finding.what_was_wrong:
            summary += "\n\n判断中失效：" + "；".join(finding.what_was_wrong)
        if finding.invalidation_conditions_triggered:
            summary += "\n\n已触发失效条件：" + "；".join(
                finding.invalidation_conditions_triggered
            )
        counterfactual = finding.counterfactual.model_dump(mode="json")
        counterfactual["reflection_metadata"] = {
            "what_was_right": list(finding.what_was_right),
            "what_was_wrong": list(finding.what_was_wrong),
            "invalidation_conditions_triggered": list(
                finding.invalidation_conditions_triggered
            ),
            "original_evidence_item_ids": list(
                finding.original_evidence_item_ids
            ),
            "missed_evidence_item_ids": list(finding.missed_evidence_item_ids),
            "source_ids": list(finding.source_ids),
        }
        payload = {
            "id": finding_id,
            "reflection_run_id": str(request.reflection_id),
            "scope_type": finding.scope_type,
            "subject_id": (
                content["agent_id"]
                if finding.scope_type == "agent"
                else finding.scope_type
            ),
            "index_code": content.get("index_code"),
            "horizon": request.horizon.value,
            "verdict": content["outcome_verdict"],
            "primary_error_type": finding.primary_error_type,
            "secondary_error_types": list(finding.secondary_error_types),
            "evidence_ids": list(
                dict.fromkeys(
                    [
                        *finding.original_evidence_item_ids,
                        *finding.missed_evidence_item_ids,
                        *finding.source_ids,
                    ]
                )
            ),
            "availability_class": availability_class,
            "causal_status": finding.causal_status,
            "counterfactual": counterfactual,
            "remediation": list(finding.remediation),
            "confidence": finding.confidence,
            "summary": summary,
            "created_at": finalized_at,
        }
        session.add(ReflectionFinding(**payload))
    for lesson in lessons:
        episode_key = request.target_date.isoformat()
        episode = session.scalar(
            select(LessonEpisode).where(
                LessonEpisode.cluster_key == lesson.recurrence_key,
                LessonEpisode.episode_key == episode_key,
            )
        )
        if episode is None:
            session.add(
                LessonEpisode(
                    id=str(uuid4()),
                    cluster_key=lesson.recurrence_key,
                    episode_key=episode_key,
                    first_reflection_run_id=str(request.reflection_id),
                    evidence_set_hash=request.evaluation_set_hash,
                    created_at=finalized_at,
                )
            )
            session.flush()
        historical_lessons = list(
            session.scalars(
                select(LessonProposal).where(
                    LessonProposal.cluster_key == lesson.recurrence_key
                )
            ).all()
        )
        independent_episode_count = int(
            session.scalar(
                select(func.count())
                .select_from(LessonEpisode)
                .where(LessonEpisode.cluster_key == lesson.recurrence_key)
            )
            or 0
        )
        replay_target_dates = max(
            (item.replay_target_dates for item in historical_lessons),
            default=0,
        )
        prior_replay_metrics = next(
            (
                item.replay_metrics
                for item in sorted(
                    historical_lessons,
                    key=lambda item: item.created_at,
                    reverse=True,
                )
                if item.replay_metrics
            ),
            {},
        )
        assessment = assess_lesson_policy(
            proposal_type=lesson.lesson_type,
            overall_severity=request.overall_severity,
            independent_episode_count=independent_episode_count,
            replay_target_dates=replay_target_dates,
            average_brier_improvement=prior_replay_metrics.get(
                "average_brier_improvement"
            ),
            calibration_improvement=prior_replay_metrics.get(
                "calibration_improvement"
            ),
            important_subgroups_non_degrading=prior_replay_metrics.get(
                "important_subgroups_non_degrading"
            ),
            completed_shadow_target_dates=completed_live_target_date_count(
                session,
                include_target_date=request.target_date,
            ),
            required_shadow_target_dates=required_shadow_target_dates,
        )
        replay_metrics = {
            **assessment.as_dict(),
            "average_brier_improvement": prior_replay_metrics.get(
                "average_brier_improvement"
            ),
            "calibration_improvement": prior_replay_metrics.get(
                "calibration_improvement"
            ),
            "important_subgroups_non_degrading": prior_replay_metrics.get(
                "important_subgroups_non_degrading"
            ),
            "revalidation_due_after_independent_episode_count": (
                independent_episode_count + LESSON_REVALIDATION_EPISODES
            ),
            "revalidation_due_after_sessions": LESSON_HALF_LIFE_SESSIONS,
        }
        payload = {
            "id": str(uuid4()),
            "reflection_run_id": str(request.reflection_id),
            "episode_key": episode_key,
            "cluster_key": lesson.recurrence_key,
            "title": lesson.title,
            "summary": f"{lesson.summary}\n\n建议动作：{lesson.proposed_action}",
            "status": "candidate",
            "proposal_type": lesson.lesson_type,
            "evidence_finding_ids": [
                finding_ids[key] for key in lesson.supporting_finding_keys
            ],
            "independent_episode_count": independent_episode_count,
            "replay_target_dates": replay_target_dates,
            "replay_metrics": replay_metrics,
            "half_life_sessions": LESSON_HALF_LIFE_SESSIONS,
            "created_at": finalized_at,
            "reviewed_at": None,
            "supersedes_id": None,
        }
        session.add(LessonProposal(**payload))
    session.flush()


def _reflection_models():
    from .. import models

    missing = [
        name
        for name in ("ReflectionRun", "ReflectionFinding", "LessonProposal")
        if not hasattr(models, name)
    ]
    if missing:
        raise RuntimeError(
            "reflection core models are not installed: " + ", ".join(missing)
        )
    return models.ReflectionRun, models.ReflectionFinding, models.LessonProposal


def _mark_reflection_failed(
    database: Database,
    reflection_id: str,
    error: str,
    completed_at: datetime,
) -> None:
    try:
        ReflectionRun, _, _ = _reflection_models()
    except RuntimeError:
        return
    with database.session_factory() as session:
        row = session.get(ReflectionRun, reflection_id)
        if row is None:
            return
        _set_reflection_stage(
            row,
            status="failed",
            completed_at=completed_at,
            error=error,
        )
        session.commit()


def _prepare_root(root: Path) -> Path:
    path = root.expanduser()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("reflection root may not be a symlink")
    resolved = path.resolve()
    os.chmod(resolved, 0o700)
    return resolved


def _resolve_job_dir(root: Path, job_dir: str | Path) -> Path:
    resolved_root = _prepare_root(root)
    candidate = Path(job_dir).expanduser()
    if not candidate.is_absolute():
        candidate = resolved_root / candidate
    try:
        UUID(candidate.name)
    except ValueError as exc:
        raise ValueError("reflection job directory name must be a UUID") from exc
    if candidate.is_symlink():
        raise ValueError("reflection job directory may not be a symlink")
    if candidate.parent.resolve() != resolved_root:
        raise ValueError("reflection job directory must be a direct child of reflection root")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_dir() or resolved.parent != resolved_root:
        raise ValueError("invalid reflection job directory")
    return resolved


def _secure_read_json(path: Path) -> tuple[bytes, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"reflection file is not regular: {path.name}")
        if metadata.st_size <= 0 or metadata.st_size > MAX_JSON_BYTES:
            raise ValueError(f"reflection file size is invalid: {path.name}")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != metadata.st_size:
            raise ValueError(f"reflection file changed while reading: {path.name}")
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid UTF-8 JSON in {path.name}: {exc}") from exc
    return raw, payload


def _job_file(directory: Path, relative: str) -> Path:
    """Return a contained job file while rejecting symlinked path components."""

    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("reflection file path must be a contained relative path")
    candidate = directory / relative_path
    current = directory
    for part in relative_path.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"reflection path component may not be a symlink: {part}")
    if candidate.is_symlink():
        raise ValueError(f"reflection file may not be a symlink: {candidate.name}")
    if candidate.parent.resolve() != (directory / relative_path.parent).resolve():
        raise ValueError("reflection file parent could not be resolved safely")
    try:
        candidate.resolve(strict=True).relative_to(directory)
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError("reflection file escaped its job directory or is missing") from exc
    return candidate


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _atomic_write(path: Path, payload: bytes, *, mode: int) -> None:
    temporary_path = _stage_file(path, payload, mode=mode)
    try:
        _publish_staged_file(temporary_path, path, payload)
    finally:
        _unlink_if_exists(temporary_path)


def _stage_file(path: Path, payload: bytes, *, mode: int) -> Path:
    """Durably stage one same-directory file without making it visible."""

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        return temporary_path
    except Exception:
        _unlink_if_exists(temporary_path)
        raise


def _publish_staged_file(
    temporary_path: Path,
    destination: Path,
    payload: bytes,
) -> bool:
    """Atomically publish a staged file; return whether this call created it."""

    if destination.is_symlink():
        raise ValueError(f"reflection output may not be a symlink: {destination.name}")
    if destination.exists():
        metadata = destination.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(
                f"reflection output is not a regular file: {destination.name}"
            )
        if destination.read_bytes() != payload:
            raise ValueError(
                f"existing reflection output differs: {destination.name}"
            )
        os.chmod(destination, stat.S_IMODE(temporary_path.stat().st_mode))
        _unlink_if_exists(temporary_path)
        return False
    os.replace(temporary_path, destination)
    try:
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        _unlink_if_exists(destination)
        raise
    return True


def _remove_published_files(paths: list[Path]) -> None:
    for path in reversed(paths):
        if path.is_symlink():
            continue
        _unlink_if_exists(path)


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalize_now(value: datetime | None, zone: ZoneInfo) -> datetime:
    if value is None:
        return datetime.now(zone)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def _aware(value: datetime) -> datetime:
    zone = ZoneInfo("Asia/Shanghai")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)
