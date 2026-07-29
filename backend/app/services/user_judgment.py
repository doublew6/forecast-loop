"""Immutable human shadow judgments, Wiki seals and deterministic scoring."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..agent_contracts import agent_spec
from ..domain import USER_JUDGMENT_AGENT, Direction
from ..market_universe import DEFAULT_MARKET_UNIVERSE
from ..models import (
    EvaluationBatch,
    EvaluationResult,
    Forecast,
    UserJudgment,
    UserJudgmentEvaluation,
    WorkflowRun,
)
from ..schemas import (
    DirectionMetrics,
    ScorecardRead,
    UserJudgmentCreate,
)
from .signal_contract import persist_agent_spec
from .user_judgment_markdown import (
    USER_JUDGMENT_POLICY_V1,
    USER_JUDGMENT_POLICY_V2,
    UserJudgmentWikiError,
    load_verified_user_judgment_markdown,
    publish_user_judgment_markdown,
    remove_user_judgment_markdown,
    render_user_judgment_markdown,
    user_judgment_schema_for_policy,
)

USER_JUDGMENT_POLICY_VERSION = USER_JUDGMENT_POLICY_V2
USER_JUDGMENT_EVALUATION_POLICY_VERSION = "user-judgment-evaluation/v1"
USER_JUDGMENT_MODEL_NAME = "human-self-report-v1"
SUPPORTED_USER_JUDGMENT_POLICY_VERSIONS = frozenset(
    {USER_JUDGMENT_POLICY_V1, USER_JUDGMENT_POLICY_V2}
)


class UserJudgmentNotFoundError(ValueError):
    pass


class UserJudgmentConflictError(ValueError):
    pass


class UserJudgmentClosedError(ValueError):
    pass


def create_user_judgment(
    session: Session,
    *,
    request: UserJudgmentCreate,
    actor_id: str,
    wiki_root: Path,
    timezone: str,
    window_minutes: int,
    expected_mode: Literal["demo", "live"],
    market_universe_hash: str,
    now: datetime | None = None,
) -> tuple[UserJudgment, bool]:
    """Seal one user judgment and its Markdown projection.

    An identical retry returns the original row. A different second opinion for
    the same actor and forecast is rejected because hindsight-safe history
    cannot be edited in place. ``expected_mode`` is part of the caller's access
    boundary: a Forecast from the other mode is deliberately indistinguishable
    from a missing Forecast.
    """

    fallback_zone = ZoneInfo(timezone)
    current_input = now or datetime.now(fallback_zone)
    actor = _validate_actor_id(actor_id)
    if expected_mode not in {"demo", "live"}:
        raise ValueError(f"unsupported expected forecast mode: {expected_mode}")
    forecast = session.scalar(
        select(Forecast)
        .join(WorkflowRun, WorkflowRun.id == Forecast.run_id)
        .options(
            selectinload(Forecast.run),
            selectinload(Forecast.evaluation),
            selectinload(Forecast.user_judgments).selectinload(
                UserJudgment.evaluation
            ),
        )
        .where(
            Forecast.id == request.forecast_id,
            WorkflowRun.mode == expected_mode,
            WorkflowRun.market_universe_hash == market_universe_hash,
        )
    )
    if forecast is None:
        raise UserJudgmentNotFoundError("Forecast not found")
    if forecast.run.mode != expected_mode:
        raise UserJudgmentNotFoundError("Forecast not found")
    zone, _session_close = _run_market_clock(
        forecast,
        fallback_timezone=timezone,
    )
    current = _aware(current_input, zone)

    existing = next(
        (item for item in forecast.user_judgments if item.actor_id == actor),
        None,
    )
    if existing is not None:
        if _same_request(existing, request):
            verify_user_judgment(
                existing,
                wiki_root=wiki_root,
                timezone=timezone,
            )
            return existing, False
        raise UserJudgmentConflictError(
            "This user already sealed a different judgment for the forecast"
        )

    _validate_request(request)
    deadline = user_judgment_submission_deadline(
        forecast,
        timezone=timezone,
        window_minutes=window_minutes,
    )
    run = forecast.run
    if run.status != "completed" or run.completed_at is None:
        raise UserJudgmentClosedError(
            "Only a completed committee run can receive a user judgment"
        )
    completed_at = _aware(run.completed_at, zone)
    if current < completed_at:
        raise UserJudgmentClosedError(
            "User judgment time cannot predate committee completion"
        )
    if run.mode == "live":
        if forecast.evaluation is not None:
            raise UserJudgmentClosedError(
                "The forecast already has an outcome and cannot receive a judgment"
            )
        if deadline is None or current >= deadline:
            raise UserJudgmentClosedError(
                "The user judgment window has closed"
            )
    elif run.mode != "demo":
        raise ValueError(f"unsupported forecast mode: {run.mode}")

    judgment_id = str(uuid4())
    relative_path = _wiki_relative_path(
        judgment_id=judgment_id,
        target_date=forecast.target_date.isoformat(),
        index_code=forecast.index_code,
        horizon=forecast.horizon,
    )
    formal_score_eligible = run.mode == "live" and request.blind_attestation
    payload = {
        "schema": user_judgment_schema_for_policy(
            USER_JUDGMENT_POLICY_VERSION
        ),
        "id": judgment_id,
        "actor_id": actor,
        "agent_id": USER_JUDGMENT_AGENT.id,
        "agent_version": USER_JUDGMENT_AGENT.version,
        "forecast_id": forecast.id,
        "run_id": run.id,
        "mode": run.mode,
        "index_code": forecast.index_code,
        "horizon": forecast.horizon,
        "target_date": forecast.target_date.isoformat(),
        "direction": request.direction,
        "confidence_hex": request.confidence.hex(),
        "rationale": request.rationale,
        "counter_evidence": request.counter_evidence,
        "invalidation_condition": request.invalidation_condition,
        "blind_attestation": request.blind_attestation,
        "submitted_at": current.isoformat(),
        "submission_deadline": deadline.isoformat() if deadline else None,
        "formal_score_eligible": formal_score_eligible,
        "run_input_hash": run.input_hash,
        "forecast_input_hash": forecast.input_hash,
        "policy_version": USER_JUDGMENT_POLICY_VERSION,
        "wiki_path": relative_path,
    }
    content_hash = _canonical_hash(payload)
    markdown = render_user_judgment_markdown(payload, content_hash)
    artifact_hash = publish_user_judgment_markdown(
        wiki_root,
        relative_path,
        markdown,
    )
    specification = agent_spec(USER_JUDGMENT_AGENT.id)
    if specification.agent_version != USER_JUDGMENT_AGENT.version:
        remove_user_judgment_markdown(wiki_root, relative_path)
        raise UserJudgmentConflictError(
            "User Judgment AgentSpec version does not match the agent"
        )
    row = UserJudgment(
        id=judgment_id,
        actor_id=actor,
        agent_id=USER_JUDGMENT_AGENT.id,
        agent_version=USER_JUDGMENT_AGENT.version,
        agent_spec_hash=specification.content_hash,
        forecast_id=forecast.id,
        run_id=run.id,
        mode=run.mode,
        index_code=forecast.index_code,
        horizon=forecast.horizon,
        target_date=forecast.target_date,
        direction=request.direction,
        confidence=request.confidence,
        rationale=request.rationale,
        counter_evidence=request.counter_evidence,
        invalidation_condition=request.invalidation_condition,
        blind_attestation=request.blind_attestation,
        submitted_at=current,
        submission_deadline=deadline,
        formal_score_eligible=formal_score_eligible,
        run_input_hash=run.input_hash,
        forecast_input_hash=forecast.input_hash,
        policy_version=USER_JUDGMENT_POLICY_VERSION,
        content_hash=content_hash,
        wiki_path=relative_path,
        wiki_artifact_hash=artifact_hash,
        forecast=forecast,
    )
    try:
        session.add(row)
        persist_agent_spec(session, specification)
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        remove_user_judgment_markdown(wiki_root, relative_path)
        concurrent = session.scalar(
            select(UserJudgment)
            .options(
                selectinload(UserJudgment.forecast).selectinload(
                    Forecast.evaluation
                ),
                selectinload(UserJudgment.forecast).selectinload(Forecast.run),
                selectinload(UserJudgment.evaluation),
            )
            .where(
                UserJudgment.actor_id == actor,
                UserJudgment.forecast_id == forecast.id,
            )
        )
        if concurrent is not None and _same_request(concurrent, request):
            verify_user_judgment(
                concurrent,
                wiki_root=wiki_root,
                timezone=timezone,
            )
            return concurrent, False
        raise UserJudgmentConflictError(
            "A user judgment already exists for this forecast"
        ) from exc
    except Exception:
        session.rollback()
        remove_user_judgment_markdown(wiki_root, relative_path)
        raise
    return row, True


def user_judgment_submission_deadline(
    forecast: Forecast,
    *,
    timezone: str,
    window_minutes: int,
) -> datetime | None:
    if forecast.run.mode == "demo":
        return None
    if forecast.run.completed_at is None:
        return None
    zone, session_close = _run_market_clock(
        forecast,
        fallback_timezone=timezone,
    )
    completed_at = _aware(forecast.run.completed_at, zone)
    target_close = datetime.combine(
        forecast.target_date,
        session_close,
        tzinfo=zone,
    )
    return min(completed_at + timedelta(minutes=window_minutes), target_close)


def forecast_market_zone(
    forecast: Forecast,
    *,
    fallback_timezone: str,
) -> ZoneInfo:
    """Return the immutable market timezone sealed with a Forecast's run."""

    zone, _session_close = _run_market_clock(
        forecast,
        fallback_timezone=fallback_timezone,
    )
    return zone


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


