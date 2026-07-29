"""Forecast evaluation and Agent scorecard calculations."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..agent_contracts import agent_spec
from ..domain import (
    AGENT_BY_ID,
    Direction,
    classify_return,
    directional_confidence,
    multiclass_brier_score,
    realized_return,
)
from ..market_universe import DEFAULT_MARKET_UNIVERSE
from ..models import (
    AgentOpinion,
    EvaluationBatch,
    EvaluationResult,
    Forecast,
    OpinionEvaluation,
    PriceObservation,
    WorkflowRun,
)
from ..schemas import CalibrationPoint, DirectionMetrics, EvaluationRead, ScorecardRead
from ..serializers import evaluation_result_read
from .evaluation_facade import evaluation_plan
from .snapshot import validate_trusted_source_url

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ForecastAlreadyEvaluatedError(ValueError):
    pass


class DemoForecastNotScoredError(ValueError):
    """Demo forecasts are illustrative only and never enter formal scores."""


class ForecastNotMatureError(ValueError):
    pass


class PriceObservationConflictError(ValueError):
    pass


def evaluate_forecast(
    session: Session,
    *,
    forecast: Forecast,
    price_source: str,
    observed_at: datetime,
    start_trade_date: date,
    start_close: float,
    start_source_url: str,
    start_source_hash: str,
    end_trade_date: date,
    end_close: float,
    end_source_url: str,
    end_source_hash: str,
    timezone: str = "Asia/Shanghai",
    now: datetime | None = None,
    trusted_sources_only: bool = False,
) -> EvaluationResult:
    """Evaluate one matured forecast from immutable, source-bound close prices.

    The API never accepts a caller-computed return. It stores both price points,
    their source digests and a canonical hash of the evaluation input.
    """

    if forecast.run.mode != "live":
        raise DemoForecastNotScoredError(
            "Demo forecasts are illustrative and are never formally evaluated"
        )
    if forecast.evaluation is not None:
        raise ForecastAlreadyEvaluatedError("forecast has already been evaluated")
    if forecast.run.status != "completed":
        raise ValueError("only completed forecast runs can be evaluated")
    zone, session_close = _run_market_clock(forecast, fallback_timezone=timezone)
    observed_at = _aware(observed_at, zone)
    current = _aware(now or datetime.now(zone), zone)
    maturity = (
        datetime.combine(forecast.target_date, session_close, tzinfo=zone)
        + timedelta(minutes=5)
    )
    if current < maturity:
        raise ForecastNotMatureError(f"forecast does not mature until {maturity.isoformat()}")
    if observed_at < maturity:
        raise ValueError("observation predates the target-session close")
    if observed_at > current + timedelta(minutes=5):
        raise ValueError("observation timestamp is in the future")
    if start_trade_date != forecast.base_trade_date:
        raise ValueError("start price date must equal the frozen base trading session")
    if end_trade_date != forecast.target_date:
        raise ValueError("end price date must equal the forecast target date")
    if start_trade_date >= end_trade_date:
        raise ValueError("start price must precede the target-session close")
    if not price_source.strip():
        raise ValueError("price_source is required")
    _validate_source(
        start_source_url,
        start_source_hash,
        trusted_sources_only=trusted_sources_only,
    )
    _validate_source(
        end_source_url,
        end_source_hash,
        trusted_sources_only=trusted_sources_only,
    )

    actual_return = realized_return(start_close, end_close)
    label = classify_return(actual_return, forecast.threshold)
    probabilities = {
        "up": forecast.probability_up,
        "neutral": forecast.probability_neutral,
        "down": forecast.probability_down,
    }
    result = EvaluationResult(
        id=str(uuid4()),
        forecast_id=forecast.id,
        actual_return=actual_return,
        actual_label=label.value,
        correct=forecast.direction == label.value,
        brier_score=multiclass_brier_score(probabilities, label),
        evaluated_at=current,
        price_source=price_source.strip(),
        observed_at=observed_at,
        start_trade_date=start_trade_date,
        start_close=start_close,
        start_source_url=start_source_url,
        start_source_hash=start_source_hash,
        end_trade_date=end_trade_date,
        end_close=end_close,
        end_source_url=end_source_url,
        end_source_hash=end_source_hash,
        observation_hash=_observation_hash(
            forecast=forecast,
            price_source=price_source.strip(),
            observed_at=observed_at,
            start_trade_date=start_trade_date,
            start_close=start_close,
            start_source_url=start_source_url,
            start_source_hash=start_source_hash,
            end_trade_date=end_trade_date,
            end_close=end_close,
            end_source_url=end_source_url,
            end_source_hash=end_source_hash,
        ),
    )
    _record_price(
        session,
        mode=forecast.run.mode,
        index_code=forecast.index_code,
        trade_date=start_trade_date,
        close=start_close,
        source=price_source.strip(),
        source_url=start_source_url,
        source_hash=start_source_hash,
        ingested_at=current,
    )
    _record_price(
        session,
        mode=forecast.run.mode,
        index_code=forecast.index_code,
        trade_date=end_trade_date,
        close=end_close,
        source=price_source.strip(),
        source_url=end_source_url,
        source_hash=end_source_hash,
        ingested_at=current,
    )
    session.add(result)
    session.flush()
    return result


def _run_market_clock(
    forecast: Forecast,
    *,
    fallback_timezone: str,
) -> tuple[ZoneInfo, time]:
    del fallback_timezone  # Legacy rows are always the default Universe.
    quality = forecast.run.data_quality or {}
    raw_universe = quality.get("market_universe")
    if raw_universe is None:
        if (
            forecast.run.market_universe_hash
            != DEFAULT_MARKET_UNIVERSE.content_hash
        ):
            raise ValueError("forecast market-universe clock metadata is missing")
        timezone_name = DEFAULT_MARKET_UNIVERSE.timezone
        session_close = DEFAULT_MARKET_UNIVERSE.session_close
    elif isinstance(raw_universe, dict):
        if (
            raw_universe.get("content_hash")
            != forecast.run.market_universe_hash
        ):
            raise ValueError("forecast market-universe clock metadata is invalid")
        timezone_name = raw_universe.get("timezone")
        session_close = raw_universe.get("session_close")
    else:
        raise ValueError("forecast market-universe clock metadata is invalid")
    if not isinstance(timezone_name, str) or not isinstance(session_close, str):
        raise ValueError("forecast market-universe clock metadata is invalid")
    try:
        hour_text, minute_text = session_close.split(":", maxsplit=1)
        close = time(int(hour_text), int(minute_text))
        zone = ZoneInfo(timezone_name)
    except (ValueError, TypeError) as exc:
        raise ValueError("forecast market-universe clock metadata is invalid") from exc
    return zone, close


def scorecard(
    session: Session,
    *,
    agent_id: str,
    index_code: str | None = None,
    horizon: str,
    mode: str,
    market_universe_hash: str,
    model_name: str | None = None,
    forecast_model_version: str | None = None,
) -> ScorecardRead:
    if agent_id not in AGENT_BY_ID:
        raise KeyError(agent_id)
    agent = AGENT_BY_ID[agent_id]
    plan = evaluation_plan(agent_spec(agent_id))
    if not plan.direction:
        return ScorecardRead(
            agent_id=agent_id,
            index_code=index_code,
            horizon=horizon,
            sample_size=0,
            sample_sufficient=False,
            accuracy=None,
            average_brier=None,
            direction_metrics=_empty_direction_metrics(),
            agent_version=agent.version,
            model_name=model_name,
            note=(
                "该 Agent 当前不可用，不产生判断，也不进入成绩单。"
                if agent.status == "unavailable"
                else "该 Agent 的版本化能力不包含方向评价。"
            ),
        )

    statement = (
        select(AgentOpinion, EvaluationResult, OpinionEvaluation)
        .join(OpinionEvaluation, OpinionEvaluation.agent_opinion_id == AgentOpinion.id)
        .join(EvaluationBatch, EvaluationBatch.id == OpinionEvaluation.batch_id)
        .join(
            Forecast,
            and_(
                Forecast.run_id == AgentOpinion.run_id,
                Forecast.index_code == AgentOpinion.index_code,
                Forecast.horizon == AgentOpinion.horizon,
            ),
        )
        .join(
            EvaluationResult,
            EvaluationResult.id == OpinionEvaluation.evaluation_result_id,
        )
        .join(WorkflowRun, WorkflowRun.id == Forecast.run_id)
        .where(AgentOpinion.agent_id == agent_id)
    )
    statement = statement.where(
        WorkflowRun.mode == mode,
        WorkflowRun.status == "completed",
        WorkflowRun.market_universe_hash == market_universe_hash,
        EvaluationBatch.status == "completed",
        OpinionEvaluation.included_in_direction_score.is_(True),
        AgentOpinion.agent_version == agent.version,
        AgentOpinion.horizon == horizon,
        AgentOpinion.direction.in_((Direction.UP.value, Direction.DOWN.value)),
    )
    if model_name:
        statement = statement.where(AgentOpinion.model_name == model_name)
    if forecast_model_version:
        statement = statement.where(Forecast.model_version == forecast_model_version)
    if index_code:
        statement = statement.where(AgentOpinion.index_code == index_code)
    rows = session.execute(statement).all()
    if not rows:
        model_identity = f" / {model_name}" if model_name else ""
        return ScorecardRead(
            agent_id=agent_id,
            index_code=index_code,
            horizon=horizon,
            sample_size=0,
            sample_sufficient=False,
            accuracy=None,
            average_brier=None,
            direction_metrics=_empty_direction_metrics(),
            agent_version=agent.version,
            model_name=model_name,
            note=(
                f"当前版本 v{agent.version}{model_identity} 尚无已到期样本；"
                "不会用旧版本结果回填。"
            ),
        )

    brier_scores: list[float] = []
    sign_values: list[bool] = []
    material_values: list[bool] = []
    calibration_bins: dict[int, list[tuple[float, bool]]] = {}
    counters = {
        direction: {"predicted": 0, "actual": 0, "true_positive": 0} for direction in Direction
    }
    for opinion, evaluation, opinion_evaluation in rows:
        probabilities = None
        if plan.multiclass_brier or plan.calibration:
            probabilities = {
                "up": opinion.probability_up,
                "neutral": opinion.probability_neutral,
                "down": opinion.probability_down,
            }
        predicted = Direction(opinion.direction)
        actual = Direction(evaluation.actual_label)
        counters[predicted]["predicted"] += 1
        counters[actual]["actual"] += 1
        if predicted is actual:
            counters[actual]["true_positive"] += 1
        # Directional confidence is conditional on the realized move leaving
        # the evaluation noise band, so neutral outcomes do not belong in
        # this binary calibration curve. Three-outcome quality remains
        # measured by the multiclass Brier score below.
        if plan.calibration and actual is not Direction.NEUTRAL:
            if probabilities is None:  # Defensive fail-closed guard.
                raise ValueError("calibration requires complete probabilities")
            confidence = directional_confidence(probabilities)
            bucket = min(int(confidence * 10), 9)
            calibration_bins.setdefault(bucket, []).append((confidence, predicted is actual))
        if opinion_evaluation.sign_correct is not None:
            sign_values.append(opinion_evaluation.sign_correct)
        if opinion_evaluation.material_direction_correct is not None:
            material_values.append(opinion_evaluation.material_direction_correct)
        if plan.multiclass_brier:
            if opinion_evaluation.brier_score is None:
                raise ValueError(
                    "multiclass Agent evaluation is missing its Brier score"
                )
            brier_scores.append(opinion_evaluation.brier_score)
    sample_size = len(rows)
    sign_correct = sum(sign_values)
    material_correct = sum(material_values)
    sign_accuracy = sign_correct / len(sign_values) if sign_values else None
    material_accuracy = (
        material_correct / len(material_values) if material_values else None
    )
    prediction_dates = len({opinion.target_date for opinion, _, _ in rows})
    metrics = []
    for direction in Direction:
        values = counters[direction]
        predicted_count = values["predicted"]
        actual_count = values["actual"]
        true_positive = values["true_positive"]
        metrics.append(
            DirectionMetrics(
                label=direction,
                predicted=predicted_count,
                actual=actual_count,
                true_positive=true_positive,
                precision=true_positive / predicted_count if predicted_count else None,
                recall=true_positive / actual_count if actual_count else None,
            )
        )
    # Five index outcomes from one committee date are useful observations, but
    # they are correlated and must not count as five independent prediction
    # periods when deciding whether the UI may compare agent ability.
    sufficient = prediction_dates >= 20
    calibration = [
        CalibrationPoint(
            bucket=f"{bucket / 10:.1f}-{(bucket + 1) / 10:.1f}",
            predicted=sum(confidence for confidence, _ in values) / len(values),
            observed=sum(int(outcome) for _, outcome in values) / len(values),
            count=len(values),
        )
        for bucket, values in sorted(calibration_bins.items())
    ]
    directional_calibration_count = sum(point.count for point in calibration)
    expected_calibration_error = (
        sum(
            point.count
            / directional_calibration_count
            * abs(point.predicted - point.observed)
            for point in calibration
        )
        if directional_calibration_count
        else None
    )
    return ScorecardRead(
        agent_id=agent_id,
        index_code=index_code,
        horizon=horizon,
        sample_size=sample_size,
        sample_sufficient=sufficient,
        accuracy=sign_accuracy,
        sign_sample_size=len(sign_values),
        sign_correct=sign_correct,
        sign_accuracy=sign_accuracy,
        material_sample_size=len(material_values),
        material_correct=material_correct,
        material_direction_accuracy=material_accuracy,
        average_brier=(
            sum(brier_scores) / len(brier_scores)
            if brier_scores
            else None
        ),
        direction_metrics=metrics,
        calibration=calibration,
        expected_calibration_error=expected_calibration_error,
        agent_version=agent.version,
        model_name=model_name,
        note=(
            f"已覆盖 {prediction_dates} 个预测截面、{sample_size} 条"
            f" Agent×日期×指数观察；符号口径 {len(sign_values)} 条，"
            f"重大行情口径 {len(material_values)} 条，其中"
            f" {directional_calibration_count} 条进入方向校准；达到最低展示门槛。"
            if sufficient
            else (
                f"仅覆盖 {prediction_dates} 个预测截面、{sample_size} 条"
                f" Agent×日期×指数观察；符号口径 {len(sign_values)} 条，"
                f"重大行情口径 {len(material_values)} 条，其中"
                f" {directional_calibration_count} 条进入方向校准；"
                "少于20个预测截面，不做能力结论。"
            )
        ),
    )


def evaluation_read(result: EvaluationResult) -> EvaluationRead:
    return evaluation_result_read(result)


def _empty_direction_metrics() -> list[DirectionMetrics]:
    return [
        DirectionMetrics(
            label=direction,
            predicted=0,
            actual=0,
            true_positive=0,
            precision=None,
            recall=None,
        )
        for direction in Direction
    ]


def _aware(value: datetime, zone: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def _validate_source(
    source_url: str,
    source_hash: str,
    *,
    trusted_sources_only: bool,
) -> None:
    if not source_url.startswith("https://"):
        raise ValueError("price source URL must use https")
    if not _SHA256_PATTERN.fullmatch(source_hash):
        raise ValueError("price source hash must be a lowercase SHA-256 digest")
    if source_hash == "0" * 64:
        raise ValueError("price source hash cannot be a placeholder digest")
    if trusted_sources_only:
        validate_trusted_source_url(source_url, label="evaluation price")


def _record_price(
    session: Session,
    *,
    mode: str,
    index_code: str,
    trade_date: date,
    close: float,
    source: str,
    source_url: str,
    source_hash: str,
    ingested_at: datetime,
) -> PriceObservation:
    existing = session.scalar(
        select(PriceObservation).where(
            PriceObservation.mode == mode,
            PriceObservation.index_code == index_code,
            PriceObservation.trade_date == trade_date,
        )
    )
    if existing is not None:
        same = (
            math.isclose(existing.close, close, rel_tol=0, abs_tol=1e-10)
            and existing.source == source
            and existing.source_url == source_url
            and existing.source_hash == source_hash
        )
        if not same:
            raise PriceObservationConflictError(
                f"immutable price observation conflict for {index_code} {trade_date}"
            )
        return existing
    observation = PriceObservation(
        mode=mode,
        index_code=index_code,
        trade_date=trade_date,
        close=close,
        source=source,
        source_url=source_url,
        source_hash=source_hash,
        ingested_at=ingested_at,
    )
    session.add(observation)
    return observation


def _observation_hash(
    *,
    forecast: Forecast,
    price_source: str,
    observed_at: datetime,
    start_trade_date: date,
    start_close: float,
    start_source_url: str,
    start_source_hash: str,
    end_trade_date: date,
    end_close: float,
    end_source_url: str,
    end_source_hash: str,
) -> str:
    payload = {
        "forecast_id": forecast.id,
        "index_code": forecast.index_code,
        "horizon": forecast.horizon,
        "target_date": forecast.target_date.isoformat(),
        "price_source": price_source,
        "observed_at": observed_at.isoformat(),
        "start": {
            "trade_date": start_trade_date.isoformat(),
            "close_hex": start_close.hex(),
            "source_url": start_source_url,
            "source_hash": start_source_hash,
        },
        "end": {
            "trade_date": end_trade_date.isoformat(),
            "close_hex": end_close.hex(),
            "source_url": end_source_url,
            "source_hash": end_source_hash,
        },
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
