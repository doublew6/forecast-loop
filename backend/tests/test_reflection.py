from __future__ import annotations

import hashlib
from datetime import datetime, time
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from app.models import (
    AgentOpinion,
    EvaluationBatch,
    Forecast,
    LessonProposal,
    OpinionEvaluation,
    ReflectionFinding,
    ReflectionRun,
    WorkflowRun,
)
from app.services.evaluation import evaluate_forecast
from app.services.reflection import (
    MarketSnapshotFact,
    create_reflection_run,
    diagnose_outcome,
    due_live_forecasts,
    is_systemic_extreme_down,
    materialize_evaluation_batch,
    record_blocked_upstream_batch,
)
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

ZONE = ZoneInfo("Asia/Shanghai")


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _create_live_evaluated_forecast(
    client: TestClient,
) -> tuple[object, WorkflowRun, Forecast]:
    response = client.post(
        "/api/runs",
        json={"as_of": "2026-07-10T15:00:00+08:00"},
    )
    assert response.status_code == 201, response.text
    session = client.app.state.database.session_factory()
    run = session.scalar(
        select(WorkflowRun)
        .options(selectinload(WorkflowRun.forecasts))
        .where(WorkflowRun.id == response.json()["id"])
    )
    assert run is not None
    run.mode = "live"
    forecast = next(
        item
        for item in run.forecasts
        if item.horizon == "D1" and item.index_code == "000300.SH"
    )
    now = datetime.combine(forecast.target_date, time(15, 10), tzinfo=ZONE)
    evaluate_forecast(
        session,
        forecast=forecast,
        price_source="synthetic-market-source",
        observed_at=now,
        start_trade_date=forecast.base_trade_date,
        start_close=100.0,
        start_source_url="https://example.com/market-source/base",
        start_source_hash=_digest("base"),
        end_trade_date=forecast.target_date,
        end_close=96.0,
        end_source_url="https://example.com/market-source/target",
        end_source_hash=_digest("target"),
        now=now,
    )
    session.commit()
    session.refresh(forecast)
    return session, run, forecast


@pytest.mark.parametrize(
    ("actual_return", "threshold", "percentile", "expected"),
    [
        (0.002, 0.003, 0.99, "noise"),
        (0.004, 0.003, 0.80, "directional"),
        (0.013, 0.003, 0.80, "large"),
        (0.025, 0.003, 0.80, "extreme"),
        (0.004, 0.003, 0.99, "extreme"),
    ],
)
def test_deterministic_severity_policy(
    actual_return: float,
    threshold: float,
    percentile: float,
    expected: str,
) -> None:
    result = diagnose_outcome(
        predicted_direction="up",
        actual_return=actual_return,
        threshold=threshold,
        historical_percentile=percentile,
        history_sample_size=300,
        breadth_down_ratio=0.3,
    )
    assert result.severity == expected
    assert result.sign_correct is True
    assert result.material_direction_correct is (None if expected == "noise" else True)


def test_systemic_extreme_down_requires_all_three_gates() -> None:
    event = [(-0.04, -2.1, 0.86) for _ in range(5)]
    assert is_systemic_extreme_down(event)
    assert not is_systemic_extreme_down(event[:4])
    assert not is_systemic_extreme_down([(-0.04, -1.2, 0.86) for _ in range(5)])
    assert not is_systemic_extreme_down([(-0.04, -2.1, None) for _ in range(5)])
    assert not is_systemic_extreme_down(
        [(-0.04, -2.1, ratio) for ratio in (0.86, 0.86, 0.86, 0.86, 0.79)]
    )


@pytest.mark.parametrize(
    ("actual_return", "threshold", "percentile", "expected_severity"),
    [
        (0.0, 0.004, 0.99, "noise"),
        (0.004, 0.004, 0.99, "noise"),
        (0.016, 0.004, 0.50, "large"),
        (0.032, 0.004, 0.50, "extreme"),
        (0.005, 0.004, 0.95, "large"),
        (0.005, 0.004, 0.99, "extreme"),
    ],
)
def test_outcome_policy_boundary_values(
    actual_return: float,
    threshold: float,
    percentile: float,
    expected_severity: str,
) -> None:
    result = diagnose_outcome(
        predicted_direction="up",
        actual_return=actual_return,
        threshold=threshold,
        historical_percentile=percentile,
        history_sample_size=300,
        breadth_down_ratio=0.2,
    )

    assert result.severity == expected_severity
    if actual_return == 0:
        assert result.sign_correct is None
        assert result.material_direction_correct is None


