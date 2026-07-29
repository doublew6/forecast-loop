from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from app.domain import INDEXES
from app.market_universe import DEFAULT_MARKET_UNIVERSE
from app.models import EvaluationBatch, EvaluationResult, Forecast, WorkflowRun
from app.services.market_outcome import (
    _canonical_hash as canonical_hash,
)
from app.services.market_outcome import (
    import_market_snapshot,
    load_market_snapshot,
    pending_live_forecasts,
    record_blocked_upstream,
)
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

ZONE = ZoneInfo("Asia/Shanghai")
QUALITY_CHECKS = {
    "quality_policy_passed": True,
    "target_session_published": True,
    "target_calendar_open": True,
    "required_instruments_complete": True,
    "outcome_metrics_complete": True,
    "publication_freshness_passed": True,
}


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _create_live_run(client: TestClient) -> tuple[str, list[Forecast]]:
    response = client.post(
        "/api/runs",
        json={"as_of": "2026-07-15T15:00:00+08:00"},
    )
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    with client.app.state.database.session_factory() as session:
        run = session.scalar(
            select(WorkflowRun)
            .options(selectinload(WorkflowRun.forecasts))
            .where(WorkflowRun.id == run_id)
        )
        assert run is not None
        run.mode = "live"
        session.commit()
        forecasts = [
            forecast
            for forecast in run.forecasts
            if forecast.horizon == "D1"
        ]
        assert {forecast.index_code for forecast in forecasts} == {
            index.code for index in INDEXES
        }
        return run_id, forecasts


def _write_snapshot(
    root: Path,
    forecasts: list[Forecast],
    *,
    captured_at: datetime,
) -> Path:
    root.mkdir(parents=True)
    target_date = forecasts[0].target_date
    items = []
    for position, forecast in enumerate(
        sorted(forecasts, key=lambda row: row.index_code)
    ):
        base_close = 1000.0 + position * 100
        target_close = base_close * (0.97 + position * 0.001)
        base_hash = _digest(
            f"{forecast.index_code}:{forecast.base_trade_date}:base"
        )
        target_hash = _digest(
            f"{forecast.index_code}:{forecast.target_date}:target"
        )
        items.append(
            {
                "index_code": forecast.index_code,
                "index_name": forecast.index_name,
                "target_date": target_date.isoformat(),
                "base_trade_date": forecast.base_trade_date.isoformat(),
                "base_close": base_close,
                "target_close": target_close,
                "actual_return": target_close / base_close - 1.0,
                "source_url": "https://www.sse.com.cn/example/index-close",
                "source_hash": _digest(
                    f"{base_hash}:{target_hash}:session"
                ),
                "base_source_url": "https://www.sse.com.cn/example/index-close",
                "base_source_hash": base_hash,
                "target_source_url": "https://www.sse.com.cn/example/index-close",
                "target_source_hash": target_hash,
                "captured_at": captured_at.isoformat(),
                "amount": 1_000_000.0,
                "advancers": 800,
                "decliners": 4_400,
                "unchanged": 200,
                "limit_down_count": 100,
                "breadth_down_ratio": 4_400 / 5_400,
                "sector_contributions": [],
                "weight_contributions": [],
                "historical_abs_return_percentile": 0.99,
                "history_sample_size": 1250,
            }
        )
    payload = {
        "protocol_version": "2.0.0",
        "target_date": target_date.isoformat(),
        "horizon": "D1",
        "captured_at": captured_at.isoformat(),
        "data_quality": {
            "status": "passed",
            "source_id": "synthetic-market-source",
            "policy_version": "synthetic-quality-v1",
            "checked_at": captured_at.isoformat(),
            "report_hash": _digest("quality"),
            "checks": QUALITY_CHECKS,
            "warnings": [],
        },
        "trading_calendar": {
            "target_date": target_date.isoformat(),
            "calendar_id": "synthetic-exchange-calendar",
            "is_open": True,
            "source_url": "https://www.sse.com.cn/example/calendar",
            "source_hash": _digest("calendar"),
            "observed_at": captured_at.isoformat(),
        },
        "publication": {
            "source_id": "synthetic-market-source",
            "artifact_hashes": {
                "index-history": _digest("index-partition"),
                "publication-receipt": _digest("manifest"),
                "market-breadth": _digest("breadth"),
                "market-limits": _digest("limits"),
            },
        },
        "items": items,
    }
    sealed = {**payload, "content_hash": canonical_hash(payload)}
    path = root / f"{target_date}-D1-{sealed['content_hash']}.json"
    path.write_text(
        json.dumps(sealed, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return path


def test_market_import_is_live_only_and_idempotent(
    client: TestClient,
    tmp_path: Path,
) -> None:
    _, forecasts = _create_live_run(client)
    captured_at = datetime(2026, 7, 16, 17, 50, tzinfo=ZONE)
    settings = client.app.state.settings
    settings.market_snapshot_root = tmp_path / "market-snapshots"
    snapshot = _write_snapshot(
        settings.market_snapshot_root,
        forecasts,
        captured_at=captured_at,
    )

    first = import_market_snapshot(settings, snapshot, now=captured_at)
    second = import_market_snapshot(settings, snapshot, now=captured_at)

    assert first.status == "completed"
    assert first.evaluated_forecasts == 5
    assert first.existing_forecasts == 0
    assert second.evaluated_forecasts == 0
    assert second.existing_forecasts == 5
    with client.app.state.database.session_factory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(EvaluationResult)
            .join(Forecast, Forecast.id == EvaluationResult.forecast_id)
            .where(Forecast.horizon == "D1")
        )
        assert count == 5


def test_market_outcome_paths_exclude_custom_market_universe(
    client: TestClient,
    tmp_path: Path,
) -> None:
    _, forecasts = _create_live_run(client)
    captured_at = datetime(2026, 7, 16, 17, 50, tzinfo=ZONE)
    settings = client.app.state.settings
    settings.market_snapshot_root = tmp_path / "market-snapshots"
    snapshot = _write_snapshot(
        settings.market_snapshot_root,
        forecasts,
        captured_at=captured_at,
    )
    target_date = forecasts[0].target_date
    with client.app.state.database.session_factory() as session:
        run = session.get(WorkflowRun, forecasts[0].run_id)
        assert run is not None
        run.data_quality = {
            **(run.data_quality or {}),
            "market_universe": {
                "content_hash": "f" * 64,
                "universe_id": "custom-market",
            },
        }
        run.market_universe_hash = "f" * 64
        session.commit()
        assert pending_live_forecasts(
            session,
            target_date=target_date,
            horizon="D1",
        ) == []

    imported = import_market_snapshot(settings, snapshot, now=captured_at)
    blocked = record_blocked_upstream(
        settings,
        target_date=target_date,
        horizon="D1",
        reason_code="market_snapshot_gate_failed",
        error="custom market must not create a formal reflection block",
        now=captured_at,
    )

    assert imported.status == "no_due_live_forecast"
    assert imported.source_run_ids == ()
    assert blocked is None
    with client.app.state.database.session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(EvaluationResult))
            == 0
        )
        assert (
            session.scalar(select(func.count()).select_from(EvaluationBatch))
            == 0
        )


