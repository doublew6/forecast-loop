from __future__ import annotations

import hashlib
import math
from copy import deepcopy
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.models import (
    EvaluationBatch,
    EvaluationResult,
    Forecast,
    ForecastDiagnostic,
    LessonEpisode,
    LessonLifecycleEvent,
    LessonProposal,
    LessonReplayBatch,
    MarketSessionSnapshot,
    ReflectionRun,
    WorkflowRun,
)
from app.services.lesson_lifecycle import (
    CandidateTransform,
    approve_lesson,
    build_replay_manifest_hashes,
    lesson_revalidation_due_reasons,
    parse_replay_bundle,
    record_lesson_replay,
    revalidate_lesson,
    verify_lesson_audit,
)
from fastapi.testclient import TestClient
from sqlalchemy import func, select

ZONE = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 17, 18, 30, tzinfo=ZONE)


def _add_lesson(
    session,
    *,
    suffix: str = "one",
    cluster_key: str = "lesson.market-breadth",
    proposal_type: str = "calibration",
    replay_metrics: dict | None = None,
) -> LessonProposal:
    run = WorkflowRun(
        id=f"lifecycle-run-{suffix}",
        as_of=NOW + timedelta(days=len(suffix)),
        data_cutoff=NOW + timedelta(days=len(suffix)),
        status="completed",
        mode="live",
        started_at=NOW,
        completed_at=NOW,
        duration_seconds=1.0,
        error=None,
        data_quality={},
        workflow_steps=[],
        input_hash=(suffix[0] * 64)[:64],
    )
    batch = EvaluationBatch(
        id=f"lifecycle-batch-{suffix}",
        target_date=date(2026, 7, 17) + timedelta(days=len(suffix)),
        horizon="D1",
        status="completed",
        evaluation_set_hash=("2" * 63 + suffix[0])[:64],
        source_hash=("3" * 63 + suffix[0])[:64],
        data_quality={},
        started_at=NOW,
        completed_at=NOW,
        error=None,
    )
    reflection = ReflectionRun(
        id=f"lifecycle-reflection-{suffix}",
        source_run_id=run.id,
        source_batch_id=batch.id,
        horizon="D1",
        target_date=batch.target_date,
        schema_version="1.0.0",
        evaluation_set_hash=batch.evaluation_set_hash,
        status="completed",
        supersedes_id=None,
        created_at=NOW,
        completed_at=NOW,
        error=None,
        input_hash="4" * 64,
        source_snapshot_hash="5" * 64,
        output_hash="6" * 64,
        receipt_hash="7" * 64,
    )
    lesson = LessonProposal(
        id=f"lifecycle-lesson-{suffix}",
        reflection_run_id=reflection.id,
        episode_key=batch.target_date.isoformat(),
        cluster_key=cluster_key,
        title="Market breadth gate",
        summary="Replay this proposed rule.",
        status="candidate",
        proposal_type=proposal_type,
        evidence_finding_ids=[],
        independent_episode_count=5,
        replay_target_dates=0,
        replay_metrics=replay_metrics or {},
        half_life_sessions=60,
        created_at=NOW,
        reviewed_at=None,
        supersedes_id=None,
    )
    session.add_all([run, batch, reflection, lesson])
    session.flush()
    return lesson


