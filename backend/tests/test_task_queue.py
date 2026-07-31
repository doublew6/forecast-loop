from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest
from app.config import Settings
from app.db import Database
from app.domain import TaskStatus
from app.main import create_app
from app.market_universe import DEFAULT_MARKET_UNIVERSE
from app.models import WorkflowRun, WorkflowTask
from app.services.task_queue import (
    EXECUTION_MANIFEST_SCHEMA,
    ExecutionFence,
    PersistentTaskQueue,
    StaleTaskLeaseError,
    TaskIdempotencyConflictError,
    assert_execution_fence,
    default_idempotency_key,
)
from app.workflow import PreparedRun
from fastapi.testclient import TestClient

ZONE = ZoneInfo("Asia/Shanghai")


def test_default_task_idempotency_preserves_legacy_key_and_scopes_custom() -> None:
    as_of = datetime(2026, 7, 29, 15, tzinfo=ZONE)

    assert default_idempotency_key(
        as_of,
        market_universe_hash=DEFAULT_MARKET_UNIVERSE.content_hash,
    ) == f"live:{as_of.isoformat()}"
    assert default_idempotency_key(
        as_of,
        market_universe_hash="f" * 64,
    ) == f"live:{'f' * 64}:{as_of.isoformat()}"


@pytest.fixture
def queue_database(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'queue.sqlite3'}")
    database.create_all()
    try:
        yield database
    finally:
        database.dispose()


def _prepared(
    database: Database,
    *,
    run_id: str,
    as_of: datetime,
) -> PreparedRun:
    input_hash = (run_id[0] if run_id else "a") * 64
    row = WorkflowRun(
        id=run_id,
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
        input_hash=input_hash,
    )
    with database.session_factory() as session:
        session.add(row)
        session.commit()
    return PreparedRun(
        row=row,
        initial={
            "run_id": run_id,
            "input_hash": input_hash,
            "as_of": as_of.isoformat(),
            "data_cutoff": as_of.isoformat(),
        },
        execution_manifest={
            "schema": EXECUTION_MANIFEST_SCHEMA,
            "test_worker": "stable",
        },
    )


def test_enqueue_is_idempotent_but_rejects_a_different_frozen_run(
    queue_database: Database,
) -> None:
    queue = PersistentTaskQueue(queue_database, timezone="Asia/Shanghai")
    first = _prepared(
        queue_database,
        run_id="a-run",
        as_of=datetime(2026, 7, 27, 15, tzinfo=ZONE),
    )
    second = _prepared(
        queue_database,
        run_id="b-run",
        as_of=datetime(2026, 7, 28, 15, tzinfo=ZONE),
    )

    task, created = queue.enqueue(first, idempotency_key="daily:2026-07-27")
    replay, replay_created = queue.enqueue(
        first,
        idempotency_key="daily:2026-07-27",
    )

    assert created is True
    assert replay_created is False
    assert replay.id == task.id
    with pytest.raises(TaskIdempotencyConflictError):
        queue.enqueue(second, idempotency_key="daily:2026-07-27")


