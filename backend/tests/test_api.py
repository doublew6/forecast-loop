from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from app import api as api_module
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.market_universe import DEFAULT_MARKET_UNIVERSE
from app.models import Forecast, WorkflowRun, WorkflowTask
from app.services.prediction_status import write_prediction_prepare_receipt
from app.services.reflection import MarketSnapshotFact, materialize_evaluation_batch
from app.services.task_queue import EXECUTION_MANIFEST_SCHEMA
from app.workflow import CommitteeWorkflow, PreparedRun
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

TEST_OPERATOR_TOKEN = "test-operator-token-0123456789abcdef"
OPERATOR_HEADERS = {"Authorization": f"Bearer {TEST_OPERATOR_TOKEN}"}


def _enable_live_http(client: TestClient) -> None:
    client.app.state.settings.execution_provider = "codex_file"
    client.app.state.settings.demo_mode = False
    client.app.state.settings.operator_token = SecretStr(TEST_OPERATOR_TOKEN)


def _create_run(client: TestClient) -> dict:
    response = client.post(
        "/api/runs",
        json={"as_of": datetime(2026, 7, 10, 15, 0).isoformat()},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_health_and_agent_registry(client: TestClient) -> None:
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["mode"] == "demo"
    agents = client.get("/api/agents").json()["items"]
    assert len(agents) == 8
    strategy = next(agent for agent in agents if agent["id"] == "strategy_agent")
    assert strategy["kind"] == "strategy"
    assert strategy["workflow_role"] == "strategy"
    assert strategy["source_type"] == "ai"
    assert strategy["status"] == "active"
    assert strategy["spec"]["schema_version"] == "forecast-loop.agent-spec/v1"
    assert strategy["spec"]["capabilities"]["probability_mode"] == "multiclass"
    assert strategy["spec"]["participation"]["mode"] == "formal"
    assert len(strategy["spec"]["content_hash"]) == 64
    quant = next(agent for agent in agents if agent["id"] == "quant_agent")
    assert quant["workflow_role"] == "research"
    assert quant["source_type"] == "quant"
    assert quant["status"] == "unavailable"
    assert quant["weight"] == 0
    assert quant["spec"]["agent_version"] == "0.3.0"
    assert quant["spec"]["capabilities"]["probability_mode"] == "multiclass"
    assert quant["spec"]["participation"]["mode"] == "shadow"
    assert quant["spec"]["participation"]["influence"] == "none"
    cio = next(agent for agent in agents if agent["id"] == "cio_agent")
    assert cio["workflow_role"] == "decision"
    assert cio["source_type"] == "deterministic"
    user = next(agent for agent in agents if agent["id"] == "user_judgment_agent")
    assert user["kind"] == "human"
    assert user["workflow_role"] == "shadow"
    assert user["source_type"] == "manual"
    assert user["status"] == "shadow"
    assert user["weight"] == 0
    assert user["spec"]["capabilities"]["probability_mode"] == "confidence"
    assert user["spec"]["participation"]["mode"] == "shadow"

    spec_response = client.get("/api/agents/user_judgment_agent/spec")
    assert spec_response.status_code == 200
    assert spec_response.json() == user["spec"]
    assert client.get("/api/agents/missing/spec").status_code == 404

    agent_schema = client.get("/api/contracts/agent-spec/schema")
    assert agent_schema.status_code == 200
    assert agent_schema.json()["title"] == "AgentSpec"
    signal_schema = client.get("/api/contracts/signal-envelope/schema")
    assert signal_schema.status_code == 200
    assert signal_schema.json()["title"] == "SignalEnvelope"
    quant_schema = client.get("/api/contracts/quant-signal-bundle/schema")
    assert quant_schema.status_code == 200
    assert quant_schema.json()["title"] == "QuantSignalBundle"
    universe_schema = client.get("/api/contracts/market-universe/schema")
    assert universe_schema.status_code == 200
    assert universe_schema.json()["title"] == "MarketUniverseSpec"


def test_prediction_status_api_exposes_sanitized_daily_receipt(client: TestClient) -> None:
    attempted_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    write_prediction_prepare_receipt(
        client.app.state.settings,
        base_session=attempted_at.date(),
        attempted_at=attempted_at,
        result={
            "status": "blocked_upstream",
            "error": "quality mismatch at /Users/private/data_quality_latest.parquet",
        },
    )

    response = client.get("/api/prediction-status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["today"]["state"] == "blocked"
    assert payload["history"][0]["error_code"] == "quality_gate_failed"
    assert "/Users/" not in response.text


def test_missing_live_credentials_never_silently_fall_back_to_demo(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'blocked.sqlite3'}",
        checkpoint_path=tmp_path / "blocked-checkpoint.sqlite3",
        wiki_path=tmp_path / "wiki",
        demo_mode=False,
        llm_api_key=None,
        operator_token=TEST_OPERATOR_TOKEN,
        auto_seed=False,
    )
    with TestClient(
        create_app(settings, allow_schema_bootstrap=True)
    ) as blocked_client:
        assert blocked_client.get("/api/health").json()["mode"] == "blocked-live"
        response = blocked_client.post(
            "/api/runs",
            json={},
            headers=OPERATOR_HEADERS,
        )
        assert response.status_code == 409
        assert "LLM_API_KEY" in response.text


def test_blank_live_snapshot_path_is_blocked_instead_of_current_directory(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'blocked-blank.sqlite3'}",
        checkpoint_path=tmp_path / "blocked-blank-checkpoint.sqlite3",
        wiki_path=tmp_path / "wiki",
        demo_mode=False,
        llm_api_key="test-key",
        evidence_snapshot_path="",
        operator_token=TEST_OPERATOR_TOKEN,
        auto_seed=False,
    )
    assert settings.evidence_snapshot_path is None
    with TestClient(
        create_app(settings, allow_schema_bootstrap=True)
    ) as blocked_client:
        assert blocked_client.get("/api/health").json()["mode"] == "blocked-live"
        response = blocked_client.post(
            "/api/runs",
            json={},
            headers=OPERATOR_HEADERS,
        )
        assert response.status_code == 409
        assert "EVIDENCE_SNAPSHOT_PATH" in response.text