def _bundle(
    lesson_id: str,
    *,
    start: date,
    count: int,
    quality: str = "good",
) -> dict:
    transform = CandidateTransform.model_validate(
        {
            "transform_type": "temperature_class_bias_v1",
            "temperature": 1.0,
            "class_logit_bias": {"up": 6.0, "neutral": 0.0, "down": 0.0},
        }
    )
    observations = []
    for offset in range(count):
        target_date = start + timedelta(days=offset)
        if quality == "good":
            baseline = {"up": 0.45, "neutral": 0.10, "down": 0.45}
            actual_label = "up"
        else:
            baseline = {"up": 0.05, "neutral": 0.05, "down": 0.90}
            actual_label = "down"
        logits = {
            label: math.log(baseline[label])
            + transform.class_logit_bias.model_dump()[label]
            for label in ("up", "neutral", "down")
        }
        maximum = max(logits.values())
        exponentials = {
            label: math.exp(logits[label] - maximum)
            for label in ("up", "neutral", "down")
        }
        total = sum(exponentials.values())
        candidate = {
            label: exponentials[label] / total
            for label in ("up", "neutral", "down")
        }
        identity = target_date.strftime("%Y%m%d")
        observations.append(
            {
                "target_date": target_date.isoformat(),
                "index_code": "000300.SH",
                "horizon": "D1",
                "actual_label": actual_label,
                "forecast_id": f"replay-forecast-{identity}",
                "forecast_diagnostic_id": f"replay-diagnostic-{identity}",
                "outcome_snapshot_hash": _digest(f"outcome-{identity}"),
                "market_snapshot_hash": _digest(f"market-{identity}"),
                "baseline_probabilities": baseline,
                "candidate_probabilities": candidate,
                "important_subgroups": ["broad-index"],
            }
        )
    payload = {
        "protocol_version": "1.0.0",
        "lesson_id": lesson_id,
        "baseline_rule_version": "forecast-probabilities-v1",
        "candidate_rule_version": "candidate-v1",
        "wiki_version": "wiki-1.0.0",
        "threshold_policy_version": "1.0.0",
        "replay_generator": "deterministic_rule_engine",
        "candidate_transform": transform.model_dump(mode="json"),
        "observations": observations,
    }
    payload.update(
        build_replay_manifest_hashes(
            lesson_id=lesson_id,
            candidate_rule_version=payload["candidate_rule_version"],
            wiki_version=payload["wiki_version"],
            threshold_policy_version=payload["threshold_policy_version"],
            candidate_transform=transform,
        )
    )
    return payload


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _materialize_replay_outcomes(session, payload: dict) -> None:
    for observation in payload["observations"]:
        if session.get(ForecastDiagnostic, observation["forecast_diagnostic_id"]):
            continue
        target_date = date.fromisoformat(observation["target_date"])
        identity = target_date.strftime("%Y%m%d")
        actual_return = 0.01 if observation["actual_label"] == "up" else -0.01
        run = WorkflowRun(
            id=f"replay-run-{identity}",
            as_of=datetime(
                target_date.year,
                target_date.month,
                target_date.day,
                8,
                tzinfo=ZONE,
            ),
            data_cutoff=datetime(
                target_date.year,
                target_date.month,
                target_date.day,
                8,
                tzinfo=ZONE,
            ),
            status="completed",
            mode="live",
            started_at=NOW,
            completed_at=NOW,
            duration_seconds=1.0,
            error=None,
            data_quality={},
            workflow_steps=[],
            input_hash=_digest(f"run-{identity}"),
        )
        baseline = observation["baseline_probabilities"]
        forecast = Forecast(
            id=observation["forecast_id"],
            run_id=run.id,
            index_code=observation["index_code"],
            index_name="沪深300",
            horizon=observation["horizon"],
            base_trade_date=target_date - timedelta(days=1),
            target_date=target_date,
            as_of=run.as_of,
            data_cutoff=run.data_cutoff,
            direction="up",
            probability_up=baseline["up"],
            probability_neutral=baseline["neutral"],
            probability_down=baseline["down"],
            threshold=0.005,
            confidence=0.6,
            rationale="Frozen replay baseline.",
            counter_evidence=[],
            invalidation_conditions=[],
            citations=[],
            abstain=False,
            model_name="codex",
            model_version="1.0.0",
            wiki_version="wiki-1.0.0",
            input_hash=_digest(f"forecast-{identity}"),
            created_at=NOW,
        )
        evaluation = EvaluationResult(
            id=f"replay-evaluation-{identity}",
            forecast_id=forecast.id,
            actual_return=actual_return,
            actual_label=observation["actual_label"],
            correct=observation["actual_label"] == "up",
            brier_score=0.0,
            evaluated_at=NOW,
            price_source="synthetic-market-source",
            observed_at=NOW,
            start_trade_date=forecast.base_trade_date,
            start_close=100.0,
            start_source_url="https://example.com/base",
            start_source_hash=_digest(f"base-{identity}"),
            end_trade_date=target_date,
            end_close=100.0 * (1 + actual_return),
            end_source_url="https://example.com/target",
            end_source_hash=_digest(f"target-{identity}"),
            observation_hash=observation["outcome_snapshot_hash"],
        )
        batch = EvaluationBatch(
            id=f"replay-batch-{identity}",
            target_date=target_date,
            horizon=observation["horizon"],
            status="completed",
            evaluation_set_hash=_digest(f"evaluation-set-{identity}"),
            source_hash=_digest(f"source-{identity}"),
            data_quality={},
            started_at=NOW,
            completed_at=NOW,
            error=None,
        )
        snapshot = MarketSessionSnapshot(
            id=f"replay-snapshot-{identity}",
            batch_id=batch.id,
            index_code=forecast.index_code,
            index_name=forecast.index_name,
            target_date=target_date,
            base_trade_date=forecast.base_trade_date,
            base_close=100.0,
            target_close=100.0 * (1 + actual_return),
            actual_return=actual_return,
            amount=None,
            advancers=None,
            decliners=None,
            unchanged=None,
            limit_down_count=None,
            breadth_down_ratio=None,
            sector_contributions=[],
            weight_contributions=[],
            historical_abs_return_percentile=None,
            history_sample_size=1250,
            source_url="https://example.com/market",
            source_hash=_digest(f"market-source-{identity}"),
            captured_at=NOW,
            content_hash=observation["market_snapshot_hash"],
        )
        diagnostic = ForecastDiagnostic(
            id=observation["forecast_diagnostic_id"],
            batch_id=batch.id,
            forecast_id=forecast.id,
            evaluation_result_id=evaluation.id,
            signed_sigma=1.0 if actual_return > 0 else -1.0,
            severity="large",
            systemic_extreme_down=False,
            historical_abs_return_percentile=None,
            history_sample_size=1250,
            data_incomplete=False,
            sign_correct=evaluation.correct,
            material_direction_correct=evaluation.correct,
            brier_score=0.0,
            policy_version="1.0.0",
            created_at=NOW,
        )
        session.add_all(
            [run, forecast, evaluation, batch, snapshot, diagnostic]
        )
    session.flush()


