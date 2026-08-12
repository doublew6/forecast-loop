from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from app.config import Settings
from app.db import Database
from app.models import ResearchActivationEventV2, WorkflowRun
from app.research_v2 import CSI1000_D1_TARGET, DEFAULT_RESEARCH_PROGRAM_V2
from app.services.handoff import prepare_handoff
from app.services.schema_readiness import upgrade_database
from app.services.task_queue import PersistentTaskQueue
from app.services.v1_run_admission import V1RunAdmissionError
from app.workflow import PreparedRun
from sqlalchemy import func, select

ZONE = ZoneInfo("Asia/Shanghai")


def _activation(
    client,
    *,
    event_type: str = "activated",
    occurred_at: datetime | None = None,
) -> ResearchActivationEventV2:
    timestamp = occurred_at or datetime(2026, 8, 12, 18, 0, tzinfo=ZONE)
    marker = f"{event_type}:{timestamp.isoformat()}:{uuid4()}"
    row = ResearchActivationEventV2(
        id=str(uuid4()),
        schema_version="forecast-loop.research-activation/v2",
        program_hash=DEFAULT_RESEARCH_PROGRAM_V2.content_hash,
        target_id=CSI1000_D1_TARGET,
        event_type=event_type,
        policy_version="2.0.0",
        evidence={"test": True},
        actor="test",
        occurred_at=timestamp,
        previous_event_hash=None,
        content_hash=hashlib.sha256(marker.encode()).hexdigest(),
    )
    with client.app.state.database.session_factory() as session:
        session.add(row)
        session.commit()
    return row


def test_http_stops_all_new_v1_runs_but_keeps_history_readable(client) -> None:
    historical = client.post(
        "/api/runs",
        json={"as_of": "2026-08-11T15:00:00+08:00"},
    )
    assert historical.status_code == 201
    historical_id = historical.json()["id"]
    _activation(client)

    rejected = client.post(
        "/api/runs",
        json={"as_of": "2026-08-12T15:00:00+08:00"},
    )

    assert rejected.status_code == 409
    assert "creation of new legacy five-index v1 runs has stopped" in rejected.json()[
        "detail"
    ]
    rows = client.get("/api/runs").json()["items"]
    assert any(item["id"] == historical_id for item in rows)


def test_latest_retired_event_reopens_v1_admission(client) -> None:
    _activation(
        client,
        event_type="activated",
        occurred_at=datetime(2026, 8, 12, 18, 0, tzinfo=ZONE),
    )
    _activation(
        client,
        event_type="retired",
        occurred_at=datetime(2026, 8, 12, 19, 0, tzinfo=ZONE),
    )

    created = client.post(
        "/api/runs",
        json={"as_of": "2026-08-12T15:00:00+08:00"},
    )

    assert created.status_code == 201


def test_other_program_activation_does_not_retire_canonical_v1(client) -> None:
    row = ResearchActivationEventV2(
        id=str(uuid4()),
        schema_version="forecast-loop.research-activation/v2",
        program_hash="f" * 64,
        target_id=CSI1000_D1_TARGET,
        event_type="activated",
        policy_version="2.0.0",
        evidence={"test": True},
        actor="test",
        occurred_at=datetime(2026, 8, 12, 18, 0, tzinfo=ZONE),
        previous_event_hash=None,
        content_hash=hashlib.sha256(b"foreign-program-activation").hexdigest(),
    )
    with client.app.state.database.session_factory() as session:
        session.add(row)
        session.commit()

    created = client.post(
        "/api/runs",
        json={"as_of": "2026-08-12T15:00:00+08:00"},
    )

    assert created.status_code == 201


def test_codex_file_prepare_is_blocked_before_creating_run_or_job(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'handoff.sqlite3'}",
        handoff_root=tmp_path / "v1-handoffs",
        wiki_path=tmp_path / "wiki",
        demo_mode=True,
        auto_seed=False,
    )
    upgrade_database(settings.database_url)
    database = Database(settings.database_url)
    activation = ResearchActivationEventV2(
        id=str(uuid4()),
        schema_version="forecast-loop.research-activation/v2",
        program_hash=DEFAULT_RESEARCH_PROGRAM_V2.content_hash,
        target_id=CSI1000_D1_TARGET,
        event_type="activated",
        policy_version="2.0.0",
        evidence={"test": True},
        actor="test",
        occurred_at=datetime(2026, 8, 12, 18, 0, tzinfo=ZONE),
        previous_event_hash=None,
        content_hash=hashlib.sha256(b"handoff-activation").hexdigest(),
    )
    with database.session_factory() as session:
        session.add(activation)
        session.commit()

    try:
        with pytest.raises(V1RunAdmissionError, match="creation of new legacy"):
            prepare_handoff(settings, handoff_root=settings.handoff_root)

        with database.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(WorkflowRun)) == 0
        assert settings.handoff_root.exists()
        assert not any(settings.handoff_root.iterdir())
    finally:
        database.dispose()


def test_queue_rechecks_activation_before_persisting_new_v1_run(client) -> None:
    database = client.app.state.database
    as_of = datetime(2026, 8, 12, 15, 0, tzinfo=ZONE)
    row = WorkflowRun(
        id=str(uuid4()),
        as_of=as_of,
        data_cutoff=as_of,
        status="queued",
        mode="demo",
        started_at=as_of,
        completed_at=None,
        duration_seconds=None,
        error=None,
        data_quality={},
        workflow_steps=[],
        input_hash="a" * 64,
    )
    prepared = PreparedRun(
        row=row,
        initial={
            "run_id": row.id,
            "as_of": as_of.isoformat(),
            "data_cutoff": as_of.isoformat(),
            "input_hash": row.input_hash,
        },
        execution_manifest={
            "schema": "forecast-loop.execution-manifest/v1",
            "test": "stable",
        },
    )
    _activation(client)
    queue = PersistentTaskQueue(database, timezone="Asia/Shanghai")

    with pytest.raises(V1RunAdmissionError, match="creation of new legacy"):
        queue.enqueue(prepared, idempotency_key="blocked-after-activation")

    with database.session_factory() as session:
        assert session.get(WorkflowRun, row.id) is None
