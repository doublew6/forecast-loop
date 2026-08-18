"""Public contracts for an audited pre-market open-to-open research episode.

The protocol is intentionally separate from research-program/v2.  Existing
close-to-close forecasts keep their original identity and meaning; new
pre-market forecasts use a distinct target, snapshot, and evaluation schema.
Provider-specific collectors remain private adapters that emit this contract.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from datetime import date, datetime, time
from enum import Enum, StrEnum
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PREMARKET_PROGRAM_SCHEMA_V1 = "forecast-loop.premarket-program/v1"
PREMARKET_SNAPSHOT_SCHEMA_V1 = "forecast-loop.premarket-evidence-snapshot/v1"
PREMARKET_HANDOFF_SCHEMA_V1 = "forecast-loop.premarket-handoff/v1"
PREMARKET_FORECAST_SCHEMA_V1 = "forecast-loop.premarket-forecast/v1"
PREMARKET_OUTCOME_SCHEMA_V1 = "forecast-loop.premarket-outcome/v1"
PREMARKET_EVALUATION_SCHEMA_V1 = "forecast-loop.premarket-evaluation/v1"

CSI1000 = "000852.SH"
CSI1000_OPEN_TO_OPEN_D1_TARGET = "csi1000-open-to-open-d1"
PREMARKET_TIMEZONE = "Asia/Shanghai"

ANALYST_AGENT_IDS = (
    "macro_policy_agent",
    "market_news_agent",
    "global_market_agent",
    "ai_storage_industry_agent",
)
STRATEGY_AGENT_ID = "strategy_agent"
RISK_CRITIC_AGENT_ID = "risk_critic_agent"
CODEX_AGENT_IDS = ANALYST_AGENT_IDS + (STRATEGY_AGENT_ID, RISK_CRITIC_AGENT_ID)
ALL_AGENT_IDS = CODEX_AGENT_IDS + ("cio_agent",)


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _jsonable(value: Any) -> Any:
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


class EvidenceCategoryV1(StrEnum):
    NEWS = "news"
    MACRO_POLICY = "macro_policy"
    GLOBAL_EQUITY = "global_equity"
    FX_RATES = "fx_rates"
    COMMODITY = "commodity"
    INDUSTRY = "industry"
    DOMESTIC_MARKET = "domestic_market"


class SourceTierV1(StrEnum):
    TIER_1 = "tier_1_primary"
    TIER_2 = "tier_2_professional"
    TIER_3 = "tier_3_secondary"
    TIER_4 = "tier_4_unverified"


class ProbabilitiesV1(ContractModel):
    up: float = Field(ge=0, le=1, allow_inf_nan=False)
    neutral: float = Field(ge=0, le=1, allow_inf_nan=False)
    down: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_probabilities(self) -> ProbabilitiesV1:
        values = self.model_dump()
        if not math.isclose(sum(values.values()), 1.0, abs_tol=1e-6):
            raise ValueError("probabilities must sum to one")
        return self

    @property
    def direction(self) -> Literal["up", "neutral", "down"]:
        values = self.model_dump()
        maximum = max(values.values())
        winners = [key for key, value in values.items() if math.isclose(value, maximum)]
        if len(winners) != 1:
            raise ValueError("probabilities must have a unique maximum")
        return winners[0]  # type: ignore[return-value]


class PremarketProgramBodyV1(ContractModel):
    schema_version: Literal["forecast-loop.premarket-program/v1"] = PREMARKET_PROGRAM_SCHEMA_V1
    program_id: Literal["csi1000-premarket-open-to-open"] = "csi1000-premarket-open-to-open"
    version: Literal["1.0.0"] = "1.0.0"
    market: Literal["CN"] = "CN"
    timezone: Literal["Asia/Shanghai"] = PREMARKET_TIMEZONE
    calendar_id: Literal["SSE"] = "SSE"
    instrument: Literal["000852.SH"] = CSI1000
    target_id: Literal["csi1000-open-to-open-d1"] = CSI1000_OPEN_TO_OPEN_D1_TARGET
    target_window: Literal["open_to_open"] = "open_to_open"
    horizon_exchange_sessions: Literal[1] = 1
    evidence_window_start: Literal["previous_session_close"] = "previous_session_close"
    evidence_cutoff_local: Literal["09:10"] = "09:10"
    decision_time_local: Literal["09:15"] = "09:15"
    finalization_deadline_local: Literal["09:24"] = "09:24"
    lane: Literal["shadow"] = "shadow"


class PremarketProgramV1(PremarketProgramBodyV1):
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_seal(self) -> PremarketProgramV1:
        if self.content_hash != content_hash(self):
            raise ValueError("premarket program content_hash mismatch")
        return self


def seal_premarket_program(body: PremarketProgramBodyV1) -> PremarketProgramV1:
    payload = body.model_dump(mode="json")
    return PremarketProgramV1(**payload, content_hash=content_hash(payload))


DEFAULT_PREMARKET_PROGRAM_V1 = seal_premarket_program(PremarketProgramBodyV1())


class SourceStampV1(ContractModel):
    source_url: str = Field(min_length=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    ingested_at: datetime

    @field_validator("observed_at", "ingested_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("source timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_order(self) -> SourceStampV1:
        if self.ingested_at < self.observed_at:
            raise ValueError("source was ingested before it was observed")
        return self


class TradingCalendarStampV1(SourceStampV1):
    sessions: list[date] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_sessions(self) -> TradingCalendarStampV1:
        if self.sessions != sorted(self.sessions) or len(set(self.sessions)) != 3:
            raise ValueError("calendar sessions must be three ordered unique sessions")
        expected = content_hash(
            {
                "schema_version": "forecast-loop.premarket-calendar/v1",
                "sessions": self.sessions,
            }
        )
        if self.source_hash != expected:
            raise ValueError("calendar source_hash does not bind the three sessions")
        return self


class HistoricalOpenV1(ContractModel):
    trade_date: date
    open_price: float = Field(gt=0, allow_inf_nan=False)
    source: SourceStampV1


class PremarketEvidenceItemV1(ContractModel):
    item_id: str = Field(min_length=1, max_length=160)
    independence_key: str = Field(min_length=1, max_length=240)
    external_id: str | None = Field(default=None, max_length=240)
    category: EvidenceCategoryV1
    source_tier: SourceTierV1
    title: str = Field(min_length=1, max_length=1000)
    summary: str = Field(min_length=1, max_length=8000)
    published_at: datetime
    observed_at: datetime
    ingested_at: datetime
    source_url: str = Field(min_length=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    entities: list[str] = Field(default_factory=list, max_length=100)
    assigned_agent_ids: list[
        Literal[
            "macro_policy_agent",
            "market_news_agent",
            "global_market_agent",
            "ai_storage_industry_agent",
        ]
    ] = Field(min_length=1, max_length=4)

    @field_validator("published_at", "observed_at", "ingested_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evidence timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_item(self) -> PremarketEvidenceItemV1:
        if self.ingested_at < self.observed_at:
            raise ValueError("evidence was ingested before it was observed")
        if len(self.assigned_agent_ids) != len(set(self.assigned_agent_ids)):
            raise ValueError("assigned_agent_ids must be unique")
        if self.source_tier is SourceTierV1.TIER_4:
            raise ValueError("unverified Tier 4 material cannot enter a formal snapshot")
        return self


class PremarketWikiReferenceV1(ContractModel):
    entry_id: str = Field(pattern=r"^VC-WIKI-[A-Z0-9-]+$")
    title: str = Field(min_length=1)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    section: str = Field(min_length=1, max_length=160)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    published_at: datetime
    assigned_agent_ids: list[
        Literal[
            "macro_policy_agent",
            "market_news_agent",
            "global_market_agent",
            "ai_storage_industry_agent",
            "strategy_agent",
            "risk_critic_agent",
        ]
    ] = Field(min_length=1, max_length=6)

    @field_validator("published_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Wiki publication time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_agents(self) -> PremarketWikiReferenceV1:
        if len(self.assigned_agent_ids) != len(set(self.assigned_agent_ids)):
            raise ValueError("Wiki assigned_agent_ids must be unique")
        return self


class PremarketEvidenceSnapshotBodyV1(ContractModel):
    schema_version: Literal["forecast-loop.premarket-evidence-snapshot/v1"] = (
        PREMARKET_SNAPSHOT_SCHEMA_V1
    )
    program_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_session: date
    forecast_session: date
    target_session: date
    evidence_start: datetime
    evidence_cutoff: datetime
    decision_time: datetime
    finalization_deadline: datetime
    created_at: datetime
    calendar_source: TradingCalendarStampV1
    open_history: list[HistoricalOpenV1] = Field(min_length=21, max_length=21)
    volatility_20d: float = Field(ge=0, allow_inf_nan=False)
    items: list[PremarketEvidenceItemV1] = Field(min_length=1, max_length=1000)
    wiki_references: list[PremarketWikiReferenceV1] = Field(min_length=1, max_length=100)

    @field_validator(
        "evidence_start",
        "evidence_cutoff",
        "decision_time",
        "finalization_deadline",
        "created_at",
    )
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("snapshot timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_boundary(self) -> PremarketEvidenceSnapshotBodyV1:
        if self.program_hash != DEFAULT_PREMARKET_PROGRAM_V1.content_hash:
            raise ValueError("snapshot program_hash does not match premarket program")
        if not self.previous_session < self.forecast_session < self.target_session:
            raise ValueError("premarket session order is invalid")
        expected_sessions = [
            self.previous_session,
            self.forecast_session,
            self.target_session,
        ]
        if self.calendar_source.sessions != expected_sessions:
            raise ValueError("calendar must bind previous, forecast, and target sessions")
        zone = ZoneInfo(PREMARKET_TIMEZONE)
        start = self.evidence_start.astimezone(zone)
        cutoff = self.evidence_cutoff.astimezone(zone)
        decision = self.decision_time.astimezone(zone)
        deadline = self.finalization_deadline.astimezone(zone)
        created = self.created_at.astimezone(zone)
        if start.date() != self.previous_session or start.time() < time(15, 0):
            raise ValueError("evidence window must start after the previous session close")
        if cutoff.date() != self.forecast_session or cutoff.time() != time(9, 10):
            raise ValueError("premarket evidence cutoff must be 09:10 Asia/Shanghai")
        if decision.date() != self.forecast_session or decision.time() != time(9, 15):
            raise ValueError("premarket decision time must be 09:15 Asia/Shanghai")
        if deadline.date() != self.forecast_session or deadline.time() != time(9, 24):
            raise ValueError("premarket finalization deadline must be 09:24 Asia/Shanghai")
        if not start < cutoff < decision < deadline or not cutoff <= created < deadline:
            raise ValueError("premarket snapshot time ordering is invalid")
        if (
            self.calendar_source.observed_at > self.evidence_cutoff
            or self.calendar_source.ingested_at > self.evidence_cutoff
        ):
            raise ValueError("calendar crossed the evidence cutoff")

        history_dates = [item.trade_date for item in self.open_history]
        if history_dates != sorted(history_dates) or len(set(history_dates)) != 21:
            raise ValueError("open history must contain 21 ordered unique sessions")
        if history_dates[-1] != self.previous_session:
            raise ValueError("open history must end on the previous completed session")
        if any(
            item.source.observed_at > self.evidence_cutoff
            or item.source.ingested_at > self.evidence_cutoff
            for item in self.open_history
        ):
            raise ValueError("open history crossed the evidence cutoff")
        returns = [
            current.open_price / previous.open_price - 1.0
            for previous, current in zip(self.open_history[:-1], self.open_history[1:], strict=True)
        ]
        expected_volatility = statistics.stdev(returns)
        if not math.isclose(
            self.volatility_20d,
            expected_volatility,
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            raise ValueError("volatility_20d must match the 20 frozen open-to-open returns")

        item_ids = [item.item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("evidence item IDs must be unique")
        for item in self.items:
            if (
                item.published_at < self.evidence_start
                or item.published_at > self.evidence_cutoff
                or item.observed_at > self.evidence_cutoff
                or item.ingested_at > self.evidence_cutoff
            ):
                raise ValueError("dynamic evidence falls outside the premarket window")
        categories = {item.category for item in self.items}
        required_categories = {
            EvidenceCategoryV1.NEWS,
            EvidenceCategoryV1.GLOBAL_EQUITY,
            EvidenceCategoryV1.FX_RATES,
        }
        if not required_categories.issubset(categories):
            raise ValueError("snapshot requires news, global-equity, and FX/rates evidence")
        for agent_id in ANALYST_AGENT_IDS:
            if not any(agent_id in item.assigned_agent_ids for item in self.items):
                raise ValueError(f"snapshot has no dynamic evidence for {agent_id}")

        wiki_agents = {
            agent_id for item in self.wiki_references for agent_id in item.assigned_agent_ids
        }
        if not set(CODEX_AGENT_IDS).issubset(wiki_agents):
            raise ValueError("each Codex agent requires a frozen Wiki reference")
        if any(item.published_at > self.evidence_cutoff for item in self.wiki_references):
            raise ValueError("Wiki material was published after the evidence cutoff")
        return self


class PremarketEvidenceSnapshotV1(PremarketEvidenceSnapshotBodyV1):
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_seal(self) -> PremarketEvidenceSnapshotV1:
        if self.content_hash != content_hash(self):
            raise ValueError("premarket snapshot content_hash mismatch")
        return self


def seal_premarket_snapshot(
    body: PremarketEvidenceSnapshotBodyV1,
) -> PremarketEvidenceSnapshotV1:
    payload = body.model_dump(mode="json")
    return PremarketEvidenceSnapshotV1(**payload, content_hash=content_hash(payload))


class PremarketAssignmentV1(ContractModel):
    assignment_id: str
    agent_id: Literal[
        "macro_policy_agent",
        "market_news_agent",
        "global_market_agent",
        "ai_storage_industry_agent",
        "strategy_agent",
        "risk_critic_agent",
    ]
    role: Literal["analyst", "strategy", "risk"]
    target_id: Literal["csi1000-open-to-open-d1"] = CSI1000_OPEN_TO_OPEN_D1_TARGET
    allowed_evidence_item_ids: list[str]
    wiki_reference: PremarketWikiReferenceV1
    depends_on_assignment_ids: list[str] = Field(default_factory=list)


class PremarketHandoffBodyV1(ContractModel):
    schema_version: Literal["forecast-loop.premarket-handoff/v1"] = PREMARKET_HANDOFF_SCHEMA_V1
    run_id: str
    program: PremarketProgramV1
    snapshot: PremarketEvidenceSnapshotV1
    prepared_at: datetime
    assignments: list[PremarketAssignmentV1] = Field(min_length=6, max_length=6)

    @field_validator("prepared_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("prepared_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_handoff(self) -> PremarketHandoffBodyV1:
        if self.program.content_hash != self.snapshot.program_hash:
            raise ValueError("handoff program/snapshot mismatch")
        if self.prepared_at < self.snapshot.created_at:
            raise ValueError("handoff cannot precede snapshot creation")
        identities = [item.assignment_id for item in self.assignments]
        if len(identities) != len(set(identities)):
            raise ValueError("assignment IDs must be unique")
        if {item.agent_id for item in self.assignments} != set(CODEX_AGENT_IDS):
            raise ValueError("handoff requires four analysts, Strategy, and Risk Critic")
        available = set(identities)
        for item in self.assignments:
            if not set(item.depends_on_assignment_ids).issubset(available):
                raise ValueError("assignment dependency is unavailable")
            if item.assignment_id in item.depends_on_assignment_ids:
                raise ValueError("assignment cannot depend on itself")
        return self


class PremarketHandoffV1(PremarketHandoffBodyV1):
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_hash(self) -> PremarketHandoffV1:
        if self.request_hash != content_hash(self, exclude=("request_hash",)):
            raise ValueError("premarket handoff request_hash mismatch")
        return self


def build_premarket_handoff(
    *,
    run_id: str,
    snapshot: PremarketEvidenceSnapshotV1,
    prepared_at: datetime,
) -> PremarketHandoffV1:
    assignments: list[PremarketAssignmentV1] = []
    analyst_assignment_ids: list[str] = []
    for agent_id in ANALYST_AGENT_IDS:
        assignment_id = f"{agent_id}:{CSI1000_OPEN_TO_OPEN_D1_TARGET}"
        analyst_assignment_ids.append(assignment_id)
        assignments.append(
            PremarketAssignmentV1(
                assignment_id=assignment_id,
                agent_id=agent_id,
                role="analyst",
                allowed_evidence_item_ids=[
                    item.item_id for item in snapshot.items if agent_id in item.assigned_agent_ids
                ],
                wiki_reference=_wiki_for_agent(snapshot, agent_id),
            )
        )
    all_evidence = [item.item_id for item in snapshot.items]
    strategy_id = f"{STRATEGY_AGENT_ID}:{CSI1000_OPEN_TO_OPEN_D1_TARGET}"
    assignments.append(
        PremarketAssignmentV1(
            assignment_id=strategy_id,
            agent_id=STRATEGY_AGENT_ID,
            role="strategy",
            allowed_evidence_item_ids=all_evidence,
            wiki_reference=_wiki_for_agent(snapshot, STRATEGY_AGENT_ID),
            depends_on_assignment_ids=analyst_assignment_ids,
        )
    )
    assignments.append(
        PremarketAssignmentV1(
            assignment_id=f"{RISK_CRITIC_AGENT_ID}:{CSI1000_OPEN_TO_OPEN_D1_TARGET}",
            agent_id=RISK_CRITIC_AGENT_ID,
            role="risk",
            allowed_evidence_item_ids=all_evidence,
            wiki_reference=_wiki_for_agent(snapshot, RISK_CRITIC_AGENT_ID),
            depends_on_assignment_ids=[strategy_id],
        )
    )
    body = PremarketHandoffBodyV1(
        run_id=run_id,
        program=DEFAULT_PREMARKET_PROGRAM_V1,
        snapshot=snapshot,
        prepared_at=prepared_at,
        assignments=assignments,
    )
    payload = body.model_dump(mode="json")
    return PremarketHandoffV1(**payload, request_hash=content_hash(payload))


def _wiki_for_agent(
    snapshot: PremarketEvidenceSnapshotV1,
    agent_id: str,
) -> PremarketWikiReferenceV1:
    matches = [item for item in snapshot.wiki_references if agent_id in item.assigned_agent_ids]
    if not matches:
        raise ValueError(f"no Wiki reference is assigned to {agent_id}")
    return sorted(matches, key=lambda item: (item.entry_id, item.section))[0]


class PremarketAgentDraftV1(ContractModel):
    assignment_id: str
    agent_id: Literal[
        "macro_policy_agent",
        "market_news_agent",
        "global_market_agent",
        "ai_storage_industry_agent",
        "strategy_agent",
        "risk_critic_agent",
    ]
    role: Literal["analyst", "strategy", "risk"]
    target_id: Literal["csi1000-open-to-open-d1"] = CSI1000_OPEN_TO_OPEN_D1_TARGET
    direction: Literal["up", "neutral", "down"] | None = None
    probabilities: ProbabilitiesV1 | None = None
    risk_severity: Literal["none", "low", "medium", "high"] | None = None
    rationale: str = Field(min_length=1, max_length=8000)
    transmission_chain: list[str] = Field(default_factory=list, max_length=20)
    counter_evidence: list[str] = Field(min_length=1, max_length=20)
    invalidation_conditions: list[str] = Field(min_length=1, max_length=20)
    evidence_item_ids: list[str] = Field(min_length=1, max_length=100)
    wiki_entry_id: str
    wiki_version: str
    wiki_section: str
    wiki_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_role_payload(self) -> PremarketAgentDraftV1:
        if self.role == "risk":
            if self.direction is not None or self.probabilities is not None:
                raise ValueError("Risk Critic cannot cast a direction vote")
            if self.risk_severity is None:
                raise ValueError("Risk Critic requires risk_severity")
        else:
            if self.probabilities is None or self.direction is None:
                raise ValueError("analyst and Strategy drafts require probabilities")
            if self.direction != self.probabilities.direction:
                raise ValueError("direction must match the unique maximum probability")
            if self.risk_severity is not None:
                raise ValueError("directional drafts cannot declare risk_severity")
        if len(self.evidence_item_ids) != len(set(self.evidence_item_ids)):
            raise ValueError("draft evidence_item_ids must be unique")
        return self


class PremarketDraftBundleV1(ContractModel):
    schema_version: Literal["forecast-loop.premarket-handoff/v1"] = PREMARKET_HANDOFF_SCHEMA_V1
    run_id: str
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    generated_by: dict[str, str]
    drafts: list[PremarketAgentDraftV1] = Field(min_length=6, max_length=6)

    @field_validator("generated_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_bundle(self) -> PremarketDraftBundleV1:
        if self.generated_by.get("surface") != "codex":
            raise ValueError("premarket drafts must identify the Codex surface")
        if self.generated_by.get("model") != "gpt-5.6-sol":
            raise ValueError("premarket drafts require gpt-5.6-sol")
        if self.generated_by.get("reasoning_effort") != "high":
            raise ValueError("premarket drafts require high reasoning effort")
        if {item.agent_id for item in self.drafts} != set(CODEX_AGENT_IDS):
            raise ValueError("draft bundle requires every Codex assignment")
        identities = [item.assignment_id for item in self.drafts]
        if len(identities) != len(set(identities)):
            raise ValueError("draft assignment IDs must be unique")
        return self


class PremarketForecastBodyV1(ContractModel):
    schema_version: Literal["forecast-loop.premarket-forecast/v1"] = PREMARKET_FORECAST_SCHEMA_V1
    run_id: str
    target_id: Literal["csi1000-open-to-open-d1"] = CSI1000_OPEN_TO_OPEN_D1_TARGET
    instrument: Literal["000852.SH"] = CSI1000
    target_window: Literal["open_to_open"] = "open_to_open"
    forecast_session: date
    target_session: date
    evidence_cutoff: datetime
    decision_time: datetime
    created_at: datetime
    program_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    draft_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    lane: Literal["shadow"] = "shadow"
    direction: Literal["up", "neutral", "down"]
    probabilities: ProbabilitiesV1
    threshold: float = Field(ge=0, allow_inf_nan=False)
    risk_severity: Literal["none", "low", "medium", "high"]
    rationale: str = Field(min_length=1, max_length=8000)
    evidence_item_ids: list[str] = Field(min_length=1, max_length=100)
    wiki_references: list[PremarketWikiReferenceV1] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def validate_forecast(self) -> PremarketForecastBodyV1:
        if self.forecast_session >= self.target_session:
            raise ValueError("open-to-open target sessions must be ordered")
        if self.direction != self.probabilities.direction:
            raise ValueError("forecast direction must match maximum probability")
        return self


class PremarketForecastV1(PremarketForecastBodyV1):
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_seal(self) -> PremarketForecastV1:
        if self.content_hash != content_hash(self):
            raise ValueError("premarket forecast content_hash mismatch")
        return self


def finalize_premarket_forecast(
    handoff: PremarketHandoffV1,
    bundle: PremarketDraftBundleV1,
    *,
    accepted_at: datetime,
) -> PremarketForecastV1:
    if bundle.run_id != handoff.run_id or bundle.request_hash != handoff.request_hash:
        raise ValueError("draft bundle does not bind the premarket handoff")
    if accepted_at.tzinfo is None or accepted_at.utcoffset() is None:
        raise ValueError("accepted_at must be timezone-aware")
    if bundle.generated_at < handoff.snapshot.evidence_cutoff:
        raise ValueError("drafts were generated before the frozen evidence cutoff")
    if bundle.generated_at > accepted_at:
        raise ValueError("draft generation cannot follow acceptance")
    if accepted_at < handoff.snapshot.decision_time:
        raise ValueError("premarket forecast cannot finalize before decision time")
    if accepted_at >= handoff.snapshot.finalization_deadline:
        raise ValueError("premarket finalization deadline has passed")

    assignments = {item.assignment_id: item for item in handoff.assignments}
    drafts = {item.assignment_id: item for item in bundle.drafts}
    if set(drafts) != set(assignments):
        raise ValueError("drafts must match every premarket assignment")
    for assignment_id, draft in drafts.items():
        assignment = assignments[assignment_id]
        if draft.agent_id != assignment.agent_id or draft.role != assignment.role:
            raise ValueError("draft assignment identity mismatch")
        if not set(draft.evidence_item_ids).issubset(assignment.allowed_evidence_item_ids):
            raise ValueError("draft references evidence outside its assignment")
        wiki = assignment.wiki_reference
        if (
            draft.wiki_entry_id != wiki.entry_id
            or draft.wiki_version != wiki.version
            or draft.wiki_section != wiki.section
            or draft.wiki_content_hash != wiki.content_hash
        ):
            raise ValueError("draft changed the frozen Wiki identity")

    strategy = next(item for item in bundle.drafts if item.agent_id == STRATEGY_AGENT_ID)
    critic = next(item for item in bundle.drafts if item.agent_id == RISK_CRITIC_AGENT_ID)
    assert strategy.probabilities is not None
    assert critic.risk_severity is not None
    discount = {"none": 0.0, "low": 0.05, "medium": 0.12, "high": 0.20}[critic.risk_severity]
    uniform = 1.0 / 3.0
    values = {
        label: (1.0 - discount) * getattr(strategy.probabilities, label) + discount * uniform
        for label in ("up", "neutral", "down")
    }
    probabilities = ProbabilitiesV1(**values)
    direction = probabilities.direction
    evidence_ids = sorted(
        {evidence_id for draft in bundle.drafts for evidence_id in draft.evidence_item_ids}
    )
    body = PremarketForecastBodyV1(
        run_id=handoff.run_id,
        forecast_session=handoff.snapshot.forecast_session,
        target_session=handoff.snapshot.target_session,
        evidence_cutoff=handoff.snapshot.evidence_cutoff,
        decision_time=handoff.snapshot.decision_time,
        created_at=accepted_at,
        program_hash=handoff.program.content_hash,
        snapshot_hash=handoff.snapshot.content_hash,
        request_hash=handoff.request_hash,
        draft_hash=hashlib.sha256(canonical_json(bundle)).hexdigest(),
        direction=direction,
        probabilities=probabilities,
        threshold=0.25 * handoff.snapshot.volatility_20d,
        risk_severity=critic.risk_severity,
        rationale=strategy.rationale,
        evidence_item_ids=evidence_ids,
        wiki_references=[item.wiki_reference for item in handoff.assignments],
    )
    payload = body.model_dump(mode="json")
    return PremarketForecastV1(**payload, content_hash=content_hash(payload))


class PremarketOutcomeBodyV1(ContractModel):
    schema_version: Literal["forecast-loop.premarket-outcome/v1"] = PREMARKET_OUTCOME_SCHEMA_V1
    forecast_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    forecast_session: date
    target_session: date
    start_open: float = Field(gt=0, allow_inf_nan=False)
    end_open: float = Field(gt=0, allow_inf_nan=False)
    observed_at: datetime
    source: SourceStampV1

    @field_validator("observed_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("outcome observed_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> PremarketOutcomeBodyV1:
        if self.forecast_session >= self.target_session:
            raise ValueError("outcome sessions must be ordered")
        if self.source.ingested_at > self.observed_at:
            raise ValueError("outcome source crossed observed_at")
        return self


class PremarketOutcomeV1(PremarketOutcomeBodyV1):
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_seal(self) -> PremarketOutcomeV1:
        if self.content_hash != content_hash(self):
            raise ValueError("premarket outcome content_hash mismatch")
        return self


def seal_premarket_outcome(body: PremarketOutcomeBodyV1) -> PremarketOutcomeV1:
    payload = body.model_dump(mode="json")
    return PremarketOutcomeV1(**payload, content_hash=content_hash(payload))


class PremarketEvaluationBodyV1(ContractModel):
    schema_version: Literal["forecast-loop.premarket-evaluation/v1"] = (
        PREMARKET_EVALUATION_SCHEMA_V1
    )
    forecast_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    realized_return: float = Field(allow_inf_nan=False)
    actual_label: Literal["up", "neutral", "down"]
    direction_correct: bool
    brier_score: float = Field(ge=0, allow_inf_nan=False)
    evaluated_at: datetime

    @field_validator("evaluated_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        return value


class PremarketEvaluationV1(PremarketEvaluationBodyV1):
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_seal(self) -> PremarketEvaluationV1:
        if self.content_hash != content_hash(self):
            raise ValueError("premarket evaluation content_hash mismatch")
        return self


def evaluate_premarket_forecast(
    forecast: PremarketForecastV1,
    outcome: PremarketOutcomeV1,
    *,
    evaluated_at: datetime,
) -> PremarketEvaluationV1:
    if outcome.forecast_hash != forecast.content_hash:
        raise ValueError("outcome does not bind the forecast")
    if (
        outcome.forecast_session != forecast.forecast_session
        or outcome.target_session != forecast.target_session
    ):
        raise ValueError("outcome sessions do not match the forecast")
    realized = outcome.end_open / outcome.start_open - 1.0
    if realized > forecast.threshold:
        actual: Literal["up", "neutral", "down"] = "up"
    elif realized < -forecast.threshold:
        actual = "down"
    else:
        actual = "neutral"
    brier = (
        sum(
            (getattr(forecast.probabilities, label) - (1.0 if actual == label else 0.0)) ** 2
            for label in ("up", "neutral", "down")
        )
        / 3.0
    )
    body = PremarketEvaluationBodyV1(
        forecast_hash=forecast.content_hash,
        outcome_hash=outcome.content_hash,
        realized_return=realized,
        actual_label=actual,
        direction_correct=forecast.direction == actual,
        brier_score=brier,
        evaluated_at=evaluated_at,
    )
    payload = body.model_dump(mode="json")
    return PremarketEvaluationV1(**payload, content_hash=content_hash(payload))