def _record(
    session,
    lesson: LessonProposal,
    *,
    start: date,
    count: int = 20,
    quality: str = "good",
    recorded_at: datetime = NOW,
):
    payload = _bundle(lesson.id, start=start, count=count, quality=quality)
    _materialize_replay_outcomes(session, payload)
    return record_lesson_replay(
        session,
        bundle=parse_replay_bundle(payload),
        submitted_by="replay-operator",
        recorded_at=recorded_at,
        required_shadow_target_dates=1,
    )


def test_detailed_replay_is_hashed_idempotent_and_human_activated(
    client: TestClient,
) -> None:
    with client.app.state.database.session_factory() as session:
        lesson = _add_lesson(session)
        payload = _bundle(lesson.id, start=date(2026, 1, 1), count=20)
        _materialize_replay_outcomes(session, payload)
        bundle = parse_replay_bundle(payload)
        first = record_lesson_replay(
            session,
            bundle=bundle,
            submitted_by="replay-operator",
            recorded_at=NOW,
            required_shadow_target_dates=1,
        )
        repeated = record_lesson_replay(
            session,
            bundle=bundle,
            submitted_by="replay-operator",
            recorded_at=NOW,
            required_shadow_target_dates=1,
        )
        assert repeated.idempotent is True
        assert repeated.batch.id == first.batch.id
        assert len(first.batch.content_hash) == 64
        assert lesson.replay_target_dates == 20
        assert lesson.replay_metrics["average_brier_improvement"] > 0
        assert lesson.replay_metrics["calibration_improvement"] > 0
        assert lesson.replay_metrics["important_subgroups_non_degrading"] is True
        assert lesson.replay_metrics["wiki_review_ready"] is True
        assert lesson.replay_metrics["automatic_promotion_allowed"] is False
        assert (
            session.scalar(select(func.count()).select_from(LessonReplayBatch))
            == 1
        )

        transition = approve_lesson(
            session,
            lesson_id=lesson.id,
            reviewer="lesson-reviewer",
            notes="Replay evidence and subgroup metrics checked.",
            approved_at=NOW + timedelta(minutes=5),
        )
        session.commit()
        assert transition.lesson.status == "active"
        assert transition.lesson.replay_metrics["wiki_promotion_status"] == "not_promoted"
        assert transition.event.event_type == "approved"

    response = client.get("/api/lessons")
    assert response.status_code == 200
    payload = response.json()["items"][0]
    assert payload["status"] == "active"
    assert payload["latest_replay_hash"] == first.batch.content_hash
    assert payload["replay_batch_count"] == 1
    assert [item["event_type"] for item in payload["lifecycle_history"]] == [
        "replay_recorded",
        "approved",
    ]


