from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.config import Settings
from app.schemas import EvidenceItem, FrozenEvidenceSnapshot
from app.services.snapshot import (
    LiveEvidenceRequiredError,
    canonical_hash,
    evidence_item_hash,
    load_evidence_snapshot,
)

from scripts.build_snapshot import build_snapshot

INDEX_CODES = {
    "000300.SH",
    "000905.SH",
    "000852.SH",
    "399006.SZ",
    "000688.SH",
}


def _valid_live_payload(as_of: datetime) -> dict:
    item = EvidenceItem(
        id="SSE-EVIDENCE-1",
        title="可审计事实",
        summary="交易所发布了预测时点前可见的市场事实。",
        quote="交易所发布了预测时点前可见的市场事实。",
        source_url="https://www.sse.com.cn/disclosure/example",
        event_time=as_of - timedelta(hours=2),
        published_at=as_of - timedelta(hours=1, minutes=30),
        ingested_at=as_of - timedelta(hours=1),
        entities=[],
        event_type="market_notice",
        content_hash="0" * 64,
    )
    item = item.model_copy(update={"content_hash": evidence_item_hash(item)})
    market_data = {
        code: {
            "trade_date": as_of.date(),
            "source_url": "https://www.sse.com.cn/market/close",
            "source_hash": hashlib.sha256(f"{code}|close".encode()).hexdigest(),
            "observed_at": as_of - timedelta(minutes=2),
            "ingested_at": as_of,
        }
        for code in INDEX_CODES
    }
    base = {
        "as_of": as_of,
        "data_cutoff": as_of,
        "created_at": as_of + timedelta(minutes=5),
        "base_session": as_of.date(),
        "trading_calendar": {
            "sessions": [
                as_of.date(),
                as_of.date() + timedelta(days=1),
                as_of.date() + timedelta(days=2),
            ],
            "source_url": "https://www.sse.com.cn/market/trading-calendar",
            "source_hash": hashlib.sha256(b"sse-trading-calendar").hexdigest(),
            "observed_at": as_of - timedelta(minutes=2),
            "ingested_at": as_of,
        },
        "volatility_20d": {code: 0.01 for code in INDEX_CODES},
        "market_data": market_data,
        "target_sessions": [as_of.date() + timedelta(days=1), as_of.date() + timedelta(days=2)],
        "items": [item],
    }
    provisional = FrozenEvidenceSnapshot(**base, content_hash="0" * 64)
    return provisional.model_copy(
        update={
            "content_hash": canonical_hash(
                provisional.model_dump(mode="json", exclude={"content_hash"})
            )
        }
    ).model_dump(mode="json")


