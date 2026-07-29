from __future__ import annotations

import hashlib
import json
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from app import api as api_module
from app.config import Settings
from app.main import create_app
from app.market_universe import (
    DEFAULT_MARKET_UNIVERSE,
    MARKET_UNIVERSE_SCHEMA,
    MarketUniverseBody,
    MarketUniverseError,
    MarketUniverseSpec,
    load_market_universe,
    seal_market_universe,
)
from app.models import Forecast, WorkflowRun
from app.serializers import run_read
from app.services.evaluation import ForecastNotMatureError, evaluate_forecast
from app.services.judgment_bundle import (
    FORECAST_NAME,
    JUDGMENT_NAME,
    export_judgment_bundle,
    verify_judgment_bundle,
)
from app.services.task_queue import (
    EXECUTION_MANIFEST_SCHEMA,
    default_idempotency_key,
)
from app.services.user_judgment import user_judgment_submission_deadline
from app.workflow import PreparedRun
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import selectinload


def _universe_payload(
    *,
    market: str = "US",
    timezone: str = "America/New_York",
    instruments: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": MARKET_UNIVERSE_SCHEMA,
        "universe_id": f"{market.lower()}-research",
        "version": "1.0.0",
        "market": market,
        "timezone": timezone,
        "calendar_id": "XNYS" if market == "US" else "XHKG",
        "session_close": "16:00",
        "horizons": ["D1", "D2"],
        "instruments": instruments
        or [
            {
                "code": "SPX.US",
                "name": "S&P 500",
                "asset_type": "index",
                "exchange": "XNYS",
                "currency": "USD",
                "strategy_bucket": "large_cap",
                "tags": ["broad-market"],
            },
            {
                "code": "AAPL.US",
                "name": "Apple",
                "asset_type": "equity",
                "exchange": "XNAS",
                "currency": "USD",
                "sector": "Information Technology",
                "strategy_bucket": "growth",
                "tags": ["single-stock", "technology"],
                "agent_briefs": {
                    "ai_storage_industry_agent": (
                        "分析公司基本面、行业竞争、估值与个股特有催化，不把 A 股或"
                        "AI 存储框架机械套用到该标的。"
                    )
                },
            },
        ],
    }


def _write_universe(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def test_market_universe_seal_is_stable_and_tamper_evident() -> None:
    body = MarketUniverseBody.model_validate(_universe_payload())
    sealed = seal_market_universe(body)

    assert sealed.codes == ("SPX.US", "AAPL.US")
    assert sealed.definitions()[1].asset_type == "equity"
    assert sealed.definitions()[1].sector == "Information Technology"
    assert seal_market_universe(body).content_hash == sealed.content_hash

    tampered = sealed.model_dump(mode="json")
    tampered["market"] = "HK"
    with pytest.raises(ValidationError, match="content_hash"):
        MarketUniverseSpec.model_validate(tampered)


def test_market_universe_rejects_duplicate_codes_and_mixed_currency() -> None:
    duplicate = _universe_payload()
    duplicate["instruments"] = [
        duplicate["instruments"][0],
        duplicate["instruments"][0],
    ]
    with pytest.raises(ValidationError, match="must be unique"):
        MarketUniverseBody.model_validate(duplicate)

    mixed = _universe_payload()
    mixed["instruments"][1]["currency"] = "HKD"
    with pytest.raises(ValidationError, match="one settlement currency"):
        MarketUniverseBody.model_validate(mixed)


def test_market_universe_loader_seals_body_and_rejects_duplicate_json_keys(
    tmp_path: Path,
) -> None:
    path = _write_universe(tmp_path / "universe.json", _universe_payload())
    loaded = load_market_universe(path)

    assert loaded.universe_id == "us-research"
    assert len(loaded.content_hash) == 64

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema_version":"forecast-loop.market-universe/v1",'
        '"schema_version":"forecast-loop.market-universe/v1"}',
        encoding="utf-8",
    )
    with pytest.raises(MarketUniverseError, match="valid canonical JSON"):
        load_market_universe(duplicate)


