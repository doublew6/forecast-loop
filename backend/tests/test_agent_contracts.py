from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from app.agent_contracts import (
    AGENT_SPEC_SCHEMA,
    PARTICIPATION_POLICY_SCHEMA,
    SIGNAL_ENVELOPE_SCHEMA,
    AgentSignalDraft,
    AgentSpec,
    AgentSpecBody,
    EvaluationMetric,
    InfluenceMode,
    ParticipationMode,
    ParticipationPolicy,
    SignalCitation,
    SignalEnvelope,
    SignalEnvelopeBody,
    SignalInputBinding,
    SignalProbabilityVector,
    SignalProvenance,
    SignalTarget,
    agent_spec,
    registered_agent_specs,
    seal_agent_spec,
    seal_signal_envelope,
    validate_signal_against_spec,
)
from app.domain import AgentSourceType, Direction
from app.ports import AgentSignalSource
from app.services.evaluation_facade import (
    evaluate_signal,
    evaluation_plan,
    route_signal,
)
from app.services.signal_contract import accept_signal_draft
from pydantic import ValidationError

ZONE = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 7, 27, 15, 0, tzinfo=ZONE)
DATA_CUTOFF = datetime(2026, 7, 27, 14, 55, tzinfo=ZONE)
SUBMITTED_AT = datetime(2026, 7, 27, 15, 2, tzinfo=ZONE)
ACCEPTED_AT = datetime(2026, 7, 27, 15, 3, tzinfo=ZONE)
DEADLINE = datetime(2026, 7, 27, 15, 30, tzinfo=ZONE)


def _signal_body(agent_id: str) -> SignalEnvelopeBody:
    spec = agent_spec(agent_id)
    common = {
        "signal_id": f"signal-{agent_id}",
        "agent_id": spec.agent_id,
        "agent_version": spec.agent_version,
        "mode": "live",
        "target": SignalTarget(
            index_code="000300.SH",
            horizon="D1",
            base_trade_date=date(2026, 7, 27),
            target_date=date(2026, 7, 28),
            as_of=AS_OF,
            data_cutoff=DATA_CUTOFF,
        ),
        "submitted_at": SUBMITTED_AT,
        "accepted_at": ACCEPTED_AT,
        "submission_deadline": DEADLINE,
        "input_binding": SignalInputBinding(
            run_id="run-contract-fixture",
            run_input_hash="a" * 64,
            agent_spec_hash=spec.content_hash,
            evidence_snapshot_hash="b" * 64,
        ),
        "participation": spec.participation,
        "rationale": "冻结信息显示流动性与风险偏好共同支持该方向。",
        "counter_evidence": ("海外利率重新上行可能压制风险资产。",),
        "invalidation_conditions": ("若价格跌破基准日低点，则该判断失效。",),
    }
    if agent_id == "user_judgment_agent":
        return SignalEnvelopeBody(
            **common,
            provenance=SignalProvenance(
                source_type=AgentSourceType.MANUAL,
                producer="local-user-interface",
                adapter="manual-form",
                adapter_version="1.0.0",
            ),
            direction="up",
            direction_confidence=0.67,
            blind_attestation=True,
            payload_schema="forecast-loop.manual/v1",
            source_payload={"entry_format": "private-wiki"},
        )
    if agent_id == "risk_critic_agent":
        return SignalEnvelopeBody(
            **common,
            provenance=SignalProvenance(
                source_type=AgentSourceType.AI,
                producer="forecast-loop",
                adapter="openai-compatible",
                adapter_version="1.0.0",
                model_name="fixture-model",
                model_version="2026-07",
                prompt_version="risk-v1",
            ),
            citations=(
                SignalCitation(
                    source_id="evidence-1",
                    source_url="https://example.com/evidence",
                    content_hash="c" * 64,
                    observed_at=DATA_CUTOFF,
                ),
            ),
            payload_schema="forecast-loop.risk/v1",
            source_payload={"critique_class": "crowding"},
        )
    return SignalEnvelopeBody(
        **common,
        provenance=SignalProvenance(
            source_type=spec.source_type,
            producer="forecast-loop",
            adapter="openai-compatible",
            adapter_version="1.0.0",
            model_name="fixture-model",
            model_version="2026-07",
            prompt_version="research-v1",
        ),
        direction="up",
        probabilities=SignalProbabilityVector(
            up=0.58,
            neutral=0.27,
            down=0.15,
        ),
        citations=(
            SignalCitation(
                source_id="evidence-1",
                source_url="https://example.com/evidence",
                content_hash="c" * 64,
                observed_at=DATA_CUTOFF,
            ),
        ),
        payload_schema="forecast-loop.ai/v1",
        source_payload={"provider_response_id": "fixture-response"},
    )