def test_replay_rejects_overlapping_observation_identity(
    client: TestClient,
) -> None:
    with client.app.state.database.session_factory() as session:
        lesson = _add_lesson(session, suffix="overlap")
        _record(
            session,
            lesson,
            start=date(2026, 1, 1),
            count=2,
        )
        with pytest.raises(ValueError, match="already recorded"):
            _record(
                session,
                lesson,
                start=date(2026, 1, 2),
                count=2,
                recorded_at=NOW + timedelta(minutes=1),
            )
        changed_manifest = _bundle(
            lesson.id,
            start=date(2026, 2, 1),
            count=2,
        )
        changed_manifest["candidate_rule_version"] = "candidate-v2"
        transform = CandidateTransform.model_validate(
            changed_manifest["candidate_transform"]
        )
        changed_manifest.update(
            build_replay_manifest_hashes(
                lesson_id=lesson.id,
                candidate_rule_version="candidate-v2",
                wiki_version=changed_manifest["wiki_version"],
                threshold_policy_version=changed_manifest[
                    "threshold_policy_version"
                ],
                candidate_transform=transform,
            )
        )
        _materialize_replay_outcomes(session, changed_manifest)
        with pytest.raises(ValueError, match="manifest changed"):
            record_lesson_replay(
                session,
                bundle=parse_replay_bundle(changed_manifest),
                submitted_by="replay-operator",
                recorded_at=NOW + timedelta(minutes=2),
                required_shadow_target_dates=1,
            )


def test_replay_rejects_forged_outcome_and_arbitrary_candidate_probabilities(
    client: TestClient,
) -> None:
    with client.app.state.database.session_factory() as session:
        lesson = _add_lesson(session, suffix="forged")
        payload = _bundle(lesson.id, start=date(2026, 4, 1), count=1)
        _materialize_replay_outcomes(session, payload)

        forged_label = deepcopy(payload)
        forged_label["observations"][0]["actual_label"] = "down"
        with pytest.raises(ValueError, match="actual_label conflicts"):
            record_lesson_replay(
                session,
                bundle=parse_replay_bundle(forged_label),
                submitted_by="replay-operator",
                recorded_at=NOW,
                required_shadow_target_dates=1,
            )

        forged_snapshot = deepcopy(payload)
        forged_snapshot["observations"][0]["market_snapshot_hash"] = _digest(
            "forged-market"
        )
        with pytest.raises(ValueError, match="market_snapshot_hash conflicts"):
            record_lesson_replay(
                session,
                bundle=parse_replay_bundle(forged_snapshot),
                submitted_by="replay-operator",
                recorded_at=NOW,
                required_shadow_target_dates=1,
            )

        forged_candidate = deepcopy(payload)
        forged_candidate["observations"][0]["candidate_probabilities"] = {
            "up": 0.01,
            "neutral": 0.01,
            "down": 0.98,
        }
        with pytest.raises(ValueError, match="registered deterministic transform"):
            record_lesson_replay(
                session,
                bundle=parse_replay_bundle(forged_candidate),
                submitted_by="replay-operator",
                recorded_at=NOW,
                required_shadow_target_dates=1,
            )


