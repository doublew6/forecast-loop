"""Admission boundary for focused v2 Manual and Quant shadow signals.

The committee handoff deliberately does not know about these participants.
This module accepts an explicit, run-bound external input only after the v2
committee run has completed, seals a standard ``AgentSignalEnvelopeV2``, and
appends one scoreable D1 natural view.  It never creates ``ForecastV2`` rows or
supplies context to Strategy/CIO.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..adapters import LocalJsonQuantSignalSource
from ..agent_contracts import SignalInputBinding, SignalTarget, agent_spec
from ..config import Settings
from ..db import Database
from ..domain import USER_JUDGMENT_AGENT, Horizon
from ..models import AgentSignalV2Record, ForecastV2, ReasoningReviewV2, ResearchRunV2
from ..research_v2 import (
    AGENT_SIGNAL_SCHEMA_V2,
    CSI1000,
    CSI1000_D1_TARGET,
    DEFAULT_RESEARCH_PROGRAM_V2,
    AgentSignalDraftV2,
    AgentSignalEnvelopeV2,
    EvidenceSnapshotV2,
    ProbabilitiesV2,
    ReasoningReviewDraftBundleV2,
    SignalKindV2,
    V2Horizon,
    canonical_json,
    content_hash,
    deterministic_review_checks,
    reasoning_review_input,
    requires_human_review,
    target_date,
    threshold_for_target,
)
from .agent_tracing import TraceRecorder
from .quant_signal import QUANT_AGENT_ID, accept_quant_candidate
from .research_v2 import ResearchV2Error

MANUAL_SHADOW_INPUT_SCHEMA_V2 = "forecast-loop.manual-shadow-input/v2"
MANUAL_SHADOW_AGENT_VERSION_V2 = "0.2.0"
MANUAL_SHADOW_MODEL_NAME_V2 = "human-explicit-multiclass"
MANUAL_SHADOW_PROMPT_VERSION_V2 = "manual-shadow-d1/v2"
QUANT_SHADOW_PROMPT_FALLBACK_V2 = "quant-shadow-d1/v2"

# AgentSignalDraftV2 currently freezes a Wiki identity for every natural view.
# External Manual/Quant inputs do not consume research Wiki prose, so this
# content-addressed identity names the public admission policy itself instead
# of pretending that an arbitrary research page was consulted.
SHADOW_POLICY_ENTRY_ID_V2 = "forecast-loop-shadow-input-policy"
SHADOW_POLICY_VERSION_V2 = "2.0.0"
SHADOW_POLICY_SECTION_V2 = "d1-only-no-influence"
SHADOW_POLICY_CONTENT_HASH_V2 = content_hash(
    {
        "schema_version": "forecast-loop.shadow-input-policy/v2",
        "target_id": CSI1000_D1_TARGET,
        "natural_horizon": V2Horizon.D1,
        "decision_horizon": None,
        "generation_reason": "external_shadow",
        "participation": "shadow",
        "influence": "none",
        "forecast_projection": False,
        "missing_input_behavior": "no_signal",
    }
)


class ManualShadowInputBodyV2(BaseModel):
    """Explicit probability submission; legacy confidence is never projected."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["forecast-loop.manual-shadow-input/v2"] = MANUAL_SHADOW_INPUT_SCHEMA_V2
    submission_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$",
    )
    run_id: str = Field(min_length=1, max_length=36)
    mode: Literal["demo", "live"]
    program_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_id: Literal["csi1000-absolute-d1"] = CSI1000_D1_TARGET
    index_code: Literal["000852.SH"] = CSI1000
    horizon: Literal["D1"] = "D1"
    anchor_date: date
    target_date: date
    data_cutoff: datetime
    submitted_at: datetime
    direction: Literal["up", "neutral", "down"]
    probabilities: ProbabilitiesV2
    rationale: str = Field(min_length=20, max_length=8000)
    counter_evidence: list[str] = Field(min_length=1, max_length=20)
    invalidation_conditions: list[str] = Field(min_length=1, max_length=20)
    blind_attestation: Literal[True]

    @field_validator("data_cutoff", "submitted_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("manual shadow timestamps must be timezone-aware")
        return value

    @field_validator("rationale")
    @classmethod
    def rationale_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 20:
            raise ValueError("rationale must contain at least 20 characters")
        return value

    @field_validator("counter_evidence", "invalidation_conditions")
    @classmethod
    def reasoning_items_are_not_blank(cls, values: list[str]) -> list[str]:
        stripped = [item.strip() for item in values]
        if any(not item for item in stripped):
            raise ValueError("reasoning items may not be blank")
        return stripped

    @model_validator(mode="after")
    def direction_is_valid(self) -> ManualShadowInputBodyV2:
        probabilities = self.probabilities.as_dict()
        maximum = max(probabilities.values())
        winners = [
            label
            for label, value in probabilities.items()
            if math.isclose(value, maximum, abs_tol=1e-12)
        ]
        if len(winners) != 1 or winners[0] != self.direction:
            raise ValueError("direction must be the unique maximum-probability class")
        if self.submitted_at < self.data_cutoff:
            raise ValueError("manual shadow submission may not predate data_cutoff")
        return self


class ManualShadowInputV2(ManualShadowInputBodyV2):
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def seal_is_valid(self) -> ManualShadowInputV2:
        if self.content_hash != content_hash(self):
            raise ValueError("manual shadow input content_hash mismatch")
        return self


def seal_manual_shadow_input_v2(
    body: ManualShadowInputV2 | dict[str, Any],
) -> ManualShadowInputV2:
    raw = (
        body.model_dump(mode="json", exclude={"content_hash"})
        if isinstance(body, ManualShadowInputV2)
        else {key: value for key, value in body.items() if key != "content_hash"}
    )
    payload = ManualShadowInputBodyV2.model_validate(raw).model_dump(mode="json")
    return ManualShadowInputV2(**payload, content_hash=content_hash(payload))


def admit_manual_shadow_signal_v2(
    database: Database,
    settings: Settings,
    *,
    submission: ManualShadowInputV2,
    accepted_at: datetime | None = None,
) -> AgentSignalV2Record:
    """Append one explicit Manual D1 signal without converting legacy confidence."""

    current = _host_time(settings, accepted_at)
    with database.session_factory() as session:
        context = _load_context(session, submission.run_id, timezone=settings.timezone)
        run, snapshot, forecast = context
        _validate_submission_binding(submission, run=run, snapshot=snapshot)
        completed_at = _localized(run.completed_at, settings.timezone)
        if submission.submitted_at < completed_at:
            raise ResearchV2Error("manual shadow submission may not predate v2 completion")
        _validate_acceptance_time(
            settings,
            submitted_at=submission.submitted_at,
            accepted_at=current,
            target=d1_shadow_target(snapshot),
            run_completed_at=completed_at,
        )
        draft = AgentSignalDraftV2(
            signal_kind=SignalKindV2.NATURAL_VIEW,
            target_id=CSI1000_D1_TARGET,
            natural_horizon=V2Horizon.D1,
            decision_horizon=None,
            direction=submission.direction,
            probabilities=submission.probabilities,
            rationale=submission.rationale,
            counter_evidence=submission.counter_evidence,
            invalidation_conditions=submission.invalidation_conditions,
            evidence_item_ids=[],
            wiki_entry_id=SHADOW_POLICY_ENTRY_ID_V2,
            wiki_version=SHADOW_POLICY_VERSION_V2,
            wiki_section=SHADOW_POLICY_SECTION_V2,
            wiki_content_hash=SHADOW_POLICY_CONTENT_HASH_V2,
        )
        signal = _seal_shadow_signal(
            run=run,
            snapshot=snapshot,
            forecast=forecast,
            agent_id=USER_JUDGMENT_AGENT.id,
            agent_version=MANUAL_SHADOW_AGENT_VERSION_V2,
            model_name=MANUAL_SHADOW_MODEL_NAME_V2,
            prompt_version=MANUAL_SHADOW_PROMPT_VERSION_V2,
            source_signal_id=submission.submission_id,
            source_hash=submission.content_hash,
            draft=draft,
            created_at=current,
        )
        record = _persist_shadow_signal(session, signal, source_hash=submission.content_hash)
    _shadow_post_admission_best_effort(
        database,
        settings,
        record=record,
        source_hash=submission.content_hash,
        external_received_at=submission.submitted_at,
        accepted_at=current,
    )
    return record


def admit_quant_shadow_signal_v2(
    database: Database,
    settings: Settings,
    *,
    run_id: str,
    quant_root: Path,
    manifest_path: Path,
    accepted_at: datetime | None = None,
) -> AgentSignalV2Record:
    """Append only the exact CSI1000 D1 candidate from a verified Quant bundle.

    Callers must not invoke this function when no bundle is available.  There is
    deliberately no missing-value candidate and no W1 fallback.
    """

    current = _host_time(settings, accepted_at)
    with database.session_factory() as session:
        run, snapshot, forecast = _load_context(session, run_id, timezone=settings.timezone)
        target = d1_shadow_target(snapshot)
        completed_at = _localized(run.completed_at, settings.timezone)
        deadline = _shadow_deadline(settings, target, completed_at)
        _validate_acceptance_time(
            settings,
            submitted_at=completed_at,
            accepted_at=current,
            target=target,
            run_completed_at=completed_at,
        )
        source = LocalJsonQuantSignalSource(
            root=quant_root,
            manifest_path=manifest_path,
        )
        candidate = source.load_candidate(target=target)
        specification = agent_spec(QUANT_AGENT_ID)
        if candidate.evidence_snapshot_hash != run.snapshot_hash:
            raise ResearchV2Error("Quant bundle must bind the exact v2 Evidence Snapshot hash")
        if candidate.draft.submitted_at > _localized(run.prepared_at, settings.timezone):
            raise ResearchV2Error("Quant bundle generated_at may not be after v2 prepare time")
        accepted = accept_quant_candidate(
            candidate=candidate,
            mode=run.mode,
            target=target,
            accepted_at=current,
            submission_deadline=deadline,
            input_binding=SignalInputBinding(
                run_id=run.id,
                run_input_hash=run.input_hash,
                agent_spec_hash=specification.content_hash,
                evidence_snapshot_hash=run.snapshot_hash,
            ),
        )
        if accepted.probabilities is None or accepted.direction is None:
            raise ResearchV2Error("Quant v2 shadow signal requires complete probabilities")
        probabilities = ProbabilitiesV2(**accepted.probabilities.as_dict())
        values = probabilities.as_dict()
        maximum = max(values.values())
        winners = [
            label for label, value in values.items() if math.isclose(value, maximum, abs_tol=1e-12)
        ]
        if len(winners) != 1:
            raise ResearchV2Error("Quant v2 probabilities need a unique maximum class")
        direction = winners[0]
        draft = AgentSignalDraftV2(
            signal_kind=SignalKindV2.NATURAL_VIEW,
            target_id=CSI1000_D1_TARGET,
            natural_horizon=V2Horizon.D1,
            decision_horizon=None,
            direction=direction,
            probabilities=probabilities,
            rationale=accepted.rationale or "Verified Quant D1 shadow signal.",
            counter_evidence=list(accepted.counter_evidence),
            invalidation_conditions=list(accepted.invalidation_conditions),
            evidence_item_ids=[],
            wiki_entry_id=SHADOW_POLICY_ENTRY_ID_V2,
            wiki_version=SHADOW_POLICY_VERSION_V2,
            wiki_section=SHADOW_POLICY_SECTION_V2,
            wiki_content_hash=SHADOW_POLICY_CONTENT_HASH_V2,
        )
        provenance = accepted.provenance
        signal = _seal_shadow_signal(
            run=run,
            snapshot=snapshot,
            forecast=forecast,
            agent_id=specification.agent_id,
            agent_version=specification.agent_version,
            model_name=provenance.model_name or "verified-quant-artifact",
            prompt_version=(
                provenance.prompt_version
                or provenance.code_version
                or QUANT_SHADOW_PROMPT_FALLBACK_V2
            ),
            source_signal_id=accepted.signal_id,
            source_hash=candidate.bundle_content_hash,
            draft=draft,
            created_at=current,
        )
        record = _persist_shadow_signal(
            session,
            signal,
            source_hash=candidate.bundle_content_hash,
        )
    _shadow_post_admission_best_effort(
        database,
        settings,
        record=record,
        source_hash=candidate.bundle_content_hash,
        external_received_at=completed_at,
        accepted_at=current,
    )
    return record


def d1_shadow_target(snapshot: EvidenceSnapshotV2) -> SignalTarget:
    """Return the sole target external v2 participants are allowed to submit."""

    return SignalTarget(
        index_code=CSI1000,
        horizon=Horizon.D1,
        base_trade_date=snapshot.base_session,
        target_date=target_date(snapshot, V2Horizon.D1),
        as_of=snapshot.as_of,
        data_cutoff=snapshot.data_cutoff,
    )


def _load_context(session, run_id: str, *, timezone: str):
    run = session.get(ResearchRunV2, run_id)
    if run is None or run.status != "completed" or run.completed_at is None:
        raise ResearchV2Error("shadow signal requires a completed v2 research run")
    if run.program_hash != DEFAULT_RESEARCH_PROGRAM_V2.content_hash:
        raise ResearchV2Error("shadow signal run uses another research program")
    snapshot = EvidenceSnapshotV2.model_validate(run.snapshot)
    if (
        snapshot.content_hash != run.snapshot_hash
        or snapshot.program_hash != run.program_hash
        or run.input_hash
        != content_hash(
            {
                "schema_version": "forecast-loop.research-run/v2",
                "program_hash": run.program_hash,
                "snapshot_hash": run.snapshot_hash,
                "mode": run.mode,
            }
        )
        or run.anchor_date != snapshot.base_session
        or not _same_instant(run.as_of, snapshot.as_of, timezone)
        or not _same_instant(run.data_cutoff, snapshot.data_cutoff, timezone)
    ):
        raise ResearchV2Error("v2 run/snapshot/input seals are inconsistent")
    forecast = session.scalar(
        select(ForecastV2).where(
            ForecastV2.run_id == run.id,
            ForecastV2.target_id == CSI1000_D1_TARGET,
            ForecastV2.horizon == V2Horizon.D1.value,
        )
    )
    if forecast is None:
        raise ResearchV2Error("completed v2 run has no frozen CSI1000 D1 forecast context")
    expected_target = target_date(snapshot, V2Horizon.D1)
    if (
        forecast.program_hash != run.program_hash
        or forecast.input_hash != run.input_hash
        or forecast.anchor_date != snapshot.base_session
        or forecast.target_date != expected_target
        or not math.isclose(
            forecast.threshold,
            threshold_for_target(snapshot, CSI1000_D1_TARGET),
            rel_tol=1e-12,
            abs_tol=1e-15,
        )
    ):
        raise ResearchV2Error("v2 D1 forecast context is not bound to the run snapshot")
    return run, snapshot, forecast


def _validate_submission_binding(
    submission: ManualShadowInputV2,
    *,
    run: ResearchRunV2,
    snapshot: EvidenceSnapshotV2,
) -> None:
    expected = (
        run.id,
        run.mode,
        run.program_hash,
        run.snapshot_hash,
        run.input_hash,
        snapshot.base_session,
        target_date(snapshot, V2Horizon.D1),
        snapshot.data_cutoff,
    )
    actual = (
        submission.run_id,
        submission.mode,
        submission.program_hash,
        submission.snapshot_hash,
        submission.run_input_hash,
        submission.anchor_date,
        submission.target_date,
        submission.data_cutoff,
    )
    if actual != expected:
        raise ResearchV2Error("manual shadow input does not exactly bind the v2 run")


def _host_time(settings: Settings, value: datetime | None) -> datetime:
    timezone = ZoneInfo(settings.timezone)
    result = value or datetime.now(timezone)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ResearchV2Error("shadow acceptance time must be timezone-aware")
    return result.astimezone(timezone)


def _localized(value: datetime | None, timezone: str) -> datetime:
    if value is None:
        raise ResearchV2Error("required v2 run timestamp is missing")
    zone = ZoneInfo(timezone)
    # SQLite may return a naive projection for timezone-aware columns.  The v2
    # host always writes these values in the configured market timezone.
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def _same_instant(left: datetime, right: datetime, timezone: str) -> bool:
    return _localized(left, timezone) == _localized(right, timezone)


def _shadow_deadline(
    settings: Settings,
    target: SignalTarget,
    run_completed_at: datetime,
) -> datetime:
    timezone = ZoneInfo(settings.timezone)
    target_midnight = datetime.combine(target.target_date, time.min, timezone)
    requested = run_completed_at.astimezone(timezone) + timedelta(
        minutes=settings.v2_shadow_submission_window_minutes
    )
    return min(requested, target_midnight)


def _validate_acceptance_time(
    settings: Settings,
    *,
    submitted_at: datetime,
    accepted_at: datetime,
    target: SignalTarget,
    run_completed_at: datetime,
) -> None:
    deadline = _shadow_deadline(settings, target, run_completed_at)
    if submitted_at < target.as_of or accepted_at < submitted_at:
        raise ResearchV2Error("shadow submission time is outside the frozen run window")
    if submitted_at >= deadline or accepted_at >= deadline:
        raise ResearchV2Error("shadow D1 submission deadline has passed")


def _seal_shadow_signal(
    *,
    run: ResearchRunV2,
    snapshot: EvidenceSnapshotV2,
    forecast: ForecastV2,
    agent_id: str,
    agent_version: str,
    model_name: str,
    prompt_version: str,
    source_signal_id: str,
    source_hash: str,
    draft: AgentSignalDraftV2,
    created_at: datetime,
) -> AgentSignalEnvelopeV2:
    if (
        draft.signal_kind is not SignalKindV2.NATURAL_VIEW
        or draft.target_id != CSI1000_D1_TARGET
        or draft.natural_horizon is not V2Horizon.D1
        or draft.decision_horizon is not None
    ):
        raise ResearchV2Error("external participants may submit only CSI1000 D1 shadow")
    signal_id = content_hash(
        {
            "schema_version": "forecast-loop.shadow-signal-id/v2",
            "run_id": run.id,
            "agent_id": agent_id,
            "source_signal_id": source_signal_id,
            "source_hash": source_hash,
        }
    )
    body = {
        "schema_version": AGENT_SIGNAL_SCHEMA_V2,
        "signal_id": signal_id,
        "run_id": run.id,
        "agent_id": agent_id,
        "agent_version": agent_version,
        "model_name": model_name[:160],
        "prompt_version": prompt_version[:80],
        "target_id": CSI1000_D1_TARGET,
        "signal_kind": SignalKindV2.NATURAL_VIEW,
        "natural_horizon": V2Horizon.D1,
        "decision_horizon": None,
        "generation_reason": "external_shadow",
        "anchor_date": snapshot.base_session,
        "target_date": target_date(snapshot, V2Horizon.D1),
        "evidence_cutoff": snapshot.data_cutoff,
        "program_hash": run.program_hash,
        # Keep the standard v2 meaning: ``input_hash`` is the exact frozen run
        # input identity.  The external source seal is committed by signal_id.
        "input_hash": run.input_hash,
        "threshold": forecast.threshold,
        "baseline_probabilities": ProbabilitiesV2(**forecast.baseline_probabilities),
        "draft": draft,
        "created_at": created_at,
    }
    return AgentSignalEnvelopeV2(**body, content_hash=content_hash(body))


def _persist_shadow_signal(
    session,
    signal: AgentSignalEnvelopeV2,
    *,
    source_hash: str,
) -> AgentSignalV2Record:
    existing = session.scalar(
        select(AgentSignalV2Record).where(
            AgentSignalV2Record.run_id == signal.run_id,
            AgentSignalV2Record.agent_id == signal.agent_id,
            AgentSignalV2Record.target_id == CSI1000_D1_TARGET,
            AgentSignalV2Record.signal_kind == SignalKindV2.NATURAL_VIEW.value,
        )
    )
    if existing is not None:
        AgentSignalEnvelopeV2.model_validate(existing.envelope)
        expected_id = signal.signal_id
        if existing.id == expected_id:
            return existing
        raise ResearchV2Error(
            "this v2 run already has a different immutable shadow signal for the Agent"
        )
    record = AgentSignalV2Record(
        id=signal.signal_id,
        run_id=signal.run_id,
        schema_version=signal.schema_version,
        agent_id=signal.agent_id,
        agent_version=signal.agent_version,
        model_name=signal.model_name,
        prompt_version=signal.prompt_version,
        target_id=signal.target_id,
        signal_kind=signal.signal_kind.value,
        natural_horizon=signal.natural_horizon.value,
        decision_horizon=None,
        anchor_date=signal.anchor_date,
        target_date=signal.target_date,
        evidence_cutoff=signal.evidence_cutoff,
        program_hash=signal.program_hash,
        input_hash=signal.input_hash,
        threshold=signal.threshold,
        baseline_probabilities=(
            signal.baseline_probabilities.model_dump()
            if signal.baseline_probabilities is not None
            else None
        ),
        state_available=True,
        abstain=False,
        content_hash=signal.content_hash,
        envelope=signal.model_dump(mode="json"),
        created_at=signal.created_at,
    )
    try:
        session.add(record)
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        concurrent = session.scalar(
            select(AgentSignalV2Record).where(
                AgentSignalV2Record.run_id == signal.run_id,
                AgentSignalV2Record.agent_id == signal.agent_id,
                AgentSignalV2Record.target_id == CSI1000_D1_TARGET,
                AgentSignalV2Record.signal_kind == SignalKindV2.NATURAL_VIEW.value,
            )
        )
        if concurrent is not None:
            AgentSignalEnvelopeV2.model_validate(concurrent.envelope)
            if concurrent.id == signal.signal_id:
                return concurrent
        raise ResearchV2Error(
            f"concurrent shadow signal admission conflicted with {source_hash}"
        ) from exc
    return record


def _shadow_post_admission_best_effort(
    database: Database,
    settings: Settings,
    *,
    record: AgentSignalV2Record,
    source_hash: str,
    external_received_at: datetime,
    accepted_at: datetime,
) -> None:
    """Trace and queue blind review after the immutable business commit.

    Both operations are deliberately non-authoritative: an unavailable trace
    database or handoff filesystem must never roll back a valid external shadow
    signal.  A replay gets a new sealed trace attempt while the review task is
    rewritten idempotently from the immutable signal envelope.
    """

    try:
        _trace_shadow_admission(
            database,
            settings,
            record=record,
            source_hash=source_hash,
            external_received_at=external_received_at,
            accepted_at=accepted_at,
        )
    except Exception:
        pass
    try:
        prepare_shadow_reasoning_review_v2(settings, signal=record)
    except Exception:
        pass


def _trace_shadow_admission(
    database: Database,
    settings: Settings,
    *,
    record: AgentSignalV2Record,
    source_hash: str,
    external_received_at: datetime,
    accepted_at: datetime,
) -> None:
    subject_id = f"shadow:{record.id}"
    recorder = TraceRecorder(database, settings)
    trace_id = recorder.start_trace(
        workflow_kind="prediction",
        subject_id=subject_id,
        mode="shadow",
        input_hash=source_hash,
        target_id=record.target_id,
        horizon=record.natural_horizon,
        attributes={
            "handoff_stage": "external_receipt",
            "external_receipt": True,
            "horizon": record.natural_horizon,
            "program_hash": record.program_hash,
        },
        started_at=external_received_at,
    )
    if trace_id is None:
        return
    external = recorder.record_span_snapshot(
        workflow_kind="prediction",
        subject_id=subject_id,
        trace_id=trace_id,
        node_id="shadow.external_receipt",
        name="receive external shadow signal",
        span_kind="external",
        started_at=external_received_at,
        completed_at=accepted_at,
        agent_id=record.agent_id,
        agent_version=record.agent_version,
        model_name=record.model_name,
        prompt_version=record.prompt_version,
        input_value={"source_hash": source_hash},
        attributes={"external_receipt": True, "handoff_stage": "external_receipt"},
    )
    validator = recorder.record_span_snapshot(
        workflow_kind="prediction",
        subject_id=subject_id,
        trace_id=trace_id,
        parent_span_id=external,
        node_id="shadow.validator",
        name="validate external shadow signal",
        span_kind="validator",
        started_at=accepted_at,
        completed_at=accepted_at,
        agent_id=record.agent_id,
        agent_version=record.agent_version,
        input_value={"signal_hash": record.content_hash},
        output_value={"signal_id": record.id},
        attributes={"horizon": record.natural_horizon},
    )
    persistence = recorder.record_span_snapshot(
        workflow_kind="prediction",
        subject_id=subject_id,
        trace_id=trace_id,
        parent_span_id=validator,
        node_id="shadow.persistence",
        name="persist immutable shadow signal",
        span_kind="persistence",
        started_at=accepted_at,
        completed_at=accepted_at,
        agent_id=record.agent_id,
        agent_version=record.agent_version,
        output_value={"signal_id": record.id, "signal_hash": record.content_hash},
        attributes={"artifact_count": 1},
    )
    link_id = recorder.link_artifact(
        workflow_kind="prediction",
        subject_id=subject_id,
        trace_id=trace_id,
        span_id=persistence,
        artifact_kind="signal",
        artifact_id=record.id,
        relation="output",
        content_hash=record.content_hash,
        created_at=accepted_at,
    )
    if external is None or validator is None or persistence is None or link_id is None:
        recorder.mark_degraded(
            workflow_kind="prediction",
            subject_id=subject_id,
            trace_id=trace_id,
            note="external shadow trace is incomplete",
        )
        status: Literal["completed", "degraded"] = "degraded"
    else:
        status = "completed"
    recorder.finish_trace(
        workflow_kind="prediction",
        subject_id=subject_id,
        trace_id=trace_id,
        status=status,
        completed_at=accepted_at,
        attributes={"artifact_count": 1, "handoff_stage": "persistence"},
    )


def prepare_shadow_reasoning_review_v2(
    settings: Settings,
    *,
    signal: AgentSignalV2Record,
) -> Path:
    """Create one independently finalizable, outcome-blind review task."""

    envelope = AgentSignalEnvelopeV2.model_validate(signal.envelope)
    review = reasoning_review_input(envelope)
    checks = deterministic_review_checks(envelope.draft)
    root = (settings.handoff_root / "v2" / signal.run_id / "shadow-reasoning").resolve()
    expected_root = (settings.handoff_root / "v2" / signal.run_id).resolve()
    if root.parent != expected_root or expected_root.name != signal.run_id:
        raise ResearchV2Error("shadow reasoning task escaped its run handoff directory")
    job_dir = root / signal.id
    if job_dir.parent != root or job_dir.is_symlink():
        raise ResearchV2Error("shadow reasoning signal directory is unsafe")
    job_dir.mkdir(parents=True, exist_ok=True)
    task = {
        "schema_version": "forecast-loop.shadow-reasoning-review-task/v2",
        "run_id": signal.run_id,
        "signal_id": signal.id,
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "outcomes_included": False,
        "deterministic_checks": checks,
        "review": review.model_dump(mode="json"),
    }
    template = {
        "schema_version": "forecast-loop.reasoning-review-drafts/v2",
        "run_id": signal.run_id,
        "generated_at": datetime.now(ZoneInfo(settings.timezone)),
        "generated_by": {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
        "reviews": [
            {
                "signal_id": signal.id,
                "review_input_hash": review.review_input_hash,
                "rubric": None,
            }
        ],
    }
    _atomic_write_shadow(job_dir / "input.json", canonical_json(task))
    _atomic_write_shadow(job_dir / "drafts.template.json", canonical_json(template))
    _atomic_write_shadow(
        job_dir / "INSTRUCTIONS.md",
        (
            b"# Blind shadow reasoning review v2\n\n"
            b"Read only `input.json`; it contains no realized outcome. Copy "
            b"`drafts.template.json` to `drafts.json` and fill the sole five-axis "
            b"rubric with `gpt-5.6-sol` and `high` effort. Finalize only this "
            b"per-signal task with `research-v2 shadow-reasoning-finalize`; it "
            b"cannot change the formal aggregation.\n"
        ),
    )
    return job_dir


def finalize_shadow_reasoning_review_v2(
    database: Database,
    settings: Settings,
    *,
    job_dir: Path,
) -> ReasoningReviewV2:
    """Finalize one late shadow signal without reopening the run-wide task."""

    resolved = job_dir.resolve(strict=True)
    root = (settings.handoff_root / "v2").resolve()
    try:
        signal_uuid = resolved.name
        run_id = resolved.parent.parent.name
        UUID(run_id)
    except (ValueError, IndexError) as exc:
        raise ResearchV2Error("invalid shadow reasoning task identity") from exc
    if (
        resolved.is_symlink()
        or resolved.parent.name != "shadow-reasoning"
        or resolved.parent.parent.parent != root
    ):
        raise ResearchV2Error("shadow reasoning task escaped the v2 handoff root")
    task = _read_shadow_json(resolved / "input.json")
    if (
        task.get("outcomes_included") is not False
        or task.get("run_id") != run_id
        or task.get("signal_id") != signal_uuid
    ):
        raise ResearchV2Error("shadow reasoning task is not a bound blind task")
    bundle = ReasoningReviewDraftBundleV2.model_validate(
        _read_shadow_json(resolved / "drafts.json")
    )
    if bundle.run_id != run_id or len(bundle.reviews) != 1:
        raise ResearchV2Error("shadow reasoning draft must contain exactly one bound review")
    draft = bundle.reviews[0]
    blind_input = task.get("review")
    if not isinstance(blind_input, dict):
        raise ResearchV2Error("shadow reasoning review input is missing")
    review_input_hash = blind_input.get("review_input_hash")
    if draft.signal_id != signal_uuid or draft.review_input_hash != review_input_hash:
        raise ResearchV2Error("shadow reasoning draft input hash mismatch")
    now = datetime.now(ZoneInfo(settings.timezone))
    with database.session_factory() as session:
        signal = session.get(AgentSignalV2Record, signal_uuid)
        if signal is None or signal.run_id != run_id:
            raise ResearchV2Error("shadow reasoning signal was not found")
        existing = session.scalar(
            select(ReasoningReviewV2).where(ReasoningReviewV2.signal_id == signal_uuid)
        )
        if existing is not None:
            if existing.review_input_hash == review_input_hash:
                return existing
            raise ResearchV2Error("shadow signal has another immutable reasoning review")
        envelope = AgentSignalEnvelopeV2.model_validate(signal.envelope)
        expected = reasoning_review_input(envelope)
        if expected.review_input_hash != review_input_hash:
            raise ResearchV2Error("shadow reasoning task no longer binds the signal")
        checks = deterministic_review_checks(envelope.draft)
        if task.get("deterministic_checks") != checks:
            raise ResearchV2Error("shadow deterministic checks changed after prepare")
        required = requires_human_review(
            review_input_hash=expected.review_input_hash,
            deterministic_checks=checks,
            rubric=draft.rubric,
        )
        body = {
            "schema_version": "forecast-loop.reasoning-review/v2",
            "signal_id": signal.id,
            "review_input_hash": expected.review_input_hash,
            "deterministic_checks": checks,
            "rubric": draft.rubric.model_dump(),
            "total_score": draft.rubric.total_score,
            "human_review_required": required,
            "human_review_status": "pending" if required else "not_required",
            "created_at": now,
        }
        row = ReasoningReviewV2(id=str(uuid4()), content_hash=content_hash(body), **body)
        try:
            session.add(row)
            session.commit()
            session.refresh(row)
        except IntegrityError as exc:
            session.rollback()
            concurrent = session.scalar(
                select(ReasoningReviewV2).where(ReasoningReviewV2.signal_id == signal_uuid)
            )
            if concurrent is not None and concurrent.review_input_hash == review_input_hash:
                return concurrent
            raise ResearchV2Error(
                "concurrent shadow reasoning review finalization conflicted"
            ) from exc
    _trace_shadow_reasoning_finalize_best_effort(
        database,
        settings,
        signal=signal,
        review=row,
        input_hash=content_hash(task),
        occurred_at=now,
    )
    return row


def _trace_shadow_reasoning_finalize_best_effort(
    database: Database,
    settings: Settings,
    *,
    signal: AgentSignalV2Record,
    review: ReasoningReviewV2,
    input_hash: str,
    occurred_at: datetime,
) -> None:
    try:
        recorder = TraceRecorder(database, settings)
        subject_id = f"reasoning:{signal.id}"
        trace_id = recorder.start_trace(
            workflow_kind="agent_eval",
            subject_id=subject_id,
            mode="shadow",
            input_hash=input_hash,
            target_id=signal.target_id,
            horizon=signal.natural_horizon,
            attributes={"handoff_stage": "finalize", "artifact_count": 1},
            started_at=occurred_at,
        )
        validator = recorder.record_span_snapshot(
            workflow_kind="agent_eval",
            subject_id=subject_id,
            trace_id=trace_id,
            node_id="shadow-reasoning.validator",
            name="validate blind shadow reasoning review",
            span_kind="validator",
            started_at=occurred_at,
            completed_at=occurred_at,
            agent_id=signal.agent_id,
            input_value={"signal_id": signal.id},
            output_value={"review_hash": review.content_hash},
        )
        persistence = recorder.record_span_snapshot(
            workflow_kind="agent_eval",
            subject_id=subject_id,
            trace_id=trace_id,
            parent_span_id=validator,
            node_id="shadow-reasoning.persistence",
            name="persist blind shadow reasoning review",
            span_kind="persistence",
            started_at=occurred_at,
            completed_at=occurred_at,
            output_value={"review_id": review.id},
        )
        recorder.link_artifact(
            workflow_kind="agent_eval",
            subject_id=subject_id,
            trace_id=trace_id,
            span_id=validator,
            artifact_kind="signal",
            artifact_id=signal.id,
            relation="input",
            content_hash=signal.content_hash,
        )
        link = recorder.link_artifact(
            workflow_kind="agent_eval",
            subject_id=subject_id,
            trace_id=trace_id,
            span_id=persistence,
            artifact_kind="reasoning_review",
            artifact_id=review.id,
            relation="output",
            content_hash=review.content_hash,
        )
        recorder.finish_trace(
            workflow_kind="agent_eval",
            subject_id=subject_id,
            trace_id=trace_id,
            status="completed" if validator and persistence and link else "degraded",
            completed_at=occurred_at,
        )
    except Exception:
        pass


def _read_shadow_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ResearchV2Error(f"required shadow reasoning file is missing: {path.name}")
    size = path.stat().st_size
    if size <= 0 or size > 2 * 1024 * 1024:
        raise ResearchV2Error(f"shadow reasoning file size is invalid: {path.name}")
    try:
        payload = json.loads(path.read_bytes())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ResearchV2Error(f"shadow reasoning file is invalid: {path.name}") from exc
    if not isinstance(payload, dict):
        raise ResearchV2Error(f"shadow reasoning file must contain an object: {path.name}")
    return payload


def _atomic_write_shadow(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