def test_startup_marks_interrupted_runs_failed_and_releases_gate(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'recovery.sqlite3'}"
    database = Database(database_url)
    database.create_all()
    interrupted_at = datetime.fromisoformat("2026-07-13T15:00:00+08:00")
    with database.session_factory() as session:
        session.add(
            WorkflowRun(
                id="interrupted-live-run",
                as_of=interrupted_at,
                data_cutoff=interrupted_at,
                status="running",
                mode="live",
                started_at=interrupted_at,
                completed_at=None,
                duration_seconds=None,
                error=None,
                data_quality={},
                workflow_steps=[],
                input_hash="c" * 64,
            )
        )
        session.commit()
    database.dispose()

    settings = Settings(
        database_url=database_url,
        checkpoint_path=tmp_path / "recovery-checkpoint.sqlite3",
        wiki_path=tmp_path / "wiki",
        demo_mode=True,
        auto_seed=False,
    )
    with TestClient(
        create_app(settings, allow_schema_bootstrap=True)
    ) as recovery_client:
        recovered = recovery_client.get("/api/runs").json()["items"][0]
        assert recovered["status"] == "failed"
        assert "persistent task payload" in recovered["error"]
        assert recovered["completed_at"].endswith("+08:00")


def test_committee_run_latest_detail_and_meeting(client: TestClient) -> None:
    run = _create_run(client)
    assert run["status"] == "completed"
    latest_response = client.get("/api/forecasts/latest")
    assert latest_response.status_code == 200
    latest = latest_response.json()
    assert latest["run_id"] == run["id"]
    assert latest["as_of"].endswith("+08:00")
    assert latest["data_cutoff"].endswith("+08:00")
    assert len(latest["forecasts"]) == 5
    assert {item["horizon"] for item in latest["forecasts"]} == {"D1"}
    assert {item["direction"] for item in latest["forecasts"]} <= {"up", "down"}
    assert all(not item["abstain"] for item in latest["forecasts"])
    assert all(abs(sum(item["probabilities"].values()) - 1) < 1e-6 for item in latest["forecasts"])
    assert all(item["citations"] for item in latest["forecasts"])

    assert client.get("/api/forecasts/latest?horizon=D2").status_code == 404
    forecast = latest["forecasts"][0]
    assert client.get(f"/api/forecasts/{forecast['id']}").json()["id"] == forecast["id"]

    meeting = client.get(f"/api/meetings/{run['id']}")
    assert meeting.status_code == 200
    body = meeting.json()
    assert len(body["opinions"]) == 30
    assert len(body["forecasts"]) == 5
    assert {step["id"] for step in body["workflow_steps"]} >= {
        "freeze_snapshot",
        "macro_policy_agent",
        "market_news_agent",
        "ai_storage_industry_agent",
        "strategy_agent",
        "risk_critic_agent",
        "evidence_validator",
        "cio_agent",
    }
    assert not [item for item in body["opinions"] if item["agent_id"] == "quant_agent"]
    assert {item["direction"] for item in body["opinions"]} <= {"up", "down"}
    strategies = [item for item in body["opinions"] if item["agent_id"] == "strategy_agent"]
    assert len(strategies) == 5
    assert all(item["weight"] == 1 for item in strategies)
    assert all(item["citations"] for item in strategies)
    for horizon in ("D1",):
        contexts = [
            item["strategy_context"] for item in strategies if item["horizon"] == horizon
        ]
        ordered = sorted(
            contexts,
            key=lambda context: context["allocation_score"],
            reverse=True,
        )
        assert ordered[0]["relative_rank"] == 1
        assert [context["relative_rank"] for context in ordered] == sorted(
            context["relative_rank"] for context in ordered
        )
    critics = [item for item in body["opinions"] if item["agent_id"] == "risk_critic_agent"]
    assert all("反证检查" in item["summary"] for item in critics)
    assert {item["direction"] for item in critics} <= {"up", "down"}