def user_judgment_submission_status(
    forecast: Forecast,
    *,
    actor_id: str,
    timezone: str,
    window_minutes: int,
    now: datetime | None = None,
) -> tuple[bool, str, datetime | None, UserJudgment | None]:
    zone, _session_close = _run_market_clock(
        forecast,
        fallback_timezone=timezone,
    )
    current = _aware(now or datetime.now(zone), zone)
    existing = next(
        (
            item
            for item in forecast.user_judgments
            if item.actor_id == actor_id
            and item.mode == forecast.run.mode
        ),
        None,
    )
    deadline = user_judgment_submission_deadline(
        forecast,
        timezone=timezone,
        window_minutes=window_minutes,
    )
    if existing is not None:
        return False, "这条目标已经封签，原记录不可覆盖。", deadline, existing
    if forecast.run.status != "completed":
        return False, "委员会运行尚未完成。", deadline, None
    if forecast.run.mode == "demo":
        return (
            True,
            "Demo 可用于练习封签，但不会进入正式成绩。",
            None,
            None,
        )
    if forecast.evaluation is not None:
        return False, "目标结果已经揭晓，禁止事后补录。", deadline, None
    if deadline is None or current >= deadline:
        return False, "事前判断窗口已经关闭。", deadline, None
    return (
        True,
        "声明盲判后可进入用户影子成绩；未声明仍可存档但不计分。",
        deadline,
        None,
    )


