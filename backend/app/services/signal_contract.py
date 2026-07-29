"""Seal, persist and verify versioned Agent signal contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent_contracts import (
    AgentSignalDraft,
    AgentSpec,
    SignalEnvelope,
    SignalEnvelopeBody,
    SignalInputBinding,
    SignalProvenance,
    SignalTarget,
    agent_spec,
    seal_signal_envelope,
    validate_signal_against_spec,
    verify_agent_spec_hash,
    verify_signal_envelope_hash,
)
from ..models import AgentSpecRecord, SignalEnvelopeRecord, WorkflowRun
from .evaluation_facade import route_signal


class SignalEnvelopeConflictError(ValueError):
    """A signal identity or source record is already bound to different bytes."""


def accept_signal_draft(
    *,
    draft: AgentSignalDraft,
    agent_id: str,
    mode: Literal["demo", "live"],
    target: SignalTarget,
    accepted_at: datetime,
    submission_deadline: datetime,
    input_binding: SignalInputBinding,
    provenance: SignalProvenance,
) -> SignalEnvelope:
    """Bind host-owned facts and seal one untrusted adapter draft.

    The active AgentSpec always comes from the host registry. Adapter output
    cannot provide its own policy, acceptance timestamp, target or provenance.
    """

    spec = agent_spec(agent_id)
    signal = seal_signal_envelope(
        SignalEnvelopeBody(
            signal_id=draft.signal_id,
            agent_id=spec.agent_id,
            agent_version=spec.agent_version,
            mode=mode,
            target=target,
            submitted_at=draft.submitted_at,
            accepted_at=accepted_at,
            submission_deadline=submission_deadline,
            input_binding=input_binding,
            participation=spec.participation,
            provenance=provenance,
            direction=draft.direction,
            probabilities=draft.probabilities,
            direction_confidence=draft.direction_confidence,
            rationale=draft.rationale,
            counter_evidence=draft.counter_evidence,
            invalidation_conditions=draft.invalidation_conditions,
            citations=draft.citations,
            blind_attestation=draft.blind_attestation,
            payload_schema=draft.payload_schema,
            source_payload=draft.source_payload,
        )
    )
    validate_signal_against_spec(signal, spec)
    return signal


def persist_agent_spec(
    session: Session,
    spec: AgentSpec,
) -> tuple[AgentSpecRecord, bool]:
    """Persist the exact spec needed to verify envelopes after future upgrades."""

    verify_agent_spec_hash(spec)
    existing = session.get(AgentSpecRecord, spec.content_hash)
    if existing is not None:
        verify_agent_spec_record(existing, expected=spec)
        return existing, False
    row = AgentSpecRecord(
        content_hash=spec.content_hash,
        schema_version=spec.schema_version,
        agent_id=spec.agent_id,
        agent_version=spec.agent_version,
        source_type=spec.source_type.value,
        participation_policy_id=spec.participation.policy_id,
        participation_policy_version=spec.participation.policy_version,
        participation_mode=spec.participation.mode.value,
        spec=spec.model_dump(mode="json"),
    )
    session.add(row)
    session.flush()
    return row, True


def persist_signal_envelope(
    session: Session,
    *,
    signal: SignalEnvelope,
    authoritative_target: SignalTarget,
    authoritative_accepted_at: datetime,
    authoritative_submission_deadline: datetime,
    authoritative_provenance: SignalProvenance,
    run_timezone: str,
    source_record_type: str | None = None,
    source_record_id: str | None = None,
) -> tuple[SignalEnvelopeRecord, bool]:
    """Persist one validated envelope without mutating its source record."""

    verify_signal_envelope_hash(signal)
    _validate_authoritative_target(signal, authoritative_target)
    _validate_authoritative_accepted_at(signal, authoritative_accepted_at)
    _validate_authoritative_deadline(
        signal,
        authoritative_submission_deadline,
        target=authoritative_target,
    )
    if signal.provenance != authoritative_provenance:
        raise ValueError(
            "SignalEnvelope provenance does not match host-bound provenance"
        )
    if (source_record_type is None) != (source_record_id is None):
        raise ValueError("source_record_type and source_record_id must be supplied together")

    existing = session.get(SignalEnvelopeRecord, signal.signal_id)
    if existing is not None:
        # Idempotent replay must resolve the archived spec by content hash
        # before consulting a mutable current registry.
        verified = verify_signal_envelope_record(existing)
        if verified.content_hash != signal.content_hash:
            raise SignalEnvelopeConflictError(
                "signal_id is already bound to different canonical content"
            )
        if (
            existing.source_record_type != source_record_type
            or existing.source_record_id != source_record_id
        ):
            raise SignalEnvelopeConflictError(
                "signal_id is already bound to a different source record"
            )
        _validate_run_binding(
            session,
            signal,
            run_timezone=run_timezone,
        )
        return existing, False

    # New admission is always governed by the host-owned active registry.
    # Archived or adapter-declared specs may verify existing history, but may
    # never self-promote a new signal into a formal lane.
    resolved_spec = agent_spec(signal.agent_id)
    validate_signal_against_spec(signal, resolved_spec)
    route = route_signal(spec=resolved_spec, signal=signal)
    _validate_run_binding(
        session,
        signal,
        run_timezone=run_timezone,
    )
    spec_record, _ = persist_agent_spec(session, resolved_spec)

    content_existing = session.scalar(
        select(SignalEnvelopeRecord).where(
            SignalEnvelopeRecord.content_hash == signal.content_hash
        )
    )
    if content_existing is not None:
        raise SignalEnvelopeConflictError(
            "canonical signal content is already bound to another signal_id"
        )
    if source_record_type is not None and source_record_id is not None:
        source_existing = session.scalar(
            select(SignalEnvelopeRecord).where(
                SignalEnvelopeRecord.source_record_type == source_record_type,
                SignalEnvelopeRecord.source_record_id == source_record_id,
            )
        )
        if source_existing is not None:
            raise SignalEnvelopeConflictError(
                "source record is already bound to another SignalEnvelope"
            )

    row = SignalEnvelopeRecord(
        id=signal.signal_id,
        schema_version=signal.schema_version,
        agent_id=signal.agent_id,
        agent_version=signal.agent_version,
        agent_spec_hash=signal.input_binding.agent_spec_hash,
        run_id=signal.input_binding.run_id,
        mode=signal.mode,
        source_type=signal.provenance.source_type.value,
        index_code=signal.target.index_code,
        horizon=signal.target.horizon.value,
        base_trade_date=signal.target.base_trade_date,
        target_date=signal.target.target_date,
        submitted_at=_utc(signal.submitted_at),
        accepted_at=_utc(signal.accepted_at),
        participation_policy_id=signal.participation.policy_id,
        participation_policy_version=signal.participation.policy_version,
        participation_mode=signal.participation.mode.value,
        routing_lane=route.lane,
        formal_aggregation=route.formal_aggregation,
        shadow_benchmark=route.shadow_benchmark,
        source_record_type=source_record_type,
        source_record_id=source_record_id,
        content_hash=signal.content_hash,
        envelope=signal.model_dump(mode="json"),
        spec_record=spec_record,
    )
    session.add(row)
    session.flush()
    return row, True


def verify_signal_envelope_record(
    row: SignalEnvelopeRecord,
    *,
    spec: AgentSpec | None = None,
) -> SignalEnvelope:
    """Re-parse canonical content and compare every indexed projection."""

    signal = SignalEnvelope.model_validate(row.envelope)
    if spec is None:
        if row.spec_record is None:
            raise ValueError("SignalEnvelope is missing its historical AgentSpec")
        resolved_spec = verify_agent_spec_record(row.spec_record)
    else:
        resolved_spec = spec
        if resolved_spec.content_hash != row.agent_spec_hash:
            raise ValueError("supplied AgentSpec does not match persisted spec hash")
    validate_signal_against_spec(signal, resolved_spec)
    route = route_signal(spec=resolved_spec, signal=signal)
    expected = {
        "id": signal.signal_id,
        "schema_version": signal.schema_version,
        "agent_id": signal.agent_id,
        "agent_version": signal.agent_version,
        "agent_spec_hash": signal.input_binding.agent_spec_hash,
        "run_id": signal.input_binding.run_id,
        "mode": signal.mode,
        "source_type": signal.provenance.source_type.value,
        "index_code": signal.target.index_code,
        "horizon": signal.target.horizon.value,
        "base_trade_date": signal.target.base_trade_date,
        "target_date": signal.target.target_date,
        "submitted_at": _utc(signal.submitted_at),
        "accepted_at": _utc(signal.accepted_at),
        "participation_policy_id": signal.participation.policy_id,
        "participation_policy_version": signal.participation.policy_version,
        "participation_mode": signal.participation.mode.value,
        "routing_lane": route.lane,
        "formal_aggregation": route.formal_aggregation,
        "shadow_benchmark": route.shadow_benchmark,
        "content_hash": signal.content_hash,
    }
    for field, value in expected.items():
        if not _projection_matches(getattr(row, field), value):
            raise ValueError(
                f"SignalEnvelope indexed field {field} does not match canonical content"
            )
    return signal


def verify_agent_spec_record(
    row: AgentSpecRecord,
    *,
    expected: AgentSpec | None = None,
) -> AgentSpec:
    spec = AgentSpec.model_validate(row.spec)
    verify_agent_spec_hash(spec)
    projection = {
        "content_hash": spec.content_hash,
        "schema_version": spec.schema_version,
        "agent_id": spec.agent_id,
        "agent_version": spec.agent_version,
        "source_type": spec.source_type.value,
        "participation_policy_id": spec.participation.policy_id,
        "participation_policy_version": spec.participation.policy_version,
        "participation_mode": spec.participation.mode.value,
    }
    for field, value in projection.items():
        if getattr(row, field) != value:
            raise ValueError(
                f"AgentSpec indexed field {field} does not match canonical content"
            )
    if expected is not None and spec != expected:
        raise SignalEnvelopeConflictError(
            "AgentSpec hash is already bound to different canonical content"
        )
    return spec


def _validate_run_binding(
    session: Session,
    signal: SignalEnvelope,
    *,
    run_timezone: str,
) -> None:
    run = session.get(WorkflowRun, signal.input_binding.run_id)
    if run is None:
        raise ValueError("SignalEnvelope run_id does not exist")
    if run.input_hash != signal.input_binding.run_input_hash:
        raise ValueError("SignalEnvelope run_input_hash does not match WorkflowRun")
    if run.mode != signal.mode:
        raise ValueError("SignalEnvelope mode does not match WorkflowRun")
    if not _same_instant(
        run.as_of,
        signal.target.as_of,
        naive_timezone=run_timezone,
    ):
        raise ValueError("SignalEnvelope as_of does not match WorkflowRun")
    if not _same_instant(
        run.data_cutoff,
        signal.target.data_cutoff,
        naive_timezone=run_timezone,
    ):
        raise ValueError("SignalEnvelope data_cutoff does not match WorkflowRun")


def _validate_authoritative_deadline(
    signal: SignalEnvelope,
    authoritative: datetime,
    *,
    target: SignalTarget,
) -> None:
    if authoritative.tzinfo is None or authoritative.utcoffset() is None:
        raise ValueError("authoritative submission deadline must include a timezone")
    declared = signal.submission_deadline
    if declared is None:  # pragma: no cover - the contract rejects this first.
        raise ValueError("SignalEnvelope is missing its submission_deadline")
    if declared.astimezone(UTC) != authoritative.astimezone(UTC):
        raise ValueError(
            "SignalEnvelope submission_deadline does not match the host deadline"
        )
    if signal.accepted_at.astimezone(UTC) >= authoritative.astimezone(UTC):
        raise ValueError("SignalEnvelope was not accepted before the host deadline")
    target_zone = target.as_of.tzinfo
    if (
        target_zone is None  # pragma: no cover - SignalTarget rejects this.
        or authoritative.astimezone(target_zone).date() >= target.target_date
    ):
        raise ValueError("host submission deadline must precede the target date")


def _validate_authoritative_accepted_at(
    signal: SignalEnvelope,
    authoritative: datetime,
) -> None:
    if authoritative.tzinfo is None or authoritative.utcoffset() is None:
        raise ValueError("authoritative accepted_at must include a timezone")
    if signal.accepted_at.astimezone(UTC) != authoritative.astimezone(UTC):
        raise ValueError(
            "SignalEnvelope accepted_at does not match the host receipt time"
        )


def _validate_authoritative_target(
    signal: SignalEnvelope,
    authoritative: SignalTarget,
) -> None:
    projected = signal.target
    for field in (
        "index_code",
        "horizon",
        "base_trade_date",
        "target_date",
    ):
        if getattr(projected, field) != getattr(authoritative, field):
            raise ValueError(
                f"SignalEnvelope target {field} does not match the host target"
            )
    for field in ("as_of", "data_cutoff"):
        received = getattr(projected, field)
        expected = getattr(authoritative, field)
        if received.astimezone(UTC) != expected.astimezone(UTC):
            raise ValueError(
                f"SignalEnvelope target {field} does not match the host target"
            )


def _same_instant(
    stored: datetime,
    canonical: datetime,
    *,
    naive_timezone: str,
) -> bool:
    if canonical.tzinfo is None or canonical.utcoffset() is None:
        return False
    if stored.tzinfo is None or stored.utcoffset() is None:
        stored = stored.replace(tzinfo=ZoneInfo(naive_timezone))
    return stored.astimezone(UTC) == canonical.astimezone(UTC)


def _projection_matches(stored, canonical) -> bool:
    # SQLite preserves the wall-clock value but drops tzinfo for DateTime
    # columns. The canonical JSON remains the authoritative timezone-bound
    # representation and is compared without inventing a different offset.
    if (
        isinstance(stored, datetime)
        and isinstance(canonical, datetime)
        and stored.tzinfo is None
        and canonical.tzinfo is not None
    ):
        return stored == canonical.replace(tzinfo=None)
    return stored == canonical


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)