def test_latest_horizon_query_reads_the_newest_matching_historical_run(
    client: TestClient,
    tmp_path,
) -> None:
    legacy_settings = client.app.state.settings.model_copy(
        update={"checkpoint_path": tmp_path / "legacy-d2-checkpoint.sqlite3"}
    )
    legacy_workflow = CommitteeWorkflow(
        settings=legacy_settings,
        database=client.app.state.database,
        provider=client.app.state.workflow.provider,
        wiki=client.app.state.workflow.wiki,
        runtime_mode="legacy_dual_horizon",
    )
    try:
        legacy_run = legacy_workflow.run(
            as_of=datetime.fromisoformat("2026-07-09T15:00:00+08:00")
        )
    finally:
        legacy_workflow.close()

    current_run = _create_run(client)

    latest_d1 = client.get("/api/forecasts/latest?horizon=D1")
    assert latest_d1.status_code == 200
    assert latest_d1.json()["run_id"] == current_run["id"]
    assert {item["horizon"] for item in latest_d1.json()["forecasts"]} == {"D1"}

    latest_d2 = client.get("/api/forecasts/latest?horizon=D2")
    assert latest_d2.status_code == 200
    assert latest_d2.json()["run_id"] == legacy_run.id
    assert {item["horizon"] for item in latest_d2.json()["forecasts"]} == {"D2"}

    historical_meeting = client.get(f"/api/meetings/{legacy_run.id}")
    assert historical_meeting.status_code == 200
    assert {
        item["horizon"] for item in historical_meeting.json()["forecasts"]
    } == {"D1", "D2"}


