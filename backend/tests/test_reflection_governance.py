from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.models import EvaluationBatch, ReflectionHumanReview, ReflectionRun, WorkflowRun
from app.services.reflection_governance import (
    approved_reflection_review_count,
    assess_lesson_policy,
    completed_live_target_date_count,
    record_reflection_human_review,
    reflection_review_gate_state,
)
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

ZONE = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 17, 18, 0, tzinfo=ZONE)


def test_extreme_checklist_can_be_candidate_but_never_auto_promotes() -> None:
    assessment = assess_lesson_policy(
        proposal_type="risk_check",
        overall_severity="systemic_extreme_down",
        independent_episode_count=1,
        replay_target_dates=0,
        average_brier_improvement=None,
        calibration_improvement=None,
        important_subgroups_non_degrading=None,
        completed_shadow_target_dates=20,
        required_shadow_target_dates=20,
    )
    assert assessment.immediate_extreme_checklist is True
    assert assessment.evidence_threshold_met is True
    assert assessment.wiki_review_ready is True
    assert assessment.automatic_promotion_allowed is False


def test_directional_lesson_requires_events_replays_and_non_degradation() -> None:
    blocked = assess_lesson_policy(
        proposal_type="calibration",
        overall_severity="large",
        independent_episode_count=4,
        replay_target_dates=19,
        average_brier_improvement=0.01,
        calibration_improvement=0.01,
        important_subgroups_non_degrading=True,
        completed_shadow_target_dates=20,
        required_shadow_target_dates=20,
    )
    assert blocked.wiki_review_ready is False
    assert "independent_episodes_below_5" in blocked.blockers
    assert "replay_target_dates_below_20" in blocked.blockers

    ready = assess_lesson_policy(
        proposal_type="calibration",
        overall_severity="large",
        independent_episode_count=5,
        replay_target_dates=20,
        average_brier_improvement=0.01,
        calibration_improvement=0.01,
        important_subgroups_non_degrading=True,
        completed_shadow_target_dates=20,
        required_shadow_target_dates=20,
    )
    assert ready.wiki_review_ready is True
    assert ready.automatic_promotion_allowed is False