def verify_user_judgment(
    row: UserJudgment,
    *,
    wiki_root: Path,
    timezone: str,
) -> str:
    payload = verify_user_judgment_record(row, timezone=timezone)
    expected = render_user_judgment_markdown(payload, row.content_hash)
    markdown = load_verified_user_judgment_markdown(
        wiki_root,
        row.wiki_path,
        expected_hash=row.wiki_artifact_hash,
        expected_content=expected,
    )
    if row.evaluation is not None:
        _verify_user_judgment_evaluation(row.evaluation, row)
    return markdown


def verify_user_judgment_record(
    row: UserJudgment,
    *,
    timezone: str,
) -> dict[str, Any]:
    """Verify the canonical database record without requiring its projection."""

    _validate_judgment_forecast_binding(row, row.forecast)
    frozen_zone = forecast_market_zone(
        row.forecast,
        fallback_timezone=timezone,
    )
    payload = _payload_from_row(row, timezone=frozen_zone.key)
    actual_hash = _canonical_hash(payload)
    if actual_hash != row.content_hash:
        raise UserJudgmentWikiError("User Judgment content hash mismatch")
    return payload


def verify_user_judgment_evaluation_record(
    row: UserJudgmentEvaluation,
    judgment: UserJudgment,
) -> dict[str, Any]:
    """Verify and return the exact content-addressed evaluation payload."""

    return _verify_user_judgment_evaluation(row, judgment)