def test_due_revalidation_challenges_then_retires_failed_lesson(
    client: TestClient,
) -> None:
    with client.app.state.database.session_factory() as session:
        lesson = _add_lesson(session, suffix="retire")
        _record(session, lesson, start=date(2026, 1, 1))
        approve_lesson(
            session,
            lesson_id=lesson.id,
            reviewer="lesson-reviewer",
            notes="Initial evidence approved.",
            approved_at=NOW,
        )

        _record(
            session,
            lesson,
            start=date(2026, 1, 21),
            recorded_at=NOW + timedelta(days=1),
        )
        assert "new_20_target_dates" in lesson_revalidation_due_reasons(
            lesson,
            as_of=NOW + timedelta(days=1),
        )
        passed = revalidate_lesson(
            session,
            lesson_id=lesson.id,
            reviewer="lesson-reviewer",
            notes="First scheduled replay remains valid.",
            reviewed_at=NOW + timedelta(days=1),
            required_shadow_target_dates=1,
        )
        assert passed.lesson.status == "active"

        _record(
            session,
            lesson,
            start=date(2026, 2, 10),
            quality="bad",
            recorded_at=NOW + timedelta(days=2),
        )
        challenged = revalidate_lesson(
            session,
            lesson_id=lesson.id,
            reviewer="lesson-reviewer",
            notes="Aggregate metrics no longer improve.",
            reviewed_at=NOW + timedelta(days=2),
            required_shadow_target_dates=1,
        )
        assert challenged.lesson.status == "challenged"
        repeated = revalidate_lesson(
            session,
            lesson_id=lesson.id,
            reviewer="lesson-reviewer",
            notes="Aggregate metrics no longer improve.",
            reviewed_at=NOW + timedelta(days=2),
            required_shadow_target_dates=1,
        )
        assert repeated.idempotent is True

        _record(
            session,
            lesson,
            start=date(2026, 3, 2),
            quality="bad",
            recorded_at=NOW + timedelta(days=3),
        )
        retired = revalidate_lesson(
            session,
            lesson_id=lesson.id,
            reviewer="lesson-reviewer",
            notes="Second consecutive due review failed.",
            reviewed_at=NOW + timedelta(days=3),
            required_shadow_target_dates=1,
        )
        session.commit()
        assert retired.lesson.status == "retired"
        assert retired.event.event_type == "retired"
        assert retired.lesson.replay_metrics["consecutive_failed_revalidations"] == 2

        event_types = session.scalars(
            select(LessonLifecycleEvent.event_type)
            .where(LessonLifecycleEvent.lesson_proposal_id == lesson.id)
            .order_by(LessonLifecycleEvent.occurred_at, LessonLifecycleEvent.id)
        ).all()
        assert "challenged" in event_types
        assert "retired" in event_types


def test_monthly_and_60_session_due_and_successor_lineage(
    client: TestClient,
) -> None:
    with client.app.state.database.session_factory() as session:
        old = _add_lesson(session, suffix="old")
        _record(session, old, start=date(2026, 1, 1))
        approve_lesson(
            session,
            lesson_id=old.id,
            reviewer="lesson-reviewer",
            notes="Activate original lesson.",
            approved_at=NOW,
        )
        assert lesson_revalidation_due_reasons(
            old,
            as_of=datetime(2026, 8, 17, 18, 30, tzinfo=ZONE),
        ) == ["monthly"]
        old.replay_target_dates += 60
        reasons = lesson_revalidation_due_reasons(
            old,
            as_of=NOW + timedelta(days=1),
        )
        assert reasons == ["new_20_target_dates", "half_life_60_sessions"]

        successor = _add_lesson(
            session,
            suffix="successor",
            cluster_key=old.cluster_key,
        )
        _record(
            session,
            successor,
            start=date(2025, 10, 1),
            recorded_at=NOW + timedelta(days=2),
        )
        with pytest.raises(ValueError, match="explicitly supersede"):
            approve_lesson(
                session,
                lesson_id=successor.id,
                reviewer="lesson-reviewer",
                notes="A second active head must be rejected.",
                approved_at=NOW + timedelta(days=2),
            )
        result = approve_lesson(
            session,
            lesson_id=successor.id,
            reviewer="lesson-reviewer",
            notes="Successor replaces the challenged rule.",
            approved_at=NOW + timedelta(days=2),
            supersedes_id=old.id,
        )
        session.commit()
        assert result.lesson.status == "active"
        assert result.lesson.supersedes_id == old.id
        assert old.status == "superseded"
        assert old.replay_metrics["superseded_by_id"] == successor.id
        assert (
            session.scalar(
                select(func.count())
                .select_from(LessonLifecycleEvent)
                .where(
                    LessonLifecycleEvent.lesson_proposal_id == old.id,
                    LessonLifecycleEvent.event_type == "superseded",
                )
            )
            == 1
        )


def test_monthly_revalidation_without_fresh_replay_challenges(
    client: TestClient,
) -> None:
    with client.app.state.database.session_factory() as session:
        lesson = _add_lesson(session, suffix="stale")
        _record(session, lesson, start=date(2026, 1, 1))
        approve_lesson(
            session,
            lesson_id=lesson.id,
            reviewer="lesson-reviewer",
            notes="Activate before monthly review.",
            approved_at=NOW,
        )
        result = revalidate_lesson(
            session,
            lesson_id=lesson.id,
            reviewer="lesson-reviewer",
            notes="No new replay evidence was available.",
            reviewed_at=datetime(2026, 8, 17, 18, 30, tzinfo=ZONE),
            required_shadow_target_dates=1,
        )
        assert result.lesson.status == "challenged"
        assert (
            "no_new_replay_evidence_since_last_validation"
            in result.event.payload["policy_assessment"]["blockers"]
        )


