from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.models import (
    AgentOpinion,
    EvaluationResult,
    Forecast,
    PriceObservation,
    WorkflowRun,
)
from app.services.evaluation import (
    DemoForecastNotScoredError,
    ForecastNotMatureError,
    PriceObservationConflictError,
    _record_price,
    evaluate_forecast,
)
from app.services.reflection import MarketSnapshotFact, materialize_evaluation_batch
from app.services.seed import seed_demo_data
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

ZONE = ZoneInfo("Asia/Shanghai")


def _create_run(client: TestClient) -> None:
    response = client.post(
        "/api/runs",
        json={"as_of": "2026-07-10T15:00:00+08:00"},
    )
    assert response.status_code == 201, response.text
    with client.app.state.database.session_factory() as session:
        run = session.get(WorkflowRun, response.json()["id"])
        assert run is not None
        run.mode = "live"
        session.commit()


def test_demo_forecast_is_never_formally_scored(client: TestClient) -> None:
    response = client.post(
        "/api/runs",
        json={"as_of": "2026-07-10T15:00:00+08:00"},
    )
    assert response.status_code == 201, response.text
    session, forecast = _forecast(client, "D1")
    try:
        now = datetime.combine(forecast.target_date, time(15, 10), tzinfo=ZONE)
        with pytest.raises(DemoForecastNotScoredError):
            _evaluate(session, forecast, now=now)
        session.rollback()
        assert session.scalar(select(func.count()).select_from(EvaluationResult)) == 0
        assert session.scalar(select(func.count()).select_from(PriceObservation)) == 0
    finally:
        session.close()


def test_demo_seed_creates_predictions_without_scores(client: TestClient) -> None:
    seed_demo_data(client.app.state.workflow, historical_days=1)
    with client.app.state.database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(WorkflowRun)) == 2
        assert session.scalar(select(func.count()).select_from(EvaluationResult)) == 0
        assert session.scalar(select(func.count()).select_from(PriceObservation)) == 0


def _forecast(client: TestClient, horizon: str) -> tuple[object, Forecast]:
    database = client.app.state.database
    session = database.session_factory()
    row = session.scalar(
        select(Forecast)
        .options(selectinload(Forecast.evaluation), selectinload(Forecast.run))
        .where(Forecast.index_code == "000300.SH", Forecast.horizon == horizon)
    )
    assert row is not None
    return session, row


def _source(label: str) -> tuple[str, str]:
    return (
        f"https://example.com/{label}",
        hashlib.sha256(label.encode()).hexdigest(),
    )


def _evaluate(
    session,
    forecast: Forecast,
    *,
    now: datetime,
    start_close: float = 100.0,
    end_close: float = 101.0,
):
    start_url, start_hash = _source(f"{forecast.index_code}-start")
    end_url, end_hash = _source(f"{forecast.index_code}-{forecast.target_date}")
    observed_at = datetime.combine(forecast.target_date, time(15, 10), tzinfo=ZONE)
    return evaluate_forecast(
        session,
        forecast=forecast,
        price_source="test-provider",
        observed_at=observed_at,
        start_trade_date=forecast.base_trade_date,
        start_close=start_close,
        start_source_url=start_url,
        start_source_hash=start_hash,
        end_trade_date=forecast.target_date,
        end_close=end_close,
        end_source_url=end_url,
        end_source_hash=end_hash,
        now=now,
    )


def test_evaluation_rejects_before_target_close_and_wrong_base_date(client: TestClient) -> None:
    _create_run(client)
    session, forecast = _forecast(client, "D1")
    try:
        before_close = datetime.combine(forecast.target_date, time(15, 4), tzinfo=ZONE)
        with pytest.raises(ForecastNotMatureError):
            _evaluate(session, forecast, now=before_close)
        session.rollback()

        after_close = before_close + timedelta(minutes=10)
        start_url, start_hash = _source("wrong-base")
        end_url, end_hash = _source("target")
        with pytest.raises(ValueError, match="frozen base"):
            evaluate_forecast(
                session,
                forecast=forecast,
                price_source="test-provider",
                observed_at=after_close,
                start_trade_date=forecast.base_trade_date - timedelta(days=1),
                start_close=100,
                start_source_url=start_url,
                start_source_hash=start_hash,
                end_trade_date=forecast.target_date,
                end_close=101,
                end_source_url=end_url,
                end_source_hash=end_hash,
                now=after_close,
            )
        session.rollback()
        assert session.scalar(select(func.count()).select_from(EvaluationResult)) == 0
        assert session.scalar(select(func.count()).select_from(PriceObservation)) == 0
    finally:
        session.close()