def _validate_judgment_forecast_binding(
    judgment: UserJudgment,
    forecast: Forecast,
) -> None:
    run = forecast.run
    expected = {
        "forecast_id": forecast.id,
        "run_id": forecast.run_id,
        "mode": run.mode,
        "index_code": forecast.index_code,
        "horizon": forecast.horizon,
        "target_date": forecast.target_date,
        "run_input_hash": run.input_hash,
        "forecast_input_hash": forecast.input_hash,
    }
    mismatches = [
        label
        for label, value in expected.items()
        if getattr(judgment, label) != value
    ]
    if mismatches:
        raise UserJudgmentWikiError(
            "User Judgment forecast binding mismatch: "
            + ", ".join(sorted(mismatches))
        )
    if judgment.agent_id != USER_JUDGMENT_AGENT.id:
        raise UserJudgmentWikiError("User Judgment agent identity mismatch")
    if not judgment.agent_version:
        raise UserJudgmentWikiError("User Judgment agent version is missing")
    if judgment.policy_version not in SUPPORTED_USER_JUDGMENT_POLICY_VERSIONS:
        raise UserJudgmentWikiError("Unsupported User Judgment policy version")
    if judgment.direction not in {Direction.UP.value, Direction.DOWN.value}:
        raise UserJudgmentWikiError("User Judgment direction is invalid")
    if judgment.mode == "live":
        if judgment.submission_deadline is None:
            raise UserJudgmentWikiError(
                "Live User Judgment submission deadline is missing"
            )
        if not _datetime_precedes(
            judgment.submitted_at,
            judgment.submission_deadline,
        ):
            raise UserJudgmentWikiError(
                "Live User Judgment was not sealed before its deadline"
            )
    if judgment.formal_score_eligible and (
        judgment.mode != "live"
        or not judgment.blind_attestation
        or judgment.submission_deadline is None
    ):
        raise UserJudgmentWikiError(
            "User Judgment formal-score eligibility is inconsistent"
        )