def test_registry_exposes_versioned_content_addressed_specs() -> None:
    specs = registered_agent_specs()
    assert len(specs) == 8
    assert {spec.agent_id for spec in specs} == {
        "macro_policy_agent",
        "market_news_agent",
        "ai_storage_industry_agent",
        "quant_agent",
        "strategy_agent",
        "risk_critic_agent",
        "cio_agent",
        "user_judgment_agent",
    }
    assert all(spec.schema_version == AGENT_SPEC_SCHEMA for spec in specs)
    assert all(
        spec.participation.schema_version == PARTICIPATION_POLICY_SCHEMA
        for spec in specs
    )
    assert all(len(spec.content_hash) == 64 for spec in specs)

    manual = agent_spec("user_judgment_agent")
    assert manual.capabilities.probability_mode == "confidence"
    assert manual.participation.mode is ParticipationMode.SHADOW
    assert EvaluationMetric.MULTICLASS_BRIER not in manual.participation.evaluation_metrics

    quant = agent_spec("quant_agent")
    assert quant.agent_version == "0.3.0"
    assert quant.capabilities.probability_mode == "multiclass"
    assert quant.participation.mode is ParticipationMode.SHADOW
    assert quant.participation.influence is InfluenceMode.NONE
    assert set(quant.participation.evaluation_metrics) >= {
        EvaluationMetric.DIRECTION,
        EvaluationMetric.MULTICLASS_BRIER,
        EvaluationMetric.CALIBRATION,
    }

    signal_schema = SignalEnvelope.model_json_schema()
    assert "submission_deadline" in signal_schema["required"]
    assert signal_schema["properties"]["submission_deadline"]["type"] == "string"


def test_source_type_does_not_determine_participation_authority() -> None:
    formal = agent_spec("macro_policy_agent")
    shadow_policy = ParticipationPolicy(
        policy_id="independent-ai-shadow",
        policy_version="1.0.0",
        mode=ParticipationMode.SHADOW,
        influence=InfluenceMode.NONE,
        evaluation_metrics=(
            EvaluationMetric.DIRECTION,
            EvaluationMetric.MULTICLASS_BRIER,
            EvaluationMetric.CALIBRATION,
            EvaluationMetric.REASONING,
        ),
    )
    shadow = seal_agent_spec(
        AgentSpecBody(
            **formal.model_dump(
                exclude={"content_hash", "participation"},
            ),
            participation=shadow_policy,
        )
    )

    assert formal.source_type is shadow.source_type is AgentSourceType.AI
    assert formal.participation.mode is ParticipationMode.FORMAL
    assert shadow.participation.mode is ParticipationMode.SHADOW
    assert formal.content_hash != shadow.content_hash

    formal_signal = seal_signal_envelope(_signal_body(formal.agent_id))
    shadow_body = _signal_body(formal.agent_id).model_dump(mode="json")
    shadow_body["signal_id"] = "signal-independent-ai-shadow"
    shadow_body["input_binding"]["agent_spec_hash"] = shadow.content_hash
    shadow_body["participation"] = shadow.participation.model_dump(mode="json")
    shadow_signal = seal_signal_envelope(
        SignalEnvelopeBody.model_validate(shadow_body)
    )
    formal_route = route_signal(spec=formal, signal=formal_signal)
    shadow_route = route_signal(spec=shadow, signal=shadow_signal)
    assert formal_route.lane == "formal_input"
    assert formal_route.formal_aggregation is True
    assert formal_route.shadow_benchmark is False
    assert shadow_route.lane == "shadow_benchmark"
    assert shadow_route.formal_aggregation is False
    assert shadow_route.shadow_benchmark is True