def test_evaluation_and_scorecards(client: TestClient) -> None:
    _create_run(client)
    forecasts = client.get("/api/forecasts/latest").json()["forecasts"]
    forecast = next(item for item in forecasts if item["horizon"] == "D1")
    rejected = client.post(
        "/api/evaluations/run",
        json={
            "observations": [
                {
                    "forecast_id": forecast["id"],
                    "actual_return": forecast["threshold"] + 0.01,
                    "price_source": "test",
                }
            ]
        },
    )
    assert rejected.status_code == 409
    assert "unavailable in Demo mode" in rejected.text

    start_close = 100.0
    end_close = start_close * (1 + forecast["threshold"] + 0.01)
    start_hash = hashlib.sha256(b"test-start-price-payload").hexdigest()
    end_hash = hashlib.sha256(b"test-end-price-payload").hexdigest()
    demo_scorecard = client.get(
        "/api/agents/macro_policy_agent/scorecard?horizon=D1"
    )
    assert demo_scorecard.status_code == 200
    assert demo_scorecard.json()["sample_size"] == 0
    demo_evaluation = client.post(
        "/api/evaluations/run",
        json={
            "observations": [
                {
                    "forecast_id": forecast["id"],
                    "price_source": "test",
                    "observed_at": f"{forecast['target_date']}T15:10:00+08:00",
                    "start": {
                        "trade_date": forecast["base_trade_date"],
                        "close": start_close,
                        "source_url": "https://www.csindex.com.cn/start-price",
                        "source_hash": start_hash,
                    },
                    "end": {
                        "trade_date": forecast["target_date"],
                        "close": end_close,
                        "source_url": "https://www.csindex.com.cn/end-price",
                        "source_hash": end_hash,
                    },
                }
            ]
        },
    )
    assert demo_evaluation.status_code == 409
    assert "unavailable in Demo mode" in demo_evaluation.text
    with client.app.state.database.session_factory() as session:
        forecast_row = session.get(Forecast, forecast["id"])
        assert forecast_row is not None
        forecast_row.run.mode = "live"
        session.commit()
    _enable_live_http(client)

    rejected = client.post(
        "/api/evaluations/run",
        json={
            "observations": [
                {
                    "forecast_id": forecast["id"],
                    "actual_return": forecast["threshold"] + 0.01,
                    "price_source": "test",
                }
            ]
        },
        headers=OPERATOR_HEADERS,
    )
    assert rejected.status_code == 422
    assert "actual_return" in rejected.text

    placeholder_hash = client.post(
        "/api/evaluations/run",
        json={
            "observations": [
                {
                    "forecast_id": forecast["id"],
                    "price_source": "test",
                    "observed_at": f"{forecast['target_date']}T15:10:00+08:00",
                    "start": {
                        "trade_date": forecast["base_trade_date"],
                        "close": start_close,
                        "source_url": "https://www.csindex.com.cn/start-price",
                        "source_hash": "0" * 64,
                    },
                    "end": {
                        "trade_date": forecast["target_date"],
                        "close": end_close,
                        "source_url": "https://www.csindex.com.cn/end-price",
                        "source_hash": "0" * 64,
                    },
                }
            ]
        },
        headers=OPERATOR_HEADERS,
    )
    assert placeholder_hash.status_code == 422
    assert "placeholder digest" in placeholder_hash.text

    evaluation = client.post(
        "/api/evaluations/run",
        json={
            "observations": [
                {
                    "forecast_id": forecast["id"],
                    "price_source": "test",
                    "observed_at": f"{forecast['target_date']}T15:10:00+08:00",
                    "start": {
                        "trade_date": forecast["base_trade_date"],
                        "close": start_close,
                        "source_url": "https://www.csindex.com.cn/start-price",
                        "source_hash": start_hash,
                    },
                    "end": {
                        "trade_date": forecast["target_date"],
                        "close": end_close,
                        "source_url": "https://www.csindex.com.cn/end-price",
                        "source_hash": end_hash,
                    },
                }
            ]
        },
        headers=OPERATOR_HEADERS,
    )
    assert evaluation.status_code == 200, evaluation.text
    assert evaluation.json()["evaluated"] == 1
    result = evaluation.json()["results"][0]
    assert result["actual_return"] == pytest.approx(forecast["threshold"] + 0.01)
    assert result["start_source_hash"] == start_hash
    assert result["end_source_hash"] == end_hash
    assert result["observation_hash"]
    assert result["evaluated_at"].endswith("+08:00")
    detail = client.get(f"/api/forecasts/{forecast['id']}").json()
    assert detail["evaluation"]["label"] == "up"

    with client.app.state.database.session_factory() as session:
        forecast_row = session.get(Forecast, forecast["id"])
        assert forecast_row is not None
        assert forecast_row.evaluation is not None
        captured_at = datetime.fromisoformat(
            f"{forecast['target_date']}T15:10:00+08:00"
        )
        materialize_evaluation_batch(
            session,
            target_date=forecast_row.target_date,
            horizon=forecast_row.horizon,
            snapshots=[
                MarketSnapshotFact(
                    index_code=forecast_row.index_code,
                    index_name=forecast_row.index_name,
                    target_date=forecast_row.target_date,
                    base_trade_date=forecast_row.base_trade_date,
                    base_close=forecast_row.evaluation.start_close,
                    target_close=forecast_row.evaluation.end_close,
                    actual_return=forecast_row.evaluation.actual_return,
                    source_url="https://www.csindex.com.cn/close",
                    source_hash=hashlib.sha256(
                        b"api-market-snapshot"
                    ).hexdigest(),
                    captured_at=captured_at,
                    advancers=3_100,
                    decliners=2_200,
                    unchanged=100,
                    limit_down_count=2,
                    breadth_down_ratio=2_200 / 5_400,
                    historical_abs_return_percentile=0.8,
                    history_sample_size=300,
                )
            ],
            source_hash=hashlib.sha256(b"api-evaluation-batch").hexdigest(),
            now=captured_at,
        )
        session.commit()

    client.app.state.settings.execution_provider = "demo"
    client.app.state.settings.demo_mode = True
    client.app.state.settings.operator_token = None
    macro = client.get("/api/agents/macro_policy_agent/scorecard?horizon=D1")
    assert macro.status_code == 200
    assert macro.json()["sample_size"] == 1
    assert macro.json()["sign_sample_size"] == 1
    assert macro.json()["material_sample_size"] == 1
    assert not macro.json()["sample_sufficient"]
    assert macro.json()["calibration"][0]["count"] == 1
    assert macro.json()["agent_version"] == "0.2.0"
    with client.app.state.database.session_factory() as session:
        scored_forecast = session.get(Forecast, forecast["id"])
        assert scored_forecast is not None
        scored_forecast.run.market_universe_hash = "f" * 64
        session.commit()
    foreign_universe_macro = client.get(
        "/api/agents/macro_policy_agent/scorecard?horizon=D1"
    )
    assert foreign_universe_macro.status_code == 200
    assert foreign_universe_macro.json()["sample_size"] == 0
    with client.app.state.database.session_factory() as session:
        scored_forecast = session.get(Forecast, forecast["id"])
        assert scored_forecast is not None
        scored_forecast.run.market_universe_hash = (
            DEFAULT_MARKET_UNIVERSE.content_hash
        )
        session.commit()
    strategy = client.get("/api/agents/strategy_agent/scorecard?horizon=D1")
    assert strategy.status_code == 200
    assert strategy.json()["sample_size"] == 1
    assert strategy.json()["agent_version"] == "0.2.0"
    assert strategy.json()["model_name"] == "deterministic-binary-demo-v3"
    assert strategy.json()["accuracy"] is not None
    assert strategy.json()["average_brier"] is not None
    assert "1 个预测截面" in strategy.json()["note"]
    quant = client.get("/api/agents/quant_agent/scorecard").json()
    assert quant["sample_size"] == 0
    assert "尚无已到期样本" in quant["note"]


