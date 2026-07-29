"""Versioned contracts shared by manual, quantitative, AI and policy Agents.

These contracts intentionally live beside the legacy ``AgentDefinition`` registry.
The legacy dataclass is part of the v1 workflow hash and run-bundle byte format;
changing it would silently rewrite historical audit semantics.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from .domain import (
    AGENT_BY_ID,
    AgentDefinition,
    AgentSourceType,
    AgentWorkflowRole,
    Horizon,
    predicted_direction,
)

AGENT_SPEC_SCHEMA = "forecast-loop.agent-spec/v1"
PARTICIPATION_POLICY_SCHEMA = "forecast-loop.participation-policy/v1"
SIGNAL_ENVELOPE_SCHEMA = "forecast-loop.signal-envelope/v1"
HASH_PATTERN = r"^[0-9a-f]{64}$"

_RESERVED_SOURCE_PAYLOAD_FIELDS = frozenset(
    {
        "schema_version",
        "signal_id",
        "agent_id",
        "agent_version",
        "agent_spec_hash",
        "mode",
        "target",
        "submitted_at",
        "accepted_at",
        "submission_deadline",
        "input_binding",
        "participation",
        "provenance",
        "direction",
        "probabilities",
        "direction_confidence",
        "rationale",
        "counter_evidence",
        "invalidation_conditions",
        "citations",
        "blind_attestation",
        "payload_schema",
        "source_payload",
        "content_hash",
    }
)


class ContractModel(BaseModel):
    """Strict base model for public, hash-bound contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class FrozenDict(dict):
    """JSON-object-compatible mapping that rejects mutation after validation."""

    @staticmethod
    def _immutable(*_args: object, **_kwargs: object) -> None:
        raise TypeError("sealed contract mappings are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


class ProbabilityMode(StrEnum):
    NONE = "none"
    CONFIDENCE = "confidence"
    MULTICLASS = "multiclass"


class ReasoningMode(StrEnum):
    NONE = "none"
    STRUCTURED = "structured"


class EvidenceMode(StrEnum):
    NONE = "none"
    DECLARED = "declared"
    FROZEN_CITATIONS = "frozen_citations"


class ParticipationMode(StrEnum):
    FORMAL = "formal"
    SHADOW = "shadow"
    DISABLED = "disabled"


class InfluenceMode(StrEnum):
    INPUT = "input"
    ADVISORY = "advisory"
    DECISION = "decision"
    NONE = "none"


class EvaluationMetric(StrEnum):
    DIRECTION = "direction"
    MULTICLASS_BRIER = "multiclass_brier"
    CALIBRATION = "calibration"
    REASONING = "reasoning"


class AgentCapabilities(ContractModel):
    """Fields that one exact Agent version is contractually able to submit."""

    direction: bool
    probability_mode: ProbabilityMode
    reasoning_mode: ReasoningMode
    evidence_mode: EvidenceMode
    supports_blind_submission: bool = False
    supports_input_binding: bool = True


class ParticipationPolicy(ContractModel):
    """Versioned authority and evaluation policy, independent of signal source."""

    schema_version: Literal["forecast-loop.participation-policy/v1"] = (
        PARTICIPATION_POLICY_SCHEMA
    )
    policy_id: str = Field(min_length=1, max_length=120)
    policy_version: str = Field(min_length=1, max_length=32)
    mode: ParticipationMode
    influence: InfluenceMode
    evaluation_metrics: tuple[EvaluationMetric, ...] = ()

    @model_validator(mode="after")
    def policy_is_internally_consistent(self) -> ParticipationPolicy:
        if len(self.evaluation_metrics) != len(set(self.evaluation_metrics)):
            raise ValueError("evaluation_metrics must not contain duplicates")
        if self.mode is ParticipationMode.DISABLED:
            if self.influence is not InfluenceMode.NONE or self.evaluation_metrics:
                raise ValueError("disabled participation may not influence or be evaluated")
        if self.mode is ParticipationMode.SHADOW and self.influence is not InfluenceMode.NONE:
            raise ValueError("shadow participation may not influence a formal decision")
        if self.mode is ParticipationMode.FORMAL and self.influence is InfluenceMode.NONE:
            raise ValueError("formal participation requires an explicit influence lane")
        if (
            EvaluationMetric.CALIBRATION in self.evaluation_metrics
            and EvaluationMetric.MULTICLASS_BRIER not in self.evaluation_metrics
        ):
            raise ValueError("calibration requires multiclass probability evaluation")
        return self


class AgentSpecBody(ContractModel):
    """Hashable identity, capability and participation declaration."""

    schema_version: Literal["forecast-loop.agent-spec/v1"] = AGENT_SPEC_SCHEMA
    agent_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    agent_version: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=2000)
    workflow_role: AgentWorkflowRole
    source_type: AgentSourceType
    capabilities: AgentCapabilities
    participation: ParticipationPolicy

    @model_validator(mode="after")
    def capabilities_cover_the_declared_metrics(self) -> AgentSpecBody:
        metrics = set(self.participation.evaluation_metrics)
        if (
            self.capabilities.probability_mode is not ProbabilityMode.NONE
            and not self.capabilities.direction
        ):
            raise ValueError("probability capability requires direction capability")
        if (
            self.capabilities.supports_blind_submission
            and not self.capabilities.direction
        ):
            raise ValueError("blind submission capability requires direction capability")
        if (
            self.participation.mode is not ParticipationMode.DISABLED
            and not self.capabilities.supports_input_binding
        ):
            raise ValueError("active participation requires input binding capability")
        if EvaluationMetric.DIRECTION in metrics and not self.capabilities.direction:
            raise ValueError("direction evaluation requires direction capability")
        if metrics.intersection(
            {EvaluationMetric.MULTICLASS_BRIER, EvaluationMetric.CALIBRATION}
        ) and self.capabilities.probability_mode is not ProbabilityMode.MULTICLASS:
            raise ValueError("probability metrics require multiclass probability capability")
        if (
            EvaluationMetric.REASONING in metrics
            and self.capabilities.reasoning_mode is not ReasoningMode.STRUCTURED
        ):
            raise ValueError("reasoning evaluation requires structured reasoning capability")
        return self