def test_human_review_is_immutable_and_counts_only_approved_live(
    client: TestClient,
) -> None:
    with client.app.state.database.session_factory() as session:
        run = WorkflowRun(
            id="review-live-run",
            as_of=NOW,
            data_cutoff=NOW,
            status="completed",
            mode="live",
            started_at=NOW,
            completed_at=NOW,
            duration_seconds=1.0,
            error=None,
            data_quality={},
            workflow_steps=[],
            input_hash="1" * 64,
        )
        batch = EvaluationBatch(
            id="review-batch",
            target_date=date(2026, 7, 17),
            horizon="D1",
            status="completed",
            evaluation_set_hash="2" * 64,
            source_hash="3" * 64,
            data_quality={},
            started_at=NOW,
            completed_at=NOW,
            error=None,
        )
        reflection = ReflectionRun(
            id="review-reflection",
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
        session.add_all([run, batch, reflection])
        session.flush()
        first = record_reflection_human_review(
            session,
            reflection_id=reflection.id,
            decision="approved",
            reviewer="operator",
            notes="evidence checked",
            reviewed_at=NOW,
        )
        repeated = record_reflection_human_review(
            session,
            reflection_id=reflection.id,
            decision="approved",
            reviewer="operator",
            notes="evidence checked",
            reviewed_at=NOW,
        )
        assert repeated.id == first.id
        assert approved_reflection_review_count(session) == 1
        with pytest.raises(ValueError, match="different immutable"):
            record_reflection_human_review(
                session,
                reflection_id=reflection.id,
                decision="rejected",
                reviewer="operator",
                notes="changed",
                reviewed_at=NOW,
            )
        assert (
            session.scalar(select(func.count()).select_from(ReflectionHumanReview))
            == 1
        )
        run.market_universe_hash = "f" * 64
        session.flush()
        assert approved_reflection_review_count(session) == 0
        assert completed_live_target_date_count(session) == 0


def test_review_gate_requires_approved_leading_prefix_and_resolves_successor(
    client: TestClient,
) -> None:
    with client.app.state.database.session_factory() as session:
        run = WorkflowRun(
            id="prefix-live-run",
            as_of=NOW,
            data_cutoff=NOW,
            status="completed",
            mode="live",
            started_at=NOW,
            completed_at=NOW,
            duration_seconds=1.0,
            error=None,
            data_quality={},
            workflow_steps=[],
            input_hash="1" * 64,
        )
        first_batch = EvaluationBatch(
            id="prefix-batch-1",
            target_date=date(2026, 7, 17),
            horizon="D1",
            status="completed",
            evaluation_set_hash="2" * 64,
            source_hash="3" * 64,
            data_quality={},
            started_at=NOW,
            completed_at=NOW,
            error=None,
        )
        second_batch = EvaluationBatch(
            id="prefix-batch-2",
            target_date=date(2026, 7, 18),
            horizon="D1",
            status="completed",
            evaluation_set_hash="4" * 64,
            source_hash="5" * 64,
            data_quality={},
            started_at=NOW,
            completed_at=NOW,
            error=None,
        )
        first = _reflection(
            reflection_id="prefix-reflection-1",
            run_id=run.id,
            batch=first_batch,
            created_at=NOW,
        )
        second = _reflection(
            reflection_id="prefix-reflection-2",
            run_id=run.id,
            batch=second_batch,
            created_at=NOW,
        )
        session.add_all([run, first_batch, second_batch, first, second])
        session.flush()
        record_reflection_human_review(
            session,
            reflection_id=first.id,
            decision="rejected",
            reviewer="operator",
            notes="needs correction",
            reviewed_at=NOW,
        )
        record_reflection_human_review(
            session,
            reflection_id=second.id,
            decision="approved",
            reviewer="operator",
            notes="checked",
            reviewed_at=NOW,
        )
        assert approved_reflection_review_count(session) == 0

        successor = _reflection(
            reflection_id="prefix-reflection-1-v2",
            run_id=run.id,
            batch=first_batch,
            created_at=NOW,
            schema_version="1.1.0",
            supersedes_id=first.id,
        )
        session.add(successor)
        session.flush()
        record_reflection_human_review(
            session,
            reflection_id=successor.id,
            decision="approved",
            reviewer="operator",
            notes="correction checked",
            reviewed_at=NOW,
        )
        state = reflection_review_gate_state(session)
        assert state.current_reflection_ids == (successor.id, second.id)
        assert state.approved_prefix_ids == (successor.id, second.id)
        assert approved_reflection_review_count(session) == 2
        assert len(state.evidence_hash) == 64


def test_review_gate_honors_cutoff_and_hashes_reviewer(
    client: TestClient,
) -> None:
    with client.app.state.database.session_factory() as session:
        run = WorkflowRun(
            id="cutoff-live-run",
            as_of=NOW,
            data_cutoff=NOW,
            status="completed",
            mode="live",
            started_at=NOW,
            completed_at=NOW,
            duration_seconds=1.0,
            error=None,
            data_quality={},
            workflow_steps=[],
            input_hash="1" * 64,
        )
        batch = EvaluationBatch(
            id="cutoff-batch",
            target_date=date(2026, 7, 17),
            horizon="D1",
            status="completed",
            evaluation_set_hash="2" * 64,
            source_hash="3" * 64,
            data_quality={},
            started_at=NOW,
            completed_at=NOW,
            error=None,
        )
        reflection = _reflection(
            reflection_id="cutoff-reflection",
            run_id=run.id,
            batch=batch,
            created_at=NOW,
        )
        session.add_all([run, batch, reflection])
        session.flush()
        review = record_reflection_human_review(
            session,
            reflection_id=reflection.id,
            decision="approved",
            reviewer="operator-a",
            notes="checked",
            reviewed_at=NOW + timedelta(minutes=10),
        )

        before = reflection_review_gate_state(
            session,
            cutoff=NOW + timedelta(minutes=5),
        )
        after = reflection_review_gate_state(
            session,
            cutoff=NOW + timedelta(minutes=10),
        )
        assert before.approved_prefix_ids == ()
        assert approved_reflection_review_count(
            session,
            cutoff=NOW + timedelta(minutes=5),
        ) == 0
        assert after.approved_prefix_ids == (reflection.id,)
        assert before.evidence_hash != after.evidence_hash

        review.reviewer = "operator-b"
        session.flush()
        changed = reflection_review_gate_state(
            session,
            cutoff=NOW + timedelta(minutes=10),
        )
        assert changed.evidence_hash != after.evidence_hash


def test_review_gate_fails_closed_on_lineage_fork(client: TestClient) -> None:
    with client.app.state.database.session_factory() as session:
        # Simulate a pre-0006 database or externally corrupted legacy file.
        session.execute(text("DROP INDEX uq_reflection_active_successor"))
        run = WorkflowRun(
            id="fork-live-run",
            as_of=NOW,
            data_cutoff=NOW,
            status="completed",
            mode="live",
            started_at=NOW,
            completed_at=NOW,
            duration_seconds=1.0,
            error=None,
            data_quality={},
            workflow_steps=[],
            input_hash="1" * 64,
        )
        batch = EvaluationBatch(
            id="fork-batch",
            target_date=date(2026, 7, 17),
            horizon="D1",
            status="completed",
            evaluation_set_hash="2" * 64,
            source_hash="3" * 64,
            data_quality={},
            started_at=NOW,
            completed_at=NOW,
            error=None,
        )
        root = _reflection(
            reflection_id="fork-root",
            run_id=run.id,
            batch=batch,
            created_at=NOW,
        )
        left = _reflection(
            reflection_id="fork-left",
            run_id=run.id,
            batch=batch,
            created_at=NOW,
            schema_version="1.1.0",
            supersedes_id=root.id,
        )
        right = _reflection(
            reflection_id="fork-right",
            run_id=run.id,
            batch=batch,
            created_at=NOW,
            schema_version="1.2.0",
            supersedes_id=root.id,
        )
        session.add_all([run, batch, root, left, right])
        session.flush()
        for reflection in (left, right):
            record_reflection_human_review(
                session,
                reflection_id=reflection.id,
                decision="approved",
                reviewer="operator",
                notes="checked",
                reviewed_at=NOW,
            )

        state = reflection_review_gate_state(session)
        assert state.current_reflection_ids == (left.id, right.id)
        assert state.lineage_conflict_ids == (left.id, right.id)
        assert state.approved_current_ids == ()
        assert state.approved_prefix_ids == ()
        assert approved_reflection_review_count(session) == 0


def _reflection(
    *,
    reflection_id: str,
    run_id: str,
    batch: EvaluationBatch,
    created_at: datetime,
    schema_version: str = "1.0.0",
    supersedes_id: str | None = None,
) -> ReflectionRun:
    return ReflectionRun(
        id=reflection_id,
        source_run_id=run_id,
        source_batch_id=batch.id,
        horizon=batch.horizon,
        target_date=batch.target_date,
        schema_version=schema_version,
        evaluation_set_hash=batch.evaluation_set_hash,
        status="completed",
        supersedes_id=supersedes_id,
        created_at=created_at,
        completed_at=created_at,
        error=None,
        input_hash="6" * 64,
        source_snapshot_hash="7" * 64,
        output_hash="8" * 64,
        receipt_hash="9" * 64,
    )