@pytest.mark.parametrize(
    ("history_sample_size", "breadth_down_ratio"),
    [(249, 0.2), (300, None)],
)
def test_incomplete_context_does_not_downgrade_material_move_to_noise(
    history_sample_size: int,
    breadth_down_ratio: float | None,
) -> None:
    result = diagnose_outcome(
        predicted_direction="down",
        actual_return=-0.006,
        threshold=0.004,
        historical_percentile=None,
        history_sample_size=history_sample_size,
        breadth_down_ratio=breadth_down_ratio,
    )

    assert result.severity == "directional"
    assert result.data_incomplete is True
    assert result.material_direction_correct is True


def test_reflection_batch_is_live_target_date_based_and_idempotent(
    client: TestClient,
) -> None:
    session, run, forecast = _create_live_evaluated_forecast(client)
    try:
        assert due_live_forecasts(
            session,
            target_date=forecast.target_date,
            horizon="D1",
        ) == [forecast]
        assert (
            due_live_forecasts(
                session,
                target_date=forecast.target_date,
                horizon="D2",
            )
            == []
        )
        evaluation = forecast.evaluation
        assert evaluation is not None
        blocked = record_blocked_upstream_batch(
            session,
            target_date=forecast.target_date,
            horizon="D1",
            source_hash=_digest("missing-market-snapshot"),
            now=evaluation.evaluated_at,
            error="trusted market snapshot is unavailable",
            data_quality={"market_snapshot_complete": False},
        )
        assert blocked.status == "blocked_upstream"
        fact = MarketSnapshotFact(
            index_code=forecast.index_code,
            index_name=forecast.index_name,
            target_date=forecast.target_date,
            base_trade_date=forecast.base_trade_date,
            base_close=evaluation.start_close,
            target_close=evaluation.end_close,
            actual_return=evaluation.actual_return,
            source_url="https://example.com/market-source/session",
            source_hash=_digest("session"),
            captured_at=evaluation.observed_at,
            advancers=500,
            decliners=4500,
            unchanged=100,
            limit_down_count=80,
            breadth_down_ratio=4500 / 5100,
            historical_abs_return_percentile=0.995,
            history_sample_size=1250,
        )
        batch = materialize_evaluation_batch(
            session,
            target_date=forecast.target_date,
            horizon="D1",
            snapshots=[fact],
            source_hash=_digest("batch-source"),
            now=evaluation.evaluated_at,
            data_quality={"errors": 0, "warnings": 0},
        )
        repeated = materialize_evaluation_batch(
            session,
            target_date=forecast.target_date,
            horizon="D1",
            snapshots=[fact],
            source_hash=_digest("batch-source"),
            now=evaluation.evaluated_at,
        )
        assert repeated.id == batch.id
        assert batch.id != blocked.id
        assert batch.diagnostics[0].severity == "extreme"
        assert batch.diagnostics[0].sign_correct == (forecast.direction == "down")
        assert batch.diagnostics[0].material_direction_correct == (
            forecast.direction == "down"
        )
        session.commit()
        opinion_rows = session.scalars(
            select(OpinionEvaluation).where(OpinionEvaluation.batch_id == batch.id)
        ).all()
        expected_opinions = session.scalar(
            select(func.count())
            .select_from(AgentOpinion)
            .where(
                AgentOpinion.run_id == run.id,
                AgentOpinion.index_code == forecast.index_code,
                AgentOpinion.horizon == forecast.horizon,
            )
        )
        assert len(opinion_rows) == expected_opinions
        assert any(not item.included_in_direction_score for item in opinion_rows)
        risk = next(
            item
            for item in opinion_rows
            if item.opinion.agent_id == "risk_critic_agent"
        )
        macro = next(
            item
            for item in opinion_rows
            if item.opinion.agent_id == "macro_policy_agent"
        )
        assert risk.brier_score is None
        assert macro.brier_score is not None
    finally:
        session.close()