def test_evaluation_batch_rejects_duplicate_forecast_ids(client: TestClient) -> None:
    _create_run(client)
    forecast = client.get("/api/forecasts/latest?horizon=D1").json()["forecasts"][0]
    _enable_live_http(client)
    observation = {
        "forecast_id": forecast["id"],
        "price_source": "test",
        "observed_at": f"{forecast['target_date']}T15:10:00+08:00",
        "start": {
            "trade_date": forecast["base_trade_date"],
            "close": 100,
            "source_url": "https://example.com/start",
            "source_hash": hashlib.sha256(b"duplicate-start").hexdigest(),
        },
        "end": {
            "trade_date": forecast["target_date"],
            "close": 101,
            "source_url": "https://example.com/end",
            "source_hash": hashlib.sha256(b"duplicate-end").hexdigest(),
        },
    }
    response = client.post(
        "/api/evaluations/run",
        json={"observations": [observation, observation]},
        headers=OPERATOR_HEADERS,
    )
    assert response.status_code == 422
    assert "forecast_id must be unique" in response.text


def test_wiki_and_run_listing(client: TestClient) -> None:
    run = _create_run(client)
    runs = client.get("/api/runs").json()["items"]
    assert runs[0]["id"] == run["id"]
    assert runs[0]["forecasts_count"] == 5
    wiki = client.get("/api/wiki").json()["items"]
    assert wiki
    entry = wiki[0]
    assert entry["referenced_by_count"] == 5
    detail = client.get(f"/api/wiki/{entry['id']}")
    assert detail.status_code == 200
    assert detail.json()["body"]