def test_custom_us_index_and_equity_run_end_to_end(tmp_path: Path) -> None:
    universe_path = _write_universe(
        tmp_path / "us-universe.json",
        _universe_payload(),
    )
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'custom.sqlite3'}",
        checkpoint_path=tmp_path / "custom-checkpoint.sqlite3",
        wiki_path=tmp_path / "wiki",
        prediction_status_root=tmp_path / "prediction-status",
        user_judgment_wiki_root=tmp_path / "user-wiki",
        market_universe_path=universe_path,
        timezone="America/New_York",
        demo_mode=True,
        auto_seed=False,
    )

    with TestClient(create_app(settings, allow_schema_bootstrap=True)) as client:
        universe = client.get("/api/market-universe")
        assert universe.status_code == 200
        assert universe.json()["market"] == "US"
        assert [item["code"] for item in universe.json()["instruments"]] == [
            "SPX.US",
            "AAPL.US",
        ]
        default_as_of = client.app.state.workflow._normalize_as_of(None)
        assert default_as_of.tzinfo == ZoneInfo("America/New_York")
        assert default_as_of.timetz().replace(tzinfo=None) == time(16, 0)

        created = client.post(
            "/api/runs",
            json={"as_of": datetime.fromisoformat("2026-07-10T16:00:00-04:00").isoformat()},
        )
        assert created.status_code == 201, created.text
        assert created.json()["status"] == "completed"
        assert created.json()["as_of"].endswith("-04:00")
        assert created.json()["data_cutoff"].endswith("-04:00")
        assert created.json()["started_at"].endswith("-04:00")
        assert created.json()["completed_at"].endswith("-04:00")

        latest = client.get("/api/forecasts/latest")
        assert latest.status_code == 200
        forecasts = latest.json()["forecasts"]
        assert len(forecasts) == 4
        assert {(item["index_code"], item["horizon"]) for item in forecasts} == {
            ("SPX.US", "D1"),
            ("SPX.US", "D2"),
            ("AAPL.US", "D1"),
            ("AAPL.US", "D2"),
        }
        assert {item["index_name"] for item in forecasts} == {"S&P 500", "Apple"}
        judgment = client.post(
            "/api/user-judgments",
            json={
                "forecast_id": forecasts[0]["id"],
                "direction": "up",
                "confidence": 0.67,
                "rationale": (
                    "美国市场流动性和风险偏好改善，可能推动目标在预测窗口内走强。"
                ),
                "counter_evidence": (
                    "若长端利率快速上行，成长资产估值可能承压并抵消流动性改善。"
                ),
                "invalidation_condition": (
                    "若目标日前成交显著萎缩且价格跌破基准日低点，则本判断失效。"
                ),
                "blind_attestation": False,
            },
        )
        assert judgment.status_code == 201, judgment.text
        judgment_id = judgment.json()["id"]
        assert judgment.json()["submitted_at"].endswith("-04:00")

        # Historical reads must use the run seal, not the currently configured
        # process timezone. This also protects canonical judgment verification
        # after an operator switches universes.
        client.app.state.settings.timezone = "Asia/Shanghai"
        latest = client.get("/api/forecasts/latest")
        assert latest.status_code == 200
        assert latest.json()["as_of"].endswith("-04:00")
        assert latest.json()["data_cutoff"].endswith("-04:00")
        assert all(
            item["as_of"].endswith("-04:00")
            and item["data_cutoff"].endswith("-04:00")
            for item in latest.json()["forecasts"]
        )
        history = client.get("/api/user-judgments")
        detail = client.get(f"/api/user-judgments/{judgment_id}")
        wiki = client.get(f"/api/user-judgments/{judgment_id}/wiki")
        assert history.status_code == 200
        assert history.json()["items"][0]["submitted_at"].endswith("-04:00")
        assert detail.status_code == 200
        assert detail.json()["submitted_at"].endswith("-04:00")
        assert wiki.status_code == 200
        bundle = export_judgment_bundle(
            client.app.state.database,
            judgment_id=judgment_id,
            output_root=tmp_path / "judgment-bundles",
            wiki_root=Path(client.app.state.settings.user_judgment_wiki_root),
            timezone="Asia/Shanghai",
        )
        verify_judgment_bundle(bundle)
        bundled_forecast = json.loads((bundle / FORECAST_NAME).read_bytes())
        bundled_judgment = json.loads((bundle / JUDGMENT_NAME).read_bytes())
        assert bundled_forecast["as_of"].endswith("-04:00")
        assert bundled_forecast["data_cutoff"].endswith("-04:00")
        assert bundled_judgment["submitted_at"].endswith("-04:00")

        meeting = client.get(f"/api/meetings/{created.json()['id']}")
        assert meeting.status_code == 200
        assert meeting.json()["run"]["as_of"].endswith("-04:00")
        assert meeting.json()["run"]["data_cutoff"].endswith("-04:00")
        assert all(
            item["as_of"].endswith("-04:00")
            and item["data_cutoff"].endswith("-04:00")
            for item in meeting.json()["forecasts"]
        )
        assert {item["index_code"] for item in meeting.json()["opinions"]} == {
            "SPX.US",
            "AAPL.US",
        }
        equity_industry_opinion = next(
            item
            for item in meeting.json()["opinions"]
            if item["index_code"] == "AAPL.US"
            and item["agent_id"] == "ai_storage_industry_agent"
        )
        assert equity_industry_opinion["role"].startswith("分析公司基本面")

        with client.app.state.database.session_factory() as session:
            forecast = session.scalar(
                select(Forecast)
                .options(selectinload(Forecast.run), selectinload(Forecast.evaluation))
                .where(
                    Forecast.index_code == "AAPL.US",
                    Forecast.horizon == "D1",
                )
            )
            assert forecast is not None
            forecast.run.mode = "live"
            session.commit()

            zone = ZoneInfo("America/New_York")
            before_maturity = datetime.combine(
                forecast.target_date,
                time(16, 4),
                tzinfo=zone,
            )
            digest = hashlib.sha256(b"custom-market-close").hexdigest()
            evaluation_kwargs = {
                "session": session,
                "forecast": forecast,
                "price_source": "test-readonly",
                "observed_at": before_maturity,
                "start_trade_date": forecast.base_trade_date,
                "start_close": 200.0,
                "start_source_url": "https://example.com/aapl-start",
                "start_source_hash": digest,
                "end_trade_date": forecast.target_date,
                "end_close": 201.0,
                "end_source_url": "https://example.com/aapl-end",
                "end_source_hash": digest,
                "timezone": "Asia/Shanghai",
                "now": before_maturity,
            }
            with pytest.raises(ForecastNotMatureError, match="16:05:00-04:00"):
                evaluate_forecast(**evaluation_kwargs)
            session.rollback()

            after_maturity = datetime.combine(
                forecast.target_date,
                time(16, 6),
                tzinfo=zone,
            )
            evaluate_forecast(
                **{
                    **evaluation_kwargs,
                    "observed_at": after_maturity,
                    "now": after_maturity,
                }
            )
            session.commit()
            forecast_id = forecast.id

            deadline = user_judgment_submission_deadline(
                forecast,
                timezone="Asia/Shanghai",
                window_minutes=120,
            )
            assert deadline is not None
            assert deadline.tzinfo == zone
            assert deadline.timetz().replace(tzinfo=None) == time(16, 0)

        forecast_detail = client.get(f"/api/forecasts/{forecast_id}")
        assert forecast_detail.status_code == 200
        assert forecast_detail.json()["as_of"].endswith("-04:00")
        assert forecast_detail.json()["data_cutoff"].endswith("-04:00")
        assert forecast_detail.json()["evaluation"]["evaluated_at"].endswith(
            "-04:00"
        )
        assert forecast_detail.json()["evaluation"]["observed_at"].endswith(
            "-04:00"
        )