def test_market_outcome_keeps_legacy_unsealed_default_runs(
    client: TestClient,
) -> None:
    _, forecasts = _create_live_run(client)
    target_date = forecasts[0].target_date
    with client.app.state.database.session_factory() as session:
        run = session.get(WorkflowRun, forecasts[0].run_id)
        assert run is not None
        quality = dict(run.data_quality or {})
        assert (
            quality["market_universe"]["content_hash"]
            == DEFAULT_MARKET_UNIVERSE.content_hash
        )
        quality.pop("market_universe")
        run.data_quality = quality
        run.market_universe_hash = DEFAULT_MARKET_UNIVERSE.content_hash
        session.commit()

        due = pending_live_forecasts(
            session,
            target_date=target_date,
            horizon="D1",
        )

    assert {forecast.index_code for forecast in due} == {
        item.code for item in INDEXES
    }


def test_market_snapshot_loader_rejects_root_escape(
    client: TestClient,
    tmp_path: Path,
) -> None:
    _, forecasts = _create_live_run(client)
    captured_at = datetime(2026, 7, 16, 17, 50, tzinfo=ZONE)
    trusted_root = tmp_path / "trusted"
    outside = _write_snapshot(
        tmp_path / "outside",
        forecasts,
        captured_at=captured_at,
    )
    trusted_root.mkdir()

    try:
        load_market_snapshot(
            outside,
            now=captured_at,
            root=trusted_root,
        )
    except ValueError as exc:
        assert "configured root" in str(exc)
    else:  # pragma: no cover - explicit assertion for a security boundary
        raise AssertionError("snapshot root escape was accepted")


def test_market_snapshot_loader_rejects_hash_tampering(
    client: TestClient,
    tmp_path: Path,
) -> None:
    _, forecasts = _create_live_run(client)
    captured_at = datetime(2026, 7, 16, 17, 50, tzinfo=ZONE)
    trusted_root = tmp_path / "trusted"
    snapshot = _write_snapshot(
        trusted_root,
        forecasts,
        captured_at=captured_at,
    )
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload["items"][0]["target_close"] += 1.0
    snapshot.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="content_hash"):
        load_market_snapshot(
            snapshot,
            now=captured_at,
            root=trusted_root,
        )