def test_prepared_run_is_persisted_before_execution(client: TestClient) -> None:
    workflow = client.app.state.workflow
    prepared = workflow.prepare_run(as_of=datetime.fromisoformat("2026-07-09T15:00:00+08:00"))
    queued = client.get("/api/runs").json()["items"][0]
    assert queued["id"] == prepared.row.id
    assert queued["status"] == "queued"

    completed = workflow.execute_prepared(prepared)
    assert completed.status == "completed"


def test_live_api_only_enqueues_and_replays_an_idempotent_request(
    tmp_path,
    monkeypatch,
) -> None:
    as_of = datetime.fromisoformat("2026-07-27T15:00:00+08:00")
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'live-queue.sqlite3'}",
        checkpoint_path=tmp_path / "live-queue-checkpoint.sqlite3",
        wiki_path=tmp_path / "wiki",
        demo_mode=False,
        llm_api_key="test-key",
        evidence_snapshot_path=tmp_path / "unused-snapshot.json",
        operator_token=TEST_OPERATOR_TOKEN,
        auto_seed=False,
    )
    monkeypatch.setattr(
        api_module,
        "_validate_live_run_request",
        lambda _payload, _request: as_of,
    )

    with TestClient(
        create_app(settings, allow_schema_bootstrap=True)
    ) as queued_client:
        database = queued_client.app.state.database

        class StubWorkflow:
            def prepare_run(self, *, as_of, persist=True):
                run = WorkflowRun(
                    id="queued-live-run",
                    as_of=as_of,
                    data_cutoff=as_of,
                    status="queued",
                    mode="live",
                    started_at=as_of,
                    completed_at=None,
                    duration_seconds=None,
                    error=None,
                    data_quality={},
                    workflow_steps=[],
                    input_hash="f" * 64,
                )
                if persist:
                    with database.session_factory() as session:
                        session.add(run)
                        session.commit()
                return PreparedRun(
                    row=run,
                    initial={
                        "run_id": run.id,
                        "input_hash": run.input_hash,
                        "as_of": as_of.isoformat(),
                        "data_cutoff": as_of.isoformat(),
                    },
                    execution_manifest={
                        "schema": EXECUTION_MANIFEST_SCHEMA,
                        "test_worker": "stable",
                    },
                )

        queued_client.app.state.workflow = StubWorkflow()
        unauthorized = queued_client.post(
            "/api/runs",
            json={},
            headers={"Idempotency-Key": "daily:2026-07-27"},
        )
        assert unauthorized.status_code == 401
        with database.session_factory() as session:
            assert session.get(WorkflowRun, "queued-live-run") is None
            assert session.scalars(select(WorkflowTask)).all() == []

        first = queued_client.post(
            "/api/runs",
            json={},
            headers={
                **OPERATOR_HEADERS,
                "Idempotency-Key": "daily:2026-07-27",
            },
        )
        replay = queued_client.post(
            "/api/runs",
            json={},
            headers={
                **OPERATOR_HEADERS,
                "Idempotency-Key": "daily:2026-07-27",
            },
        )

        assert first.status_code == 202
        assert first.json()["status"] == "queued"
        assert first.json()["task"]["status"] == "queued"
        assert first.json()["task"]["attempt_count"] == 0
        assert replay.status_code == 202
        assert replay.json()["id"] == first.json()["id"]
        with database.session_factory() as session:
            run = session.get(WorkflowRun, "queued-live-run")
            assert run is not None
            assert run.status == "queued"
            assert len(run.forecasts) == 0