def _verify_user_judgment_evaluation(
    row: UserJudgmentEvaluation,
    judgment: UserJudgment,
    *,
    expected_batch: EvaluationBatch | None = None,
    expected_evaluation: EvaluationResult | None = None,
    expected_sign_correct: bool | None = None,
    expected_material_correct: bool | None = None,
) -> dict[str, Any]:
    batch = row.batch
    evaluation = row.evaluation_result
    if row.user_judgment_id != judgment.id:
        raise UserJudgmentWikiError(
            "User Judgment evaluation judgment binding mismatch"
        )
    if evaluation.forecast_id != judgment.forecast_id:
        raise UserJudgmentWikiError(
            "User Judgment evaluation forecast binding mismatch"
        )
    if (
        batch.status != "completed"
        or batch.target_date != judgment.target_date
        or batch.horizon != judgment.horizon
    ):
        raise UserJudgmentWikiError(
            "User Judgment evaluation batch binding mismatch"
        )
    if (
        row.batch_id != batch.id
        or row.evaluation_result_id != evaluation.id
        or row.actual_return.hex() != evaluation.actual_return.hex()
        or row.actual_label != evaluation.actual_label
        or row.observation_hash != evaluation.observation_hash
        or row.policy_version != USER_JUDGMENT_EVALUATION_POLICY_VERSION
    ):
        raise UserJudgmentWikiError(
            "User Judgment evaluation source values mismatch"
        )
    derived_sign_correct: bool | None
    if row.actual_return == 0:
        derived_sign_correct = None
    else:
        actual_direction = (
            Direction.UP.value
            if row.actual_return > 0
            else Direction.DOWN.value
        )
        derived_sign_correct = judgment.direction == actual_direction
    if row.sign_correct != derived_sign_correct:
        raise UserJudgmentWikiError(
            "User Judgment evaluation sign result mismatch"
        )
    if (
        row.material_direction_correct is not None
        and row.material_direction_correct != row.sign_correct
    ):
        raise UserJudgmentWikiError(
            "User Judgment evaluation material result mismatch"
        )

    payload = _user_judgment_evaluation_payload(
        evaluation_id=row.id,
        judgment=judgment,
        batch=batch,
        evaluation=evaluation,
        actual_return=row.actual_return,
        actual_label=row.actual_label,
        sign_correct=row.sign_correct,
        material_direction_correct=row.material_direction_correct,
        observation_hash=row.observation_hash,
        policy_version=row.policy_version,
        evaluated_at=row.evaluated_at,
    )
    if _canonical_hash(payload) != row.content_hash:
        raise UserJudgmentWikiError(
            "User Judgment evaluation content hash mismatch"
        )

    if expected_batch is not None and expected_batch.id != row.batch_id:
        raise UserJudgmentConflictError(
            "User Judgment evaluation already binds a different batch"
        )
    if (
        expected_evaluation is not None
        and (
            expected_evaluation.id != row.evaluation_result_id
            or expected_evaluation.actual_return.hex()
            != row.actual_return.hex()
            or expected_evaluation.actual_label != row.actual_label
            or expected_evaluation.observation_hash != row.observation_hash
        )
    ):
        raise UserJudgmentConflictError(
            "User Judgment evaluation already binds a different outcome"
        )
    checking_expected_result = (
        expected_batch is not None or expected_evaluation is not None
    )
    if checking_expected_result and (
        expected_sign_correct != row.sign_correct
        or expected_material_correct != row.material_direction_correct
    ):
        raise UserJudgmentConflictError(
            "User Judgment evaluation already stores different derived results"
        )
    return payload


def _user_judgment_evaluation_payload(
    *,
    evaluation_id: str,
    judgment: UserJudgment,
    batch: EvaluationBatch,
    evaluation: EvaluationResult,
    actual_return: float,
    actual_label: str,
    sign_correct: bool | None,
    material_direction_correct: bool | None,
    observation_hash: str,
    policy_version: str,
    evaluated_at: datetime,
) -> dict[str, Any]:
    return {
        "schema": "vericouncil.user-judgment-evaluation/v1",
        "id": evaluation_id,
        "user_judgment_id": judgment.id,
        "user_judgment_content_hash": judgment.content_hash,
        "forecast_id": judgment.forecast_id,
        "run_id": judgment.run_id,
        "run_input_hash": judgment.run_input_hash,
        "forecast_input_hash": judgment.forecast_input_hash,
        "batch_id": batch.id,
        "batch_evaluation_set_hash": batch.evaluation_set_hash,
        "batch_source_hash": batch.source_hash,
        "evaluation_result_id": evaluation.id,
        "actual_return_hex": actual_return.hex(),
        "actual_label": actual_label,
        "sign_correct": sign_correct,
        "material_direction_correct": material_direction_correct,
        "observation_hash": observation_hash,
        "policy_version": policy_version,
        "evaluated_at": _utc_iso(evaluated_at),
    }