def test_price_history_is_immutable_across_horizons(client: TestClient) -> None:
    _create_run(client)
    session, d1 = _forecast(client, "D1")
    try:
        now = datetime.combine(d1.target_date, time(15, 10), tzinfo=ZONE)
        _evaluate(session, d1, now=now)
        session.commit()
    finally:
        session.close()

    session, d2 = _forecast(client, "D2")
    try:
        now = datetime.combine(d2.target_date, time(15, 10), tzinfo=ZONE)
        with pytest.raises(PriceObservationConflictError):
            _evaluate(session, d2, now=now, start_close=99.0)
        session.rollback()
        assert session.scalar(select(func.count()).select_from(EvaluationResult)) == 1
        assert session.scalar(select(func.count()).select_from(PriceObservation)) == 2
    finally:
        session.close()


def test_neutral_outcome_is_excluded_from_binary_direction_calibration(
    client: TestClient,
) -> None:
    _create_run(client)
    session, forecast = _forecast(client, "D1")
    try:
        now = datetime.combine(forecast.target_date, time(15, 10), tzinfo=ZONE)
        end_close = 100.0 * (1.0 + forecast.threshold / 2.0)
        result = _evaluate(session, forecast, now=now, end_close=end_close)
        assert result.actual_label == "neutral"
        session.commit()
    finally:
        session.close()

    session, forecast = _forecast(client, "D1")
    try:
        assert forecast.evaluation is not None
        macro_opinion = session.scalar(
            select(AgentOpinion).where(
                AgentOpinion.run_id == forecast.run_id,
                AgentOpinion.agent_id == "macro_policy_agent",
                AgentOpinion.index_code == forecast.index_code,
                AgentOpinion.horizon == forecast.horizon,
            )
        )
        assert macro_opinion is not None
        expected_sign_accuracy = 1.0 if macro_opinion.direction == "up" else 0.0
        materialize_evaluation_batch(
            session,
            target_date=forecast.target_date,
            horizon=forecast.horizon,
            snapshots=[
                MarketSnapshotFact(
                    index_code=forecast.index_code,
                    index_name=forecast.index_name,
                    target_date=forecast.target_date,
                    base_trade_date=forecast.base_trade_date,
                    base_close=forecast.evaluation.start_close,
                    target_close=forecast.evaluation.end_close,
                    actual_return=forecast.evaluation.actual_return,
                    source_url="https://www.csindex.com.cn/close",
                    source_hash=hashlib.sha256(b"neutral-snapshot").hexdigest(),
                    captured_at=now,
                    advancers=2_700,
                    decliners=2_600,
                    unchanged=100,
                    limit_down_count=0,
                    breadth_down_ratio=2_600 / 5_400,
                    historical_abs_return_percentile=0.4,
                    history_sample_size=300,
                )
            ],
            source_hash=hashlib.sha256(b"neutral-batch").hexdigest(),
            now=now,
        )
        session.commit()
    finally:
        session.close()

    scorecard = client.get("/api/agents/macro_policy_agent/scorecard?horizon=D1")
    assert scorecard.status_code == 200
    body = scorecard.json()
    assert body["sample_size"] == 1
    assert body["sign_sample_size"] == 1
    assert body["material_sample_size"] == 0
    assert body["accuracy"] == expected_sign_accuracy
    assert body["sign_accuracy"] == expected_sign_accuracy
    assert body["material_direction_accuracy"] is None
    assert body["calibration"] == []
    assert body["expected_calibration_error"] is None


def test_demo_and_live_price_observations_are_namespaced(client: TestClient) -> None:
    with client.app.state.database.session_factory() as session:
        common = {
            "index_code": "000300.SH",
            "trade_date": date(2026, 7, 13),
            "source_url": "https://example.com/close",
            "source_hash": hashlib.sha256(b"close").hexdigest(),
            "ingested_at": datetime(2026, 7, 13, 15, 10, tzinfo=ZONE),
        }
        _record_price(
            session,
            mode="demo",
            close=100,
            source="deterministic-demo",
            **common,
        )
        _record_price(
            session,
            mode="live",
            close=99,
            source="trusted-live-provider",
            **common,
        )
        session.commit()
        assert session.scalar(select(func.count()).select_from(PriceObservation)) == 2


def test_active_live_runs_have_a_database_level_as_of_gate(client: TestClient) -> None:
    as_of = datetime(2026, 7, 13, 15, tzinfo=ZONE)
    common = {
        "as_of": as_of,
        "data_cutoff": as_of,
        "status": "queued",
        "mode": "live",
        "started_at": as_of,
        "completed_at": None,
        "duration_seconds": None,
        "error": None,
        "data_quality": {},
        "workflow_steps": [],
    }
    with client.app.state.database.session_factory() as session:
        session.add_all(
            [
                WorkflowRun(id="live-run-a", input_hash="a" * 64, **common),
                WorkflowRun(id="live-run-b", input_hash="b" * 64, **common),
            ]
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