def test_live_api_compensates_when_enqueue_fails(
    tmp_path,
    monkeypatch,
) -> None:
    as_of = datetime.fromisoformat("2026-07-27T15:00:00+08:00")
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'enqueue-failure.sqlite3'}",
        checkpoint_path=tmp_path / "enqueue-failure-checkpoint.sqlite3",
        wiki_path=tmp_path / "wiki",
        demo_mode=False,
        llm_api_key="test-key",
        evidence_snapshot_path=tmp_path / "unused-snapshot.json",
        operator_token=TEST_OPERATOR_TOKEN,
        auto_seed=False,
    )
    monkeypatch.setattr(
        api_module,
        "_validate_live_run_request",
        lambda _payload, _request: as_of,
    )

    with TestClient(
        create_app(settings, allow_schema_bootstrap=True)
    ) as queued_client:
        database = queued_client.app.state.database

        class StubWorkflow:
            def prepare_run(self, *, as_of, persist=True):
                run = WorkflowRun(
                    id="enqueue-failure-run",
                    as_of=as_of,
                    data_cutoff=as_of,
                    status="queued",
                    mode="live",
                    started_at=as_of,
                    completed_at=None,
                    duration_seconds=None,
                    error=None,
                    data_quality={},
                    workflow_steps=[],
                    input_hash="e" * 64,
                )
                return PreparedRun(
                    row=run,
                    initial={
                        "run_id": run.id,
                        "input_hash": run.input_hash,
                        "as_of": as_of.isoformat(),
                        "data_cutoff": as_of.isoformat(),
                    },
                    execution_manifest={
                        "schema": EXECUTION_MANIFEST_SCHEMA,
                        "test_worker": "stable",
                    },
                )

        def persist_then_fail(prepared, *, idempotency_key):
            del idempotency_key
            with database.session_factory() as session:
                session.add(prepared.row)
                session.commit()
            raise RuntimeError("simulated queue storage failure")

        queued_client.app.state.workflow = StubWorkflow()
        monkeypatch.setattr(
            queued_client.app.state.task_queue,
            "enqueue",
            persist_then_fail,
        )

        response = queued_client.post(
            "/api/runs",
            json={},
            headers={
                **OPERATOR_HEADERS,
                "Idempotency-Key": "enqueue-failure",
            },
        )

        assert response.status_code == 500
        with database.session_factory() as session:
            run = session.get(WorkflowRun, "enqueue-failure-run")
            assert run is not None
            assert run.status == "failed"
            assert run.completed_at is not None
            assert "enqueue failed" in (run.error or "").lower()
            assert session.scalars(select(WorkflowTask)).all() == []