class AgentSpec(AgentSpecBody):
    """Content-addressed Agent contract returned by the registry."""

    content_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("content_hash")
    @classmethod
    def content_hash_is_not_a_placeholder(cls, value: str) -> str:
        return _non_placeholder_hash(value, label="AgentSpec content_hash")

    @model_validator(mode="after")
    def content_hash_matches_body(self) -> AgentSpec:
        expected = contract_content_hash(
            self.model_dump(mode="json", exclude={"content_hash"})
        )
        if self.content_hash != expected:
            raise ValueError("AgentSpec content_hash does not match canonical content")
        return self


class SignalProbabilityVector(ContractModel):
    up: float = Field(ge=0, le=1, allow_inf_nan=False)
    neutral: float = Field(ge=0, le=1, allow_inf_nan=False)
    down: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def probabilities_sum_to_one(self) -> SignalProbabilityVector:
        if not math.isclose(self.up + self.neutral + self.down, 1.0, abs_tol=1e-6):
            raise ValueError("probabilities must sum to one")
        return self

    def as_dict(self) -> dict[str, float]:
        return self.model_dump()


class SignalTarget(ContractModel):
    index_code: str = Field(min_length=1, max_length=32)
    horizon: Horizon
    base_trade_date: date
    target_date: date
    as_of: datetime
    data_cutoff: datetime

    @field_validator("as_of", "data_cutoff")
    @classmethod
    def timestamps_are_timezone_aware(cls, value: datetime) -> datetime:
        return _aware_datetime(value)

    @model_validator(mode="after")
    def target_times_are_ordered(self) -> SignalTarget:
        if self.data_cutoff > self.as_of:
            raise ValueError("data_cutoff may not be after as_of")
        if self.target_date <= self.base_trade_date:
            raise ValueError("target_date must be after base_trade_date")
        return self