def materialize_user_judgment_evaluation(
    session: Session,
    *,
    batch: EvaluationBatch,
    forecast: Forecast,
    evaluation: EvaluationResult,
    material_outcome: bool,
    now: datetime,
) -> list[UserJudgmentEvaluation]:
    judgments = session.scalars(
        select(UserJudgment)
        .options(
            selectinload(UserJudgment.evaluation),
            selectinload(UserJudgment.forecast).selectinload(Forecast.run),
        )
        .where(
            UserJudgment.forecast_id == forecast.id,
            UserJudgment.formal_score_eligible.is_(True),
        )
        .order_by(UserJudgment.id)
    ).all()
    rows: list[UserJudgmentEvaluation] = []
    for judgment in judgments:
        _validate_judgment_forecast_binding(judgment, forecast)
        if evaluation.actual_return == 0:
            sign_correct = None
        else:
            actual_direction = (
                Direction.UP.value
                if evaluation.actual_return > 0
                else Direction.DOWN.value
            )
            sign_correct = judgment.direction == actual_direction
        material_correct = sign_correct if material_outcome else None

        if judgment.evaluation is not None:
            _verify_user_judgment_evaluation(
                judgment.evaluation,
                judgment,
                expected_batch=batch,
                expected_evaluation=evaluation,
                expected_sign_correct=sign_correct,
                expected_material_correct=material_correct,
            )
            rows.append(judgment.evaluation)
            continue

        evaluation_id = str(uuid4())
        evaluated_at = now.astimezone(UTC) if now.tzinfo is not None else now.replace(tzinfo=UTC)
        payload = _user_judgment_evaluation_payload(
            evaluation_id=evaluation_id,
            judgment=judgment,
            batch=batch,
            evaluation=evaluation,
            actual_return=evaluation.actual_return,
            actual_label=evaluation.actual_label,
            sign_correct=sign_correct,
            material_direction_correct=material_correct,
            observation_hash=evaluation.observation_hash,
            policy_version=USER_JUDGMENT_EVALUATION_POLICY_VERSION,
            evaluated_at=evaluated_at,
        )
        row = UserJudgmentEvaluation(
            id=evaluation_id,
            user_judgment_id=judgment.id,
            batch_id=batch.id,
            evaluation_result_id=evaluation.id,
            actual_return=evaluation.actual_return,
            actual_label=evaluation.actual_label,
            sign_correct=sign_correct,
            material_direction_correct=material_correct,
            observation_hash=evaluation.observation_hash,
            policy_version=USER_JUDGMENT_EVALUATION_POLICY_VERSION,
            evaluated_at=evaluated_at,
            content_hash=_canonical_hash(payload),
        )
        session.add(row)
        session.flush()
        rows.append(row)
    return rows