def test_live_api_rechecks_idempotency_after_observing_the_winner_run(
    tmp_path,
    monkeypatch,
) -> None:
    as_of = datetime.fromisoformat("2026-07-27T15:00:00+08:00")
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'late-replay.sqlite3'}",
        checkpoint_path=tmp_path / "late-replay-checkpoint.sqlite3",
        wiki_path=tmp_path / "wiki",
        demo_mode=False,
        llm_api_key="test-key",
        evidence_snapshot_path=tmp_path / "unused-snapshot.json",
        operator_token=TEST_OPERATOR_TOKEN,
        auto_seed=False,
    )
    monkeypatch.setattr(
        api_module,
        "_validate_live_run_request",
        lambda _payload, _request: as_of,
    )

    with TestClient(
        create_app(settings, allow_schema_bootstrap=True)
    ) as queued_client:
        run = WorkflowRun(
            id="late-winner-run",
            as_of=as_of,
            data_cutoff=as_of,
            status="queued",
            mode="live",
            started_at=as_of,
            completed_at=None,
            duration_seconds=None,
            error=None,
            data_quality={},
            workflow_steps=[],
            input_hash="d" * 64,
        )
        prepared = PreparedRun(
            row=run,
            initial={
                "run_id": run.id,
                "input_hash": run.input_hash,
                "as_of": as_of.isoformat(),
                "data_cutoff": as_of.isoformat(),
            },
            execution_manifest={
                "schema": EXECUTION_MANIFEST_SCHEMA,
                "test_worker": "stable",
            },
        )
        queue = queued_client.app.state.task_queue
        queue.enqueue(prepared, idempotency_key="late-visible-winner")
        original_find = queue.find_by_idempotency_key
        calls = 0

        def stale_once(key):
            nonlocal calls
            calls += 1
            if calls == 1:
                return None
            return original_find(key)

        monkeypatch.setattr(queue, "find_by_idempotency_key", stale_once)

        response = queued_client.post(
            "/api/runs",
            json={},
            headers={
                **OPERATOR_HEADERS,
                "Idempotency-Key": "late-visible-winner",
            },
        )

        assert response.status_code == 202
        assert response.json()["id"] == "late-winner-run"
        assert calls >= 2


def test_concurrent_identical_live_requests_replay_the_winner(
    tmp_path,
    monkeypatch,
) -> None:
    as_of = datetime.fromisoformat("2026-07-27T15:00:00+08:00")
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'concurrent-live.sqlite3'}",
        checkpoint_path=tmp_path / "concurrent-live-checkpoint.sqlite3",
        wiki_path=tmp_path / "wiki",
        demo_mode=False,
        llm_api_key="test-key",
        evidence_snapshot_path=tmp_path / "unused-snapshot.json",
        operator_token=TEST_OPERATOR_TOKEN,
        auto_seed=False,
    )
    monkeypatch.setattr(
        api_module,
        "_validate_live_run_request",
        lambda _payload, _request: as_of,
    )
    barrier = Barrier(2)

    with TestClient(
        create_app(settings, allow_schema_bootstrap=True)
    ) as queued_client:
        database = queued_client.app.state.database

        class StubWorkflow:
            def prepare_run(self, *, as_of, persist=True):
                assert persist is False
                run_id = str(uuid4())
                run = WorkflowRun(
                    id=run_id,
                    as_of=as_of,
                    data_cutoff=as_of,
                    status="queued",
                    mode="live",
                    started_at=as_of,
                    completed_at=None,
                    duration_seconds=None,
                    error=None,
                    data_quality={},
                    workflow_steps=[],
                    input_hash=hashlib.sha256(run_id.encode()).hexdigest(),
                )
                prepared = PreparedRun(
                    row=run,
                    initial={
                        "run_id": run.id,
                        "input_hash": run.input_hash,
                        "as_of": as_of.isoformat(),
                        "data_cutoff": as_of.isoformat(),
                    },
                    execution_manifest={
                        "schema": EXECUTION_MANIFEST_SCHEMA,
                        "test_worker": "stable",
                    },
                )
                barrier.wait()
                return prepared

        queued_client.app.state.workflow = StubWorkflow()

        def issue_request(_index):
            return queued_client.post(
                "/api/runs",
                json={},
                headers={
                    **OPERATOR_HEADERS,
                    "Idempotency-Key": "concurrent-same-run",
                },
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(issue_request, range(2)))

        assert [item.status_code for item in responses] == [202, 202]
        assert len({item.json()["id"] for item in responses}) == 1
        with database.session_factory() as session:
            assert len(session.scalars(select(WorkflowTask)).all()) == 1
            active_runs = session.scalars(
                select(WorkflowRun).where(
                    WorkflowRun.status.in_(["queued", "running"])
                )
            ).all()
            assert len(active_runs) == 1


def test_not_found_responses(client: TestClient) -> None:
    assert client.get("/api/forecasts/missing").status_code == 404
    assert client.get("/api/meetings/missing").status_code == 404
    assert client.get("/api/agents/missing/scorecard").status_code == 404
    assert client.get("/api/wiki/missing").status_code == 404