def test_legacy_default_run_clock_does_not_follow_current_custom_timezone(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/runs",
        json={"as_of": "2026-07-10T15:00:00+08:00"},
    )
    assert created.status_code == 201, created.text

    with client.app.state.database.session_factory() as session:
        forecast = session.scalar(
            select(Forecast)
            .options(selectinload(Forecast.run), selectinload(Forecast.evaluation))
            .where(
                Forecast.run_id == created.json()["id"],
                Forecast.horizon == "D1",
            )
        )
        assert forecast is not None
        forecast.run.mode = "live"
        forecast.run.data_quality = {}
        session.commit()

        zone = ZoneInfo("Asia/Shanghai")
        before_maturity = datetime.combine(
            forecast.target_date,
            time(15, 4),
            tzinfo=zone,
        )
        digest = hashlib.sha256(b"legacy-default-market-close").hexdigest()
        evaluation_kwargs = {
            "session": session,
            "forecast": forecast,
            "price_source": "test-readonly",
            "observed_at": before_maturity,
            "start_trade_date": forecast.base_trade_date,
            "start_close": 100.0,
            "start_source_url": "https://example.com/legacy-start",
            "start_source_hash": digest,
            "end_trade_date": forecast.target_date,
            "end_close": 101.0,
            "end_source_url": "https://example.com/legacy-end",
            "end_source_hash": digest,
            "timezone": "America/New_York",
            "now": before_maturity,
        }
        with pytest.raises(ForecastNotMatureError, match="15:05:00\\+08:00"):
            evaluate_forecast(**evaluation_kwargs)

        deadline = user_judgment_submission_deadline(
            forecast,
            timezone="America/New_York",
            window_minutes=120,
        )
        assert deadline is not None
        assert deadline.tzinfo == zone
        assert deadline.timetz().replace(tzinfo=None) == time(15, 0)

        forecast.run.data_quality = {
            "market_universe": {
                "content_hash": DEFAULT_MARKET_UNIVERSE.content_hash,
                "timezone": 42,
                "session_close": "15:00",
            }
        }
        session.commit()
        with pytest.raises(ValueError, match="clock metadata is invalid"):
            evaluate_forecast(**evaluation_kwargs)
        with pytest.raises(ValueError, match="timezone is invalid"):
            run_read(forecast.run, forecasts_count=len(forecast.run.forecasts))


