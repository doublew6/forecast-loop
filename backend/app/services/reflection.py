"""Deterministic daily evaluation and append-only reflection primitives."""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..agent_contracts import agent_spec
from ..domain import AGENT_BY_ID, Direction, multiclass_brier_score
from ..market_universe import DEFAULT_MARKET_UNIVERSE
from ..models import (
    AgentOpinion,
    EvaluationBatch,
    EvaluationResult,
    Forecast,
    ForecastDiagnostic,
    MarketSessionSnapshot,
    OpinionEvaluation,
    ReflectionRun,
    WorkflowRun,
)
from .evaluation_facade import evaluation_plan
from .user_judgment import materialize_user_judgment_evaluation

REFLECTION_SCHEMA_VERSION = "1.0.0"
DIAGNOSTIC_POLICY_VERSION = "1.0.0"
REFLECTION_STATUSES = frozenset(
    {
        "awaiting_sources",
        "awaiting_analysis",
        "completed",
        "failed",
        "blocked_upstream",
    }
)
FINDING_ERROR_TYPES = frozenset(
    {
        "data_coverage_failure",
        "attention_omission",
        "reasoning_or_weighting_failure",
        "transmission_mapping",
        "horizon_timing",
        "post_cutoff_shock",
        "risk_plan_failure",
        "market_noise",
        "unresolved",
    }
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class OutcomeDiagnostic:
    signed_sigma: float
    severity: str
    historical_abs_return_percentile: float | None
    history_sample_size: int
    data_incomplete: bool
    sign_correct: bool | None
    material_direction_correct: bool | None


@dataclass(frozen=True, slots=True)
class MarketSnapshotFact:
    """Validated values copied from the trusted, read-only market-data adapter."""

    index_code: str
    index_name: str
    target_date: date
    base_trade_date: date
    base_close: float
    target_close: float
    actual_return: float
    source_url: str
    source_hash: str
    captured_at: datetime
    amount: float | None = None
    advancers: int | None = None
    decliners: int | None = None
    unchanged: int | None = None
    limit_down_count: int | None = None
    breadth_down_ratio: float | None = None
    sector_contributions: list[dict[str, Any]] = field(default_factory=list)
    weight_contributions: list[dict[str, Any]] = field(default_factory=list)
    historical_abs_return_percentile: float | None = None
    history_sample_size: int = 0


def due_live_forecasts(
    session: Session,
    *,
    target_date: date,
    horizon: str,
) -> list[Forecast]:
    """Return evaluated formal forecasts by their frozen target trading date."""

    forecasts = list(
        session.scalars(
            select(Forecast)
            .join(WorkflowRun, WorkflowRun.id == Forecast.run_id)
            .join(EvaluationResult, EvaluationResult.forecast_id == Forecast.id)
            .options(selectinload(Forecast.run), selectinload(Forecast.evaluation))
            .where(
                WorkflowRun.mode == "live",
                WorkflowRun.status == "completed",
                WorkflowRun.market_universe_hash
                == DEFAULT_MARKET_UNIVERSE.content_hash,
                Forecast.target_date == target_date,
                Forecast.horizon == horizon,
                Forecast.index_code.in_(DEFAULT_MARKET_UNIVERSE.codes),
            )
            .order_by(Forecast.index_code)
        ).all()
    )
    return forecasts


def validate_default_reflection_universe(source_run: WorkflowRun) -> None:
    """Keep the formal Daily Reflection protocol on its frozen five-index universe."""

    if (
        source_run.market_universe_hash
        != DEFAULT_MARKET_UNIVERSE.content_hash
    ):
        raise ValueError(
            "Daily Reflection supports only the default five-index A-share "
            "market universe"
        )


def historical_abs_return_percentile(
    actual_return: float,
    historical_returns: Sequence[float],
) -> tuple[float | None, int]:
    """Return an empirical percentile against at most 1,250 prior sessions."""

    history = [abs(float(value)) for value in historical_returns[-1250:] if math.isfinite(value)]
    if not history:
        return None, 0
    observed = abs(actual_return)
    at_or_below = sum(value <= observed for value in history)
    return at_or_below / len(history), len(history)


def diagnose_outcome(
    *,
    predicted_direction: str,
    actual_return: float,
    threshold: float,
    historical_percentile: float | None,
    history_sample_size: int,
    breadth_down_ratio: float | None,
) -> OutcomeDiagnostic:
    """Apply the v1 noise, severity and directional-score policy exactly."""

    if predicted_direction not in {Direction.UP.value, Direction.DOWN.value}:
        raise ValueError("formal forecasts must choose up or down")
    if not math.isfinite(actual_return):
        raise ValueError("actual_return must be finite")
    if not math.isfinite(threshold) or threshold < 0:
        raise ValueError("threshold must be a finite non-negative number")
    if historical_percentile is not None and not 0 <= historical_percentile <= 1:
        raise ValueError("historical_percentile must be between zero and one")
    if history_sample_size < 0:
        raise ValueError("history_sample_size must be non-negative")
    if breadth_down_ratio is not None and not 0 <= breadth_down_ratio <= 1:
        raise ValueError("breadth_down_ratio must be between zero and one")

    horizon_sigma = threshold / 0.25
    if horizon_sigma > 0:
        signed_sigma = actual_return / horizon_sigma
    elif actual_return == 0:
        signed_sigma = 0.0
    else:
        # A non-zero move against zero estimated volatility is effectively
        # unbounded. Keep the stored value finite and deterministically extreme.
        signed_sigma = math.copysign(1_000_000.0, actual_return)

    inside_noise_band = abs(actual_return) <= threshold
    percentile = historical_percentile
    if inside_noise_band:
        severity = "noise"
    elif abs(signed_sigma) >= 2 or (percentile is not None and percentile >= 0.99):
        severity = "extreme"
    elif abs(signed_sigma) >= 1 or (percentile is not None and percentile >= 0.95):
        severity = "large"
    else:
        severity = "directional"

    if actual_return == 0:
        sign_correct = None
    else:
        actual_direction = Direction.UP.value if actual_return > 0 else Direction.DOWN.value
        sign_correct = predicted_direction == actual_direction
    material_direction_correct = None if inside_noise_band else sign_correct
    return OutcomeDiagnostic(
        signed_sigma=signed_sigma,
        severity=severity,
        historical_abs_return_percentile=percentile,
        history_sample_size=history_sample_size,
        data_incomplete=history_sample_size < 250 or breadth_down_ratio is None,
        sign_correct=sign_correct,
        material_direction_correct=material_direction_correct,
    )


def is_systemic_extreme_down(
    diagnostics: Sequence[tuple[float, float, float | None]],
) -> bool:
    """Return true only when direction, standardized size and breadth all pass."""

    if len(diagnostics) < 5:
        return False
    returns = [actual_return for actual_return, _, _ in diagnostics]
    signed_sigmas = [signed_sigma for _, signed_sigma, _ in diagnostics]
    breadth = [ratio for _, _, ratio in diagnostics if ratio is not None]
    return (
        sum(actual_return < 0 for actual_return in returns) >= 4
        and statistics.median(signed_sigmas) <= -1.5
        and len(breadth) == len(diagnostics)
        and min(breadth) >= 0.8
    )


def materialize_evaluation_batch(
    session: Session,
    *,
    target_date: date,
    horizon: str,
    snapshots: Sequence[MarketSnapshotFact],
    source_hash: str,
    now: datetime,
    data_quality: Mapping[str, Any] | None = None,
) -> EvaluationBatch:
    """Persist deterministic diagnostics for all matured formal forecasts.

    The caller owns the trusted read-only adapter. This function only accepts
    already source-bound facts and never reaches into or writes to an upstream
    production data owner.
    Repeating the same immutable evaluation set returns the existing batch.
    """

    forecasts = due_live_forecasts(session, target_date=target_date, horizon=horizon)
    if not forecasts:
        raise ValueError("no evaluated completed live forecasts are due")
    _validate_digest(source_hash, label="source_hash")
    evaluation_set_hash = _evaluation_set_hash(forecasts)
    existing = session.scalar(
        select(EvaluationBatch).where(
            EvaluationBatch.target_date == target_date,
            EvaluationBatch.horizon == horizon,
            EvaluationBatch.evaluation_set_hash == evaluation_set_hash,
            EvaluationBatch.status == "completed",
        )
    )
    if existing is not None:
        return existing
    snapshot_by_index = {item.index_code: item for item in snapshots}
    if len(snapshot_by_index) != len(snapshots):
        raise ValueError("market snapshot index_code values must be unique")
    expected_indexes = {item.index_code for item in forecasts}
    if set(snapshot_by_index) != expected_indexes:
        raise ValueError("market snapshots must exactly match the evaluated forecast set")

    batch = EvaluationBatch(
        id=str(uuid4()),
        target_date=target_date,
        horizon=horizon,
        status="completed",
        evaluation_set_hash=evaluation_set_hash,
        source_hash=source_hash,
        data_quality=dict(data_quality or {}),
        started_at=now,
        completed_at=now,
        error=None,
    )
    session.add(batch)
    session.flush()

    diagnostic_inputs: list[tuple[ForecastDiagnostic, float, float | None]] = []
    for forecast in forecasts:
        evaluation = forecast.evaluation
        if evaluation is None:  # pragma: no cover - guarded by SQL join
            raise ValueError("forecast evaluation disappeared during batch creation")
        fact = snapshot_by_index[forecast.index_code]
        _validate_digest(fact.source_hash, label=f"{fact.index_code} source_hash")
        if not fact.source_url.startswith("https://"):
            raise ValueError("market snapshot source_url must use https")
        _validate_snapshot_matches_forecast(fact, forecast, evaluation)
        content_hash = _snapshot_content_hash(fact)
        session.add(
            MarketSessionSnapshot(
                id=str(uuid4()),
                batch_id=batch.id,
                index_code=fact.index_code,
                index_name=fact.index_name,
                target_date=fact.target_date,
                base_trade_date=fact.base_trade_date,
                base_close=fact.base_close,
                target_close=fact.target_close,
                actual_return=fact.actual_return,
                amount=fact.amount,
                advancers=fact.advancers,
                decliners=fact.decliners,
                unchanged=fact.unchanged,
                limit_down_count=fact.limit_down_count,
                breadth_down_ratio=fact.breadth_down_ratio,
                sector_contributions=fact.sector_contributions,
                weight_contributions=fact.weight_contributions,
                historical_abs_return_percentile=fact.historical_abs_return_percentile,
                history_sample_size=fact.history_sample_size,
                source_url=fact.source_url,
                source_hash=fact.source_hash,
                captured_at=fact.captured_at,
                content_hash=content_hash,
            )
        )
        outcome = diagnose_outcome(
            predicted_direction=forecast.direction,
            actual_return=evaluation.actual_return,
            threshold=forecast.threshold,
            historical_percentile=fact.historical_abs_return_percentile,
            history_sample_size=fact.history_sample_size,
            breadth_down_ratio=fact.breadth_down_ratio,
        )
        diagnostic = ForecastDiagnostic(
            id=str(uuid4()),
            batch_id=batch.id,
            forecast_id=forecast.id,
            evaluation_result_id=evaluation.id,
            signed_sigma=outcome.signed_sigma,
            severity=outcome.severity,
            systemic_extreme_down=False,
            historical_abs_return_percentile=outcome.historical_abs_return_percentile,
            history_sample_size=outcome.history_sample_size,
            data_incomplete=outcome.data_incomplete,
            sign_correct=outcome.sign_correct,
            material_direction_correct=outcome.material_direction_correct,
            brier_score=evaluation.brier_score,
            policy_version=DIAGNOSTIC_POLICY_VERSION,
            created_at=now,
        )
        session.add(diagnostic)
        diagnostic_inputs.append(
            (diagnostic, evaluation.actual_return, fact.breadth_down_ratio)
        )
        _materialize_opinion_evaluations(
            session,
            batch=batch,
            forecast=forecast,
            evaluation=evaluation,
            outcome=outcome,
            now=now,
        )
        materialize_user_judgment_evaluation(
            session,
            batch=batch,
            forecast=forecast,
            evaluation=evaluation,
            material_outcome=outcome.material_direction_correct is not None,
            now=now,
        )

    systemic = is_systemic_extreme_down(
        [
            (actual_return, diagnostic.signed_sigma, breadth_ratio)
            for diagnostic, actual_return, breadth_ratio in diagnostic_inputs
        ]
    )
    if systemic:
        for diagnostic, _, _ in diagnostic_inputs:
            diagnostic.systemic_extreme_down = True
    session.flush()
    return batch


def record_blocked_upstream_batch(
    session: Session,
    *,
    target_date: date,
    horizon: str,
    source_hash: str,
    now: datetime,
    error: str,
    data_quality: Mapping[str, Any] | None = None,
) -> EvaluationBatch:
    """Record an idempotent upstream block without consuming a reflection identity."""

    forecasts = due_live_forecasts(session, target_date=target_date, horizon=horizon)
    if not forecasts:
        raise ValueError("no evaluated completed live forecasts are due")
    _validate_digest(source_hash, label="source_hash")
    evaluation_set_hash = _evaluation_set_hash(forecasts)
    existing = session.scalar(
        select(EvaluationBatch).where(
            EvaluationBatch.target_date == target_date,
            EvaluationBatch.horizon == horizon,
            EvaluationBatch.evaluation_set_hash == evaluation_set_hash,
            EvaluationBatch.status == "blocked_upstream",
        )
    )
    if existing is not None:
        return existing
    row = EvaluationBatch(
        id=str(uuid4()),
        target_date=target_date,
        horizon=horizon,
        status="blocked_upstream",
        evaluation_set_hash=evaluation_set_hash,
        source_hash=source_hash,
        data_quality=dict(data_quality or {}),
        started_at=now,
        completed_at=now,
        error=error,
    )
    session.add(row)
    session.flush()
    return row


def create_reflection_run(
    session: Session,
    *,
    source_run: WorkflowRun,
    source_batch: EvaluationBatch,
    input_hash: str,
    now: datetime,
    schema_version: str = REFLECTION_SCHEMA_VERSION,
    supersedes: ReflectionRun | None = None,
) -> ReflectionRun:
    """Create or return the append-only identity for a formal reflection."""

    if source_run.mode != "live" or source_run.status != "completed":
        raise ValueError("only completed live runs may be reflected")
    validate_default_reflection_universe(source_run)
    if source_batch.status != "completed":
        raise ValueError("reflection requires a completed evaluation batch")
    _validate_digest(input_hash, label="input_hash")
    horizons = {
        forecast.horizon
        for forecast in source_run.forecasts
        if forecast.target_date == source_batch.target_date
    }
    if source_batch.horizon not in horizons:
        raise ValueError("evaluation batch does not belong to the source run")
    existing = session.scalar(
        select(ReflectionRun).where(
            ReflectionRun.source_run_id == source_run.id,
            ReflectionRun.horizon == source_batch.horizon,
            ReflectionRun.target_date == source_batch.target_date,
            ReflectionRun.schema_version == schema_version,
            ReflectionRun.evaluation_set_hash == source_batch.evaluation_set_hash,
        )
    )
    if existing is not None:
        expected_supersedes_id = supersedes.id if supersedes is not None else None
        if existing.supersedes_id != expected_supersedes_id:
            raise ValueError(
                "reflection schema identity already exists with different lineage"
            )
        return existing
    if supersedes is not None:
        if supersedes.status != "completed":
            raise ValueError("only a completed reflection may be superseded")
        if (
            supersedes.source_run_id != source_run.id
            or supersedes.horizon != source_batch.horizon
            or supersedes.target_date != source_batch.target_date
            or supersedes.evaluation_set_hash != source_batch.evaluation_set_hash
        ):
            raise ValueError(
                "a reflection may supersede only the same run, horizon, "
                "target date and evaluation set"
            )
        existing_successor = session.scalar(
            select(ReflectionRun).where(
                ReflectionRun.supersedes_id == supersedes.id,
                ReflectionRun.status.in_(
                    ("awaiting_sources", "awaiting_analysis", "completed")
                ),
            )
        )
        if existing_successor is not None:
            raise ValueError(
                "a reflection correction must supersede the current lineage head"
            )
    else:
        existing_lineage = session.scalar(
            select(ReflectionRun).where(
                ReflectionRun.source_run_id == source_run.id,
                ReflectionRun.horizon == source_batch.horizon,
                ReflectionRun.target_date == source_batch.target_date,
                ReflectionRun.evaluation_set_hash
                == source_batch.evaluation_set_hash,
                ReflectionRun.status.in_(
                    ("awaiting_sources", "awaiting_analysis", "completed")
                ),
            )
        )
        if existing_lineage is not None:
            raise ValueError(
                "a new reflection schema must supersede the current lineage head"
            )
    row = ReflectionRun(
        id=str(uuid4()),
        source_run_id=source_run.id,
        source_batch_id=source_batch.id,
        horizon=source_batch.horizon,
        target_date=source_batch.target_date,
        schema_version=schema_version,
        evaluation_set_hash=source_batch.evaluation_set_hash,
        status="awaiting_sources",
        supersedes_id=supersedes.id if supersedes else None,
        created_at=now,
        completed_at=None,
        error=None,
        input_hash=input_hash,
        source_snapshot_hash=None,
        output_hash=None,
        receipt_hash=None,
    )
    session.add(row)
    session.flush()
    return row


def _materialize_opinion_evaluations(
    session: Session,
    *,
    batch: EvaluationBatch,
    forecast: Forecast,
    evaluation: EvaluationResult,
    outcome: OutcomeDiagnostic,
    now: datetime,
) -> None:
    opinions = session.scalars(
        select(AgentOpinion).where(
            AgentOpinion.run_id == forecast.run_id,
            AgentOpinion.index_code == forecast.index_code,
            AgentOpinion.horizon == forecast.horizon,
        )
    ).all()
    for opinion in opinions:
        definition = AGENT_BY_ID.get(opinion.agent_id)
        metric_plan = None
        if definition is None:
            excluded = True
        else:
            try:
                metric_plan = evaluation_plan(agent_spec(opinion.agent_id))
                excluded = not metric_plan.direction
            except KeyError:
                # Preserve readable legacy opinions from an older/dynamic
                # registry, but fail closed by excluding them from formal
                # metrics until an exact AgentSpec is registered.
                excluded = True
        probabilities = {
            "up": opinion.probability_up,
            "neutral": opinion.probability_neutral,
            "down": opinion.probability_down,
        }
        if evaluation.actual_return == 0:
            sign_correct = None
        else:
            realized = Direction.UP.value if evaluation.actual_return > 0 else Direction.DOWN.value
            sign_correct = opinion.direction == realized
        session.add(
            OpinionEvaluation(
                id=str(uuid4()),
                batch_id=batch.id,
                agent_opinion_id=opinion.id,
                evaluation_result_id=evaluation.id,
                sign_correct=None if excluded else sign_correct,
                material_direction_correct=(
                    None
                    if excluded or outcome.severity == "noise"
                    else sign_correct
                ),
                brier_score=(
                    multiclass_brier_score(
                        probabilities,
                        evaluation.actual_label,
                    )
                    if metric_plan is not None
                    and metric_plan.multiclass_brier
                    else None
                ),
                included_in_direction_score=not excluded,
                evaluated_at=now,
            )
        )


def _validate_snapshot_matches_forecast(
    fact: MarketSnapshotFact,
    forecast: Forecast,
    evaluation: EvaluationResult,
) -> None:
    if fact.target_date != forecast.target_date:
        raise ValueError("snapshot target_date does not match forecast")
    if fact.base_trade_date != forecast.base_trade_date:
        raise ValueError("snapshot base_trade_date does not match forecast")
    numeric_pairs = (
        (fact.base_close, evaluation.start_close, "base_close"),
        (fact.target_close, evaluation.end_close, "target_close"),
        (fact.actual_return, evaluation.actual_return, "actual_return"),
    )
    for received, expected, label in numeric_pairs:
        if not math.isclose(received, expected, rel_tol=0, abs_tol=1e-10):
            raise ValueError(f"snapshot {label} conflicts with immutable evaluation")


def _evaluation_set_hash(forecasts: Sequence[Forecast]) -> str:
    payload = [
        {
            "forecast_id": forecast.id,
            "evaluation_id": forecast.evaluation.id if forecast.evaluation else None,
            "observation_hash": (
                forecast.evaluation.observation_hash if forecast.evaluation else None
            ),
        }
        for forecast in sorted(forecasts, key=lambda item: item.id)
    ]
    return _canonical_hash(payload)


def _snapshot_content_hash(fact: MarketSnapshotFact) -> str:
    payload = {
        key: value.isoformat() if isinstance(value, (date, datetime)) else value
        for key, value in {
            "index_code": fact.index_code,
            "index_name": fact.index_name,
            "target_date": fact.target_date,
            "base_trade_date": fact.base_trade_date,
            "base_close_hex": fact.base_close.hex(),
            "target_close_hex": fact.target_close.hex(),
            "actual_return_hex": fact.actual_return.hex(),
            "amount": fact.amount,
            "advancers": fact.advancers,
            "decliners": fact.decliners,
            "unchanged": fact.unchanged,
            "limit_down_count": fact.limit_down_count,
            "breadth_down_ratio": fact.breadth_down_ratio,
            "sector_contributions": fact.sector_contributions,
            "weight_contributions": fact.weight_contributions,
            "historical_abs_return_percentile": fact.historical_abs_return_percentile,
            "history_sample_size": fact.history_sample_size,
            "source_url": fact.source_url,
            "source_hash": fact.source_hash,
            "captured_at": fact.captured_at,
        }.items()
    }
    return _canonical_hash(payload)


def _canonical_hash(payload: object) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _validate_digest(value: str, *, label: str) -> None:
    if not _SHA256_PATTERN.fullmatch(value) or value == "0" * 64:
        raise ValueError(f"{label} must be a non-placeholder lowercase SHA-256 digest")