def _write_snapshot(path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _live_settings(tmp_path, snapshot_path) -> Settings:
    return Settings(
        demo_mode=False,
        llm_api_key="test-key",
        evidence_snapshot_path=snapshot_path,
        database_url=f"sqlite:///{tmp_path / 'db.sqlite3'}",
    )


def test_demo_snapshot_contains_all_index_volatilities(tmp_path) -> None:
    settings = Settings(
        demo_mode=True,
        wiki_path=tmp_path / "wiki",
        database_url=f"sqlite:///{tmp_path / 'db.sqlite3'}",
    )
    snapshot = load_evidence_snapshot(
        settings,
        as_of=datetime(2026, 7, 13, 15, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert set(snapshot.volatility_20d) == INDEX_CODES
    assert snapshot.items[0].event_type == "demo"
    assert [target.isoformat() for target in snapshot.target_sessions] == [
        "2026-07-14",
        "2026-07-15",
    ]
    assert set(snapshot.market_data) == INDEX_CODES


def test_live_mode_without_frozen_snapshot_is_blocked(tmp_path) -> None:
    settings = Settings(
        demo_mode=False,
        llm_api_key="test-key",
        evidence_snapshot_path=None,
        database_url=f"sqlite:///{tmp_path / 'db.sqlite3'}",
    )
    with pytest.raises(LiveEvidenceRequiredError, match="Live mode is blocked"):
        load_evidence_snapshot(
            settings,
            as_of=datetime(2026, 7, 13, 15, tzinfo=ZoneInfo("Asia/Shanghai")),
        )


def test_live_snapshot_verifies_freshness_provenance_and_canonical_hashes(tmp_path) -> None:
    as_of = datetime(2026, 7, 13, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    path = tmp_path / "snapshot.json"
    payload = _valid_live_payload(as_of)
    _write_snapshot(path, payload)

    snapshot = load_evidence_snapshot(
        _live_settings(tmp_path, path),
        as_of=as_of,
    )

    assert snapshot.as_of == as_of
    assert snapshot.items[0].content_hash == evidence_item_hash(snapshot.items[0])
    assert snapshot.market_data["000300.SH"].source_url.startswith("https://")
    assert snapshot.market_data["000300.SH"].trade_date == as_of.date()


def test_live_snapshot_rejects_stale_as_of_even_when_snapshot_hash_is_valid(tmp_path) -> None:
    as_of = datetime(2026, 7, 13, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    path = tmp_path / "snapshot.json"
    _write_snapshot(path, _valid_live_payload(as_of))

    with pytest.raises(LiveEvidenceRequiredError, match="does not exactly match"):
        load_evidence_snapshot(
            _live_settings(tmp_path, path),
            as_of=as_of + timedelta(days=1),
        )


def test_live_snapshot_rejects_item_tampering_hidden_by_resealed_outer_hash(tmp_path) -> None:
    as_of = datetime(2026, 7, 13, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    path = tmp_path / "snapshot.json"
    payload = _valid_live_payload(as_of)
    payload["items"][0]["quote"] = "被篡改但未重新封存的引文"
    payload["content_hash"] = canonical_hash(
        {key: value for key, value in payload.items() if key != "content_hash"}
    )
    _write_snapshot(path, payload)

    with pytest.raises(LiveEvidenceRequiredError, match="canonical payload"):
        load_evidence_snapshot(_live_settings(tmp_path, path), as_of=as_of)


def test_live_snapshot_rejects_allowlist_lookalike_domain(tmp_path) -> None:
    as_of = datetime(2026, 7, 13, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    path = tmp_path / "snapshot.json"
    payload = _valid_live_payload(as_of)
    payload["items"][0]["source_url"] = "https://www.sse.com.cn.evil.example/fake"
    item = EvidenceItem.model_validate(payload["items"][0])
    payload["items"][0]["content_hash"] = evidence_item_hash(item)
    payload["content_hash"] = canonical_hash(
        {key: value for key, value in payload.items() if key != "content_hash"}
    )
    _write_snapshot(path, payload)

    with pytest.raises(LiveEvidenceRequiredError, match="trusted allowlist"):
        load_evidence_snapshot(_live_settings(tmp_path, path), as_of=as_of)


def test_live_snapshot_binds_market_and_calendar_sessions_to_forecast_dates(tmp_path) -> None:
    as_of = datetime(2026, 7, 13, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    path = tmp_path / "snapshot.json"
    payload = _valid_live_payload(as_of)
    payload["market_data"]["000300.SH"]["trade_date"] = "2026-07-10"
    payload["content_hash"] = canonical_hash(
        {key: value for key, value in payload.items() if key != "content_hash"}
    )
    _write_snapshot(path, payload)

    with pytest.raises(LiveEvidenceRequiredError, match="frozen base_session"):
        load_evidence_snapshot(_live_settings(tmp_path, path), as_of=as_of)

    payload = _valid_live_payload(as_of)
    payload["trading_calendar"]["sessions"][-1] = "2026-07-16"
    payload["content_hash"] = canonical_hash(
        {key: value for key, value in payload.items() if key != "content_hash"}
    )
    _write_snapshot(path, payload)

    with pytest.raises(LiveEvidenceRequiredError, match="exactly match"):
        load_evidence_snapshot(_live_settings(tmp_path, path), as_of=as_of)


def test_live_snapshot_rejects_non_finite_volatility(tmp_path) -> None:
    as_of = datetime(2026, 7, 13, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    path = tmp_path / "snapshot.json"
    payload = _valid_live_payload(as_of)
    payload["volatility_20d"]["000300.SH"] = float("nan")
    payload["content_hash"] = canonical_hash(
        {key: value for key, value in payload.items() if key != "content_hash"}
    )
    _write_snapshot(path, payload)

    with pytest.raises(LiveEvidenceRequiredError, match="finite"):
        load_evidence_snapshot(_live_settings(tmp_path, path), as_of=as_of)


def test_snapshot_builder_recomputes_item_and_outer_hashes() -> None:
    as_of = datetime(2026, 7, 13, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    draft = _valid_live_payload(as_of)
    draft.pop("content_hash")
    draft["items"][0].pop("content_hash")
    draft["items"][0]["event_time"] = "2026-07-13T05:00:00Z"
    draft["items"][0]["published_at"] = "2026-07-13T05:30:00Z"
    draft["items"][0]["ingested_at"] = "2026-07-13T06:00:00Z"

    snapshot = build_snapshot(draft)

    assert snapshot.content_hash == canonical_hash(
        snapshot.model_dump(mode="json", exclude={"content_hash"})
    )
    assert snapshot.items[0].content_hash == evidence_item_hash(snapshot.items[0])