def test_workflow_rejects_universe_timezone_mismatch(tmp_path: Path) -> None:
    universe_path = _write_universe(
        tmp_path / "us-universe.json",
        _universe_payload(),
    )
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'mismatch.sqlite3'}",
        checkpoint_path=tmp_path / "mismatch-checkpoint.sqlite3",
        wiki_path=tmp_path / "wiki",
        market_universe_path=universe_path,
        timezone="Asia/Shanghai",
        demo_mode=True,
        auto_seed=False,
    )

    with pytest.raises(ValueError, match="timezone"):
        with TestClient(create_app(settings, allow_schema_bootstrap=True)):
            pass


def test_restarted_custom_app_does_not_display_newer_default_run(
    tmp_path: Path,
) -> None:
    universe_path = _write_universe(
        tmp_path / "us-universe.json",
        _universe_payload(),
    )
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'shared.sqlite3'}",
        checkpoint_path=tmp_path / "shared-checkpoint.sqlite3",
        wiki_path=tmp_path / "wiki",
        prediction_status_root=tmp_path / "prediction-status",
        user_judgment_wiki_root=tmp_path / "user-wiki",
        market_universe_path=universe_path,
        timezone="America/New_York",
        demo_mode=True,
        operator_token="custom-universe-operator-token-0123456789",
        auto_seed=False,
    )
    custom_as_of = datetime.fromisoformat("2026-07-10T16:00:00-04:00")

    with TestClient(create_app(settings, allow_schema_bootstrap=True)) as first:
        created = first.post(
            "/api/runs",
            json={"as_of": custom_as_of.isoformat()},
        )
        assert created.status_code == 201, created.text
        custom_run_id = created.json()["id"]
        with first.app.state.database.session_factory() as session:
            session.add(
                WorkflowRun(
                    id="newer-default-demo-run",
                    as_of=datetime.fromisoformat("2026-07-11T16:00:00-04:00"),
                    data_cutoff=datetime.fromisoformat(
                        "2026-07-11T15:55:00-04:00"
                    ),
                    status="completed",
                    mode="demo",
                    started_at=datetime.fromisoformat(
                        "2026-07-11T16:00:00-04:00"
                    ),
                    completed_at=datetime.fromisoformat(
                        "2026-07-11T16:01:00-04:00"
                    ),
                    duration_seconds=60,
                    error=None,
                    data_quality={},
                    workflow_steps=[],
                    input_hash="d" * 64,
                    market_universe_hash=DEFAULT_MARKET_UNIVERSE.content_hash,
                )
            )
            session.commit()

    with TestClient(create_app(settings, allow_schema_bootstrap=True)) as restarted:
        latest = restarted.get("/api/forecasts/latest")
        targets = restarted.get(
            "/api/user-judgments/targets",
            headers={
                "Authorization": (
                    "Bearer custom-universe-operator-token-0123456789"
                )
            },
        )
        status = restarted.get("/api/prediction-status")

    assert latest.status_code == 200
    assert latest.json()["run_id"] == custom_run_id
    assert {item["index_code"] for item in latest.json()["forecasts"]} == {
        "SPX.US",
        "AAPL.US",
    }
    assert targets.status_code == 200
    assert {item["run_id"] for item in targets.json()["items"]} == {custom_run_id}
    assert status.status_code == 200
    assert status.json()["today"]["state"] == "blocked"
    assert "自定义 Universe" in status.json()["today"]["message"]