def test_extreme_singleton_can_only_activate_as_checklist(
    client: TestClient,
) -> None:
    with client.app.state.database.session_factory() as session:
        checklist = _add_lesson(
            session,
            suffix="extreme",
            proposal_type="risk_check",
            replay_metrics={
                "policy_version": "1.0.0",
                "wiki_review_ready": True,
                "automatic_promotion_allowed": False,
                "immediate_extreme_checklist": True,
                "blockers": [],
            },
        )
        result = approve_lesson(
            session,
            lesson_id=checklist.id,
            reviewer="lesson-reviewer",
            notes="Approve only the risk checklist, not a direction rule.",
            approved_at=NOW,
        )
        assert result.lesson.status == "active"
        assert result.lesson.replay_target_dates == 0

        invalid = _add_lesson(
            session,
            suffix="invalidextreme",
            proposal_type="calibration",
            replay_metrics={
                "policy_version": "1.0.0",
                "wiki_review_ready": True,
                "automatic_promotion_allowed": False,
                "immediate_extreme_checklist": True,
                "blockers": [],
            },
        )
        with pytest.raises(ValueError, match="checklist proposal"):
            approve_lesson(
                session,
                lesson_id=invalid.id,
                reviewer="lesson-reviewer",
                notes="Should fail.",
                approved_at=NOW,
            )


def test_verify_recomputes_hashes_metrics_events_and_projection(
    client: TestClient,
) -> None:
    with client.app.state.database.session_factory() as session:
        lesson = _add_lesson(session, suffix="audit")
        reflection = session.get(ReflectionRun, lesson.reflection_run_id)
        assert reflection is not None
        session.add(
            LessonEpisode(
                id="audit-episode",
                cluster_key=lesson.cluster_key,
                episode_key=lesson.episode_key,
                first_reflection_run_id=reflection.id,
                evidence_set_hash=reflection.evaluation_set_hash,
                created_at=NOW,
            )
        )
        replay = _record(session, lesson, start=date(2026, 1, 1))
        approve_lesson(
            session,
            lesson_id=lesson.id,
            reviewer="lesson-reviewer",
            notes="Activate verified lesson.",
            approved_at=NOW,
        )
        replay_batch_id = replay.batch.id
        session.commit()
        session.expire_all()
        lesson = session.get(LessonProposal, lesson.id)
        replay_batch = session.get(LessonReplayBatch, replay_batch_id)
        assert lesson is not None
        assert replay_batch is not None
        report = verify_lesson_audit(session, lesson_id=lesson.id)
        assert report.replay_batch_count == 1
        assert report.lifecycle_event_count == 2
        assert len(report.audit_root_hash) == 64

        original_observations = deepcopy(replay_batch.observations)
        tampered_observations = deepcopy(original_observations)
        tampered_observations[0]["actual_label"] = "down"
        replay_batch.observations = tampered_observations
        session.flush()
        with pytest.raises(ValueError, match="content hash"):
            verify_lesson_audit(session, lesson_id=lesson.id)
        replay_batch.observations = original_observations
        session.flush()

        approval_event = session.scalar(
            select(LessonLifecycleEvent).where(
                LessonLifecycleEvent.lesson_proposal_id == lesson.id,
                LessonLifecycleEvent.event_type == "approved",
            )
        )
        assert approval_event is not None
        original_payload = deepcopy(approval_event.payload)
        approval_event.payload = {**original_payload, "wiki_promotion_performed": True}
        session.flush()
        with pytest.raises(ValueError, match="envelope hash"):
            verify_lesson_audit(session, lesson_id=lesson.id)
        approval_event.payload = original_payload
        original_reason = approval_event.reason
        approval_event.reason = "Tampered review reason."
        session.flush()
        with pytest.raises(ValueError, match="envelope hash"):
            verify_lesson_audit(session, lesson_id=lesson.id)
        approval_event.reason = original_reason
        lesson.status = "challenged"
        session.flush()
        with pytest.raises(ValueError, match="status projection"):
            verify_lesson_audit(session, lesson_id=lesson.id)