class SignalInputBinding(ContractModel):
    run_id: str = Field(min_length=1, max_length=36)
    run_input_hash: str = Field(pattern=HASH_PATTERN)
    agent_spec_hash: str = Field(pattern=HASH_PATTERN)
    forecast_input_hash: str | None = Field(default=None, pattern=HASH_PATTERN)
    evidence_snapshot_hash: str | None = Field(default=None, pattern=HASH_PATTERN)
    parent_signal_hashes: tuple[str, ...] = ()

    @field_validator(
        "run_input_hash",
        "agent_spec_hash",
        "forecast_input_hash",
        "evidence_snapshot_hash",
    )
    @classmethod
    def hashes_are_not_placeholders(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _non_placeholder_hash(value, label="input binding hash")

    @field_validator("parent_signal_hashes")
    @classmethod
    def parent_hashes_are_valid(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("parent_signal_hashes must be unique")
        for value in values:
            if not _is_sha256(value):
                raise ValueError("parent_signal_hashes must contain SHA-256 digests")
            _non_placeholder_hash(value, label="parent signal hash")
        return values


class SignalCitation(ContractModel):
    source_id: str = Field(min_length=1, max_length=160)
    source_url: str = Field(min_length=1, max_length=2000)
    content_hash: str = Field(pattern=HASH_PATTERN)
    observed_at: datetime

    @field_validator("content_hash")
    @classmethod
    def citation_hash_is_not_a_placeholder(cls, value: str) -> str:
        return _non_placeholder_hash(value, label="citation content_hash")

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_timezone_aware(cls, value: datetime) -> datetime:
        return _aware_datetime(value)


class SignalProvenance(ContractModel):
    """Per-signal producer details; this is not inferred from the registry."""

    source_type: AgentSourceType
    producer: str = Field(min_length=1, max_length=160)
    adapter: str = Field(min_length=1, max_length=160)
    adapter_version: str = Field(min_length=1, max_length=64)
    model_name: str | None = Field(default=None, max_length=160)
    model_version: str | None = Field(default=None, max_length=160)
    prompt_version: str | None = Field(default=None, max_length=160)
    prompt_hash: str | None = Field(default=None, pattern=HASH_PATTERN)
    code_version: str | None = Field(default=None, max_length=160)
    code_hash: str | None = Field(default=None, pattern=HASH_PATTERN)
    artifact_hashes: dict[str, str] = Field(default_factory=dict)

    @field_validator("prompt_hash", "code_hash")
    @classmethod
    def provenance_hash_is_not_a_placeholder(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _non_placeholder_hash(value, label="provenance hash")

    @field_validator("artifact_hashes")
    @classmethod
    def artifact_hashes_are_valid(cls, values: dict[str, str]) -> dict[str, str]:
        for name, value in values.items():
            if not name.strip():
                raise ValueError("artifact hash names may not be blank")
            if not _is_sha256(value):
                raise ValueError(f"artifact hash for {name!r} is not SHA-256")
            _non_placeholder_hash(value, label=f"artifact hash {name!r}")
        return FrozenDict(values)


class AgentSignalDraft(ContractModel):
    """Untrusted adapter output before host-owned acceptance fields are bound."""

    signal_id: str = Field(min_length=1, max_length=64)
    submitted_at: datetime
    direction: Literal["up", "down"] | None = None
    probabilities: SignalProbabilityVector | None = None
    direction_confidence: float | None = Field(
        default=None,
        ge=0.5,
        le=1,
        allow_inf_nan=False,
    )
    rationale: str | None = Field(default=None, max_length=8000)
    counter_evidence: tuple[str, ...] = ()
    invalidation_conditions: tuple[str, ...] = ()
    citations: tuple[SignalCitation, ...] = ()
    blind_attestation: bool | None = None
    payload_schema: str = Field(
        min_length=4,
        max_length=160,
        pattern=r"^[a-z0-9][a-z0-9._/-]*/v[1-9][0-9]*$",
    )
    source_payload: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("submitted_at")
    @classmethod
    def submitted_at_is_timezone_aware(cls, value: datetime) -> datetime:
        return _aware_datetime(value)

    @field_validator("rationale", mode="before")
    @classmethod
    def strip_optional_rationale(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("counter_evidence", "invalidation_conditions")
    @classmethod
    def reasoning_items_are_nonempty(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        stripped = tuple(item.strip() for item in values)
        if any(not item for item in stripped):
            raise ValueError("reasoning items may not be blank")
        return stripped

    @field_validator("source_payload")
    @classmethod
    def source_payload_cannot_override_shared_fields(
        cls,
        value: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        collisions = sorted(_RESERVED_SOURCE_PAYLOAD_FIELDS.intersection(value))
        if collisions:
            raise ValueError(
                "source_payload may not redefine shared fields: "
                + ", ".join(collisions)
            )
        return _deep_freeze_json(value)


class SignalEnvelopeBody(ContractModel):
    """Shared audit fields plus an isolated, versioned source payload."""

    schema_version: Literal["forecast-loop.signal-envelope/v1"] = SIGNAL_ENVELOPE_SCHEMA
    signal_id: str = Field(min_length=1, max_length=64)
    agent_id: str = Field(min_length=1, max_length=64)
    agent_version: str = Field(min_length=1, max_length=32)
    mode: Literal["demo", "live"]
    target: SignalTarget
    submitted_at: datetime
    accepted_at: datetime
    submission_deadline: datetime
    input_binding: SignalInputBinding
    participation: ParticipationPolicy
    provenance: SignalProvenance
    direction: Literal["up", "down"] | None = None
    probabilities: SignalProbabilityVector | None = None
    direction_confidence: float | None = Field(
        default=None,
        ge=0.5,
        le=1,
        allow_inf_nan=False,
    )
    rationale: str | None = Field(default=None, max_length=8000)
    counter_evidence: tuple[str, ...] = ()
    invalidation_conditions: tuple[str, ...] = ()
    citations: tuple[SignalCitation, ...] = ()
    blind_attestation: bool | None = None
    payload_schema: str = Field(
        min_length=4,
        max_length=160,
        pattern=r"^[a-z0-9][a-z0-9._/-]*/v[1-9][0-9]*$",
    )
    source_payload: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("submitted_at", "accepted_at", "submission_deadline")
    @classmethod
    def envelope_timestamps_are_timezone_aware(
        cls,
        value: datetime,
    ) -> datetime:
        return _aware_datetime(value)

    @field_validator(
        "rationale",
        mode="before",
    )
    @classmethod
    def strip_optional_rationale(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("counter_evidence", "invalidation_conditions")
    @classmethod
    def reasoning_items_are_nonempty(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        stripped = tuple(item.strip() for item in values)
        if any(not item for item in stripped):
            raise ValueError("reasoning items may not be blank")
        return stripped

    @field_validator("source_payload")
    @classmethod
    def source_payload_cannot_override_shared_fields(
        cls,
        value: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        collisions = sorted(_RESERVED_SOURCE_PAYLOAD_FIELDS.intersection(value))
        if collisions:
            raise ValueError(
                "source_payload may not redefine shared fields: "
                + ", ".join(collisions)
            )
        return _deep_freeze_json(value)

    @model_validator(mode="after")
    def common_signal_semantics_are_consistent(self) -> SignalEnvelopeBody:
        if self.accepted_at < self.submitted_at:
            raise ValueError("accepted_at may not be before submitted_at")
        if self.target.data_cutoff > self.submitted_at:
            raise ValueError("submitted_at may not predate the frozen data_cutoff")
        if self.target.as_of > self.submitted_at:
            raise ValueError("submitted_at may not predate the forecast as_of")
        if self.accepted_at >= self.submission_deadline:
            raise ValueError("signals must be accepted before the deadline")
        target_zone = self.target.as_of.tzinfo
        if (
            target_zone is None  # pragma: no cover - SignalTarget rejects this.
            or self.submission_deadline.astimezone(target_zone).date()
            >= self.target.target_date
        ):
            raise ValueError("submission_deadline must precede the target date")
        if any(
            citation.observed_at > self.target.data_cutoff
            for citation in self.citations
        ):
            raise ValueError("citations may not be observed after data_cutoff")
        if self.direction is None:
            if self.probabilities is not None or self.direction_confidence is not None:
                raise ValueError("probability or confidence requires a direction")
        elif self.probabilities is not None:
            expected = predicted_direction(self.probabilities.as_dict())
            if self.direction != expected.value:
                raise ValueError(
                    "direction must match the stronger up/down probability"
                )
            if self.direction_confidence is not None:
                values = self.probabilities.as_dict()
                directional_total = values["up"] + values["down"]
                derived = max(values["up"], values["down"]) / directional_total
                if not math.isclose(
                    self.direction_confidence,
                    derived,
                    abs_tol=1e-9,
                ):
                    raise ValueError(
                        "direction_confidence conflicts with probabilities"
                    )
        return self


class SignalEnvelope(SignalEnvelopeBody):
    content_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("content_hash")
    @classmethod
    def signal_hash_is_not_a_placeholder(cls, value: str) -> str:
        return _non_placeholder_hash(value, label="SignalEnvelope content_hash")

    @model_validator(mode="after")
    def content_hash_matches_body(self) -> SignalEnvelope:
        expected = contract_content_hash(
            self.model_dump(mode="json", exclude={"content_hash"})
        )
        if self.content_hash != expected:
            raise ValueError("SignalEnvelope content_hash does not match canonical content")
        return self


def canonical_contract_bytes(value: BaseModel | dict[str, Any]) -> bytes:
    """Canonical JSON for the new contracts only.

    Do not reuse this helper for legacy workflow, snapshot, judgment, handoff or
    run-bundle artifacts; each historical format has its own frozen byte rules.
    """

    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def contract_content_hash(value: BaseModel | dict[str, Any]) -> str:
    return hashlib.sha256(canonical_contract_bytes(value)).hexdigest()


def seal_agent_spec(value: AgentSpecBody | dict[str, Any]) -> AgentSpec:
    body = (
        value
        if isinstance(value, AgentSpecBody)
        else AgentSpecBody.model_validate(value)
    )
    payload = body.model_dump(mode="json", exclude={"content_hash"})
    return AgentSpec.model_validate(
        {**payload, "content_hash": contract_content_hash(payload)}
    )


def seal_signal_envelope(
    value: SignalEnvelopeBody | dict[str, Any],
) -> SignalEnvelope:
    body = (
        value
        if isinstance(value, SignalEnvelopeBody)
        else SignalEnvelopeBody.model_validate(value)
    )
    payload = body.model_dump(mode="json", exclude={"content_hash"})
    return SignalEnvelope.model_validate(
        {**payload, "content_hash": contract_content_hash(payload)}
    )


def validate_signal_against_spec(
    signal: SignalEnvelope,
    spec: AgentSpec,
) -> None:
    """Fail closed when an envelope exceeds or contradicts its Agent contract."""

    verify_agent_spec_hash(spec)
    verify_signal_envelope_hash(signal)
    if (
        signal.agent_id != spec.agent_id
        or signal.agent_version != spec.agent_version
    ):
        raise ValueError("signal Agent identity does not match AgentSpec")
    if signal.input_binding.agent_spec_hash != spec.content_hash:
        raise ValueError("signal is not bound to this AgentSpec hash")
    if signal.provenance.source_type is not spec.source_type:
        raise ValueError("per-signal provenance source_type does not match AgentSpec")
    if signal.participation != spec.participation:
        raise ValueError("signal participation policy does not match AgentSpec")
    if spec.participation.mode is ParticipationMode.DISABLED:
        raise ValueError("disabled Agent may not submit a signal")

    capabilities = spec.capabilities
    if capabilities.direction and signal.direction is None:
        raise ValueError("AgentSpec requires a direction")
    if not capabilities.direction and signal.direction is not None:
        raise ValueError("AgentSpec does not permit a direction")

    if capabilities.probability_mode is ProbabilityMode.NONE:
        if signal.probabilities is not None or signal.direction_confidence is not None:
            raise ValueError("AgentSpec does not permit probability fields")
    elif capabilities.probability_mode is ProbabilityMode.CONFIDENCE:
        if signal.probabilities is not None:
            raise ValueError("confidence-only Agent may not submit multiclass probabilities")
        if signal.direction_confidence is None:
            raise ValueError("confidence-only Agent must submit direction_confidence")
    else:
        if signal.probabilities is None:
            raise ValueError("multiclass Agent must submit complete probabilities")

    structured = capabilities.reasoning_mode is ReasoningMode.STRUCTURED
    has_structured_reasoning = bool(
        signal.rationale
        and signal.counter_evidence
        and signal.invalidation_conditions
    )
    if structured and not has_structured_reasoning:
        raise ValueError(
            "structured reasoning requires rationale, counter evidence and "
            "invalidation conditions"
        )
    if not structured and (
        signal.rationale
        or signal.counter_evidence
        or signal.invalidation_conditions
    ):
        raise ValueError("AgentSpec does not permit structured reasoning fields")

    if capabilities.evidence_mode is EvidenceMode.NONE and signal.citations:
        raise ValueError("AgentSpec does not permit evidence citations")
    if (
        capabilities.evidence_mode is EvidenceMode.FROZEN_CITATIONS
        and not signal.citations
    ):
        raise ValueError("AgentSpec requires frozen evidence citations")
    if signal.blind_attestation and not capabilities.supports_blind_submission:
        raise ValueError("AgentSpec does not support blind submission attestation")
    if not capabilities.supports_input_binding:
        raise ValueError("AgentSpec cannot produce a run-bound SignalEnvelope")

    provenance = signal.provenance
    if spec.source_type is AgentSourceType.AI and not (
        provenance.model_name
        and provenance.model_version
        and provenance.prompt_version
    ):
        raise ValueError("AI provenance requires model and prompt versions")
    if spec.source_type is AgentSourceType.QUANT and not (
        provenance.code_version
        and provenance.code_hash
        and provenance.artifact_hashes
    ):
        raise ValueError("Quant provenance requires code and model artifact hashes")
    if spec.source_type is AgentSourceType.DETERMINISTIC and not (
        provenance.code_version and provenance.code_hash
    ):
        raise ValueError("deterministic provenance requires code version and hash")


def verify_agent_spec_hash(spec: AgentSpec) -> None:
    expected = contract_content_hash(
        spec.model_dump(mode="json", exclude={"content_hash"})
    )
    if spec.content_hash != expected:
        raise ValueError("AgentSpec content_hash does not match canonical content")


def verify_signal_envelope_hash(signal: SignalEnvelope) -> None:
    expected = contract_content_hash(
        signal.model_dump(mode="json", exclude={"content_hash"})
    )
    if signal.content_hash != expected:
        raise ValueError("SignalEnvelope content_hash does not match canonical content")


def agent_spec(agent_id: str) -> AgentSpec:
    try:
        return AGENT_SPEC_BY_ID[agent_id]
    except KeyError as exc:
        raise KeyError(f"unknown Agent: {agent_id}") from exc


def registered_agent_specs() -> tuple[AgentSpec, ...]:
    return AGENT_SPECS


def _build_spec(
    definition: AgentDefinition,
    *,
    capabilities: AgentCapabilities,
    participation: ParticipationPolicy,
) -> AgentSpec:
    return seal_agent_spec(
        AgentSpecBody(
            agent_id=definition.id,
            agent_version=definition.version,
            name=definition.name,
            role=definition.role,
            workflow_role=definition.workflow_role,
            source_type=definition.source_type,
            capabilities=capabilities,
            participation=participation,
        )
    )


def _predictive_capabilities(
    *,
    probability_mode: ProbabilityMode = ProbabilityMode.MULTICLASS,
    evidence_mode: EvidenceMode = EvidenceMode.FROZEN_CITATIONS,
    blind: bool = False,
) -> AgentCapabilities:
    return AgentCapabilities(
        direction=True,
        probability_mode=probability_mode,
        reasoning_mode=ReasoningMode.STRUCTURED,
        evidence_mode=evidence_mode,
        supports_blind_submission=blind,
        supports_input_binding=True,
    )


def _formal_policy(
    policy_id: str,
    *,
    influence: InfluenceMode,
    reasoning: bool = True,
) -> ParticipationPolicy:
    metrics = [
        EvaluationMetric.DIRECTION,
        EvaluationMetric.MULTICLASS_BRIER,
        EvaluationMetric.CALIBRATION,
    ]
    if reasoning:
        metrics.append(EvaluationMetric.REASONING)
    return ParticipationPolicy(
        policy_id=policy_id,
        policy_version="1.0.0",
        mode=ParticipationMode.FORMAL,
        influence=influence,
        evaluation_metrics=tuple(metrics),
    )


def _registry() -> tuple[AgentSpec, ...]:
    specs: list[AgentSpec] = []
    for definition in AGENT_BY_ID.values():
        if definition.id in {
            "macro_policy_agent",
            "market_news_agent",
            "ai_storage_industry_agent",
            "strategy_agent",
        }:
            specs.append(
                _build_spec(
                    definition,
                    capabilities=_predictive_capabilities(),
                    participation=_formal_policy(
                        "committee-static",
                        influence=InfluenceMode.INPUT,
                    ),
                )
            )
        elif definition.id == "cio_agent":
            specs.append(
                _build_spec(
                    definition,
                    capabilities=_predictive_capabilities(),
                    participation=_formal_policy(
                        "committee-static",
                        influence=InfluenceMode.DECISION,
                    ),
                )
            )
        elif definition.id == "risk_critic_agent":
            specs.append(
                _build_spec(
                    definition,
                    capabilities=AgentCapabilities(
                        direction=False,
                        probability_mode=ProbabilityMode.NONE,
                        reasoning_mode=ReasoningMode.STRUCTURED,
                        evidence_mode=EvidenceMode.FROZEN_CITATIONS,
                    ),
                    participation=ParticipationPolicy(
                        policy_id="committee-static",
                        policy_version="1.0.0",
                        mode=ParticipationMode.FORMAL,
                        influence=InfluenceMode.ADVISORY,
                        evaluation_metrics=(),
                    ),
                )
            )
        elif definition.id == "quant_agent":
            specs.append(
                seal_agent_spec(
                    AgentSpecBody(
                        agent_id=definition.id,
                        # The legacy roster remains frozen at 0.2.0 for v1 run
                        # hash compatibility. The independently versioned
                        # signal contract activates only the new shadow lane.
                        agent_version="0.3.0",
                        name="量化研究员",
                        role=(
                            "从已冻结的只读特征、参数、模型和输入快照产生可复验信号；"
                            "不得访问交易、账户或上游写入路径。"
                        ),
                        workflow_role=definition.workflow_role,
                        source_type=definition.source_type,
                        capabilities=_predictive_capabilities(
                            probability_mode=ProbabilityMode.MULTICLASS,
                            evidence_mode=EvidenceMode.NONE,
                        ),
                        participation=ParticipationPolicy(
                            policy_id="quant-readonly-shadow",
                            policy_version="1.0.0",
                            mode=ParticipationMode.SHADOW,
                            influence=InfluenceMode.NONE,
                            evaluation_metrics=(
                                EvaluationMetric.DIRECTION,
                                EvaluationMetric.MULTICLASS_BRIER,
                                EvaluationMetric.CALIBRATION,
                                EvaluationMetric.REASONING,
                            ),
                        ),
                    )
                )
            )
        elif definition.id == "user_judgment_agent":
            specs.append(
                _build_spec(
                    definition,
                    capabilities=_predictive_capabilities(
                        probability_mode=ProbabilityMode.CONFIDENCE,
                        evidence_mode=EvidenceMode.NONE,
                        blind=True,
                    ),
                    participation=ParticipationPolicy(
                        policy_id="manual-shadow",
                        policy_version="1.0.0",
                        mode=ParticipationMode.SHADOW,
                        influence=InfluenceMode.NONE,
                        evaluation_metrics=(
                            EvaluationMetric.DIRECTION,
                            EvaluationMetric.REASONING,
                        ),
                    ),
                )
            )
        else:  # pragma: no cover - every built-in Agent must be mapped explicitly.
            raise RuntimeError(f"missing AgentSpec mapping for {definition.id}")
    return tuple(specs)


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _non_placeholder_hash(value: str, *, label: str) -> str:
    if value == "0" * 64:
        raise ValueError(f"{label} may not be a placeholder digest")
    return value


def _deep_freeze_json(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return FrozenDict(
            {
                key: _deep_freeze_json(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, list):
        return tuple(_deep_freeze_json(item) for item in value)  # type: ignore[return-value]
    return value


AGENT_SPECS = _registry()
AGENT_SPEC_BY_ID = {item.agent_id: item for item in AGENT_SPECS}

if set(AGENT_SPEC_BY_ID) != set(AGENT_BY_ID):  # pragma: no cover - import-time guard.
    raise RuntimeError("AgentSpec registry does not cover the legacy Agent registry")