def user_judgment_scorecard(
    session: Session,
    *,
    actor_id: str,
    horizon: str,
    timezone: str,
    market_universe_hash: str,
    index_code: str | None = None,
) -> ScorecardRead:
    statement = (
        select(UserJudgment, UserJudgmentEvaluation)
        .join(
            UserJudgmentEvaluation,
            UserJudgmentEvaluation.user_judgment_id == UserJudgment.id,
        )
        .join(WorkflowRun, WorkflowRun.id == UserJudgment.run_id)
        .where(
            UserJudgment.actor_id == actor_id,
            UserJudgment.agent_id == USER_JUDGMENT_AGENT.id,
            UserJudgment.agent_version == USER_JUDGMENT_AGENT.version,
            UserJudgment.mode == "live",
            UserJudgment.formal_score_eligible.is_(True),
            UserJudgment.horizon == horizon,
            WorkflowRun.mode == "live",
            WorkflowRun.status == "completed",
            WorkflowRun.market_universe_hash == market_universe_hash,
        )
    )
    if index_code is not None:
        statement = statement.where(UserJudgment.index_code == index_code)
    rows = session.execute(statement).all()
    if not rows:
        return _empty_user_scorecard(
            actor_id=actor_id,
            horizon=horizon,
            index_code=index_code,
        )
    for judgment, evaluation in rows:
        verify_user_judgment_record(judgment, timezone=timezone)
        _verify_user_judgment_evaluation(evaluation, judgment)

    sign_values = [
        evaluation.sign_correct
        for _, evaluation in rows
        if evaluation.sign_correct is not None
    ]
    material_values = [
        evaluation.material_direction_correct
        for _, evaluation in rows
        if evaluation.material_direction_correct is not None
    ]
    counters = {
        direction: {"predicted": 0, "actual": 0, "true_positive": 0}
        for direction in Direction
    }
    for judgment, evaluation in rows:
        predicted = Direction(judgment.direction)
        actual = Direction(evaluation.actual_label)
        counters[predicted]["predicted"] += 1
        counters[actual]["actual"] += 1
        if predicted is actual:
            counters[actual]["true_positive"] += 1
    metrics = [
        DirectionMetrics(
            label=direction,
            predicted=values["predicted"],
            actual=values["actual"],
            true_positive=values["true_positive"],
            precision=(
                values["true_positive"] / values["predicted"]
                if values["predicted"]
                else None
            ),
            recall=(
                values["true_positive"] / values["actual"]
                if values["actual"]
                else None
            ),
        )
        for direction, values in counters.items()
    ]
    target_dates = {judgment.target_date for judgment, _ in rows}
    sign_correct = sum(sign_values)
    material_correct = sum(material_values)
    sufficient = len(target_dates) >= 20
    return ScorecardRead(
        agent_id=USER_JUDGMENT_AGENT.id,
        index_code=index_code,
        horizon=horizon,
        sample_size=len(rows),
        sample_sufficient=sufficient,
        accuracy=sign_correct / len(sign_values) if sign_values else None,
        sign_sample_size=len(sign_values),
        sign_correct=sign_correct,
        sign_accuracy=sign_correct / len(sign_values) if sign_values else None,
        material_sample_size=len(material_values),
        material_correct=material_correct,
        material_direction_accuracy=(
            material_correct / len(material_values) if material_values else None
        ),
        average_brier=None,
        direction_metrics=metrics,
        calibration=[],
        expected_calibration_error=None,
        agent_version=USER_JUDGMENT_AGENT.version,
        model_name=USER_JUDGMENT_MODEL_NAME,
        note=(
            f"用户 {actor_id} 已覆盖 {len(target_dates)} 个独立目标日、"
            f"{len(rows)} 条判断；尚未提交三分类概率，因此不计算 Brier 或校准。"
            + (
                " 已达到最低展示门槛。"
                if sufficient
                else " 少于20个独立目标日，不做能力结论。"
            )
        ),
    )


def _empty_user_scorecard(
    *,
    actor_id: str,
    horizon: str,
    index_code: str | None,
) -> ScorecardRead:
    return ScorecardRead(
        agent_id=USER_JUDGMENT_AGENT.id,
        index_code=index_code,
        horizon=horizon,
        sample_size=0,
        sample_sufficient=False,
        accuracy=None,
        sign_sample_size=0,
        sign_correct=0,
        sign_accuracy=None,
        material_sample_size=0,
        material_correct=0,
        material_direction_accuracy=None,
        average_brier=None,
        direction_metrics=[
            DirectionMetrics(
                label=direction,
                predicted=0,
                actual=0,
                true_positive=0,
                precision=None,
                recall=None,
            )
            for direction in Direction
        ],
        calibration=[],
        expected_calibration_error=None,
        agent_version=USER_JUDGMENT_AGENT.version,
        model_name=USER_JUDGMENT_MODEL_NAME,
        note=(
            f"用户 {actor_id} 的当前版本尚无已到期、声明盲判的 Live 样本；"
            "Demo 与未声明独立性的记录不进入正式成绩。"
        ),
    )