def test_agent_spec_rejects_capabilities_that_cannot_produce_a_signal() -> None:
    body = agent_spec("macro_policy_agent").model_dump(
        mode="json",
        exclude={"content_hash"},
    )
    body["capabilities"]["direction"] = False
    with pytest.raises(ValidationError, match="probability capability"):
        AgentSpecBody.model_validate(body)

    body = agent_spec("macro_policy_agent").model_dump(
        mode="json",
        exclude={"content_hash"},
    )
    body["capabilities"]["supports_input_binding"] = False
    with pytest.raises(ValidationError, match="input binding capability"):
        AgentSpecBody.model_validate(body)


def test_adapter_draft_is_host_bound_and_sealed_into_an_envelope() -> None:
    class FixtureSignalSource:
        def load_signal_drafts(
            self,
            *,
            as_of: datetime,
        ) -> tuple[AgentSignalDraft, ...]:
            assert as_of == AS_OF
            return (
                AgentSignalDraft(
                    signal_id="adapter-manual-draft",
                    submitted_at=SUBMITTED_AT,
                    direction="up",
                    direction_confidence=0.67,
                    rationale="流动性与风险偏好改善可能推动目标指数走强。",
                    counter_evidence=("海外利率上行可能压制风险资产。",),
                    invalidation_conditions=("若跌破基准日低点则失效。",),
                    blind_attestation=True,
                    payload_schema="forecast-loop.manual/v1",
                    source_payload={"adapter_record_id": "source-1"},
                ),
            )

    source = FixtureSignalSource()
    assert isinstance(source, AgentSignalSource)
    draft = source.load_signal_drafts(as_of=AS_OF)[0]
    spec = agent_spec("user_judgment_agent")
    provenance = SignalProvenance(
        source_type=AgentSourceType.MANUAL,
        producer="trusted-host-configuration",
        adapter="manual-form",
        adapter_version="1.0.0",
    )
    signal = accept_signal_draft(
        draft=draft,
        agent_id=spec.agent_id,
        mode="live",
        target=_signal_body(spec.agent_id).target,
        accepted_at=ACCEPTED_AT,
        submission_deadline=DEADLINE,
        input_binding=SignalInputBinding(
            run_id="run-contract-fixture",
            run_input_hash="a" * 64,
            agent_spec_hash=spec.content_hash,
        ),
        provenance=provenance,
    )

    assert not hasattr(draft, "accepted_at")
    assert not hasattr(draft, "provenance")
    assert signal.accepted_at == ACCEPTED_AT
    assert signal.provenance == provenance
    assert signal.participation == spec.participation
    validate_signal_against_spec(signal, spec)


def test_signal_hash_and_source_payload_are_fail_closed() -> None:
    signal = seal_signal_envelope(_signal_body("macro_policy_agent"))
    assert signal.schema_version == SIGNAL_ENVELOPE_SCHEMA
    validate_signal_against_spec(signal, agent_spec(signal.agent_id))

    tampered = signal.model_dump(mode="json")
    tampered["source_payload"]["provider_response_id"] = "tampered"
    with pytest.raises(ValidationError, match="content_hash"):
        SignalEnvelope.model_validate(tampered)

    body = _signal_body("macro_policy_agent").model_dump(mode="json")
    body["source_payload"]["agent_id"] = "attempted-override"
    with pytest.raises(ValidationError, match="may not redefine shared fields"):
        SignalEnvelopeBody.model_validate(body)