def test_concurrent_workers_cannot_claim_the_same_task(
    queue_database: Database,
) -> None:
    queue = PersistentTaskQueue(queue_database, timezone="Asia/Shanghai")
    prepared = _prepared(
        queue_database,
        run_id="concurrent-run",
        as_of=datetime(2026, 7, 27, 15, tzinfo=ZONE),
    )
    queue.enqueue(prepared, idempotency_key="concurrent")
    barrier = Barrier(2)

    def claim(worker_id: str):
        barrier.wait()
        return queue.claim(worker_id=worker_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        leases = list(pool.map(claim, ("worker-a", "worker-b")))

    winners = [lease for lease in leases if lease is not None]
    assert len(winners) == 1
    assert winners[0].attempt_count == 1


def test_expired_lease_is_recovered_and_old_worker_is_fenced(
    queue_database: Database,
) -> None:
    queue = PersistentTaskQueue(
        queue_database,
        timezone="Asia/Shanghai",
        lease_seconds=1,
        timeout_seconds=10,
        retry_delay_seconds=0,
    )
    prepared = _prepared(
        queue_database,
        run_id="recover-run",
        as_of=datetime(2026, 7, 27, 15, tzinfo=ZONE),
    )
    queue.enqueue(prepared, idempotency_key="recover")
    first = queue.claim(worker_id="worker-a")
    assert first is not None

    with queue_database.session_factory() as session:
        task = session.get(WorkflowTask, first.task_id)
        run = session.get(WorkflowRun, first.run_id)
        assert task is not None
        assert run is not None
        task.lease_expires_at = datetime.now(ZONE) - timedelta(seconds=1)
        run.status = "running"
        session.commit()

    assert queue.recover_expired() == 1
    with queue_database.session_factory() as session:
        task = session.get(WorkflowTask, first.task_id)
        run = session.get(WorkflowRun, first.run_id)
        assert task is not None
        assert run is not None
        assert task.status == "retry_wait"
        assert run.status == "queued"
        with pytest.raises(StaleTaskLeaseError):
            assert_execution_fence(
                session,
                ExecutionFence(first.task_id, first.lease_token),
                timezone="Asia/Shanghai",
            )

    second = queue.claim(worker_id="worker-b")
    assert second is not None
    assert second.task_id == first.task_id
    assert second.lease_token != first.lease_token
    assert second.attempt_count == 2


def test_expired_final_attempt_fails_task_and_run(
    queue_database: Database,
) -> None:
    queue = PersistentTaskQueue(
        queue_database,
        timezone="Asia/Shanghai",
        max_attempts=1,
        lease_seconds=1,
        timeout_seconds=10,
        retry_delay_seconds=0,
    )
    prepared = _prepared(
        queue_database,
        run_id="terminal-run",
        as_of=datetime(2026, 7, 27, 15, tzinfo=ZONE),
    )
    queue.enqueue(prepared, idempotency_key="terminal")
    lease = queue.claim(worker_id="worker-a")
    assert lease is not None
    with queue_database.session_factory() as session:
        task = session.get(WorkflowTask, lease.task_id)
        run = session.get(WorkflowRun, lease.run_id)
        assert task is not None
        assert run is not None
        task.lease_expires_at = datetime.now(ZONE) - timedelta(seconds=1)
        run.status = "running"
        session.commit()

    assert queue.recover_expired() == 1
    with queue_database.session_factory() as session:
        task = session.get(WorkflowTask, lease.task_id)
        run = session.get(WorkflowRun, lease.run_id)
        assert task is not None
        assert run is not None
        assert task.status == "failed"
        assert task.attempt_count == 1
        assert run.status == "failed"
        assert run.completed_at is not None


def test_new_queue_process_can_claim_persisted_work(
    queue_database: Database,
) -> None:
    first_process = PersistentTaskQueue(
        queue_database,
        timezone="Asia/Shanghai",
    )
    prepared = _prepared(
        queue_database,
        run_id="restart-run",
        as_of=datetime(2026, 7, 27, 15, tzinfo=ZONE),
    )
    task, _ = first_process.enqueue(prepared, idempotency_key="restart")

    second_process = PersistentTaskQueue(
        queue_database,
        timezone="Asia/Shanghai",
    )
    lease = second_process.claim(worker_id="worker-after-restart")

    assert lease is not None
    assert lease.task_id == task.id
    assert lease.run_id == prepared.row.id


def test_tampered_task_payload_fails_closed_without_retry(
    queue_database: Database,
) -> None:
    queue = PersistentTaskQueue(
        queue_database,
        timezone="Asia/Shanghai",
        max_attempts=3,
    )
    prepared = _prepared(
        queue_database,
        run_id="tampered-run",
        as_of=datetime(2026, 7, 27, 15, tzinfo=ZONE),
    )
    task, _ = queue.enqueue(prepared, idempotency_key="tampered")
    with queue_database.session_factory() as session:
        persisted = session.get(WorkflowTask, task.id)
        assert persisted is not None
        persisted.payload = {
            **persisted.payload,
            "input_hash": "0" * 64,
        }
        session.commit()

    assert queue.claim(worker_id="integrity-worker") is None
    with queue_database.session_factory() as session:
        persisted = session.get(WorkflowTask, task.id)
        run = session.get(WorkflowRun, prepared.row.id)
        assert persisted is not None
        assert run is not None
        assert persisted.status == "failed"
        assert persisted.attempt_count == 1
        assert "integrity seal" in persisted.last_error
        assert run.status == "failed"


def test_api_restart_does_not_fail_a_queue_backed_running_task(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'api-restart.sqlite3'}"
    database = Database(database_url)
    database.create_all()
    queue = PersistentTaskQueue(database, timezone="Asia/Shanghai")
    prepared = _prepared(
        database,
        run_id="durable-running",
        as_of=datetime(2026, 7, 27, 15, tzinfo=ZONE),
    )
    queue.enqueue(prepared, idempotency_key="durable-running")
    lease = queue.claim(worker_id="separate-worker")
    assert lease is not None
    with database.session_factory() as session:
        run = session.get(WorkflowRun, lease.run_id)
        assert run is not None
        run.status = "running"
        session.commit()
    database.dispose()

    settings = Settings(
        database_url=database_url,
        checkpoint_path=tmp_path / "restart-checkpoint.sqlite3",
        wiki_path=tmp_path / "wiki",
        demo_mode=True,
        auto_seed=False,
    )
    with TestClient(
        create_app(settings, allow_schema_bootstrap=True)
    ) as restarted:
        item = restarted.get("/api/runs").json()["items"][0]
        assert item["status"] == "running"
        assert item["task"]["status"] == "running"
        assert item["task"]["id"] == lease.task_id


def test_worker_executes_a_persisted_prepared_run(client) -> None:
    workflow = client.app.state.workflow
    queue = client.app.state.task_queue
    prepared = workflow.prepare_run(
        as_of=datetime(2026, 7, 9, 15, tzinfo=ZONE)
    )
    task, _ = queue.enqueue(prepared, idempotency_key="demo-worker")

    result = queue.run_once(workflow, worker_id="integration-worker")

    assert result is not None
    assert result.status is TaskStatus.COMPLETED
    assert result.task_id == task.id
    with client.app.state.database.session_factory() as session:
        persisted_task = session.get(WorkflowTask, task.id)
        persisted_run = session.get(WorkflowRun, prepared.row.id)
        assert persisted_task is not None
        assert persisted_run is not None
        assert persisted_task.status == "completed"
        assert persisted_task.attempt_count == 1
        assert persisted_run.status == "completed"
        assert persisted_run.error is None
        assert len(persisted_run.forecasts) == 5


def test_worker_fails_closed_when_execution_settings_drift(
    client,
    monkeypatch,
) -> None:
    workflow = client.app.state.workflow
    queue = client.app.state.task_queue
    prepared = workflow.prepare_run(
        as_of=datetime(2026, 7, 6, 15, tzinfo=ZONE)
    )
    task, _ = queue.enqueue(prepared, idempotency_key="manifest-drift")
    drifted = {
        **prepared.execution_manifest,
        "prompt_version": "changed-after-enqueue",
    }
    monkeypatch.setattr(workflow, "execution_manifest", lambda: drifted)

    result = queue.run_once(workflow, worker_id="drifted-worker")

    assert result is not None
    assert result.status is TaskStatus.FAILED
    assert "execution settings" in (result.error or "")
    with client.app.state.database.session_factory() as session:
        persisted_task = session.get(WorkflowTask, task.id)
        persisted_run = session.get(WorkflowRun, prepared.row.id)
        assert persisted_task is not None
        assert persisted_run is not None
        assert persisted_task.status == "failed"
        assert persisted_task.attempt_count == 1
        assert persisted_run.status == "failed"
        assert len(persisted_run.forecasts) == 0


def test_retry_replays_frozen_input_in_an_isolated_checkpoint(
    client,
    monkeypatch,
) -> None:
    workflow = client.app.state.workflow
    queue = PersistentTaskQueue(
        client.app.state.database,
        timezone="Asia/Shanghai",
        max_attempts=3,
        lease_seconds=60,
        timeout_seconds=1800,
        retry_delay_seconds=0,
    )
    prepared = workflow.prepare_run(
        as_of=datetime(2026, 7, 7, 15, tzinfo=ZONE)
    )
    task, _ = queue.enqueue(
        prepared,
        idempotency_key="post-graph-retry",
    )
    original_persist = workflow._persist_result
    calls = 0

    def fail_once(session, state):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient persistence failure")
        original_persist(session, state)

    monkeypatch.setattr(workflow, "_persist_result", fail_once)

    first = queue.run_once(workflow, worker_id="retry-worker")
    second = queue.run_once(workflow, worker_id="retry-worker")

    assert first is not None
    assert first.status is TaskStatus.RETRY_WAIT
    assert second is not None
    assert second.status is TaskStatus.COMPLETED
    with client.app.state.database.session_factory() as session:
        persisted_task = session.get(WorkflowTask, task.id)
        persisted_run = session.get(WorkflowRun, prepared.row.id)
        assert persisted_task is not None
        assert persisted_run is not None
        assert persisted_task.attempt_count == 2
        assert persisted_run.status == "completed"
        assert len(persisted_run.forecasts) == 5


def test_run_api_exposes_persistent_queue_state(client) -> None:
    workflow = client.app.state.workflow
    queue = client.app.state.task_queue
    prepared = workflow.prepare_run(
        as_of=datetime(2026, 7, 8, 15, tzinfo=ZONE)
    )
    task, _ = queue.enqueue(prepared, idempotency_key="api-state")

    response = client.get("/api/runs")

    assert response.status_code == 200
    item = next(
        row
        for row in response.json()["items"]
        if row["id"] == prepared.row.id
    )
    assert item["status"] == "queued"
    assert item["task"] == {
        "id": task.id,
        "status": "queued",
        "stage": "prepared",
        "attempt_count": 0,
        "max_attempts": 3,
        "available_at": item["task"]["available_at"],
        "attempt_started_at": None,
        "lease_expires_at": None,
        "last_error": None,
        "updated_at": item["task"]["updated_at"],
    }
