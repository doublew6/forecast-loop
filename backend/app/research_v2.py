"""Versioned contracts and deterministic math for the focused v2 research loop.

The v2 protocol is additive.  It deliberately does not reinterpret legacy
``AgentOpinion`` or ``Forecast`` rows because those records did not freeze the
natural/decision horizon split introduced here.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from datetime import date, datetime
from enum import Enum, StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RESEARCH_PROGRAM_SCHEMA_V2 = "forecast-loop.research-program/v2"
EVIDENCE_SNAPSHOT_SCHEMA_V2 = "forecast-loop.evidence-snapshot/v2"
AGENT_SIGNAL_SCHEMA_V2 = "forecast-loop.agent-signal/v2"
CODEX_HANDOFF_SCHEMA_V3 = "forecast-loop.codex-handoff/v3"
REFLECTION_SCHEMA_V2 = "forecast-loop.reflection/v2"
AGENT_EVAL_SUITE_SCHEMA_V2 = "forecast-loop.agent-eval-suite/v2"
AGENT_EVAL_REPORT_SCHEMA_V2 = "forecast-loop.agent-eval-report/v2"

CSI1000 = "000852.SH"
CSI300 = "000300.SH"
CSI1000_D1_TARGET = "csi1000-absolute-d1"
CSI1000_RELATIVE_W1_TARGET = "csi1000-vs-csi300-relative-w1"
CSI1000_D20_RESEARCH_TARGET = "csi1000-absolute-d20"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class V2Horizon(StrEnum):
    D1 = "D1"
    W1 = "W1"
    D20 = "D20"

    @property
    def session_count(self) -> int:
        return {self.D1: 1, self.W1: 5, self.D20: 20}[self]


class SignalKindV2(StrEnum):
    NATURAL_VIEW = "natural_view"
    D1_IMPACT = "d1_impact"
    STRATEGY_FORECAST = "strategy_forecast"
    RISK_CRITIQUE = "risk_critique"
    DECISION_FORECAST = "decision_forecast"


class ProbabilitiesV2(ContractModel):
    up: float = Field(ge=0, le=1, allow_inf_nan=False)
    neutral: float = Field(ge=0, le=1, allow_inf_nan=False)
    down: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def sum_to_one(self) -> ProbabilitiesV2:
        if not math.isclose(self.up + self.neutral + self.down, 1.0, abs_tol=1e-6):
            raise ValueError("probabilities must sum to one")
        return self

    def as_dict(self) -> dict[str, float]:
        return self.model_dump()


class ResearchInstrumentV2(ContractModel):
    code: str
    name: str
    role: Literal["primary", "benchmark"]
    data_required: bool = True


class ResearchTargetV2(ContractModel):
    target_id: str
    label: str
    outcome_kind: Literal["absolute_return", "relative_return"]
    horizon: V2Horizon
    lane: Literal["formal", "shadow"]
    primary_instrument: str
    comparison_instrument: str | None = None

    @model_validator(mode="after")
    def validate_comparison(self) -> ResearchTargetV2:
        if self.outcome_kind == "relative_return" and not self.comparison_instrument:
            raise ValueError("relative target requires comparison_instrument")
        if self.outcome_kind == "absolute_return" and self.comparison_instrument is not None:
            raise ValueError("absolute target cannot declare comparison_instrument")
        return self


class ResearchScopeV2(ContractModel):
    target_id: str
    label: str
    horizon: V2Horizon
    instrument: str
    lane: Literal["shadow"] = "shadow"


class ResearchProgramBodyV2(ContractModel):
    schema_version: Literal["forecast-loop.research-program/v2"] = RESEARCH_PROGRAM_SCHEMA_V2
    program_id: str = "csi1000-focused-loop"
    version: str = "2.0.0"
    market: Literal["CN"] = "CN"
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    calendar_id: Literal["SSE"] = "SSE"
    instruments: tuple[ResearchInstrumentV2, ...]
    decision_targets: tuple[ResearchTargetV2, ...]
    research_scopes: tuple[ResearchScopeV2, ...]

    @model_validator(mode="after")
    def validate_focused_program(self) -> ResearchProgramBodyV2:
        instruments = {item.code: item.role for item in self.instruments}
        if instruments != {CSI1000: "primary", CSI300: "benchmark"}:
            raise ValueError("v2 research program requires CSI1000 primary and CSI300 benchmark")
        targets = {item.target_id: item for item in self.decision_targets}
        if set(targets) != {CSI1000_D1_TARGET, CSI1000_RELATIVE_W1_TARGET}:
            raise ValueError("v2 research program requires exactly the D1 and W1 targets")
        d1 = targets[CSI1000_D1_TARGET]
        w1 = targets[CSI1000_RELATIVE_W1_TARGET]
        if (
            d1.horizon is not V2Horizon.D1
            or d1.lane != "formal"
            or d1.primary_instrument != CSI1000
            or w1.horizon is not V2Horizon.W1
            or w1.lane != "shadow"
            or w1.primary_instrument != CSI1000
            or w1.comparison_instrument != CSI300
        ):
            raise ValueError("v2 target semantics are fixed")
        scopes = {item.target_id: item for item in self.research_scopes}
        if set(scopes) != {CSI1000_D20_RESEARCH_TARGET}:
            raise ValueError("v2 research program requires the CSI1000 D20 macro scope")
        return self


class ResearchProgramV2(ResearchProgramBodyV2):
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _jsonable(value: Any) -> Any:
    """Recursively match Pydantic JSON mode before hashing nested contracts."""

    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def content_hash(value: Any, *, exclude: tuple[str, ...] = ("content_hash",)) -> str:
    if isinstance(value, BaseModel):
        body = value.model_dump(mode="json", exclude=set(exclude))
    elif isinstance(value, dict):
        body = {key: item for key, item in value.items() if key not in exclude}
    else:
        body = value
    return hashlib.sha256(canonical_json(body)).hexdigest()


def seal_research_program(body: ResearchProgramBodyV2) -> ResearchProgramV2:
    payload = body.model_dump(mode="json")
    return ResearchProgramV2(**payload, content_hash=content_hash(payload))


DEFAULT_RESEARCH_PROGRAM_V2 = seal_research_program(
    ResearchProgramBodyV2(
        instruments=(
            ResearchInstrumentV2(code=CSI1000, name="中证1000", role="primary"),
            ResearchInstrumentV2(code=CSI300, name="沪深300", role="benchmark"),
        ),
        decision_targets=(
            ResearchTargetV2(
                target_id=CSI1000_D1_TARGET,
                label="中证1000下一交易日",
                outcome_kind="absolute_return",
                horizon=V2Horizon.D1,
                lane="formal",
                primary_instrument=CSI1000,
            ),
            ResearchTargetV2(
                target_id=CSI1000_RELATIVE_W1_TARGET,
                label="中证1000相对沪深300未来5个交易日",
                outcome_kind="relative_return",
                horizon=V2Horizon.W1,
                lane="shadow",
                primary_instrument=CSI1000,
                comparison_instrument=CSI300,
            ),
        ),
        research_scopes=(
            ResearchScopeV2(
                target_id=CSI1000_D20_RESEARCH_TARGET,
                label="中证1000未来20个交易日宏观状态",
                horizon=V2Horizon.D20,
                instrument=CSI1000,
            ),
        ),
    )
)


class SourceStampV2(ContractModel):
    source_url: str = Field(min_length=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    ingested_at: datetime

    @field_validator("observed_at", "ingested_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("source timestamps must be timezone-aware")
        return value


class TradingCalendarStampV2(SourceStampV2):
    """Source stamp whose hash commits the exact frozen exchange sessions."""

    base_session: date
    sessions: list[date] = Field(min_length=20)

    @model_validator(mode="after")
    def validate_session_payload(self) -> TradingCalendarStampV2:
        if (
            self.sessions != sorted(self.sessions)
            or len(self.sessions) != len(set(self.sessions))
            or any(session <= self.base_session for session in self.sessions)
        ):
            raise ValueError(
                "trading calendar sessions must be unique exchange sessions after base_session"
            )
        expected_hash = content_hash(
            {
                "schema_version": "forecast-loop.trading-calendar-payload/v2",
                "base_session": self.base_session,
                "sessions": self.sessions,
            }
        )
        if self.source_hash != expected_hash:
            raise ValueError("calendar source_hash does not bind the frozen sessions")
        return self


class DailyReturnV2(ContractModel):
    trade_date: date
    daily_return: float = Field(allow_inf_nan=False)
    source: SourceStampV2


class InstrumentEvidenceV2(ContractModel):
    code: str
    volatility_20d: float = Field(ge=0, allow_inf_nan=False)
    returns: list[DailyReturnV2] = Field(min_length=20)

    @model_validator(mode="after")
    def ordered_unique_history(self) -> InstrumentEvidenceV2:
        dates = [item.trade_date for item in self.returns]
        if dates != sorted(dates) or len(dates) != len(set(dates)):
            raise ValueError("return history must be strictly ordered and unique")
        recomputed = statistics.stdev(
            item.daily_return for item in self.returns[-20:]
        )
        if not math.isclose(
            self.volatility_20d,
            recomputed,
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            raise ValueError(
                "volatility_20d must equal the sample volatility recomputed from "
                "the frozen trailing 20 daily returns"
            )
        return self


class EvidenceItemV2(ContractModel):
    item_id: str
    title: str
    summary: str
    published_at: datetime
    ingested_at: datetime
    source_url: str
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    entities: list[str] = Field(default_factory=list)

    @field_validator("published_at", "ingested_at")
    @classmethod
    def aware_item_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evidence item timestamps must be timezone-aware")
        return value


class EvidenceSnapshotBodyV2(ContractModel):
    schema_version: Literal["forecast-loop.evidence-snapshot/v2"] = EVIDENCE_SNAPSHOT_SCHEMA_V2
    program_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    as_of: datetime
    data_cutoff: datetime
    created_at: datetime
    base_session: date
    future_sessions: list[date] = Field(min_length=20)
    calendar_source: TradingCalendarStampV2
    instruments: dict[str, InstrumentEvidenceV2]
    items: list[EvidenceItemV2] = Field(default_factory=list)

    @field_validator("as_of", "data_cutoff", "created_at")
    @classmethod
    def aware_snapshot_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("snapshot timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_snapshot_boundary(self) -> EvidenceSnapshotBodyV2:
        if self.program_hash != DEFAULT_RESEARCH_PROGRAM_V2.content_hash:
            raise ValueError("snapshot program_hash does not match the v2 research program")
        if set(self.instruments) != {CSI1000, CSI300}:
            raise ValueError("snapshot must contain exactly CSI1000 and CSI300")
        if any(key != item.code for key, item in self.instruments.items()):
            raise ValueError("instrument evidence key/code mismatch")
        if (
            self.future_sessions != sorted(self.future_sessions)
            or len(self.future_sessions) != len(set(self.future_sessions))
            or any(session <= self.base_session for session in self.future_sessions)
        ):
            raise ValueError("future sessions must be unique exchange sessions after base_session")
        if (
            self.calendar_source.base_session != self.base_session
            or self.calendar_source.sessions != self.future_sessions
        ):
            raise ValueError("future sessions must exactly match the frozen calendar payload")
        if self.data_cutoff > self.as_of or self.created_at < self.data_cutoff:
            raise ValueError("snapshot time ordering is invalid")
        if self.calendar_source.observed_at > self.data_cutoff:
            raise ValueError("calendar was observed after the evidence cutoff")
        if self.calendar_source.ingested_at > self.data_cutoff:
            raise ValueError("calendar was ingested after the evidence cutoff")
        return_dates = {
            code: [row.trade_date for row in item.returns]
            for code, item in self.instruments.items()
        }
        if return_dates[CSI1000] != return_dates[CSI300]:
            raise ValueError("CSI1000 and CSI300 return histories must align by session")
        for item in self.instruments.values():
            if item.returns[-1].trade_date != self.base_session:
                raise ValueError("return history must end on base_session")
            if any(
                row.source.observed_at > self.data_cutoff
                or row.source.ingested_at > self.data_cutoff
                for row in item.returns
            ):
                raise ValueError("return history crossed the evidence cutoff")
        for item in self.items:
            if item.published_at > self.data_cutoff or item.ingested_at > self.data_cutoff:
                raise ValueError("evidence item is later than the evidence cutoff")
        return self


class EvidenceSnapshotV2(EvidenceSnapshotBodyV2):
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_seal(self) -> EvidenceSnapshotV2:
        if self.content_hash != content_hash(self):
            raise ValueError("evidence snapshot content_hash mismatch")
        return self


def seal_evidence_snapshot(body: EvidenceSnapshotBodyV2) -> EvidenceSnapshotV2:
    payload = body.model_dump(mode="json")
    return EvidenceSnapshotV2(**payload, content_hash=content_hash(payload))


class AgentSignalDraftV2(ContractModel):
    signal_kind: SignalKindV2
    target_id: str
    natural_horizon: V2Horizon
    decision_horizon: V2Horizon | None = None
    direction: Literal["up", "neutral", "down"] | None = None
    probabilities: ProbabilitiesV2 | None = None
    impact: Literal["positive", "none", "negative"] | None = None
    importance: Literal["none", "low", "medium", "high"] | None = None
    risk_severity: Literal["none", "low", "medium", "high"] | None = None
    state_available: bool = True
    abstain: bool = False
    rationale: str = Field(min_length=1, max_length=8000)
    transmission_chain: list[str] = Field(default_factory=list, max_length=20)
    counter_evidence: list[str] = Field(default_factory=list, max_length=20)
    invalidation_conditions: list[str] = Field(default_factory=list, max_length=20)
    evidence_item_ids: list[str] = Field(default_factory=list, max_length=100)
    wiki_entry_id: str
    wiki_version: str
    wiki_section: str
    wiki_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_kind_payload(self) -> AgentSignalDraftV2:
        probabilistic = {
            SignalKindV2.NATURAL_VIEW,
            SignalKindV2.STRATEGY_FORECAST,
            SignalKindV2.DECISION_FORECAST,
        }
        if self.signal_kind in probabilistic:
            if self.probabilities is None or self.direction is None:
                raise ValueError("probabilistic signal requires probabilities and direction")
            values = self.probabilities.as_dict()
            maximum = max(values.values())
            winners = [label for label, value in values.items() if math.isclose(value, maximum)]
            if len(winners) != 1:
                raise ValueError("the predicted class must have a unique maximum probability")
            expected = winners[0]
            if self.direction != expected:
                raise ValueError("direction must match the maximum-probability class")
            if self.impact is not None or self.importance is not None:
                raise ValueError("probabilistic signal cannot contain impact fields")
            if self.risk_severity is not None:
                raise ValueError("probabilistic signal cannot contain risk severity")
            if self.abstain:
                raise ValueError("probabilistic signals must provide a complete forecast")
        elif self.signal_kind is SignalKindV2.D1_IMPACT:
            if self.probabilities is not None or self.direction is not None:
                raise ValueError("D1 impact is not a market probability forecast")
            if self.risk_severity is not None:
                raise ValueError("D1 impact cannot contain risk severity")
            if self.impact is None or self.importance is None:
                raise ValueError("D1 impact requires impact and importance")
            if not self.state_available:
                if not self.abstain or self.impact != "none" or self.importance != "none":
                    raise ValueError(
                        "missing natural state must produce an explicit no-impact abstention"
                    )
            if self.abstain and (self.impact != "none" or self.importance != "none"):
                raise ValueError("an abstaining D1 impact must be explicitly no-impact")
            if (self.impact == "none") != (self.importance == "none"):
                raise ValueError("no-impact and no-importance must be declared together")
            if not self.transmission_chain and not self.abstain:
                raise ValueError("non-abstaining D1 impact requires a transmission chain")
        else:
            if any(
                value is not None
                for value in (
                    self.probabilities,
                    self.direction,
                    self.impact,
                    self.importance,
                )
            ):
                raise ValueError("risk critique cannot contain a direction vote")
            if self.risk_severity is None:
                raise ValueError("risk critique requires risk_severity")
            if not self.counter_evidence or not self.invalidation_conditions:
                raise ValueError(
                    "risk critique requires counter evidence and invalidation conditions"
                )
        return self


class AgentSignalEnvelopeV2(ContractModel):
    schema_version: Literal["forecast-loop.agent-signal/v2"] = AGENT_SIGNAL_SCHEMA_V2
    signal_id: str
    run_id: str
    agent_id: str
    agent_version: str
    model_name: str
    prompt_version: str
    target_id: str
    signal_kind: SignalKindV2
    natural_horizon: V2Horizon
    decision_horizon: V2Horizon | None
    generation_reason: Literal["daily", "scheduled", "bootstrap", "external_shadow"]
    anchor_date: date
    target_date: date
    evidence_cutoff: datetime
    program_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    threshold: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    baseline_probabilities: ProbabilitiesV2 | None = None
    draft: AgentSignalDraftV2
    created_at: datetime
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_identity_and_seal(self) -> AgentSignalEnvelopeV2:
        if self.program_hash != DEFAULT_RESEARCH_PROGRAM_V2.content_hash:
            raise ValueError("signal program hash mismatch")
        identity = (
            self.target_id,
            self.signal_kind,
            self.natural_horizon,
            self.decision_horizon,
        )
        draft_identity = (
            self.draft.target_id,
            self.draft.signal_kind,
            self.draft.natural_horizon,
            self.draft.decision_horizon,
        )
        if identity != draft_identity:
            raise ValueError("signal envelope/draft identity mismatch")
        if self.target_date <= self.anchor_date:
            raise ValueError("signal target_date must follow anchor_date")
        if self.content_hash != content_hash(self):
            raise ValueError("agent signal content_hash mismatch")
        return self


class HandoffAssignmentV3(ContractModel):
    assignment_id: str
    agent_id: str
    agent_version: str
    model_name: str
    prompt_version: str
    producer: Literal["codex", "deterministic"] = "codex"
    signal_kind: SignalKindV2
    target_id: str
    natural_horizon: V2Horizon
    decision_horizon: V2Horizon | None
    generation_reason: Literal["daily", "scheduled", "bootstrap"]
    anchor_date: date
    target_date: date
    state_available: bool = True
    prior_signal_id: str | None = None
    context_signal_ids: list[str] = Field(default_factory=list)
    depends_on_assignment_ids: list[str] = Field(default_factory=list)
    baseline_probabilities: ProbabilitiesV2 | None = None
    allowed_evidence_item_ids: list[str] = Field(default_factory=list)
    wiki_entry_id: str
    wiki_version: str
    wiki_section: str
    wiki_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: str


class CodexHandoffRequestV3(ContractModel):
    schema_version: Literal["forecast-loop.codex-handoff/v3"] = CODEX_HANDOFF_SCHEMA_V3
    run_id: str
    program: ResearchProgramV2
    snapshot: EvidenceSnapshotV2
    frozen_wiki: list[dict[str, Any]] = Field(min_length=1)
    context_signals: list[AgentSignalEnvelopeV2] = Field(default_factory=list)
    prepared_at: datetime
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    trace_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    assignments: list[HandoffAssignmentV3] = Field(min_length=1)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_request_hash(self) -> CodexHandoffRequestV3:
        identities = [item.assignment_id for item in self.assignments]
        if len(identities) != len(set(identities)):
            raise ValueError("handoff assignment IDs must be unique")
        context_ids = [item.signal_id for item in self.context_signals]
        if len(context_ids) != len(set(context_ids)):
            raise ValueError("handoff context signal IDs must be unique")
        available = set(context_ids).union(identities)
        for assignment in self.assignments:
            if not set(assignment.context_signal_ids).issubset(context_ids):
                raise ValueError("assignment references an unavailable context signal")
            if not set(assignment.depends_on_assignment_ids).issubset(identities):
                raise ValueError("assignment dependency is unavailable")
            if assignment.assignment_id in assignment.depends_on_assignment_ids:
                raise ValueError("assignment cannot depend on itself")
        del available
        if self.request_hash != content_hash(self, exclude=("request_hash",)):
            raise ValueError("handoff request hash mismatch")
        return self


class CodexDraftRecordV3(ContractModel):
    assignment_id: str
    draft: AgentSignalDraftV2


class CodexDraftBundleV3(ContractModel):
    schema_version: Literal["forecast-loop.codex-handoff/v3"] = CODEX_HANDOFF_SCHEMA_V3
    run_id: str
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    generated_by: dict[str, str]
    drafts: list[CodexDraftRecordV3] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_assignments(self) -> CodexDraftBundleV3:
        identities = [item.assignment_id for item in self.drafts]
        if len(identities) != len(set(identities)):
            raise ValueError("draft assignment IDs must be unique")
        if self.generated_by.get("surface") != "codex":
            raise ValueError("prediction drafts must identify the Codex surface")
        if self.generated_by.get("model") != "gpt-5.6-sol":
            raise ValueError("prediction drafts must use the frozen model")
        if self.generated_by.get("reasoning_effort") != "high":
            raise ValueError("prediction drafts must use the frozen effort")
        return self


class ReasoningReviewInputV2(ContractModel):
    schema_version: Literal["forecast-loop.reasoning-review-input/v2"] = (
        "forecast-loop.reasoning-review-input/v2"
    )
    signal_id: str
    agent_id: str
    target_id: str
    signal_kind: SignalKindV2
    natural_horizon: V2Horizon
    decision_horizon: V2Horizon | None
    anchor_date: date
    target_date: date
    evidence_cutoff: datetime
    rationale: str
    transmission_chain: list[str]
    counter_evidence: list[str]
    invalidation_conditions: list[str]
    probabilities: ProbabilitiesV2 | None
    impact: Literal["positive", "none", "negative"] | None
    importance: Literal["none", "low", "medium", "high"] | None
    risk_severity: Literal["none", "low", "medium", "high"] | None
    state_available: bool
    abstain: bool
    evidence_item_ids: list[str]
    wiki_entry_id: str
    wiki_version: str
    wiki_section: str
    wiki_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_blind_hash(self) -> ReasoningReviewInputV2:
        if self.review_input_hash != content_hash(self, exclude=("review_input_hash",)):
            raise ValueError("reasoning review input hash mismatch")
        return self


class ReasoningReviewDraftRecordV2(ContractModel):
    signal_id: str
    review_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    rubric: ReasoningRubricDraftV2


class ReasoningReviewDraftBundleV2(ContractModel):
    schema_version: Literal["forecast-loop.reasoning-review-drafts/v2"] = (
        "forecast-loop.reasoning-review-drafts/v2"
    )
    run_id: str
    generated_at: datetime
    generated_by: dict[str, str]
    reviews: list[ReasoningReviewDraftRecordV2] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_generator(self) -> ReasoningReviewDraftBundleV2:
        if self.generated_by.get("model") != "gpt-5.6-sol":
            raise ValueError("reasoning review requires gpt-5.6-sol")
        if self.generated_by.get("reasoning_effort") != "high":
            raise ValueError("reasoning review requires high effort")
        signal_ids = [item.signal_id for item in self.reviews]
        if len(signal_ids) != len(set(signal_ids)):
            raise ValueError("reasoning review signal IDs must be unique")
        return self


class ReflectionDraftBodyV2(ContractModel):
    """Outcome-bound operator analysis for one immutable v2 forecast episode."""

    schema_version: Literal["forecast-loop.reflection/v2"] = REFLECTION_SCHEMA_V2
    forecast_id: str = Field(min_length=1)
    forecast_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_id: str = Field(min_length=1)
    evaluation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_id: Literal[
        "csi1000-absolute-d1",
        "csi1000-vs-csi300-relative-w1",
    ]
    anchor_date: date
    target_date: date
    actual_label: Literal["up", "neutral", "down"]
    verdict: Literal[
        "right_reason",
        "lucky_correct",
        "wrong",
        "noise",
        "unresolved",
    ]
    findings: list[dict[str, Any]] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_episode(self) -> ReflectionDraftBodyV2:
        if self.target_date <= self.anchor_date:
            raise ValueError("reflection target_date must follow anchor_date")
        return self


class ReflectionDraftV2(ReflectionDraftBodyV2):
    """Sealed reflection input persisted verbatim for deterministic replay."""

    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_content_hash(self) -> ReflectionDraftV2:
        if self.content_hash != content_hash(self):
            raise ValueError("reflection content_hash mismatch")
        return self


def seal_reflection_draft_v2(body: ReflectionDraftBodyV2) -> ReflectionDraftV2:
    payload = body.model_dump(mode="json")
    return ReflectionDraftV2(**payload, content_hash=content_hash(payload))


def reasoning_review_input(signal: AgentSignalEnvelopeV2) -> ReasoningReviewInputV2:
    """Project only pre-outcome reasoning fields; realized outcomes cannot enter."""

    payload = {
        "schema_version": "forecast-loop.reasoning-review-input/v2",
        "signal_id": signal.signal_id,
        "agent_id": signal.agent_id,
        "target_id": signal.target_id,
        "signal_kind": signal.signal_kind,
        "natural_horizon": signal.natural_horizon,
        "decision_horizon": signal.decision_horizon,
        "anchor_date": signal.anchor_date,
        "target_date": signal.target_date,
        "evidence_cutoff": signal.evidence_cutoff,
        "rationale": signal.draft.rationale,
        "transmission_chain": signal.draft.transmission_chain,
        "counter_evidence": signal.draft.counter_evidence,
        "invalidation_conditions": signal.draft.invalidation_conditions,
        "probabilities": signal.draft.probabilities,
        "impact": signal.draft.impact,
        "importance": signal.draft.importance,
        "risk_severity": signal.draft.risk_severity,
        "state_available": signal.draft.state_available,
        "abstain": signal.draft.abstain,
        "evidence_item_ids": signal.draft.evidence_item_ids,
        "wiki_entry_id": signal.draft.wiki_entry_id,
        "wiki_version": signal.draft.wiki_version,
        "wiki_section": signal.draft.wiki_section,
        "wiki_content_hash": signal.draft.wiki_content_hash,
    }
    return ReasoningReviewInputV2(
        **payload,
        review_input_hash=content_hash(payload, exclude=("review_input_hash",)),
    )


class OutcomePriceSourceStampV2(ContractModel):
    instrument: Literal[CSI1000, CSI300]
    start_trade_date: date
    end_trade_date: date
    source_url: str = Field(min_length=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    ingested_at: datetime

    @field_validator("observed_at", "ingested_at")
    @classmethod
    def aware_source_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("outcome source timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_source_order(self) -> OutcomePriceSourceStampV2:
        if self.end_trade_date <= self.start_trade_date:
            raise ValueError("outcome price source dates must be strictly ordered")
        if self.ingested_at < self.observed_at:
            raise ValueError("outcome price source was ingested before it was observed")
        return self


class OutcomeCalendarSourceStampV2(ContractModel):
    sessions: list[date] = Field(min_length=2, max_length=21)
    source_url: str = Field(min_length=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    ingested_at: datetime

    @field_validator("observed_at", "ingested_at")
    @classmethod
    def aware_source_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("outcome calendar timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_calendar_order(self) -> OutcomeCalendarSourceStampV2:
        if self.sessions != sorted(self.sessions) or len(self.sessions) != len(
            set(self.sessions)
        ):
            raise ValueError("outcome calendar sessions must be ordered and unique")
        if self.ingested_at < self.observed_at:
            raise ValueError("outcome calendar was ingested before it was observed")
        return self


class OutcomeObservationBodyV2(ContractModel):
    schema_version: Literal["forecast-loop.outcome-observation/v2"] = (
        "forecast-loop.outcome-observation/v2"
    )
    program_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: Literal["demo", "live"] = "demo"
    target_id: str
    anchor_date: date
    target_date: date
    primary_start_close: float = Field(gt=0, allow_inf_nan=False)
    primary_end_close: float = Field(gt=0, allow_inf_nan=False)
    comparison_start_close: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    comparison_end_close: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    observed_at: datetime
    primary_source: OutcomePriceSourceStampV2 | None = None
    comparison_source: OutcomePriceSourceStampV2 | None = None
    calendar_source: OutcomeCalendarSourceStampV2 | None = None
    source_hashes: dict[str, str] = Field(default_factory=dict)

    @field_validator("observed_at")
    @classmethod
    def aware_observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("outcome observed_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_observation(self) -> OutcomeObservationBodyV2:
        if self.program_hash != DEFAULT_RESEARCH_PROGRAM_V2.content_hash:
            raise ValueError("outcome program hash mismatch")
        valid_targets = {
            CSI1000_D1_TARGET,
            CSI1000_RELATIVE_W1_TARGET,
            CSI1000_D20_RESEARCH_TARGET,
        }
        if self.target_id not in valid_targets:
            raise ValueError("outcome target is not part of the v2 program")
        if self.target_date <= self.anchor_date:
            raise ValueError("outcome target_date must follow anchor_date")
        if self.target_id == CSI1000_RELATIVE_W1_TARGET and (
            self.comparison_start_close is None or self.comparison_end_close is None
        ):
            raise ValueError("relative outcome requires benchmark closes")
        if self.target_id != CSI1000_RELATIVE_W1_TARGET and (
            self.comparison_start_close is not None
            or self.comparison_end_close is not None
        ):
            raise ValueError("absolute outcomes cannot contain benchmark closes")
        if not self.source_hashes or any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in self.source_hashes.values()
        ):
            raise ValueError("outcome source hashes must be lowercase SHA-256 values")
        if self.mode == "live":
            self._validate_live_provenance()
        return self

    def _validate_live_provenance(self) -> None:
        if self.primary_source is None or self.calendar_source is None:
            raise ValueError("live outcome requires primary and calendar source stamps")
        if (
            self.primary_source.instrument != CSI1000
            or self.primary_source.start_trade_date != self.anchor_date
            or self.primary_source.end_trade_date != self.target_date
        ):
            raise ValueError("live primary source is not bound to the outcome episode")
        expected_sessions = {
            CSI1000_D1_TARGET: 2,
            CSI1000_RELATIVE_W1_TARGET: 6,
            CSI1000_D20_RESEARCH_TARGET: 21,
        }[self.target_id]
        if (
            len(self.calendar_source.sessions) != expected_sessions
            or self.calendar_source.sessions[0] != self.anchor_date
            or self.calendar_source.sessions[-1] != self.target_date
        ):
            raise ValueError(
                "live outcome calendar is not bound to the target horizon and episode"
            )
        if self.target_id == CSI1000_RELATIVE_W1_TARGET:
            if (
                self.comparison_source is None
                or self.comparison_source.instrument != CSI300
                or self.comparison_source.start_trade_date != self.anchor_date
                or self.comparison_source.end_trade_date != self.target_date
            ):
                raise ValueError("live benchmark source is not bound to the outcome episode")
        elif self.comparison_source is not None:
            raise ValueError("absolute live outcomes cannot contain a benchmark source stamp")
        stamps = [self.primary_source, self.calendar_source]
        if self.comparison_source is not None:
            stamps.append(self.comparison_source)
        if any(stamp.ingested_at > self.observed_at for stamp in stamps):
            raise ValueError("live outcome source stamp crossed observed_at")
        required_hashes = {
            "calendar": self.calendar_source.source_hash,
            CSI1000: self.primary_source.source_hash,
        }
        if self.comparison_source is not None:
            required_hashes[CSI300] = self.comparison_source.source_hash
        if self.source_hashes != required_hashes:
            raise ValueError("live outcome source_hashes do not match the bound stamps")


class OutcomeObservationV2(OutcomeObservationBodyV2):
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_seal(self) -> OutcomeObservationV2:
        if self.content_hash != content_hash(self):
            raise ValueError("outcome observation content_hash mismatch")
        return self


def seal_outcome_observation(
    body: OutcomeObservationV2 | dict[str, Any],
) -> OutcomeObservationV2:
    source = (
        body.model_dump(mode="json", exclude={"content_hash"})
        if isinstance(body, OutcomeObservationV2)
        else {key: value for key, value in body.items() if key != "content_hash"}
    )
    payload = OutcomeObservationBodyV2.model_validate(source).model_dump(mode="json")
    return OutcomeObservationV2(**payload, content_hash=content_hash(payload))


class ReasoningRubricDraftV2(ContractModel):
    model: Literal["gpt-5.6-sol"]
    reasoning_effort: Literal["high"]
    evidence_relevance: int = Field(ge=0, le=2)
    causal_chain: int = Field(ge=0, le=2)
    target_horizon_mapping: int = Field(ge=0, le=2)
    counter_evidence_and_invalidation: int = Field(ge=0, le=2)
    probability_uncertainty_consistency: int = Field(ge=0, le=2)
    advisory: str = Field(min_length=1, max_length=4000)

    @property
    def total_score(self) -> int:
        return sum(
            (
                self.evidence_relevance,
                self.causal_chain,
                self.target_horizon_mapping,
                self.counter_evidence_and_invalidation,
                self.probability_uncertainty_consistency,
            )
        )

    @property
    def has_zero_dimension(self) -> bool:
        return 0 in (
            self.evidence_relevance,
            self.causal_chain,
            self.target_horizon_mapping,
            self.counter_evidence_and_invalidation,
            self.probability_uncertainty_consistency,
        )


def target_date(snapshot: EvidenceSnapshotV2, horizon: V2Horizon) -> date:
    return snapshot.future_sessions[horizon.session_count - 1]


def absolute_threshold(volatility_20d: float, horizon: V2Horizon) -> float:
    if not math.isfinite(volatility_20d) or volatility_20d < 0:
        raise ValueError("volatility must be finite and non-negative")
    return 0.25 * volatility_20d * math.sqrt(horizon.session_count)


def relative_volatility_20d(snapshot: EvidenceSnapshotV2) -> float:
    primary = {row.trade_date: row.daily_return for row in snapshot.instruments[CSI1000].returns}
    benchmark = {row.trade_date: row.daily_return for row in snapshot.instruments[CSI300].returns}
    shared = sorted(set(primary).intersection(benchmark))[-20:]
    if len(shared) < 20:
        raise ValueError("relative volatility requires 20 aligned return observations")
    differences = [primary[item] - benchmark[item] for item in shared]
    return statistics.stdev(differences)


def threshold_for_target(snapshot: EvidenceSnapshotV2, target_id: str) -> float:
    if target_id == CSI1000_D1_TARGET:
        return absolute_threshold(
            snapshot.instruments[CSI1000].volatility_20d,
            V2Horizon.D1,
        )
    if target_id == CSI1000_RELATIVE_W1_TARGET:
        return absolute_threshold(relative_volatility_20d(snapshot), V2Horizon.W1)
    if target_id == CSI1000_D20_RESEARCH_TARGET:
        return absolute_threshold(
            snapshot.instruments[CSI1000].volatility_20d,
            V2Horizon.D20,
        )
    raise KeyError(target_id)


def classify_outcome(value: float, threshold: float) -> Literal["up", "neutral", "down"]:
    if not math.isfinite(value) or not math.isfinite(threshold) or threshold < 0:
        raise ValueError("outcome and threshold must be finite")
    if value > threshold:
        return "up"
    if value < -threshold:
        return "down"
    return "neutral"


def realized_outcome(observation: OutcomeObservationV2) -> float:
    primary = observation.primary_end_close / observation.primary_start_close - 1.0
    if observation.target_id != CSI1000_RELATIVE_W1_TARGET:
        return primary
    assert observation.comparison_start_close is not None
    assert observation.comparison_end_close is not None
    comparison = observation.comparison_end_close / observation.comparison_start_close - 1.0
    return primary - comparison


def multiclass_brier(probabilities: ProbabilitiesV2, actual: str) -> float:
    if actual not in {"up", "neutral", "down"}:
        raise ValueError("unknown actual class")
    return sum(
        (probabilities.as_dict()[label] - (1.0 if actual == label else 0.0)) ** 2
        for label in ("up", "neutral", "down")
    ) / 3.0


def smoothed_baseline(labels: list[str]) -> ProbabilitiesV2:
    counts = {label: 1 for label in ("up", "neutral", "down")}
    for label in labels:
        if label not in counts:
            raise ValueError("unknown historical label")
        counts[label] += 1
    total = sum(counts.values())
    return ProbabilitiesV2(**{label: count / total for label, count in counts.items()})


def deterministic_review_checks(draft: AgentSignalDraftV2) -> dict[str, bool]:
    """Checks that can be run without revealing a realized outcome."""

    return {
        "has_citable_identity": bool(draft.wiki_entry_id and draft.wiki_section),
        "has_rationale": bool(draft.rationale.strip()),
        "has_counter_evidence": bool(draft.counter_evidence),
        "has_invalidation_condition": bool(draft.invalidation_conditions),
        "has_transmission_chain_when_applicable": (
            draft.signal_kind is not SignalKindV2.D1_IMPACT
            or draft.abstain
            or bool(draft.transmission_chain)
        ),
        "probabilities_are_structurally_valid": (
            draft.probabilities is not None
            if draft.signal_kind
            in {
                SignalKindV2.NATURAL_VIEW,
                SignalKindV2.STRATEGY_FORECAST,
                SignalKindV2.DECISION_FORECAST,
            }
            else True
        ),
    }


def deterministic_human_sample(review_input_hash: str) -> bool:
    if len(review_input_hash) != 64:
        raise ValueError("review_input_hash must be SHA-256")
    return int(review_input_hash, 16) % 10 == 0


def requires_human_review(
    *,
    review_input_hash: str,
    deterministic_checks: dict[str, bool],
    rubric: ReasoningRubricDraftV2,
) -> bool:
    return (
        deterministic_human_sample(review_input_hash)
        or not all(deterministic_checks.values())
        or rubric.total_score < 7
        or rubric.has_zero_dimension
    )
