"""Database-backed queue for restart-safe committee workflow execution."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import Database
from ..domain import RunStatus, TaskStatus
from ..market_universe import DEFAULT_MARKET_UNIVERSE
from ..models import WorkflowRun, WorkflowTask
from .v1_run_admission import assert_v1_run_creation_allowed

if TYPE_CHECKING:
    from ..workflow import CommitteeWorkflow, PreparedRun

TASK_KIND = "committee_run"
TASK_PAYLOAD_SCHEMA = "forecast-loop.workflow-task/v1"
EXECUTION_MANIFEST_SCHEMA = "forecast-loop.execution-manifest/v1"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_LEASE_SECONDS = 60
DEFAULT_TIMEOUT_SECONDS = 30 * 60
DEFAULT_RETRY_DELAY_SECONDS = 5
_IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,254}$")


class TaskQueueError(RuntimeError):
    """Base class for durable queue failures."""


class TaskIdempotencyConflictError(TaskQueueError):
    """The same idempotency key was bound to a different frozen run."""


class TaskPayloadIntegrityError(TaskQueueError):
    """A persisted task payload no longer matches its hash or run seal."""


class StaleTaskLeaseError(TaskQueueError):
    """A worker tried to write after its lease was replaced or expired."""


@dataclass(frozen=True, slots=True)
class ExecutionFence:
    task_id: str
    lease_token: str


@dataclass(frozen=True, slots=True)
class TaskLease:
    task_id: str
    run_id: str
    lease_token: str
    worker_id: str
    attempt_count: int
    max_attempts: int
    timeout_seconds: int
    payload: dict[str, Any]
    payload_hash: str


@dataclass(frozen=True, slots=True)
class TaskExecutionResult:
    task_id: str
    run_id: str
    status: TaskStatus
    attempt_count: int
    error: str | None = None


class PersistentTaskQueue:
    """Coordinate queue claims with compare-and-swap leases."""

    def __init__(
        self,
        database: Database,
        *,
        timezone: str,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be at least one")
        if timeout_seconds < lease_seconds:
            raise ValueError("timeout_seconds must not be shorter than the lease")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must be non-negative")
        self.database = database
        self.timezone = timezone
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        self.timeout_seconds = timeout_seconds
        self.retry_delay_seconds = retry_delay_seconds

    def enqueue(
        self,
        prepared: PreparedRun,
        *,
        idempotency_key: str,
    ) -> tuple[WorkflowTask, bool]:
        """Persist one frozen run payload, returning an existing exact replay."""

        key = validate_idempotency_key(idempotency_key)
        execution_manifest = _validated_execution_manifest(
            prepared.execution_manifest
        )
        payload = {
            "schema": TASK_PAYLOAD_SCHEMA,
            "run_id": prepared.row.id,
            "input_hash": prepared.row.input_hash,
            "initial": prepared.initial,
            "execution_manifest": execution_manifest,
        }
        payload_hash = _payload_hash(payload)
        now = self._now()
        with self.database.session_factory() as session:
            assert_v1_run_creation_allowed(session)
            existing = session.scalar(
                select(WorkflowTask).where(WorkflowTask.idempotency_key == key)
            )
            if existing is not None:
                self._validate_replay(
                    existing,
                    run_id=prepared.row.id,
                    payload_hash=payload_hash,
                )
                return existing, False
            persisted_run = session.get(WorkflowRun, prepared.row.id)
            if persisted_run is None:
                session.add(prepared.row)
            elif (
                persisted_run.input_hash != prepared.row.input_hash
                or persisted_run.status != prepared.row.status
            ):
                raise TaskPayloadIntegrityError(
                    "prepared run conflicts with its persisted database row"
                )
            task = WorkflowTask(
                id=str(uuid4()),
                run_id=prepared.row.id,
                kind=TASK_KIND,
                status=TaskStatus.QUEUED.value,
                stage="prepared",
                idempotency_key=key,
                payload=payload,
                payload_hash=payload_hash,
                attempt_count=0,
                max_attempts=self.max_attempts,
                available_at=now,
                attempt_started_at=None,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                timeout_seconds=self.timeout_seconds,
                last_error=None,
                created_at=now,
                updated_at=now,
                completed_at=None,
                version=0,
            )
            session.add(task)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                raced = session.scalar(
                    select(WorkflowTask).where(
                        WorkflowTask.idempotency_key == key
                    )
                )
                if raced is None:
                    raise
                self._validate_replay(
                    raced,
                    run_id=prepared.row.id,
                    payload_hash=payload_hash,
                )
                return raced, False
            session.refresh(task)
            return task, True

    def find_by_idempotency_key(self, idempotency_key: str) -> WorkflowTask | None:
        key = validate_idempotency_key(idempotency_key)
        with self.database.session_factory() as session:
            return session.scalar(
                select(WorkflowTask).where(WorkflowTask.idempotency_key == key)
            )

    def claim(self, *, worker_id: str) -> TaskLease | None:
        """Claim one eligible task; concurrent workers can only win once."""

        owner = _validate_worker_id(worker_id)
        self.recover_expired()
        now = self._now()
        with self.database.session_factory() as session:
            candidates = session.execute(
                select(WorkflowTask.id, WorkflowTask.version)
                .where(
                    WorkflowTask.status.in_(
                        [TaskStatus.QUEUED.value, TaskStatus.RETRY_WAIT.value]
                    ),
                    WorkflowTask.available_at <= now,
                    WorkflowTask.attempt_count < WorkflowTask.max_attempts,
                )
                .order_by(WorkflowTask.available_at, WorkflowTask.created_at)
                .limit(20)
            ).all()
            for task_id, version in candidates:
                token = str(uuid4())
                expires_at = now + timedelta(seconds=self.lease_seconds)
                claimed = session.execute(
                    update(WorkflowTask)
                    .where(
                        WorkflowTask.id == task_id,
                        WorkflowTask.version == version,
                        WorkflowTask.status.in_(
                            [
                                TaskStatus.QUEUED.value,
                                TaskStatus.RETRY_WAIT.value,
                            ]
                        ),
                        WorkflowTask.available_at <= now,
                        WorkflowTask.attempt_count
                        < WorkflowTask.max_attempts,
                    )
                    .values(
                        status=TaskStatus.RUNNING.value,
                        stage="executing",
                        attempt_count=WorkflowTask.attempt_count + 1,
                        attempt_started_at=now,
                        lease_owner=owner,
                        lease_token=token,
                        lease_expires_at=expires_at,
                        updated_at=now,
                        version=WorkflowTask.version + 1,
                    )
                )
                if claimed.rowcount != 1:
                    session.rollback()
                    continue
                session.commit()
                row = session.get(WorkflowTask, task_id)
                assert row is not None
                lease = TaskLease(
                    task_id=row.id,
                    run_id=row.run_id,
                    lease_token=token,
                    worker_id=owner,
                    attempt_count=row.attempt_count,
                    max_attempts=row.max_attempts,
                    timeout_seconds=row.timeout_seconds,
                    payload=dict(row.payload),
                    payload_hash=row.payload_hash,
                )
                try:
                    self._validate_payload(row)
                except TaskPayloadIntegrityError as exc:
                    self._terminal_fail_in_session(
                        session,
                        lease=lease,
                        error=str(exc),
                        now=now,
                    )
                    session.commit()
                    continue
                return lease
        return None

    def renew(self, lease: TaskLease) -> bool:
        """Extend a live lease without crossing the per-attempt timeout."""

        now = self._now()
        with self.database.session_factory() as session:
            row = session.get(WorkflowTask, lease.task_id)
            if (
                row is None
                or row.status != TaskStatus.RUNNING.value
                or row.lease_token != lease.lease_token
                or row.attempt_started_at is None
            ):
                return False
            deadline = _aware(row.attempt_started_at, self.timezone) + timedelta(
                seconds=row.timeout_seconds
            )
            if now >= deadline:
                return False
            expires_at = min(
                now + timedelta(seconds=self.lease_seconds),
                deadline,
            )
            renewed = session.execute(
                update(WorkflowTask)
                .where(
                    WorkflowTask.id == lease.task_id,
                    WorkflowTask.status == TaskStatus.RUNNING.value,
                    WorkflowTask.lease_token == lease.lease_token,
                    WorkflowTask.lease_expires_at > now,
                    WorkflowTask.version == row.version,
                )
                .values(
                    lease_expires_at=expires_at,
                    updated_at=now,
                    version=WorkflowTask.version + 1,
                )
                .execution_options(synchronize_session=False)
            )
            if renewed.rowcount != 1:
                session.rollback()
                return False
            session.commit()
            return True

    def recover_expired(self) -> int:
        """Return expired work to retry_wait, or fail it after the final try."""

        now = self._now()
        recovered = 0
        with self.database.session_factory() as session:
            candidates = session.scalars(
                select(WorkflowTask)
                .where(WorkflowTask.status == TaskStatus.RUNNING.value)
                .order_by(WorkflowTask.created_at)
            ).all()
            for row in candidates:
                if not self._attempt_expired(row, now=now):
                    continue
                terminal = row.attempt_count >= row.max_attempts
                next_status = (
                    TaskStatus.FAILED if terminal else TaskStatus.RETRY_WAIT
                )
                message = (
                    "Worker lease expired or the execution attempt exceeded "
                    f"{row.timeout_seconds} seconds."
                )
                changed = session.execute(
                    update(WorkflowTask)
                    .where(
                        WorkflowTask.id == row.id,
                        WorkflowTask.status == TaskStatus.RUNNING.value,
                        WorkflowTask.version == row.version,
                        WorkflowTask.lease_token == row.lease_token,
                    )
                    .values(
                        status=next_status.value,
                        stage=next_status.value,
                        available_at=(
                            now
                            if terminal
                            else now
                            + timedelta(seconds=self.retry_delay_seconds)
                        ),
                        lease_owner=None,
                        lease_token=None,
                        lease_expires_at=None,
                        last_error=message,
                        updated_at=now,
                        completed_at=now if terminal else None,
                        version=WorkflowTask.version + 1,
                    )
                )
                if changed.rowcount != 1:
                    session.rollback()
                    continue
                self._update_run_after_attempt(
                    session,
                    run_id=row.run_id,
                    terminal=terminal,
                    error=message,
                    now=now,
                )
                session.commit()
                recovered += 1
        return recovered

    def complete(self, lease: TaskLease) -> bool:
        now = self._now()
        with self.database.session_factory() as session:
            run = session.get(WorkflowRun, lease.run_id)
            if run is None or run.status != RunStatus.COMPLETED.value:
                raise TaskQueueError(
                    f"run {lease.run_id} is not completed; refusing to complete task"
                )
            existing = session.get(WorkflowTask, lease.task_id)
            if (
                existing is not None
                and existing.run_id == lease.run_id
                and existing.status == TaskStatus.COMPLETED.value
            ):
                return True
            changed = session.execute(
                update(WorkflowTask)
                .where(
                    WorkflowTask.id == lease.task_id,
                    WorkflowTask.status == TaskStatus.RUNNING.value,
                    WorkflowTask.lease_token == lease.lease_token,
                    WorkflowTask.lease_expires_at > now,
                )
                .values(
                    status=TaskStatus.COMPLETED.value,
                    stage="completed",
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error=None,
                    updated_at=now,
                    completed_at=now,
                    version=WorkflowTask.version + 1,
                )
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                session.rollback()
                return False
            session.commit()
            return True

    def fail(self, lease: TaskLease, *, error: str) -> TaskStatus | None:
        """Record one failed attempt while preserving finite retry semantics."""

        message = _safe_error(error)
        now = self._now()
        retryable = lease.attempt_count < lease.max_attempts
        next_status = (
            TaskStatus.RETRY_WAIT if retryable else TaskStatus.FAILED
        )
        with self.database.session_factory() as session:
            changed = session.execute(
                update(WorkflowTask)
                .where(
                    WorkflowTask.id == lease.task_id,
                    WorkflowTask.status == TaskStatus.RUNNING.value,
                    WorkflowTask.lease_token == lease.lease_token,
                    WorkflowTask.lease_expires_at > now,
                )
                .values(
                    status=next_status.value,
                    stage=next_status.value,
                    available_at=(
                        now + timedelta(seconds=self.retry_delay_seconds)
                        if retryable
                        else now
                    ),
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error=message,
                    updated_at=now,
                    completed_at=None if retryable else now,
                    version=WorkflowTask.version + 1,
                )
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                session.rollback()
                return None
            self._update_run_after_attempt(
                session,
                run_id=lease.run_id,
                terminal=not retryable,
                error=message,
                now=now,
            )
            session.commit()
            return next_status

    def fail_terminal(self, lease: TaskLease, *, error: str) -> bool:
        """Fail closed for non-retryable integrity or referential errors."""

        now = self._now()
        with self.database.session_factory() as session:
            changed = self._terminal_fail_in_session(
                session,
                lease=lease,
                error=error,
                now=now,
            )
            if not changed:
                session.rollback()
                return False
            session.commit()
            return True

    def run_once(
        self,
        workflow: CommitteeWorkflow,
        *,
        worker_id: str,
    ) -> TaskExecutionResult | None:
        lease = self.claim(worker_id=worker_id)
        if lease is None:
            return None
        already_completed = False
        try:
            with self.database.session_factory() as session:
                run = session.get(WorkflowRun, lease.run_id)
                if run is None:
                    raise TaskPayloadIntegrityError(
                        f"prepared run disappeared: {lease.run_id}"
                    )
                if run.status == RunStatus.COMPLETED.value:
                    already_completed = True
                else:
                    self._validate_payload_for_run(lease.payload, run)
                    execution_manifest = _validated_execution_manifest(
                        lease.payload.get("execution_manifest")
                    )
                    if execution_manifest != workflow.execution_manifest():
                        raise TaskPayloadIntegrityError(
                            "worker execution settings do not match the "
                            "prepared task manifest"
                        )
                    from ..workflow import PreparedRun

                    prepared = PreparedRun(
                        row=run,
                        initial=dict(lease.payload["initial"]),
                        execution_manifest=execution_manifest,
                    )
        except TaskPayloadIntegrityError as exc:
            changed = self.fail_terminal(lease, error=str(exc))
            if changed:
                status = TaskStatus.FAILED
                persisted_error = _safe_error(str(exc))
            else:
                self.recover_expired()
                status, persisted_error = self._current_task_outcome(
                    lease.task_id,
                    fallback=TaskStatus.FAILED,
                )
            return TaskExecutionResult(
                task_id=lease.task_id,
                run_id=lease.run_id,
                status=status,
                attempt_count=lease.attempt_count,
                error=persisted_error,
            )
        if already_completed:
            self.complete(lease)
            return TaskExecutionResult(
                task_id=lease.task_id,
                run_id=lease.run_id,
                status=TaskStatus.COMPLETED,
                attempt_count=lease.attempt_count,
            )

        heartbeat = _LeaseHeartbeat(self, lease)
        try:
            with heartbeat:
                workflow.execute_prepared(
                    prepared,
                    raise_errors=True,
                    execution_fence=ExecutionFence(
                        task_id=lease.task_id,
                        lease_token=lease.lease_token,
                    ),
                    allow_recovery=prepared.row.status
                    == RunStatus.RUNNING.value,
                    retryable_failure=lease.attempt_count
                    < lease.max_attempts,
                    # Each finite attempt owns an isolated checkpoint stream.
                    # Recovery cleanly replays the sealed input, so
                    # a timed-out stale worker cannot corrupt a new attempt's
                    # reducer state.
                    checkpoint_thread_id=(
                        f"{lease.run_id}:task:{lease.task_id}:"
                        f"attempt:{lease.attempt_count}"
                    ),
                )
            if not self.complete(lease):
                raise StaleTaskLeaseError(
                    f"task {lease.task_id} lost its lease before completion"
                )
            return TaskExecutionResult(
                task_id=lease.task_id,
                run_id=lease.run_id,
                status=TaskStatus.COMPLETED,
                attempt_count=lease.attempt_count,
            )
        except StaleTaskLeaseError as exc:
            self.recover_expired()
            status, persisted_error = self._current_task_outcome(
                lease.task_id,
                fallback=TaskStatus.RETRY_WAIT,
            )
            return TaskExecutionResult(
                task_id=lease.task_id,
                run_id=lease.run_id,
                status=status,
                attempt_count=lease.attempt_count,
                error=persisted_error or str(exc),
            )
        except Exception as exc:
            next_status = self.fail(lease, error=str(exc))
            persisted_error = _safe_error(str(exc))
            if next_status is None:
                self.recover_expired()
                next_status, persisted_error = self._current_task_outcome(
                    lease.task_id,
                    fallback=TaskStatus.RETRY_WAIT,
                )
            assert next_status is not None
            return TaskExecutionResult(
                task_id=lease.task_id,
                run_id=lease.run_id,
                status=next_status,
                attempt_count=lease.attempt_count,
                error=persisted_error,
            )

    def run_forever(
        self,
        workflow: CommitteeWorkflow,
        *,
        worker_id: str,
        poll_interval_seconds: float,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        while True:
            result = self.run_once(workflow, worker_id=worker_id)
            if result is None:
                time.sleep(poll_interval_seconds)

    def _attempt_expired(
        self,
        row: WorkflowTask,
        *,
        now: datetime,
    ) -> bool:
        if row.lease_expires_at is None or row.attempt_started_at is None:
            return True
        lease_expired = _aware(row.lease_expires_at, self.timezone) <= now
        timed_out = (
            _aware(row.attempt_started_at, self.timezone)
            + timedelta(seconds=row.timeout_seconds)
            <= now
        )
        return lease_expired or timed_out

    def _current_task_outcome(
        self,
        task_id: str,
        *,
        fallback: TaskStatus,
    ) -> tuple[TaskStatus, str | None]:
        with self.database.session_factory() as session:
            row = session.get(WorkflowTask, task_id)
            if row is None:
                return fallback, None
            try:
                status = TaskStatus(row.status)
            except ValueError:  # pragma: no cover - protected by DB constraint
                status = fallback
            return status, row.last_error

    def _update_run_after_attempt(
        self,
        session: Session,
        *,
        run_id: str,
        terminal: bool,
        error: str,
        now: datetime,
    ) -> None:
        values: dict[str, Any] = {
            "status": (
                RunStatus.FAILED.value
                if terminal
                else RunStatus.QUEUED.value
            ),
            "error": error,
        }
        if terminal:
            values["completed_at"] = now
        else:
            values["completed_at"] = None
            values["duration_seconds"] = None
        session.execute(
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run_id,
                WorkflowRun.status != RunStatus.COMPLETED.value,
            )
            .values(**values)
        )

    def _terminal_fail_in_session(
        self,
        session: Session,
        *,
        lease: TaskLease,
        error: str,
        now: datetime,
    ) -> bool:
        message = _safe_error(error)
        changed = session.execute(
            update(WorkflowTask)
            .where(
                WorkflowTask.id == lease.task_id,
                WorkflowTask.status == TaskStatus.RUNNING.value,
                WorkflowTask.lease_token == lease.lease_token,
                WorkflowTask.lease_expires_at > now,
            )
            .values(
                status=TaskStatus.FAILED.value,
                stage=TaskStatus.FAILED.value,
                available_at=now,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                last_error=message,
                updated_at=now,
                completed_at=now,
                version=WorkflowTask.version + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            return False
        self._update_run_after_attempt(
            session,
            run_id=lease.run_id,
            terminal=True,
            error=message,
            now=now,
        )
        return True

    @staticmethod
    def _validate_replay(
        row: WorkflowTask,
        *,
        run_id: str,
        payload_hash: str,
    ) -> None:
        if row.run_id != run_id or row.payload_hash != payload_hash:
            raise TaskIdempotencyConflictError(
                "idempotency key is already bound to another frozen run"
            )

    @staticmethod
    def _validate_payload(row: WorkflowTask) -> None:
        if row.kind != TASK_KIND or _payload_hash(row.payload) != row.payload_hash:
            raise TaskPayloadIntegrityError(
                f"task {row.id} payload failed its integrity seal"
            )

    def _validate_payload_for_run(
        self,
        payload: dict[str, Any],
        run: WorkflowRun,
    ) -> None:
        if (
            payload.get("schema") != TASK_PAYLOAD_SCHEMA
            or payload.get("run_id") != run.id
            or payload.get("input_hash") != run.input_hash
        ):
            raise TaskPayloadIntegrityError(
                f"task payload no longer matches run {run.id}"
            )
        initial = payload.get("initial")
        if (
            not isinstance(initial, dict)
            or initial.get("run_id") != run.id
            or initial.get("input_hash") != run.input_hash
        ):
            raise TaskPayloadIntegrityError(
                f"task initial state no longer matches run {run.id}"
            )
        try:
            initial_as_of = datetime.fromisoformat(str(initial["as_of"]))
            initial_cutoff = datetime.fromisoformat(str(initial["data_cutoff"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise TaskPayloadIntegrityError(
                f"task dates no longer match run {run.id}"
            ) from exc
        if (
            _aware(initial_as_of, self.timezone)
            != _aware(run.as_of, self.timezone)
            or _aware(initial_cutoff, self.timezone)
            != _aware(run.data_cutoff, self.timezone)
        ):
            raise TaskPayloadIntegrityError(
                f"task dates no longer match run {run.id}"
            )

    def _now(self) -> datetime:
        return datetime.now(ZoneInfo(self.timezone))


def assert_execution_fence(
    session: Session,
    fence: ExecutionFence,
    *,
    timezone: str,
) -> WorkflowTask:
    """Reject stale workers at each database mutation boundary."""

    row = session.get(WorkflowTask, fence.task_id)
    now = datetime.now(ZoneInfo(timezone))
    if (
        row is None
        or row.status != TaskStatus.RUNNING.value
        or row.lease_token != fence.lease_token
        or row.lease_expires_at is None
        or row.attempt_started_at is None
        or _aware(row.lease_expires_at, timezone) <= now
        or _aware(row.attempt_started_at, timezone)
        + timedelta(seconds=row.timeout_seconds)
        <= now
    ):
        raise StaleTaskLeaseError(
            f"task {fence.task_id} lease is stale or expired"
        )
    return row


def fence_execution(
    session: Session,
    fence: ExecutionFence,
    *,
    run_id: str,
    timezone: str,
    stage: str,
) -> WorkflowTask:
    """Acquire a transactional write fence for a live execution lease."""

    now = datetime.now(ZoneInfo(timezone))
    changed = session.execute(
        update(WorkflowTask)
        .where(
            WorkflowTask.id == fence.task_id,
            WorkflowTask.run_id == run_id,
            WorkflowTask.status == TaskStatus.RUNNING.value,
            WorkflowTask.lease_token == fence.lease_token,
            WorkflowTask.lease_expires_at > now,
            WorkflowTask.attempt_started_at.is_not(None),
        )
        .values(stage=stage, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:
        raise StaleTaskLeaseError(
            f"task {fence.task_id} lease is stale or expired"
        )
    row = session.get(WorkflowTask, fence.task_id)
    assert row is not None
    if (
        row.attempt_started_at is None
        or _aware(row.attempt_started_at, timezone)
        + timedelta(seconds=row.timeout_seconds)
        <= now
    ):
        raise StaleTaskLeaseError(
            f"task {fence.task_id} attempt deadline has expired"
        )
    return row


def finalize_execution_fence(
    session: Session,
    fence: ExecutionFence,
    *,
    run_id: str,
    timezone: str,
) -> WorkflowTask:
    """Complete a task under the same transaction that publishes its result."""

    row = fence_execution(
        session,
        fence,
        run_id=run_id,
        timezone=timezone,
        stage="finalizing",
    )
    now = datetime.now(ZoneInfo(timezone))
    changed = session.execute(
        update(WorkflowTask)
        .where(
            WorkflowTask.id == fence.task_id,
            WorkflowTask.run_id == run_id,
            WorkflowTask.status == TaskStatus.RUNNING.value,
            WorkflowTask.lease_token == fence.lease_token,
        )
        .values(
            status=TaskStatus.COMPLETED.value,
            stage=TaskStatus.COMPLETED.value,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            last_error=None,
            updated_at=now,
            completed_at=now,
            version=WorkflowTask.version + 1,
        )
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:  # pragma: no cover - row is locked above
        raise StaleTaskLeaseError(
            f"task {fence.task_id} lease was replaced before finalization"
        )
    return row


def validate_idempotency_key(value: str) -> str:
    key = value.strip()
    if not _IDEMPOTENCY_PATTERN.fullmatch(key):
        raise ValueError(
            "idempotency key must be 1-255 safe ASCII characters "
            "(letters, digits, '.', '_', ':', '/', '+', '-')"
        )
    return key


def default_idempotency_key(
    as_of: datetime,
    *,
    market_universe_hash: str,
) -> str:
    if market_universe_hash == DEFAULT_MARKET_UNIVERSE.content_hash:
        # Preserve retries for default-universe tasks created before the
        # configurable-universe identity was introduced.
        return f"live:{as_of.isoformat()}"
    return f"live:{market_universe_hash}:{as_of.isoformat()}"


def _payload_hash(payload: dict[str, Any]) -> str:
    body = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _validated_execution_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TaskPayloadIntegrityError(
            "prepared task is missing its execution manifest"
        )
    manifest = dict(value)
    if manifest.get("schema") != EXECUTION_MANIFEST_SCHEMA:
        raise TaskPayloadIntegrityError(
            "prepared task has an unsupported execution manifest"
        )
    return manifest


def _validate_worker_id(value: str) -> str:
    worker_id = value.strip()
    if not worker_id or len(worker_id) > 120:
        raise ValueError("worker_id must contain 1-120 characters")
    return worker_id


def _aware(value: datetime, timezone: str) -> datetime:
    zone = ZoneInfo(timezone)
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def _safe_error(value: str) -> str:
    message = " ".join(value.split()).strip()
    if not message:
        message = "task execution failed without an error message"
    return message[:4000]


class _LeaseHeartbeat:
    def __init__(self, queue: PersistentTaskQueue, lease: TaskLease) -> None:
        self.queue = queue
        self.lease = lease
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self) -> _LeaseHeartbeat:
        interval = max(0.25, min(self.queue.lease_seconds / 3, 30.0))
        self.thread = threading.Thread(
            target=self._run,
            args=(interval,),
            name=f"task-heartbeat-{self.lease.task_id}",
            daemon=True,
        )
        self.thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=1)

    def _run(self, interval: float) -> None:
        while not self.stop.wait(interval):
            if not self.queue.renew(self.lease):
                self.queue.recover_expired()
                return
