from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from app.adapters import LocalJsonEvidenceSnapshotSource
from app.ports import (
    EvidenceSnapshotAccessError,
    EvidenceSnapshotFormatError,
    EvidenceSnapshotSource,
    EvidenceSnapshotValidationError,
)
from app.schemas import EvidenceItem, FrozenEvidenceSnapshot
from app.services.snapshot import canonical_hash, evidence_item_hash

INDEX_CODES = {
    "000300.SH",
    "000905.SH",
    "000852.SH",
    "399006.SZ",
    "000688.SH",
}
AS_OF = datetime(2026, 7, 13, 15, tzinfo=ZoneInfo("Asia/Shanghai"))


def _valid_snapshot_payload() -> dict:
    item = EvidenceItem(
        id="SSE-EVIDENCE-ADAPTER",
        title="可审计事实",
        summary="交易所发布了预测时点前可见的市场事实。",
        quote="交易所发布了预测时点前可见的市场事实。",
        source_url="https://www.sse.com.cn/disclosure/example",
        event_time=AS_OF - timedelta(hours=2),
        published_at=AS_OF - timedelta(hours=1, minutes=30),
        ingested_at=AS_OF - timedelta(hours=1),
        entities=[],
        event_type="market_notice",
        content_hash="0" * 64,
    )
    item = item.model_copy(update={"content_hash": evidence_item_hash(item)})
    market_data = {
        code: {
            "trade_date": AS_OF.date(),
            "source_url": "https://www.sse.com.cn/market/close",
            "source_hash": hashlib.sha256(f"{code}|close".encode()).hexdigest(),
            "observed_at": AS_OF - timedelta(minutes=2),
            "ingested_at": AS_OF,
        }
        for code in INDEX_CODES
    }
    base = {
        "as_of": AS_OF,
        "data_cutoff": AS_OF,
        "created_at": AS_OF + timedelta(minutes=5),
        "base_session": AS_OF.date(),
        "trading_calendar": {
            "sessions": [
                AS_OF.date(),
                AS_OF.date() + timedelta(days=1),
                AS_OF.date() + timedelta(days=2),
            ],
            "source_url": "https://www.sse.com.cn/market/trading-calendar",
            "source_hash": hashlib.sha256(b"sse-trading-calendar").hexdigest(),
            "observed_at": AS_OF - timedelta(minutes=2),
            "ingested_at": AS_OF,
        },
        "volatility_20d": {code: 0.01 for code in INDEX_CODES},
        "market_data": market_data,
        "target_sessions": [
            AS_OF.date() + timedelta(days=1),
            AS_OF.date() + timedelta(days=2),
        ],
        "items": [item],
    }
    provisional = FrozenEvidenceSnapshot(**base, content_hash="0" * 64)
    snapshot = provisional.model_copy(
        update={
            "content_hash": canonical_hash(
                provisional.model_dump(mode="json", exclude={"content_hash"})
            )
        }
    )
    return snapshot.model_dump(mode="json")


def _write_snapshot(path: Path, payload: dict | None = None) -> None:
    path.write_text(
        json.dumps(payload or _valid_snapshot_payload(), ensure_ascii=False),
        encoding="utf-8",
    )


def test_local_json_source_implements_port_and_validates_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    _write_snapshot(path)
    source = LocalJsonEvidenceSnapshotSource(
        root=tmp_path,
        snapshot_path=Path("snapshot.json"),
    )

    assert isinstance(source, EvidenceSnapshotSource)
    snapshot = source.load_snapshot(as_of=AS_OF)

    assert snapshot.as_of == AS_OF
    assert snapshot.content_hash == _valid_snapshot_payload()["content_hash"]


def test_local_json_source_rejects_path_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.json"
    _write_snapshot(outside)
    source = LocalJsonEvidenceSnapshotSource(
        root=root,
        snapshot_path=Path("../outside.json"),
    )

    with pytest.raises(EvidenceSnapshotAccessError, match="escaped.*configured root"):
        source.load_snapshot(as_of=AS_OF)


def test_local_json_source_rejects_symlink_file(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    _write_snapshot(target)
    link = tmp_path / "snapshot.json"
    link.symlink_to(target)
    source = LocalJsonEvidenceSnapshotSource(root=tmp_path, snapshot_path=link)

    with pytest.raises(EvidenceSnapshotAccessError, match="may not contain symlinks"):
        source.load_snapshot(as_of=AS_OF)


def test_local_json_source_rejects_symlink_directory(tmp_path: Path) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    _write_snapshot(real_directory / "snapshot.json")
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    source = LocalJsonEvidenceSnapshotSource(
        root=tmp_path,
        snapshot_path=Path("linked/snapshot.json"),
    )

    with pytest.raises(EvidenceSnapshotAccessError, match="may not contain symlinks"):
        source.load_snapshot(as_of=AS_OF)


def test_local_json_source_reports_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text("{not-json", encoding="utf-8")
    source = LocalJsonEvidenceSnapshotSource(root=tmp_path, snapshot_path=path)

    with pytest.raises(EvidenceSnapshotFormatError, match="valid UTF-8 JSON"):
        source.load_snapshot(as_of=AS_OF)


def test_local_json_source_reports_schema_errors(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    _write_snapshot(path, {"as_of": AS_OF.isoformat()})
    source = LocalJsonEvidenceSnapshotSource(root=tmp_path, snapshot_path=path)

    with pytest.raises(EvidenceSnapshotFormatError, match="FrozenEvidenceSnapshot"):
        source.load_snapshot(as_of=AS_OF)


def test_local_json_source_rejects_tampered_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    payload = _valid_snapshot_payload()
    payload["volatility_20d"]["000300.SH"] = 0.02
    _write_snapshot(path, payload)
    source = LocalJsonEvidenceSnapshotSource(root=tmp_path, snapshot_path=path)

    with pytest.raises(
        EvidenceSnapshotValidationError,
        match="freshness, provenance, or integrity validation",
    ):
        source.load_snapshot(as_of=AS_OF)
