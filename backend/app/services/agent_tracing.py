"""Best-effort private tracing for Agent workflows.

Trace records are deliberately non-authoritative.  Forecast and reflection
execution must continue when local tracing or an optional OTLP exporter is
unavailable.  Only allowlisted, bounded metadata is persisted or exported.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from ..config import Settings
from ..db import Database
from ..models import AgentTrace, AgentTraceArtifactLink, AgentTraceSpan

TRACE_POLICY_VERSION = "2.0.0"
_MAX_ATTRIBUTE_ITEMS = 24
_MAX_ATTRIBUTE_TEXT = 240
_ALLOWED_ATTRIBUTES = {
    "agent_count",
    "artifact_count",
    "attempt_count",
    "case_id",
    "decision_schema_version",
    "evidence_count",
    "experiment_id",
    "forecast_count",
    "horizon",
    "handoff_stage",
    "index_count",
    "market_universe_hash",
    "mode",
    "external_receipt",
    "provider",
    "program_hash",
    "retry_count",
    "source_snapshot_hash",
    "suite_hash",
    "target_date",
    "telemetry_note",
    "wiki_reference_count",
    "evidence_reference_count",
    "workflow_version",
}

SpanKind = Literal["workflow", "agent", "llm", "validator", "persistence", "external"]
ArtifactKind = Literal[
    "signal", "forecast", "evaluation", "reasoning_review", "reflection", "bad_case"
]
ArtifactRelation = Literal["input", "output", "reused", "diagnostic"]


@dataclass(frozen=True, slots=True)
class TraceSpanHandle:
    trace_id: str
    span_id: str


@dataclass(frozen=True, slots=True)
class TraceStorageMetrics:
    database_size_bytes: int | None
    trace_storage_bytes: int | None
    trace_count: int
    span_count: int
    artifact_link_count: int
    warning: bool


@dataclass(frozen=True, slots=True)
class _OtelSetup:
    tracer: Any | None
    telemetry_complete: bool
    note: str | None = None


class TraceRecorder:
    """Persist sanitized local traces and optionally mirror spans to OpenTelemetry."""

    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        self.enabled = settings.agent_trace_enabled
        otel = _build_otel_tracer(settings)
        self._otel_tracer = otel.tracer
        self._otlp_telemetry_complete = otel.telemetry_complete
        self._otlp_telemetry_note = otel.note

    @property
    def otlp_telemetry_complete(self) -> bool:
        """Whether the optional OTLP mirror is currently usable.

        Local trace persistence is independent of this state.  The property is
        intentionally observable so health endpoints and tests do not need to
        infer exporter health from private OpenTelemetry objects.
        """

        return self._otlp_telemetry_complete

    @property
    def otlp_telemetry_note(self) -> str | None:
        return self._otlp_telemetry_note

    def start_trace(
        self,
        *,
        workflow_kind: Literal["prediction", "reflection", "agent_eval"],
        subject_id: str,
        mode: str,
        input_hash: str | None,
        attempt_number: int | None = None,
        target_id: str | None = None,
        horizon: str | None = None,
        attributes: dict[str, Any] | None = None,
        started_at: datetime | None = None,
    ) -> str | None:
        if not self.enabled:
            return None
        trace_id = secrets.token_hex(16)
        now = started_at or self._now()
        try:
            with self.database.session_factory() as session:
                subject_id = subject_id[:64]
                base = select(AgentTrace).where(
                    AgentTrace.workflow_kind == workflow_kind,
                    AgentTrace.subject_id == subject_id,
                )
                if attempt_number is not None:
                    if attempt_number < 1:
                        return None
                    existing = session.scalar(
                        base.where(AgentTrace.attempt_number == attempt_number)
                    )
                else:
                    existing = session.scalar(
                        base.where(AgentTrace.status == "running").order_by(
                            AgentTrace.attempt_number.desc(), AgentTrace.started_at.desc()
                        )
                    )
                if existing is not None:
                    return existing.id
                next_attempt = (
                    attempt_number
                    or (
                        session.scalar(
                            select(func.max(AgentTrace.attempt_number)).where(
                                AgentTrace.workflow_kind == workflow_kind,
                                AgentTrace.subject_id == subject_id,
                            )
                        )
                        or 0
                    )
                    + 1
                )
                trace_attributes = sanitize_trace_attributes(attributes or {})
                if self._otlp_telemetry_note is not None:
                    trace_attributes = {
                        **trace_attributes,
                        "telemetry_note": self._summary(self._otlp_telemetry_note),
                    }
                session.add(
                    AgentTrace(
                        id=trace_id,
                        workflow_kind=workflow_kind,
                        subject_id=subject_id,
                        attempt_number=next_attempt,
                        target_id=target_id[:120] if target_id else None,
                        horizon=horizon[:32] if horizon else None,
                        mode=mode[:24],
                        status="running",
                        started_at=now,
                        completed_at=None,
                        input_hash=_hash_or_none(input_hash),
                        trace_policy_version=TRACE_POLICY_VERSION,
                        telemetry_complete=self._otlp_telemetry_complete,
                        error_code=None,
                        error_summary=None,
                        attributes=trace_attributes,
                    )
                )
                session.commit()
            return trace_id
        except IntegrityError:
            return self.trace_id_for(
                workflow_kind,
                subject_id,
                attempt_number=attempt_number,
                running_only=attempt_number is None,
            )
        except Exception:
            return None

    def trace_id_for(
        self,
        workflow_kind: str,
        subject_id: str,
        *,
        attempt_number: int | None = None,
        running_only: bool = False,
    ) -> str | None:
        if not self.enabled:
            return None
        try:
            with self.database.session_factory() as session:
                statement = select(AgentTrace.id).where(
                    AgentTrace.workflow_kind == workflow_kind,
                    AgentTrace.subject_id == subject_id[:64],
                )
                if attempt_number is not None:
                    statement = statement.where(AgentTrace.attempt_number == attempt_number)
                if running_only:
                    statement = statement.where(AgentTrace.status == "running")
                return session.scalar(
                    statement.order_by(
                        AgentTrace.attempt_number.desc(), AgentTrace.started_at.desc()
                    )
                )
        except Exception:
            return None

    @contextmanager
    def span(
        self,
        *,
        workflow_kind: Literal["prediction", "reflection", "agent_eval"],
        subject_id: str,
        node_id: str,
        name: str,
        span_kind: SpanKind,
        parent_span_id: str | None = None,
        agent_id: str | None = None,
        agent_version: str | None = None,
        model_name: str | None = None,
        prompt_version: str | None = None,
        summary: str | None = None,
        input_value: Any | None = None,
        attributes: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> Iterator[TraceSpanHandle | None]:
        handle = self._start_span(
            workflow_kind=workflow_kind,
            subject_id=subject_id,
            node_id=node_id,
            name=name,
            span_kind=span_kind,
            parent_span_id=parent_span_id,
            agent_id=agent_id,
            agent_version=agent_version,
            model_name=model_name,
            prompt_version=prompt_version,
            summary=summary,
            input_value=input_value,
            attributes=attributes,
            trace_id=trace_id,
        )
        otel_context = _otel_span_context(
            self._otel_tracer,
            name,
            sanitize_trace_attributes(
                {
                    **(attributes or {}),
                    "workflow_kind": workflow_kind,
                    "subject_id": subject_id,
                    "agent_id": agent_id,
                    "model_name": model_name,
                }
            ),
            on_failure=lambda note: self._mark_otlp_failure(
                workflow_kind=workflow_kind,
                subject_id=subject_id,
                trace_id=handle.trace_id if handle is not None else trace_id,
                note=note,
            ),
        )
        try:
            with otel_context:
                yield handle
        except Exception as exc:
            self._finish_span(handle, error=exc)
            raise
        else:
            self._finish_span(handle)

    def finish_trace(
        self,
        *,
        workflow_kind: Literal["prediction", "reflection", "agent_eval"],
        subject_id: str,
        status: Literal["completed", "failed", "degraded"],
        error: Exception | str | None = None,
        attributes: dict[str, Any] | None = None,
        completed_at: datetime | None = None,
        trace_id: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            with self.database.session_factory() as session:
                row = self._trace_row(
                    session,
                    workflow_kind=workflow_kind,
                    subject_id=subject_id,
                    trace_id=trace_id,
                    running_only=True,
                )
                if row is None or row.status != "running":
                    return
                row.status = status
                row.completed_at = completed_at or self._now()
                if attributes:
                    row.attributes = {
                        **(row.attributes or {}),
                        **sanitize_trace_attributes(attributes),
                    }
                if error is not None:
                    row.error_code = (
                        type(error).__name__ if isinstance(error, Exception) else "error"
                    )
                    row.error_summary = self._summary(str(error))
                session.commit()
        except Exception:
            return

    def record_span_snapshot(
        self,
        *,
        workflow_kind: Literal["prediction", "reflection", "agent_eval"],
        subject_id: str,
        node_id: str,
        name: str,
        span_kind: SpanKind,
        started_at: datetime,
        completed_at: datetime,
        parent_span_id: str | None = None,
        agent_id: str | None = None,
        agent_version: str | None = None,
        model_name: str | None = None,
        prompt_version: str | None = None,
        summary: str | None = None,
        input_value: Any | None = None,
        output_value: Any | None = None,
        status: Literal["completed", "failed"] = "completed",
        error: Exception | str | None = None,
        attributes: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> str | None:
        """Persist an already-completed span from deterministic workflow receipts."""

        trace_id = trace_id or self.trace_id_for(workflow_kind, subject_id, running_only=True)
        if trace_id is None:
            return None
        span_id = secrets.token_hex(8)
        normalized_start = self._localized(started_at)
        normalized_end = self._localized(completed_at)
        if normalized_end < normalized_start:
            normalized_end = normalized_start
        try:
            with self.database.session_factory() as session:
                trace = session.get(AgentTrace, trace_id)
                if trace is None or trace.status != "running":
                    return None
                if parent_span_id is not None and not self._parent_exists(
                    session, trace_id, parent_span_id
                ):
                    return None
                existing = session.scalar(
                    select(AgentTraceSpan).where(
                        AgentTraceSpan.trace_id == trace_id,
                        AgentTraceSpan.node_id == node_id[:120],
                    )
                )
                if existing is not None:
                    return existing.span_id
                session.add(
                    AgentTraceSpan(
                        id=str(uuid4()),
                        trace_id=trace_id,
                        span_id=span_id,
                        parent_span_id=parent_span_id,
                        node_id=node_id[:120],
                        name=name[:200],
                        span_kind=span_kind,
                        status=status,
                        started_at=normalized_start,
                        completed_at=normalized_end,
                        duration_ms=max(
                            0.0,
                            (normalized_end - normalized_start).total_seconds() * 1000,
                        ),
                        agent_id=agent_id[:64] if agent_id else None,
                        agent_version=agent_version[:32] if agent_version else None,
                        model_name=model_name[:160] if model_name else None,
                        prompt_version=prompt_version[:80] if prompt_version else None,
                        input_tokens=None,
                        output_tokens=None,
                        total_tokens=None,
                        estimated_cost_usd=None,
                        input_digest=(
                            canonical_digest(input_value) if input_value is not None else None
                        ),
                        output_digest=(
                            canonical_digest(output_value) if output_value is not None else None
                        ),
                        summary=self._summary(summary) if summary else None,
                        error_code=(
                            type(error).__name__[:120]
                            if isinstance(error, Exception)
                            else "error"
                            if error
                            else None
                        ),
                        error_summary=self._summary(str(error)) if error else None,
                        attributes=sanitize_trace_attributes(attributes or {}),
                    )
                )
                session.commit()
            return span_id
        except Exception:
            self.mark_degraded(
                workflow_kind=workflow_kind,
                subject_id=subject_id,
                note=f"could not persist span snapshot {node_id}",
            )
            return None

    def link_artifact(
        self,
        *,
        workflow_kind: str,
        subject_id: str,
        artifact_kind: ArtifactKind,
        artifact_id: str,
        relation: ArtifactRelation,
        span_id: str | None = None,
        content_hash: str | None = None,
        trace_id: str | None = None,
        created_at: datetime | None = None,
    ) -> str | None:
        """Append a bounded artifact identity while an attempt is still open."""

        if not self.enabled or not artifact_id:
            return None
        trace_id = trace_id or self.trace_id_for(workflow_kind, subject_id, running_only=True)
        if trace_id is None:
            return None
        try:
            with self.database.session_factory() as session:
                trace = session.get(AgentTrace, trace_id)
                if trace is None or trace.status != "running":
                    return None
                if span_id is not None and not self._parent_exists(session, trace_id, span_id):
                    return None
                existing = session.scalar(
                    select(AgentTraceArtifactLink.id).where(
                        AgentTraceArtifactLink.trace_id == trace_id,
                        AgentTraceArtifactLink.span_id == span_id,
                        AgentTraceArtifactLink.artifact_kind == artifact_kind,
                        AgentTraceArtifactLink.artifact_id == artifact_id[:160],
                        AgentTraceArtifactLink.relation == relation,
                    )
                )
                if existing is not None:
                    return existing
                link_id = str(uuid4())
                session.add(
                    AgentTraceArtifactLink(
                        id=link_id,
                        trace_id=trace_id,
                        span_id=span_id,
                        artifact_kind=artifact_kind,
                        artifact_id=artifact_id[:160],
                        relation=relation,
                        content_hash=_hash_or_none(content_hash),
                        created_at=created_at or self._now(),
                    )
                )
                session.commit()
                return link_id
        except Exception:
            self.mark_degraded(
                workflow_kind=workflow_kind,
                subject_id=subject_id,
                trace_id=trace_id,
                note=f"could not link {artifact_kind} artifact",
            )
            return None

    def mark_degraded(
        self,
        *,
        workflow_kind: str,
        subject_id: str,
        note: str,
        trace_id: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            with self.database.session_factory() as session:
                row = self._trace_row(
                    session,
                    workflow_kind=workflow_kind,
                    subject_id=subject_id,
                    trace_id=trace_id,
                    running_only=True,
                )
                if row is None or row.status != "running":
                    return
                row.telemetry_complete = False
                row.attributes = {
                    **(row.attributes or {}),
                    "telemetry_note": self._summary(note),
                }
                session.commit()
        except Exception:
            return

    def _mark_otlp_failure(
        self,
        *,
        workflow_kind: str,
        subject_id: str,
        trace_id: str | None,
        note: str,
    ) -> None:
        """Remember exporter failure and downgrade the open local attempt."""

        self._otlp_telemetry_complete = False
        self._otlp_telemetry_note = self._summary(note)
        self.mark_degraded(
            workflow_kind=workflow_kind,
            subject_id=subject_id,
            trace_id=trace_id,
            note=note,
        )

    def storage_metrics(self) -> TraceStorageMetrics:
        """Return portable row counts plus best-effort physical database bytes."""

        database_size: int | None = None
        trace_storage: int | None = None
        trace_count = span_count = artifact_link_count = 0
        try:
            with self.database.session_factory() as session:
                trace_count = int(session.scalar(select(func.count(AgentTrace.id))) or 0)
                span_count = int(session.scalar(select(func.count(AgentTraceSpan.id))) or 0)
                artifact_link_count = int(
                    session.scalar(select(func.count(AgentTraceArtifactLink.id))) or 0
                )
                if session.bind is not None and session.bind.dialect.name == "sqlite":
                    page_count = int(session.scalar(text("PRAGMA page_count")) or 0)
                    page_size = int(session.scalar(text("PRAGMA page_size")) or 0)
                    database_size = page_count * page_size
                elif session.bind is not None and session.bind.dialect.name == "postgresql":
                    database_size = int(
                        session.scalar(text("SELECT pg_database_size(current_database())")) or 0
                    )
                    trace_storage = int(
                        session.scalar(
                            text(
                                "SELECT pg_total_relation_size('agent_traces') + "
                                "pg_total_relation_size('agent_trace_spans') + "
                                "pg_total_relation_size('agent_trace_artifact_links')"
                            )
                        )
                        or 0
                    )
        except Exception:
            pass
        monitored_size = trace_storage if trace_storage is not None else database_size
        return TraceStorageMetrics(
            database_size_bytes=database_size,
            trace_storage_bytes=trace_storage,
            trace_count=trace_count,
            span_count=span_count,
            artifact_link_count=artifact_link_count,
            warning=(
                monitored_size is not None
                and monitored_size >= self.settings.agent_trace_storage_warning_bytes
            ),
        )

    @contextmanager
    def child_span(
        self,
        *,
        parent: TraceSpanHandle,
        node_id: str,
        name: str,
        span_kind: SpanKind,
        agent_id: str | None = None,
        agent_version: str | None = None,
        model_name: str | None = None,
        prompt_version: str | None = None,
        summary: str | None = None,
        input_value: Any | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[TraceSpanHandle | None]:
        """Open a child without forcing callers to re-resolve its attempt."""

        try:
            with self.database.session_factory() as session:
                trace = session.get(AgentTrace, parent.trace_id)
                if trace is None:
                    yield None
                    return
                workflow_kind = trace.workflow_kind
                subject_id = trace.subject_id
        except Exception:
            yield None
            return
        with self.span(
            workflow_kind=workflow_kind,  # type: ignore[arg-type]
            subject_id=subject_id,
            trace_id=parent.trace_id,
            parent_span_id=parent.span_id,
            node_id=node_id,
            name=name,
            span_kind=span_kind,
            agent_id=agent_id,
            agent_version=agent_version,
            model_name=model_name,
            prompt_version=prompt_version,
            summary=summary,
            input_value=input_value,
            attributes=attributes,
        ) as handle:
            yield handle

    def _start_span(
        self,
        *,
        workflow_kind: str,
        subject_id: str,
        node_id: str,
        name: str,
        span_kind: SpanKind,
        parent_span_id: str | None,
        agent_id: str | None,
        agent_version: str | None,
        model_name: str | None,
        prompt_version: str | None,
        summary: str | None,
        input_value: Any | None,
        attributes: dict[str, Any] | None,
        trace_id: str | None,
    ) -> TraceSpanHandle | None:
        trace_id = trace_id or self.trace_id_for(workflow_kind, subject_id, running_only=True)
        if trace_id is None:
            return None
        span_id = secrets.token_hex(8)
        try:
            with self.database.session_factory() as session:
                trace = session.get(AgentTrace, trace_id)
                if trace is None or trace.status != "running":
                    return None
                if parent_span_id is not None and not self._parent_exists(
                    session, trace_id, parent_span_id
                ):
                    return None
                session.add(
                    AgentTraceSpan(
                        id=str(uuid4()),
                        trace_id=trace_id,
                        span_id=span_id,
                        parent_span_id=parent_span_id,
                        node_id=node_id[:120],
                        name=name[:200],
                        span_kind=span_kind,
                        status="running",
                        started_at=self._now(),
                        completed_at=None,
                        duration_ms=None,
                        agent_id=agent_id[:64] if agent_id else None,
                        agent_version=agent_version[:32] if agent_version else None,
                        model_name=model_name[:160] if model_name else None,
                        prompt_version=prompt_version[:80] if prompt_version else None,
                        input_tokens=None,
                        output_tokens=None,
                        total_tokens=None,
                        estimated_cost_usd=None,
                        input_digest=canonical_digest(input_value)
                        if input_value is not None
                        else None,
                        output_digest=None,
                        summary=self._summary(summary) if summary else None,
                        error_code=None,
                        error_summary=None,
                        attributes=sanitize_trace_attributes(attributes or {}),
                    )
                )
                session.commit()
            return TraceSpanHandle(trace_id=trace_id, span_id=span_id)
        except Exception:
            self.mark_degraded(
                workflow_kind=workflow_kind,
                subject_id=subject_id,
                note=f"could not persist span {node_id}",
                trace_id=trace_id,
            )
            return None

    def _finish_span(
        self,
        handle: TraceSpanHandle | None,
        *,
        output_value: Any | None = None,
        error: Exception | None = None,
    ) -> None:
        if handle is None:
            return
        try:
            completed_at = self._now()
            with self.database.session_factory() as session:
                row = session.scalar(
                    select(AgentTraceSpan).where(
                        AgentTraceSpan.trace_id == handle.trace_id,
                        AgentTraceSpan.span_id == handle.span_id,
                    )
                )
                if row is None or row.status != "running":
                    return
                row.completed_at = completed_at
                started_at = row.started_at
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=ZoneInfo(self.settings.timezone))
                row.duration_ms = max(0.0, (completed_at - started_at).total_seconds() * 1000)
                row.status = "failed" if error is not None else "completed"
                if output_value is not None:
                    row.output_digest = canonical_digest(output_value)
                if error is not None:
                    row.error_code = type(error).__name__[:120]
                    row.error_summary = self._summary(str(error))
                session.commit()
        except Exception:
            return

    @staticmethod
    def _parent_exists(session, trace_id: str, span_id: str) -> bool:
        return (
            session.scalar(
                select(AgentTraceSpan.id).where(
                    AgentTraceSpan.trace_id == trace_id,
                    AgentTraceSpan.span_id == span_id,
                )
            )
            is not None
        )

    @staticmethod
    def _trace_row(
        session,
        *,
        workflow_kind: str,
        subject_id: str,
        trace_id: str | None,
        running_only: bool,
    ) -> AgentTrace | None:
        if trace_id is not None:
            row = session.get(AgentTrace, trace_id)
            if (
                row is None
                or row.workflow_kind != workflow_kind
                or row.subject_id != subject_id[:64]
            ):
                return None
            return row
        statement = select(AgentTrace).where(
            AgentTrace.workflow_kind == workflow_kind,
            AgentTrace.subject_id == subject_id[:64],
        )
        if running_only:
            statement = statement.where(AgentTrace.status == "running")
        return session.scalar(
            statement.order_by(AgentTrace.attempt_number.desc(), AgentTrace.started_at.desc())
        )

    def _now(self) -> datetime:
        return datetime.now(ZoneInfo(self.settings.timezone))

    def _localized(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=ZoneInfo(self.settings.timezone))
        return value.astimezone(ZoneInfo(self.settings.timezone))

    def _summary(self, value: str) -> str:
        normalized = " ".join(value.split())
        return normalized[: self.settings.agent_trace_summary_max_chars]


def canonical_digest(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        encoded = str(value).encode("utf-8", errors="replace")
    return hashlib.sha256(encoded).hexdigest()


def sanitize_trace_attributes(values: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in sorted(values):
        if key not in _ALLOWED_ATTRIBUTES:
            continue
        value = values[key]
        if value is None or isinstance(value, (bool, int, float)):
            result[key] = value
        elif isinstance(value, str):
            result[key] = value[:_MAX_ATTRIBUTE_TEXT]
        elif isinstance(value, (list, tuple)):
            result[key] = [
                item if isinstance(item, (bool, int, float)) else str(item)[:80]
                for item in value[:_MAX_ATTRIBUTE_ITEMS]
            ]
    return result


def _hash_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) == 64 and all(character in "0123456789abcdef" for character in value):
        return value
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _build_otel_tracer(settings: Settings) -> _OtelSetup:
    try:
        from opentelemetry import trace
    except ImportError:
        if settings.otlp_traces_endpoint:
            return _OtelSetup(
                tracer=None,
                telemetry_complete=False,
                note="OTLP setup failed: OpenTelemetry is unavailable",
            )
        return _OtelSetup(tracer=None, telemetry_complete=True)

    if settings.otlp_traces_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = trace.get_tracer_provider()
            if not isinstance(provider, TracerProvider):
                provider = TracerProvider(
                    resource=Resource.create({"service.name": "forecast-loop"})
                )
                provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_traces_endpoint))
                )
                trace.set_tracer_provider(provider)
        except Exception as exc:
            return _OtelSetup(
                tracer=None,
                telemetry_complete=False,
                note=f"OTLP setup failed: {type(exc).__name__}",
            )
    try:
        tracer = trace.get_tracer("forecast-loop.agent-tracing", TRACE_POLICY_VERSION)
    except Exception as exc:
        return _OtelSetup(
            tracer=None,
            telemetry_complete=not bool(settings.otlp_traces_endpoint),
            note=(
                f"OTLP tracer creation failed: {type(exc).__name__}"
                if settings.otlp_traces_endpoint
                else None
            ),
        )
    return _OtelSetup(tracer=tracer, telemetry_complete=True)


@contextmanager
def _otel_span_context(
    tracer,
    name: str,
    attributes: dict[str, Any],
    *,
    on_failure,
):
    if tracer is None:
        yield None
        return
    try:
        manager = tracer.start_as_current_span(name, attributes=attributes)
        span = manager.__enter__()
    except Exception as exc:
        on_failure(f"OTLP span start failed: {type(exc).__name__}")
        yield None
        return
    try:
        yield span
    except BaseException as exc:
        try:
            manager.__exit__(type(exc), exc, exc.__traceback__)
        except Exception as close_exc:
            on_failure(f"OTLP span close failed: {type(close_exc).__name__}")
        raise
    else:
        try:
            manager.__exit__(None, None, None)
        except Exception as exc:
            on_failure(f"OTLP span close failed: {type(exc).__name__}")