def test_sealed_nested_payload_is_immutable_and_rehashed_at_boundaries() -> None:
    signal = seal_signal_envelope(_signal_body("macro_policy_agent"))
    with pytest.raises(TypeError, match="immutable"):
        signal.source_payload["provider_response_id"] = "tampered"
    with pytest.raises(TypeError, match="immutable"):
        signal.provenance.artifact_hashes["model"] = "d" * 64
    with pytest.raises(TypeError, match="immutable"):
        signal.source_payload |= {"provider_response_id": "tampered"}
    assert signal.source_payload["provider_response_id"] == "fixture-response"

    bypassed = signal.model_copy(
        update={"source_payload": {"provider_response_id": "tampered"}}
    )
    with pytest.raises(ValueError, match="content_hash"):
        validate_signal_against_spec(
            bypassed,
            agent_spec("macro_policy_agent"),
        )


def test_manual_signal_never_receives_probability_metrics() -> None:
    spec = agent_spec("user_judgment_agent")
    signal = seal_signal_envelope(_signal_body(spec.agent_id))

    result = evaluate_signal(
        spec=spec,
        signal=signal,
        actual_label=Direction.UP,
    )

    assert result.direction_correct is True
    assert result.brier_score is None
    assert result.calibration_eligible is False
    assert result.reasoning_review_eligible is True


def test_multiclass_signal_receives_probability_metrics() -> None:
    spec = agent_spec("macro_policy_agent")
    signal = seal_signal_envelope(_signal_body(spec.agent_id))

    result = evaluate_signal(
        spec=spec,
        signal=signal,
        actual_label=Direction.UP,
    )

    assert result.direction_correct is True
    assert result.brier_score == pytest.approx(
        ((1 - 0.58) ** 2 + 0.27**2 + 0.15**2) / 3
    )
    assert result.calibration_eligible is True


def test_critic_does_not_receive_direction_metrics() -> None:
    critic = agent_spec("risk_critic_agent")
    signal = seal_signal_envelope(_signal_body(critic.agent_id))
    validate_signal_against_spec(signal, critic)
    plan = evaluation_plan(critic, signal)
    assert plan.direction is False
    assert plan.multiclass_brier is False
    assert plan.calibration is False


def test_contract_rejects_naive_time_nonfinite_values_and_extra_fields() -> None:
    body = _signal_body("macro_policy_agent").model_dump(mode="json")
    body["accepted_at"] = "2026-07-27T15:03:00"
    with pytest.raises(ValidationError, match="timezone"):
        SignalEnvelopeBody.model_validate(body)

    body = _signal_body("macro_policy_agent").model_dump(mode="json")
    body["probabilities"]["up"] = float("nan")
    with pytest.raises(ValidationError):
        SignalEnvelopeBody.model_validate(body)

    spec = agent_spec("macro_policy_agent").model_dump(mode="json")
    spec["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        AgentSpec.model_validate(spec)


def test_formal_signal_accepted_at_deadline_is_rejected() -> None:
    body = _signal_body("macro_policy_agent").model_copy(
        update={"accepted_at": DEADLINE},
    )
    with pytest.raises(ValidationError, match="before the deadline"):
        SignalEnvelopeBody.model_validate(body.model_dump(mode="json"))


def test_signal_requires_deadline_and_pre_cutoff_citations() -> None:
    body = _signal_body("macro_policy_agent").model_dump(mode="json")
    body.pop("submission_deadline")
    with pytest.raises(ValidationError, match="Field required"):
        SignalEnvelopeBody.model_validate(body)

    body = _signal_body("macro_policy_agent").model_dump(mode="json")
    body["citations"][0]["observed_at"] = "2026-07-27T14:56:00+08:00"
    with pytest.raises(ValidationError, match="after data_cutoff"):
        SignalEnvelopeBody.model_validate(body)


@pytest.mark.parametrize(
    "agent_id",
    ("macro_policy_agent", "user_judgment_agent"),
)
def test_formal_and_shadow_deadlines_must_precede_target_date(
    agent_id: str,
) -> None:
    body = _signal_body(agent_id).model_dump(mode="json")
    body["submission_deadline"] = "2026-07-28T09:00:00+08:00"
    body["accepted_at"] = "2026-07-28T08:59:00+08:00"
    with pytest.raises(ValidationError, match="precede the target date"):
        SignalEnvelopeBody.model_validate(body)
