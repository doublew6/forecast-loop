from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from app.config import Settings
from app.services.reflection_handoff import (
    FrozenSource,
    FrozenSourceSnapshot,
    _canonical_hash,
)
from app.services.reflection_sources import load_frozen_source_timeline

ZONE = ZoneInfo("Asia/Shanghai")


def _write_snapshot(tmp_path):
    reflection_id = uuid4()
    source_run_id = uuid4()
    root = tmp_path / "reflections"
    job = root / str(reflection_id)
    job.mkdir(parents=True)
    source_values = {
        "id": "source-1",
        "title": "盘中公开事件",
        "summary": "来源内容由可信采集器冻结。",
        "quote": "用于完整性测试的短摘录。",
        "source_url": "https://example.com/source-1",
        "event_time": datetime(2026, 7, 16, 10, 30, tzinfo=ZONE),
        "published_at": datetime(2026, 7, 16, 10, 31, tzinfo=ZONE),
        "ingested_at": datetime(2026, 7, 16, 18, 35, tzinfo=ZONE),
        "source_kind": "official",
        "related_index_codes": ["000300.SH"],
    }
    source = FrozenSource(
        **source_values,
        time_class="post_cutoff_preclose",
        content_hash=_canonical_hash(
            FrozenSource(
                **source_values,
                time_class="post_cutoff_preclose",
                content_hash="0" * 64,
            ).model_dump(
                mode="json",
                exclude={"content_hash", "time_class"},
            )
        ),
    )
    unsigned = FrozenSourceSnapshot(
        reflection_id=reflection_id,
        source_run_id=source_run_id,
        frozen_at=datetime(2026, 7, 16, 18, 36, tzinfo=ZONE),
        discovery_hash="2" * 64,
        items=[source],
        unresolved_without_post_outcome_sources=False,
        content_hash="0" * 64,
    )
    snapshot = unsigned.model_copy(
        update={
            "content_hash": _canonical_hash(
                unsigned.model_dump(mode="json", exclude={"content_hash"})
            )
        }
    )
    path = job / "sources.json"
    path.write_text(
        json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    settings = Settings(
        reflection_root=root,
        auto_seed=False,
    )
    return settings, snapshot, path


def test_source_timeline_requires_matching_database_seal(tmp_path) -> None:
    settings, snapshot, _ = _write_snapshot(tmp_path)

    items = load_frozen_source_timeline(
        settings,
        reflection_id=str(snapshot.reflection_id),
        source_run_id=str(snapshot.source_run_id),
        expected_hash=snapshot.content_hash,
    )

    assert [item.id for item in items] == ["source-1"]
    with pytest.raises(ValueError, match="database seal"):
        load_frozen_source_timeline(
            settings,
            reflection_id=str(snapshot.reflection_id),
            source_run_id=str(snapshot.source_run_id),
            expected_hash="f" * 64,
        )


def test_source_timeline_rejects_tampering_after_freeze(tmp_path) -> None:
    settings, snapshot, path = _write_snapshot(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["items"][0]["title"] = "被篡改的标题"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="content hash"):
        load_frozen_source_timeline(
            settings,
            reflection_id=str(snapshot.reflection_id),
            source_run_id=str(snapshot.source_run_id),
            expected_hash=snapshot.content_hash,
        )


def test_source_timeline_is_empty_before_a_source_seal(tmp_path) -> None:
    settings = Settings(
        reflection_root=tmp_path / "not-created",
        auto_seed=False,
    )

    assert load_frozen_source_timeline(
        settings,
        reflection_id=str(uuid4()),
        source_run_id=str(uuid4()),
        expected_hash=None,
    ) == []