def _payload_from_row(row: UserJudgment, *, timezone: str) -> dict[str, Any]:
    zone = ZoneInfo(timezone)
    return {
        "schema": user_judgment_schema_for_policy(row.policy_version),
        "id": row.id,
        "actor_id": row.actor_id,
        "agent_id": row.agent_id,
        "agent_version": row.agent_version,
        "forecast_id": row.forecast_id,
        "run_id": row.run_id,
        "mode": row.mode,
        "index_code": row.index_code,
        "horizon": row.horizon,
        "target_date": row.target_date.isoformat(),
        "direction": row.direction,
        "confidence_hex": row.confidence.hex(),
        "rationale": row.rationale,
        "counter_evidence": row.counter_evidence,
        "invalidation_condition": row.invalidation_condition,
        "blind_attestation": row.blind_attestation,
        "submitted_at": _aware(row.submitted_at, zone).isoformat(),
        "submission_deadline": (
            _aware(row.submission_deadline, zone).isoformat()
            if row.submission_deadline is not None
            else None
        ),
        "formal_score_eligible": row.formal_score_eligible,
        "run_input_hash": row.run_input_hash,
        "forecast_input_hash": row.forecast_input_hash,
        "policy_version": row.policy_version,
        "wiki_path": row.wiki_path,
    }


def _same_request(row: UserJudgment, request: UserJudgmentCreate) -> bool:
    return (
        row.direction == request.direction
        and row.confidence.hex() == request.confidence.hex()
        and row.rationale == request.rationale
        and row.counter_evidence == request.counter_evidence
        and row.invalidation_condition == request.invalidation_condition
        and row.blind_attestation == request.blind_attestation
    )


def _validate_request(request: UserJudgmentCreate) -> None:
    if request.direction not in {Direction.UP.value, Direction.DOWN.value}:
        raise ValueError("User Judgment direction must be up or down")
    if not math.isfinite(request.confidence) or not 0.5 <= request.confidence <= 1:
        raise ValueError("User Judgment confidence must be between 0.5 and 1.0")
    fields = {
        "rationale": (request.rationale, 20, 4000),
        "counter_evidence": (request.counter_evidence, 10, 2000),
        "invalidation_condition": (
            request.invalidation_condition,
            10,
            2000,
        ),
    }
    for label, (value, minimum, maximum) in fields.items():
        if not minimum <= len(value.strip()) <= maximum:
            raise ValueError(
                f"User Judgment {label} must contain {minimum}-{maximum} characters"
            )


def _validate_actor_id(value: str) -> str:
    actor = value.strip()
    if not actor or len(actor) > 120:
        raise ValueError("User Judgment actor_id must contain 1-120 characters")
    if any(ord(character) < 32 for character in actor):
        raise ValueError("User Judgment actor_id contains control characters")
    return actor


def _wiki_relative_path(
    *,
    judgment_id: str,
    target_date: str,
    index_code: str,
    horizon: str,
) -> str:
    safe_index = "".join(
        character if character.isalnum() else "-"
        for character in index_code
    ).strip("-")
    return (
        f"decisions/{target_date}/"
        f"{target_date}-{horizon}-{safe_index}-{judgment_id}.md"
    )


def _canonical_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _aware(value: datetime, zone: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def _datetime_precedes(left: datetime, right: datetime) -> bool:
    return _comparable_datetime(left) < _comparable_datetime(right)


def _comparable_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        normalized = value.replace(tzinfo=UTC)
    else:
        normalized = value.astimezone(UTC)
    return normalized.isoformat().replace("+00:00", "Z")