def test_live_post_default_idempotency_and_active_lookup_are_universe_scoped(
    tmp_path: Path,
    monkeypatch,
) -> None:
    universe_path = _write_universe(
        tmp_path / "us-universe.json",
        _universe_payload(),
    )
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'live-shared.sqlite3'}",
        checkpoint_path=tmp_path / "live-shared-checkpoint.sqlite3",
        wiki_path=tmp_path / "wiki",
        market_universe_path=universe_path,
        timezone="America/New_York",
        demo_mode=False,
        llm_api_key="test-key",
        evidence_snapshot_path=tmp_path / "unused-snapshot.json",
        operator_token="custom-universe-operator-token-0123456789",
        auto_seed=False,
    )
    as_of = datetime.fromisoformat("2026-07-27T16:00:00-04:00")
    monkeypatch.setattr(
        api_module,
        "_validate_live_run_request",
        lambda _payload, _request: as_of,
    )

    with TestClient(create_app(settings, allow_schema_bootstrap=True)) as client:
        database = client.app.state.database
        custom_universe = client.app.state.workflow.universe
        assert default_idempotency_key(
            as_of,
            market_universe_hash=DEFAULT_MARKET_UNIVERSE.content_hash,
        ) == f"live:{as_of.isoformat()}"
        assert default_idempotency_key(
            as_of,
            market_universe_hash=custom_universe.content_hash,
        ) != f"live:{as_of.isoformat()}"
        default_key = default_idempotency_key(
            as_of,
            market_universe_hash=DEFAULT_MARKET_UNIVERSE.content_hash,
        )
        default_run = WorkflowRun(
            id="default-live-run",
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
            market_universe_hash=DEFAULT_MARKET_UNIVERSE.content_hash,
        )
        client.app.state.task_queue.enqueue(
            PreparedRun(
                row=default_run,
                initial={
                    "run_id": default_run.id,
                    "input_hash": default_run.input_hash,
                    "as_of": as_of.isoformat(),
                    "data_cutoff": as_of.isoformat(),
                },
                execution_manifest={
                    "schema": EXECUTION_MANIFEST_SCHEMA,
                    "test_worker": "default-universe",
                },
            ),
            idempotency_key=default_key,
        )

        class StubWorkflow:
            universe = custom_universe

            def prepare_run(self, *, as_of, persist=True):
                assert persist is False
                row = WorkflowRun(
                    id="custom-live-run",
                    as_of=as_of,
                    data_cutoff=as_of,
                    status="queued",
                    mode="live",
                    started_at=as_of,
                    completed_at=None,
                    duration_seconds=None,
                    error=None,
                    data_quality={
                        "market_universe": {
                            "content_hash": custom_universe.content_hash,
                            "timezone": custom_universe.timezone,
                            "session_close": custom_universe.session_close,
                        }
                    },
                    workflow_steps=[],
                    input_hash="c" * 64,
                    market_universe_hash=custom_universe.content_hash,
                )
                return PreparedRun(
                    row=row,
                    initial={
                        "run_id": row.id,
                        "input_hash": row.input_hash,
                        "as_of": as_of.isoformat(),
                        "data_cutoff": as_of.isoformat(),
                    },
                    execution_manifest={
                        "schema": EXECUTION_MANIFEST_SCHEMA,
                        "test_worker": "custom-universe",
                    },
                )

        client.app.state.workflow = StubWorkflow()
        headers = {
            "Authorization": (
                "Bearer custom-universe-operator-token-0123456789"
            )
        }
        created = client.post("/api/runs", json={}, headers=headers)
        foreign_replay = client.post(
            "/api/runs",
            json={},
            headers={**headers, "Idempotency-Key": default_key},
        )

        with database.session_factory() as session:
            identities = {
                row.market_universe_hash
                for row in session.scalars(
                    select(WorkflowRun).where(WorkflowRun.as_of == as_of)
                )
            }

    assert created.status_code == 202, created.text
    assert created.json()["id"] == "custom-live-run"
    assert identities == {
        DEFAULT_MARKET_UNIVERSE.content_hash,
        custom_universe.content_hash,
    }
    assert foreign_replay.status_code == 409
    assert "another Market Universe" in foreign_replay.text