def test_due_live_forecasts_excludes_configurable_market_runs(
    client: TestClient,
) -> None:
    session, run, forecast = _create_live_evaluated_forecast(client)
    try:
        run.data_quality = {
            **(run.data_quality or {}),
            "market_universe": {
                "content_hash": "f" * 64,
                "universe_id": "custom-market",
            },
        }
        run.market_universe_hash = "f" * 64
        session.commit()

        assert (
            due_live_forecasts(
                session,
                target_date=forecast.target_date,
                horizon=forecast.horizon,
            )
            == []
        )
    finally:
        session.close()


def test_read_only_reflection_and_lesson_api_excludes_demo(
    client: TestClient,
) -> None:
    session, run, forecast = _create_live_evaluated_forecast(client)
    try:
        evaluation = forecast.evaluation
        assert evaluation is not None
        fact = MarketSnapshotFact(
            index_code=forecast.index_code,
            index_name=forecast.index_name,
            target_date=forecast.target_date,
            base_trade_date=forecast.base_trade_date,
            base_close=evaluation.start_close,
            target_close=evaluation.end_close,
            actual_return=evaluation.actual_return,
            source_url="https://example.com/market-source/session-api",
            source_hash=_digest("session-api"),
            captured_at=evaluation.observed_at,
            breadth_down_ratio=0.9,
            historical_abs_return_percentile=0.995,
            history_sample_size=1250,
        )
        batch = materialize_evaluation_batch(
            session,
            target_date=forecast.target_date,
            horizon=forecast.horizon,
            snapshots=[fact],
            source_hash=_digest("api-batch"),
            now=evaluation.evaluated_at,
        )
        reflection = create_reflection_run(
            session,
            source_run=run,
            source_batch=batch,
            input_hash=_digest("reflection-input"),
            now=evaluation.evaluated_at,
        )
        reflection.status = "completed"
        reflection.completed_at = evaluation.evaluated_at
        reflection.output_hash = _digest("reflection-output")
        reflection.receipt_hash = _digest("reflection-receipt")
        finding = ReflectionFinding(
            id=str(uuid4()),
            reflection_run_id=reflection.id,
            scope_type="committee",
            subject_id="committee",
            index_code=forecast.index_code,
            horizon=forecast.horizon,
            verdict="wrong",
            primary_error_type="reasoning_or_weighting_failure",
            secondary_error_types=["attention_omission"],
            evidence_ids=["evidence-1"],
            availability_class="available_missed",
            causal_status="supported",
            counterfactual={"would_flip": True, "analysis_type": "sensitivity"},
            remediation=["提高事前反证权重"],
            confidence=0.8,
            summary="方向判断未覆盖已知风险。",
            created_at=evaluation.evaluated_at,
        )
        session.add(finding)
        session.flush()
        session.add(
            LessonProposal(
                id=str(uuid4()),
                reflection_run_id=reflection.id,
                episode_key=forecast.target_date.isoformat(),
                cluster_key="committee|reasoning|D1|broad-market",
                title="极端行情风险检查",
                summary="候选检查表，未经人工晋升。",
                status="candidate",
                proposal_type="risk_checklist",
                evidence_finding_ids=[finding.id],
                independent_episode_count=1,
                replay_target_dates=0,
                replay_metrics={},
                half_life_sessions=60,
                created_at=evaluation.evaluated_at,
                reviewed_at=None,
                supersedes_id=None,
            )
        )
        session.commit()

        listing = client.get("/api/reflections")
        assert listing.status_code == 200, listing.text
        assert listing.json()["items"][0]["id"] == reflection.id
        assert listing.json()["items"][0]["overall_severity"] == "extreme"
        detail = client.get(f"/api/reflections/{reflection.id}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["outcomes"][0]["actual_return"] == pytest.approx(-0.04)
        assert body["diagnostics"][0]["severity"] == "extreme"
        assert body["findings"][0]["availability_class"] == "available_missed"
        assert body["findings"][0]["direction_correct"] is False
        assert body["decision_chain"][0]["subject_id"] == "committee"
        assert body["source_timeline"] == []
        lessons = client.get("/api/lessons")
        assert lessons.status_code == 200
        assert lessons.json()["items"][0]["status"] == "candidate"
        assert client.post("/api/reflections", json={}).status_code == 405

        run.mode = "demo"
        session.commit()
        assert client.get("/api/reflections").json()["items"] == []
        assert client.get(f"/api/reflections/{reflection.id}").status_code == 404
        assert client.get("/api/lessons").json()["items"] == []
    finally:
        session.close()


def test_reflection_identity_is_append_only(client: TestClient) -> None:
    session, run, forecast = _create_live_evaluated_forecast(client)
    try:
        evaluation = forecast.evaluation
        assert evaluation is not None
        fact = MarketSnapshotFact(
            index_code=forecast.index_code,
            index_name=forecast.index_name,
            target_date=forecast.target_date,
            base_trade_date=forecast.base_trade_date,
            base_close=evaluation.start_close,
            target_close=evaluation.end_close,
            actual_return=evaluation.actual_return,
            source_url="https://example.com/market-source/identity",
            source_hash=_digest("identity"),
            captured_at=evaluation.observed_at,
            breadth_down_ratio=0.9,
            history_sample_size=300,
            historical_abs_return_percentile=0.99,
        )
        batch = materialize_evaluation_batch(
            session,
            target_date=forecast.target_date,
            horizon=forecast.horizon,
            snapshots=[fact],
            source_hash=_digest("identity-batch"),
            now=evaluation.evaluated_at,
        )
        first = create_reflection_run(
            session,
            source_run=run,
            source_batch=batch,
            input_hash=_digest("input"),
            now=evaluation.evaluated_at,
        )
        second = create_reflection_run(
            session,
            source_run=run,
            source_batch=batch,
            input_hash=_digest("different-input"),
            now=evaluation.evaluated_at,
        )
        assert second.id == first.id
        assert second.input_hash == _digest("input")
        assert session.scalar(select(func.count()).select_from(ReflectionRun)) == 1
        assert session.scalar(select(func.count()).select_from(EvaluationBatch)) == 1

        first.status = "completed"
        first.completed_at = evaluation.evaluated_at
        first.output_hash = _digest("first-output")
        first.receipt_hash = _digest("first-receipt")
        successor = create_reflection_run(
            session,
            source_run=run,
            source_batch=batch,
            input_hash=_digest("successor-input"),
            now=evaluation.evaluated_at,
            schema_version="1.1.0",
            supersedes=first,
        )
        assert successor.supersedes_id == first.id
        with pytest.raises(ValueError, match="different lineage"):
            create_reflection_run(
                session,
                source_run=run,
                source_batch=batch,
                input_hash=_digest("same-identity-wrong-lineage"),
                now=evaluation.evaluated_at,
                schema_version="1.1.0",
            )
        with pytest.raises(ValueError, match="current lineage head"):
            create_reflection_run(
                session,
                source_run=run,
                source_batch=batch,
                input_hash=_digest("fork-input"),
                now=evaluation.evaluated_at,
                schema_version="1.2.0",
                supersedes=first,
            )
        with pytest.raises(ValueError, match="must supersede"):
            create_reflection_run(
                session,
                source_run=run,
                source_batch=batch,
                input_hash=_digest("parallel-root-input"),
                now=evaluation.evaluated_at,
                schema_version="2.0.0",
            )
        session.add(
            ReflectionRun(
                id=str(uuid4()),
                source_run_id=run.id,
                source_batch_id=batch.id,
                horizon=batch.horizon,
                target_date=batch.target_date,
                schema_version="1.2.0",
                evaluation_set_hash=batch.evaluation_set_hash,
                status="awaiting_sources",
                supersedes_id=first.id,
                created_at=evaluation.evaluated_at,
                completed_at=None,
                error=None,
                input_hash=_digest("concurrent-fork-input"),
                source_snapshot_hash=None,
                output_hash=None,
                receipt_hash=None,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
    finally:
        session.close()
