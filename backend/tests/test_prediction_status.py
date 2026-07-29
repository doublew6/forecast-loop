from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from app.config import Settings
from app.db import Database
from app.market_universe import (
    DEFAULT_MARKET_UNIVERSE,
    MarketUniverseBody,
    load_market_universe,
    seal_market_universe,
)
from app.models import WorkflowRun
from app.services.prediction_status import (
    build_prediction_status,
    load_prediction_prepare_receipts,
    receipt_uses_market_universe,
    write_prediction_prepare_receipt,
)

ZONE = ZoneInfo("Asia/Shanghai")
MONDAY = date(2026, 7, 20)


def _settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'prediction-status.sqlite3'}",
        checkpoint_path=tmp_path / "checkpoint.sqlite3",
        prediction_status_root=tmp_path / "prediction-status",
        auto_seed=False,
    )


def _database(settings: Settings) -> Database:
    database = Database(settings.database_url)
    database.create_all()
    return database


def _custom_universe_path(tmp_path):
    payload = DEFAULT_MARKET_UNIVERSE.model_dump(
        mode="json",
        exclude={"content_hash"},
    )
    payload["universe_id"] = "alternate-a-share-research"
    payload["version"] = "2.0.0"
    universe = seal_market_universe(MarketUniverseBody.model_validate(payload))
    path = tmp_path / "custom-universe.json"
    path.write_text(
        json.dumps(universe.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    return path, universe


def test_missing_receipt_exposes_pending_stale_and_weekend_holiday(tmp_path) -> None:
    settings = _settings(tmp_path)
    database = _database(settings)
    try:
        with database.session_factory() as session:
            pending = build_prediction_status(
                settings,
                session,
                now=datetime(2026, 7, 20, 17, 30, tzinfo=ZONE),
            )
            stale = build_prediction_status(
                settings,
                session,
                now=datetime(2026, 7, 20, 18, 0, tzinfo=ZONE),
            )
            holiday = build_prediction_status(
                settings,
                session,
                now=datetime(2026, 7, 19, 18, 0, tzinfo=ZONE),
            )
    finally:
        database.dispose()

    assert pending.today.state == "pending"
    assert stale.today.state == "stale"
    assert holiday.today.state == "holiday"
    assert not settings.prediction_status_root.exists()


def test_blocked_receipt_is_hash_sealed_and_does_not_leak_local_paths(tmp_path) -> None:
    settings = _settings(tmp_path)
    attempted_at = datetime(2026, 7, 20, 17, 55, tzinfo=ZONE)
    receipt = write_prediction_prepare_receipt(
        settings,
        base_session=MONDAY,
        attempted_at=attempted_at,
        result={
            "status": "blocked_upstream",
            "error": (
                "quality mismatch at /srv/private-market-data/"
                "state/data-quality-latest.bin"
            ),
        },
    )
    database = _database(settings)
    try:
        with database.session_factory() as session:
            status = build_prediction_status(
                settings,
                session,
                now=datetime(2026, 7, 20, 18, 0, tzinfo=ZONE),
            )
    finally:
        database.dispose()

    receipt_path = next(settings.prediction_status_root.rglob("*.json"))
    raw = receipt_path.read_text(encoding="utf-8")
    assert receipt.receipt_hash in raw
    assert receipt.protocol_version == "1.1.0"
    assert receipt.market_universe_hash == DEFAULT_MARKET_UNIVERSE.content_hash
    assert "/Users/" not in raw
    assert status.today.state == "blocked"
    assert status.today.message == "上游数据质量闸门未通过。"
    assert status.history[0].state == "blocked"
    assert status.history[0].error_code == "quality_gate_failed"
    assert status.history[0].receipt_hash == receipt.receipt_hash


def test_status_and_receipt_history_are_isolated_by_market_universe(tmp_path) -> None:
    settings = _settings(tmp_path)
    custom_path, custom_universe = _custom_universe_path(tmp_path)
    custom_settings = settings.model_copy(
        update={"market_universe_path": custom_path},
    )
    default_receipt = write_prediction_prepare_receipt(
        settings,
        base_session=MONDAY,
        attempted_at=datetime(2026, 7, 20, 17, 55, tzinfo=ZONE),
        result={"status": "blocked_upstream", "error": "quality gate failed"},
    )
    write_prediction_prepare_receipt(
        custom_settings,
        base_session=MONDAY,
        attempted_at=datetime(2026, 7, 20, 17, 56, tzinfo=ZONE),
        result={"status": "blocked_upstream", "error": "manifest gate failed"},
    )
    database = _database(settings)
    try:
        with database.session_factory() as session:
            session.add_all(
                [
                    WorkflowRun(
                        id="default-live-completed",
                        as_of=datetime(2026, 7, 20, 15, 0, tzinfo=ZONE),
                        data_cutoff=datetime(2026, 7, 20, 14, 55, tzinfo=ZONE),
                        status="completed",
                        mode="live",
                        started_at=datetime(2026, 7, 20, 15, 0, tzinfo=ZONE),
                        completed_at=datetime(2026, 7, 20, 20, 0, tzinfo=ZONE),
                        duration_seconds=0,
                        error=None,
                        data_quality={},
                        workflow_steps=[],
                        input_hash="d" * 64,
                        market_universe_hash=DEFAULT_MARKET_UNIVERSE.content_hash,
                    ),
                    WorkflowRun(
                        id="custom-live-completed",
                        as_of=datetime(2026, 7, 19, 15, 0, tzinfo=ZONE),
                        data_cutoff=datetime(2026, 7, 19, 14, 55, tzinfo=ZONE),
                        status="completed",
                        mode="live",
                        started_at=datetime(2026, 7, 19, 15, 0, tzinfo=ZONE),
                        completed_at=datetime(2026, 7, 19, 20, 0, tzinfo=ZONE),
                        duration_seconds=0,
                        error=None,
                        data_quality={
                            "market_universe": {
                                "content_hash": custom_universe.content_hash,
                            }
                        },
                        workflow_steps=[],
                        input_hash="c" * 64,
                        market_universe_hash=custom_universe.content_hash,
                    ),
                ]
            )
            session.commit()
            default_status = build_prediction_status(
                settings,
                session,
                now=datetime(2026, 7, 20, 18, 0, tzinfo=ZONE),
            )
            custom_status = build_prediction_status(
                custom_settings,
                session,
                now=datetime(2026, 7, 20, 18, 0, tzinfo=ZONE),
                universe=custom_universe,
            )
    finally:
        database.dispose()

    assert default_status.latest_completed_run_id == "default-live-completed"
    assert len(default_status.history) == 1
    assert default_status.history[0].receipt_hash == default_receipt.receipt_hash
    assert custom_status.latest_completed_run_id == "custom-live-completed"
    assert custom_status.today.state == "blocked"
    assert "自定义 Universe" in custom_status.today.message
    assert "配置的时间" not in custom_status.today.message
    assert custom_status.history == []


def test_legacy_receipt_without_universe_hash_is_default_only(tmp_path) -> None:
    settings = _settings(tmp_path)
    custom_path, custom_universe = _custom_universe_path(tmp_path)
    write_prediction_prepare_receipt(
        settings,
        base_session=MONDAY,
        attempted_at=datetime(2026, 7, 20, 17, 55, tzinfo=ZONE),
        result={"status": "blocked_upstream", "error": "quality gate failed"},
    )
    receipt_path = next(settings.prediction_status_root.rglob("*.json"))
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["protocol_version"] = "1.0.0"
    payload.pop("market_universe_hash")
    unsigned = {key: value for key, value in payload.items() if key != "receipt_hash"}
    payload["receipt_hash"] = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    os.chmod(receipt_path, 0o600)
    receipt_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    receipt = load_prediction_prepare_receipts(settings)[0]

    assert receipt.protocol_version == "1.0.0"
    assert receipt.market_universe_hash is None
    assert receipt_uses_market_universe(receipt, DEFAULT_MARKET_UNIVERSE)
    assert not receipt_uses_market_universe(
        receipt,
        load_market_universe(custom_path),
    )
    assert custom_universe.content_hash != DEFAULT_MARKET_UNIVERSE.content_hash


def test_prepared_receipt_exposes_awaiting_overdue_and_completed(tmp_path) -> None:
    settings = _settings(tmp_path)
    run_id = "live-prediction-20260720"
    attempted_at = datetime(2026, 7, 20, 17, 55, tzinfo=ZONE)
    write_prediction_prepare_receipt(
        settings,
        base_session=MONDAY,
        attempted_at=attempted_at,
        result={
            "status": "prepared",
            "run_id": run_id,
            "run_status": "awaiting_draft",
            "snapshot_hash": "a" * 64,
        },
    )
    database = _database(settings)
    try:
        with database.session_factory() as session:
            session.add(
                WorkflowRun(
                    id=run_id,
                    as_of=attempted_at,
                    data_cutoff=datetime(2026, 7, 20, 17, 46, tzinfo=ZONE),
                    status="awaiting_draft",
                    mode="live",
                    started_at=attempted_at,
                    completed_at=None,
                    duration_seconds=None,
                    error=None,
                    data_quality={
                        "handoff": {
                            "finalize_deadline": "2026-07-21T00:00:00+08:00"
                        }
                    },
                    workflow_steps=[],
                    input_hash="b" * 64,
                )
            )
            session.commit()
            awaiting = build_prediction_status(
                settings,
                session,
                now=datetime(2026, 7, 20, 18, 0, tzinfo=ZONE),
            )
            overdue = build_prediction_status(
                settings,
                session,
                now=datetime(2026, 7, 20, 23, 59, 30, tzinfo=ZONE),
            )
            row = session.get(WorkflowRun, run_id)
            assert row is not None
            row.status = "completed"
            row.completed_at = datetime(2026, 7, 20, 20, 35, tzinfo=ZONE)
            session.commit()
            completed = build_prediction_status(
                settings,
                session,
                now=datetime(2026, 7, 20, 20, 36, tzinfo=ZONE),
            )
    finally:
        database.dispose()

    assert awaiting.today.state == "awaiting"
    assert overdue.today.state == "overdue"
    assert completed.today.state == "completed"
    assert completed.history[0].state == "completed"
    assert completed.latest_completed_run_id == run_id
    assert completed.latest_completed_as_of == attempted_at


def test_previous_incomplete_day_remains_visible_as_overdue_once_date_rolls(
    tmp_path,
) -> None:
    settings = _settings(tmp_path)
    run_id = "live-prediction-20260720"
    attempted_at = datetime(2026, 7, 20, 17, 55, tzinfo=ZONE)
    write_prediction_prepare_receipt(
        settings,
        base_session=MONDAY,
        attempted_at=attempted_at,
        result={
            "status": "prepared",
            "run_id": run_id,
            "run_status": "awaiting_draft",
            "snapshot_hash": "a" * 64,
        },
    )
    write_prediction_prepare_receipt(
        settings,
        base_session=MONDAY,
        attempted_at=datetime(2026, 7, 20, 19, 55, tzinfo=ZONE),
        result={
            "status": "already_prepared",
            "run_id": run_id,
            "run_status": "awaiting_draft",
        },
    )
    database = _database(settings)
    try:
        with database.session_factory() as session:
            session.add(
                WorkflowRun(
                    id=run_id,
                    as_of=attempted_at,
                    data_cutoff=datetime(2026, 7, 20, 17, 46, tzinfo=ZONE),
                    status="awaiting_draft",
                    mode="live",
                    started_at=attempted_at,
                    completed_at=None,
                    duration_seconds=None,
                    error=None,
                    data_quality={
                        "handoff": {
                            "finalize_deadline": "2026-07-21T00:00:00+08:00"
                        }
                    },
                    workflow_steps=[],
                    input_hash="b" * 64,
                )
            )
            session.commit()
            status = build_prediction_status(
                settings,
                session,
                now=datetime(2026, 7, 21, 10, 0, tzinfo=ZONE),
            )
    finally:
        database.dispose()

    assert status.today.state == "pending"
    assert len(status.history) == 1
    assert status.history[0].base_session == MONDAY
    assert status.history[0].status == "already_prepared"
    assert status.history[0].state == "overdue"
    assert status.history[0].message == "该日 Live 预测未在配置的运行时限内完成。"


def test_no_open_session_receipt_exposes_exchange_holiday(tmp_path) -> None:
    settings = _settings(tmp_path)
    write_prediction_prepare_receipt(
        settings,
        base_session=MONDAY,
        attempted_at=datetime(2026, 7, 20, 17, 55, tzinfo=ZONE),
        result={"status": "no_open_session", "reason": "private calendar detail"},
    )
    database = _database(settings)
    try:
        with database.session_factory() as session:
            status = build_prediction_status(
                settings,
                session,
                now=datetime(2026, 7, 20, 18, 0, tzinfo=ZONE),
            )
    finally:
        database.dispose()

    assert status.today.state == "holiday"
    assert status.today.attempt_status == "no_open_session"


def test_tampered_prepare_receipt_fails_closed(tmp_path) -> None:
    settings = _settings(tmp_path)
    write_prediction_prepare_receipt(
        settings,
        base_session=MONDAY,
        attempted_at=datetime(2026, 7, 20, 17, 55, tzinfo=ZONE),
        result={"status": "blocked_upstream", "error": "quality gate failed"},
    )
    receipt_path = next(settings.prediction_status_root.rglob("*.json"))
    os.chmod(receipt_path, 0o600)
    receipt_path.write_text(
        receipt_path.read_text(encoding="utf-8").replace(
            "quality_gate_failed",
            "manifest_gate_failed",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="hash mismatch"):
        load_prediction_prepare_receipts(settings)


def test_prepare_receipt_rejects_inconsistent_success_bindings_and_date(tmp_path) -> None:
    settings = _settings(tmp_path)
    attempted_at = datetime(2026, 7, 20, 17, 55, tzinfo=ZONE)

    with pytest.raises(ValueError, match="run_id"):
        write_prediction_prepare_receipt(
            settings,
            base_session=MONDAY,
            attempted_at=attempted_at,
            result={"status": "prepared", "snapshot_hash": "a" * 64},
        )
    with pytest.raises(ValueError, match="snapshot_hash"):
        write_prediction_prepare_receipt(
            settings,
            base_session=MONDAY,
            attempted_at=attempted_at,
            result={"status": "prepared", "run_id": "run-without-snapshot"},
        )
    with pytest.raises(ValueError, match="base_session"):
        write_prediction_prepare_receipt(
            settings,
            base_session=date(2026, 7, 21),
            attempted_at=attempted_at,
            result={"status": "blocked_upstream", "error": "quality gate"},
        )
