"""Capability-driven evaluation planning for every Agent source type."""

from __future__ import annotations

from dataclasses import dataclass

from ..agent_contracts import (
    AgentSpec,
    EvaluationMetric,
    InfluenceMode,
    ParticipationMode,
    ProbabilityMode,
    ReasoningMode,
    SignalEnvelope,
    agent_spec,
    validate_signal_against_spec,
)
from ..domain import Direction, multiclass_brier_score


@dataclass(frozen=True, slots=True)
class EvaluationPlan:
    """Metrics that are valid for one Agent or one exact signal.

    ``reasoning`` means that a frozen reasoning artifact is eligible for a
    versioned review. It does not invent a reasoning score before the rubric in
    the later governance milestone exists.
    """

    direction: bool
    multiclass_brier: bool
    calibration: bool
    reasoning: bool


@dataclass(frozen=True, slots=True)
class SignalEvaluation:
    actual_label: Direction
    direction_correct: bool | None
    brier_score: float | None
    calibration_eligible: bool
    reasoning_review_eligible: bool


@dataclass(frozen=True, slots=True)
class SignalRoute:
    """Deterministic lane selected only from the frozen participation policy."""

    lane: str
    formal_aggregation: bool
    shadow_benchmark: bool
    influence: InfluenceMode


def evaluation_plan(
    spec: AgentSpec,
    signal: SignalEnvelope | None = None,
) -> EvaluationPlan:
    """Return the fail-closed metric plan declared by ``spec``.

    Passing a signal additionally proves that the concrete payload supplies the
    required fields and is bound to the same immutable AgentSpec.
    """

    if signal is not None:
        validate_signal_against_spec(signal, spec)
    if spec.participation.mode is ParticipationMode.DISABLED:
        return EvaluationPlan(
            direction=False,
            multiclass_brier=False,
            calibration=False,
            reasoning=False,
        )
    metrics = set(spec.participation.evaluation_metrics)
    direction = (
        EvaluationMetric.DIRECTION in metrics
        and spec.capabilities.direction
        and (signal is None or signal.direction is not None)
    )
    complete_probabilities = (
        spec.capabilities.probability_mode is ProbabilityMode.MULTICLASS
        and (signal is None or signal.probabilities is not None)
    )
    brier = (
        EvaluationMetric.MULTICLASS_BRIER in metrics
        and complete_probabilities
    )
    calibration = EvaluationMetric.CALIBRATION in metrics and complete_probabilities
    reasoning = (
        EvaluationMetric.REASONING in metrics
        and spec.capabilities.reasoning_mode is ReasoningMode.STRUCTURED
        and (signal is None or bool(signal.rationale))
    )
    return EvaluationPlan(
        direction=direction,
        multiclass_brier=brier,
        calibration=calibration,
        reasoning=reasoning,
    )


def route_signal(*, spec: AgentSpec, signal: SignalEnvelope) -> SignalRoute:
    """Route a validated signal without inferring authority from source type."""

    validate_signal_against_spec(signal, spec)
    policy = signal.participation
    if policy.mode is ParticipationMode.SHADOW:
        return SignalRoute(
            lane="shadow_benchmark",
            formal_aggregation=False,
            shadow_benchmark=True,
            influence=InfluenceMode.NONE,
        )
    if policy.mode is ParticipationMode.DISABLED:
        # ``validate_signal_against_spec`` rejects this first; retain an
        # explicit defensive branch for future policy versions.
        raise ValueError("disabled Agent may not be routed")
    lanes = {
        InfluenceMode.INPUT: "formal_input",
        InfluenceMode.ADVISORY: "formal_advisory",
        InfluenceMode.DECISION: "formal_decision",
    }
    try:
        lane = lanes[policy.influence]
    except KeyError as exc:  # pragma: no cover - policy validation guards this.
        raise ValueError("formal participation has no supported influence lane") from exc
    return SignalRoute(
        lane=lane,
        formal_aggregation=True,
        shadow_benchmark=False,
        influence=policy.influence,
    )


def evaluate_signal(
    *,
    spec: AgentSpec,
    signal: SignalEnvelope,
    actual_label: Direction | str,
) -> SignalEvaluation:
    """Evaluate only the metrics supported by the exact signal contract."""

    plan = evaluation_plan(spec, signal)
    actual = Direction(actual_label)
    direction_correct = (
        signal.direction == actual.value
        if plan.direction and actual is not Direction.NEUTRAL
        else None
    )
    brier_score = None
    if plan.multiclass_brier:
        if signal.probabilities is None:  # Defensive guard after validation.
            raise ValueError("multiclass evaluation requires complete probabilities")
        brier_score = multiclass_brier_score(
            signal.probabilities.as_dict(),
            actual,
        )
    return SignalEvaluation(
        actual_label=actual,
        direction_correct=direction_correct,
        brier_score=brier_score,
        calibration_eligible=plan.calibration,
        reasoning_review_eligible=plan.reasoning,
    )


def agent_scorecard(
    session,
    *,
    agent_id: str,
    index_code: str | None,
    horizon: str,
    mode: str,
    actor_id: str,
    timezone: str,
    market_universe_hash: str,
    model_name: str | None = None,
    forecast_model_version: str | None = None,
    latest_frozen_partition: bool = False,
):
    """Route scorecard storage after resolving capability-driven metrics.

    Storage layout is deliberately not inferred from ``source_type``. The
    private manual ledger remains separate from legacy committee opinions even
    though both expose one public scorecard contract.
    """

    spec = agent_spec(agent_id)
    evaluation_plan(spec)
    if agent_id == "user_judgment_agent":
        from .user_judgment import user_judgment_scorecard

        return user_judgment_scorecard(
            session,
            actor_id=actor_id,
            index_code=index_code,
            horizon=horizon,
            timezone=timezone,
            market_universe_hash=market_universe_hash,
        )

    from .evaluation import scorecard

    return scorecard(
        session,
        agent_id=agent_id,
        index_code=index_code,
        horizon=horizon,
        mode=mode,
        market_universe_hash=market_universe_hash,
        model_name=model_name,
        forecast_model_version=forecast_model_version,
        latest_frozen_partition=latest_frozen_partition,
    )