def test_legacy_user_judgment_clock_is_default_and_malformed_custom_fails(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/runs",
        json={"as_of": "2026-07-10T15:00:00+08:00"},
    )
    assert created.status_code == 201, created.text
    with client.app.state.database.session_factory() as session:
        forecast = session.scalar(
            select(Forecast)
            .options(selectinload(Forecast.run))
            .where(
                Forecast.run_id == created.json()["id"],
                Forecast.horizon == "D1",
            )
        )
        assert forecast is not None
        forecast.run.mode = "live"
        forecast.run.completed_at = datetime.combine(
            forecast.target_date,
            time(14, 0),
            tzinfo=ZoneInfo("Asia/Shanghai"),
        )
        quality = dict(forecast.run.data_quality or {})
        quality.pop("market_universe", None)
        forecast.run.data_quality = quality
        forecast.run.market_universe_hash = (
            DEFAULT_MARKET_UNIVERSE.content_hash
        )
        session.commit()

        deadline = user_judgment_submission_deadline(
            forecast,
            timezone="America/New_York",
            window_minutes=120,
        )
        assert deadline is not None
        assert deadline.tzinfo == ZoneInfo("Asia/Shanghai")
        assert deadline.timetz().replace(tzinfo=None) == time(15, 0)

        forecast.run.market_universe_hash = "f" * 64
        session.flush()
        with pytest.raises(ValueError, match="metadata is missing"):
            user_judgment_submission_deadline(
                forecast,
                timezone="America/New_York",
                window_minutes=120,
            )
