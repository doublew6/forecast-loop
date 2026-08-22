"""File-first focused v2 research workflow and deterministic evaluation.

Codex may write only ``drafts.json`` and ``reasoning/drafts.json``.  Python
owns the program, snapshot, assignment identities, hashes, scoring, persistence,
and activation lane.  The service intentionally has no HTTP mutation surface.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..config import Settings
from ..db import Database
from ..domain import AGENT_BY_ID
from ..models import (
    AgentSignalV2Record,
    AgentTrace,
    AgentTraceArtifactLink,
    ForecastEvaluationV2,
    ForecastV2,
    OutcomeObservationV2Record,
    ReasoningReviewHumanEventV2,
    ReasoningReviewV2,
    ReflectionReviewEventV2,
    ReflectionV2,
    ResearchActivationEventV2,
    ResearchRunV2,
    SignalEvaluationV2,
)
from ..research_v2 import (
    AGENT_SIGNAL_SCHEMA_V2,
    CODEX_HANDOFF_SCHEMA_V3,
    CSI300,
    CSI1000,
    CSI1000_D1_TARGET,
    CSI1000_D20_RESEARCH_TARGET,
    CSI1000_RELATIVE_W1_TARGET,
    DEFAULT_RESEARCH_PROGRAM_V2,
    REFLECTION_SCHEMA_V2,
    AgentSignalDraftV2,
    AgentSignalEnvelopeV2,
    CodexDraftBundleV3,
    CodexHandoffRequestV3,
    EvidenceSnapshotV2,
    HandoffAssignmentV3,
    OutcomeObservationV2,
    ProbabilitiesV2,
    ReasoningReviewDraftBundleV2,
    ReasoningReviewInputV2,
    ReflectionDraftV2,
    SignalKindV2,
    V2Horizon,
    canonical_json,
    classify_outcome,
    content_hash,
    deterministic_review_checks,
    multiclass_brier,
    realized_outcome,
    reasoning_review_input,
    requires_human_review,
    smoothed_baseline,
    target_date,
    threshold_for_target,
)
from .agent_tracing import TraceRecorder
from .snapshot import validate_trusted_source_url
from .wiki import FrozenWikiCatalog, WikiCatalog

MAX_JSON_BYTES = 32 * 1024 * 1024
RESEARCH_RUN_SCHEMA_V2 = "forecast-loop.research-run/v2"
FORECAST_SCHEMA_V2 = "forecast-loop.forecast/v2"
REASONING_REVIEW_SCHEMA_V2 = "forecast-loop.reasoning-review/v2"
ACTIVATION_SCHEMA_V2 = "forecast-loop.research-activation/v2"
EVALUATOR_VERSION_V2 = "2.0.0"
SCORECARD_ECE_BINS_V2 = 10

RESEARCH_AGENT_IDS = (
    "macro_policy_agent",
    "market_news_agent",
    "ai_storage_industry_agent",
)
STRATEGY_AGENT_ID = "strategy_agent"
CRITIC_AGENT_ID = "risk_critic_agent"
CIO_AGENT_ID = "cio_agent"
FROZEN_CODEX_MODEL = "gpt-5.6-sol"
FROZEN_CODEX_EFFORT = "high"


class ResearchV2Error(RuntimeError):
    pass


class ReceiptV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["forecast-loop.research-receipt/v2"] = (
        "forecast-loop.research-receipt/v2"
    )
    run_id: str
    status: Literal["completed", "failed"]
    program_hash: str
    snapshot_hash: str
    input_hash: str
    request_hash: str
    drafts_raw_hash: str
    signal_ids: list[str]
    forecast_ids: list[str]
    completed_at: datetime
    receipt_hash: str

    @model_validator(mode="after")
    def validate_hash(self) -> ReceiptV2:
        if self.receipt_hash != content_hash(self, exclude=("receipt_hash",)):
            raise ValueError("research receipt hash mismatch")
        return self


def prepare_research_run(
    database: Database,
    settings: Settings,
    *,
    snapshot_path: Path,
    mode: Literal["demo", "live"],
) -> Path:
    snapshot = _read_model(snapshot_path, EvidenceSnapshotV2)
    if mode == "live":
        _validate_live_snapshot_sources(snapshot)
    program = DEFAULT_RESEARCH_PROGRAM_V2
    input_hash = content_hash(
        {
            "schema_version": RESEARCH_RUN_SCHEMA_V2,
            "program_hash": program.content_hash,
            "snapshot_hash": snapshot.content_hash,
            "mode": mode,
        }
    )
    now = datetime.now(ZoneInfo(settings.timezone))
    with database.session_factory() as session:
        existing = session.scalars(
            select(ResearchRunV2)
            .where(
                ResearchRunV2.program_hash == program.content_hash,
                ResearchRunV2.mode == mode,
                ResearchRunV2.anchor_date == snapshot.base_session,
            )
            .order_by(ResearchRunV2.prepared_at, ResearchRunV2.id)
        ).all()
        if len(existing) > 1:
            raise ResearchV2Error(
                "multiple frozen v2 runs already exist for the same program, mode, "
                "and anchor date"
            )
        if existing:
            frozen = existing[0]
            if frozen.status == "failed":
                raise ResearchV2Error(
                    "the v2 anchor date already belongs to a failed frozen run"
                )
            return _validated_job_dir(settings, frozen.id, must_exist=True)
        row = ResearchRunV2(
            id=str(uuid4()),
            schema_version=RESEARCH_RUN_SCHEMA_V2,
            program_hash=program.content_hash,
            snapshot_hash=snapshot.content_hash,
            input_hash=input_hash,
            request_hash=None,
            mode=mode,
            status="awaiting_draft",
            anchor_date=snapshot.base_session,
            as_of=snapshot.as_of,
            data_cutoff=snapshot.data_cutoff,
            prepared_at=now,
            completed_at=None,
            error=None,
            program=program.model_dump(mode="json"),
            snapshot=snapshot.model_dump(mode="json"),
            receipt={},
        )
        session.add(row)
        session.commit()
        run_id = row.id

    recorder = TraceRecorder(database, settings)
    trace_id = recorder.start_trace(
        workflow_kind="prediction",
        subject_id=run_id,
        mode=mode,
        input_hash=input_hash,
        target_id=None,
        horizon=None,
        attributes={
            "program_hash": program.content_hash,
            "source_snapshot_hash": snapshot.content_hash,
            "handoff_stage": "prepare",
        },
        started_at=now,
    )
    prepare_span = _trace_span_snapshot(
        recorder,
        trace_id=trace_id,
        workflow_kind="prediction",
        subject_id=run_id,
        node_id="run.prepare",
        name="prepare frozen v2 handoff",
        span_kind="workflow",
        started_at=now,
        completed_at=datetime.now(ZoneInfo(settings.timezone)),
        input_value={"program_hash": program.content_hash, "snapshot_hash": snapshot.content_hash},
        attributes={"handoff_stage": "prepare", "evidence_count": len(snapshot.items)},
    )

    wiki = WikiCatalog.from_settings(settings).freeze(
        allow_demo_fallback=mode == "demo",
        cutoff=snapshot.data_cutoff if mode == "live" else None,
    )
    latest_signals = _latest_natural_views(database, mode=mode)
    context_signals = {
        identity: row
        for identity, row in latest_signals.items()
        if row.target_date > snapshot.base_session
        and row.agent_id in {"macro_policy_agent", "ai_storage_industry_agent"}
    }
    for signal in context_signals.values():
        _trace_link(
            recorder,
            trace_id=trace_id,
            workflow_kind="prediction",
            subject_id=run_id,
            span_id=prepare_span,
            artifact_kind="signal",
            artifact_id=signal.id,
            relation="reused",
            content_hash=signal.content_hash,
        )
    baselines = _frozen_baselines(database, snapshot.data_cutoff, mode=mode)
    assignments = _build_assignments(
        database,
        snapshot,
        wiki,
        context_signals=context_signals,
        latest_signals=latest_signals,
        baselines=baselines,
        mode=mode,
    )
    request_body = {
        "run_id": run_id,
        "program": program,
        "snapshot": snapshot,
        "frozen_wiki": wiki.snapshot(),
        "context_signals": [
            AgentSignalEnvelopeV2.model_validate(item.envelope)
            for item in sorted(context_signals.values(), key=lambda item: item.id)
        ],
        "prepared_at": now,
        "input_hash": input_hash,
        "trace_id": trace_id,
        "assignments": assignments,
    }
    normalized_request = CodexHandoffRequestV3.model_construct(
        **request_body,
        request_hash="0" * 64,
    )
    request = CodexHandoffRequestV3(
        **request_body,
        request_hash=content_hash(normalized_request, exclude=("request_hash",)),
    )
    job_dir = _validated_job_dir(settings, run_id, must_exist=False)
    job_dir.mkdir(parents=True, exist_ok=False)
    (job_dir / "reasoning").mkdir()
    _atomic_write(job_dir / "input.json", canonical_json(request))
    _atomic_write(
        job_dir / "drafts.template.json",
        canonical_json(
            {
                "schema_version": CODEX_HANDOFF_SCHEMA_V3,
                "run_id": run_id,
                "request_hash": request.request_hash,
                "generated_at": now,
                "generated_by": {
                    "surface": "codex",
                    "model": FROZEN_CODEX_MODEL,
                    "reasoning_effort": FROZEN_CODEX_EFFORT,
                },
                "drafts": [
                    {"assignment_id": item.assignment_id, "draft": None}
                    for item in assignments
                    if item.producer == "codex"
                ],
            }
        ),
    )
    _atomic_write(
        job_dir / "INSTRUCTIONS.md",
        _prediction_instructions(request).encode("utf-8"),
    )
    with database.session_factory() as session:
        row = session.get(ResearchRunV2, run_id)
        assert row is not None
        row.request_hash = request.request_hash
        session.commit()
    _trace_span_snapshot(
        recorder,
        trace_id=trace_id,
        workflow_kind="prediction",
        subject_id=run_id,
        node_id="run.awaiting_external_draft",
        name="external Codex draft receipt pending",
        span_kind="external",
        parent_span_id=prepare_span,
        started_at=now,
        completed_at=datetime.now(ZoneInfo(settings.timezone)),
        attributes={"handoff_stage": "prepare", "external_receipt": True},
    )
    return job_dir


def finalize_research_run(
    database: Database,
    settings: Settings,
    *,
    job_dir: Path,
    now: datetime | None = None,
) -> ResearchRunV2:
    """Finalize one immutable attempt and close failed telemetry attempts.

    ``now`` is a test seam only; production callers always use the host clock.
    """

    try:
        return _finalize_research_run_attempt(
            database,
            settings,
            job_dir=job_dir,
            now=now,
        )
    except Exception as exc:
        _close_failed_finalize_trace(database, settings, job_dir=job_dir, error=exc)
        raise


def _finalize_research_run_attempt(
    database: Database,
    settings: Settings,
    *,
    job_dir: Path,
    now: datetime | None,
) -> ResearchRunV2:
    job_dir = _validated_explicit_job_dir(settings, job_dir)
    drafts_path = job_dir / "drafts.json"
    raw_drafts = _safe_read(drafts_path)
    request, bundle = validate_research_draft_bundle(
        settings,
        job_dir=job_dir,
        raw_drafts=raw_drafts,
    )
    with database.session_factory() as session:
        persisted_run = session.get(ResearchRunV2, request.run_id)
        if persisted_run is None:
            raise ResearchV2Error("research run does not exist")
        if persisted_run.status == "completed":
            return _recover_completed_research_run(
                database,
                settings,
                job_dir=job_dir,
                request=request,
                raw_drafts=raw_drafts,
                run=persisted_run,
            )
    accepted_at = now or datetime.now(ZoneInfo(settings.timezone))
    if accepted_at.tzinfo is None or accepted_at.utcoffset() is None:
        raise ResearchV2Error("finalize host time must be timezone-aware")
    accepted_at = accepted_at.astimezone(ZoneInfo(settings.timezone))
    deadline = _prediction_finalize_deadline(request, settings)
    if accepted_at >= deadline:
        raise ResearchV2Error(
            "research finalize cutoff has passed; no forecast may be accepted on or "
            "after the earliest target session"
        )
    by_assignment = {item.assignment_id: item for item in bundle.drafts}
    codex_assignments = [
        item for item in request.assignments if item.producer == "codex"
    ]
    assignments = {item.assignment_id: item for item in request.assignments}

    now = accepted_at
    recorder = TraceRecorder(database, settings)
    with database.session_factory() as trace_session:
        trace_run = trace_session.get(ResearchRunV2, request.run_id)
        trace_mode = trace_run.mode if trace_run is not None else "live"
    trace_id = recorder.trace_id_for(
        "prediction", request.run_id, running_only=True
    ) or recorder.start_trace(
        workflow_kind="prediction",
        subject_id=request.run_id,
        mode=trace_mode,
        input_hash=request.input_hash,
        target_id=None,
        horizon=None,
    )
    external_span = _trace_span_snapshot(
        recorder,
        trace_id=trace_id,
        workflow_kind="prediction",
        subject_id=request.run_id,
        node_id="run.external_draft_receipt",
        name="external Codex draft receipt",
        span_kind="external",
        started_at=bundle.generated_at,
        completed_at=now,
        output_value={"draft_hash": __import__("hashlib").sha256(raw_drafts).hexdigest()},
        attributes={"handoff_stage": "external_receipt", "external_receipt": True},
    )
    with database.session_factory() as session:
        run = session.get(ResearchRunV2, request.run_id)
        if run is None:
            raise ResearchV2Error("research run does not exist")
        if run.status == "completed":
            return run
        if run.status != "awaiting_draft":
            raise ResearchV2Error(f"research run cannot finalize from {run.status}")
        if (
            run.input_hash != request.input_hash
            or run.request_hash != request.request_hash
            or run.snapshot_hash != request.snapshot.content_hash
            or run.program_hash != request.program.content_hash
        ):
            raise ResearchV2Error("database seals no longer match the handoff request")
        signals: dict[str, AgentSignalV2Record] = {}
        for assignment in codex_assignments:
            record = by_assignment[assignment.assignment_id]
            signal = _seal_signal(request, assignment, record.draft, created_at=now)
            row = _signal_record(signal)
            session.add(row)
            signals[assignment.assignment_id] = row
        session.flush()

        for target in request.program.decision_targets:
            if not _request_has_decision_target(request, target.target_id):
                continue
            strategy_id = _assignment_id(
                STRATEGY_AGENT_ID,
                SignalKindV2.STRATEGY_FORECAST,
                target.target_id,
                target.horizon,
                target.horizon,
            )
            critic_id = _assignment_id(
                CRITIC_AGENT_ID,
                SignalKindV2.RISK_CRITIQUE,
                target.target_id,
                target.horizon,
                target.horizon,
            )
            cio_id = _assignment_id(
                CIO_AGENT_ID,
                SignalKindV2.DECISION_FORECAST,
                target.target_id,
                target.horizon,
                target.horizon,
            )
            cio_assignment = assignments[cio_id]
            strategy = AgentSignalEnvelopeV2.model_validate(signals[strategy_id].envelope)
            critic = AgentSignalEnvelopeV2.model_validate(signals[critic_id].envelope)
            cio_draft = _deterministic_cio_draft(strategy, critic, cio_assignment)
            cio_signal = _seal_signal(
                request,
                cio_assignment,
                cio_draft,
                created_at=now,
            )
            cio_row = _signal_record(cio_signal)
            session.add(cio_row)
            signals[cio_id] = cio_row
        session.flush()

        forecast_rows: list[ForecastV2] = []
        for target in request.program.decision_targets:
            if not _request_has_decision_target(request, target.target_id):
                continue
            assignment_id = _assignment_id(
                CIO_AGENT_ID,
                SignalKindV2.DECISION_FORECAST,
                target.target_id,
                target.horizon,
                target.horizon,
            )
            source = signals[assignment_id]
            envelope = AgentSignalEnvelopeV2.model_validate(source.envelope)
            assert envelope.draft.probabilities is not None
            forecast_body = {
                "schema_version": FORECAST_SCHEMA_V2,
                "run_id": run.id,
                "source_signal_id": source.id,
                "program_hash": request.program.content_hash,
                "target_id": target.target_id,
                "horizon": target.horizon.value,
                "configured_lane": target.lane,
                # No activation event exists at launch, so both targets remain shadow.
                "effective_lane": _effective_lane(
                    session, target.target_id, target.lane, mode=run.mode
                ),
                "anchor_date": request.snapshot.base_session,
                "target_date": target_date(request.snapshot, target.horizon),
                "probabilities": envelope.draft.probabilities.model_dump(),
                "threshold": envelope.threshold,
                "baseline_probabilities": envelope.baseline_probabilities.model_dump()
                if envelope.baseline_probabilities
                else ProbabilitiesV2(up=1 / 3, neutral=1 / 3, down=1 / 3).model_dump(),
                "rationale": envelope.draft.rationale,
                "counter_evidence": envelope.draft.counter_evidence,
                "invalidation_conditions": envelope.draft.invalidation_conditions,
                "input_hash": request.input_hash,
                "created_at": now,
            }
            forecast_hash = content_hash(forecast_body)
            probabilities = forecast_body.pop("probabilities")
            row = ForecastV2(
                id=str(uuid4()),
                content_hash=forecast_hash,
                probability_up=probabilities["up"],
                probability_neutral=probabilities["neutral"],
                probability_down=probabilities["down"],
                **forecast_body,
            )
            session.add(row)
            forecast_rows.append(row)
        session.flush()
        run.status = "completed"
        run.completed_at = now
        run.error = None
        receipt_body = {
            "schema_version": "forecast-loop.research-receipt/v2",
            "run_id": run.id,
            "status": "completed",
            "program_hash": run.program_hash,
            "snapshot_hash": run.snapshot_hash,
            "input_hash": run.input_hash,
            "request_hash": request.request_hash,
            "drafts_raw_hash": __import__("hashlib").sha256(raw_drafts).hexdigest(),
            "signal_ids": sorted(item.id for item in signals.values()),
            "forecast_ids": sorted(item.id for item in forecast_rows),
            "completed_at": now,
        }
        receipt = ReceiptV2(
            **receipt_body,
            receipt_hash=content_hash(receipt_body, exclude=("receipt_hash",)),
        )
        run.receipt = receipt.model_dump(mode="json")
        session.commit()
        session.refresh(run)
    _atomic_write(job_dir / "receipt.json", canonical_json(receipt))
    _record_finalized_research_trace(
        recorder,
        trace_id=trace_id,
        request=request,
        assignments=assignments,
        codex_assignments=codex_assignments,
        signals=signals,
        forecasts=forecast_rows,
        external_span=external_span,
        started_at=bundle.generated_at,
        completed_at=now,
    )
    reasoning_error = _prepare_reasoning_review_best_effort(
        database,
        settings,
        run_id=request.run_id,
        job_dir=job_dir,
    )
    recorder.finish_trace(
        workflow_kind="prediction",
        subject_id=request.run_id,
        trace_id=trace_id,
        status="degraded" if reasoning_error is not None else "completed",
        error=reasoning_error,
        attributes={
            "forecast_count": len(forecast_rows),
            "artifact_count": len(signals) + len(forecast_rows),
            "handoff_stage": "finalize",
        },
    )
    return run


def prepare_reasoning_review(
    database: Database,
    settings: Settings,
    *,
    run_id: str,
    job_dir: Path | None = None,
) -> Path:
    job_dir = job_dir or _validated_job_dir(settings, run_id, must_exist=True)
    with database.session_factory() as session:
        run = session.scalar(
            select(ResearchRunV2)
            .options(selectinload(ResearchRunV2.signals))
            .where(ResearchRunV2.id == run_id)
        )
        if run is None or run.status != "completed":
            raise ResearchV2Error("reasoning review requires a completed v2 run")
        inputs = [
            reasoning_review_input(AgentSignalEnvelopeV2.model_validate(item.envelope))
            for item in sorted(run.signals, key=lambda item: item.id)
        ]
    review_dir = job_dir / "reasoning"
    review_dir.mkdir(exist_ok=True)
    _atomic_write(
        review_dir / "input.json",
        canonical_json(
            {
                "schema_version": "forecast-loop.reasoning-review-task/v2",
                "run_id": run_id,
                "model": "gpt-5.6-sol",
                "reasoning_effort": "high",
                "outcomes_included": False,
                "reviews": [item.model_dump(mode="json") for item in inputs],
            }
        ),
    )
    _atomic_write(
        review_dir / "drafts.template.json",
        canonical_json(
            {
                "schema_version": "forecast-loop.reasoning-review-drafts/v2",
                "run_id": run_id,
                "generated_at": datetime.now(ZoneInfo(settings.timezone)),
                "generated_by": {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
                "reviews": [
                    {
                        "signal_id": item.signal_id,
                        "review_input_hash": item.review_input_hash,
                        "rubric": None,
                    }
                    for item in inputs
                ],
            }
        ),
    )
    _atomic_write(review_dir / "INSTRUCTIONS.md", _reasoning_instructions().encode("utf-8"))
    return review_dir


def finalize_reasoning_review(
    database: Database,
    settings: Settings,
    *,
    job_dir: Path,
) -> list[ReasoningReviewV2]:
    job_dir = _validated_explicit_job_dir(settings, job_dir)
    review_dir = job_dir / "reasoning"
    task = _safe_read_json(review_dir / "input.json")
    if task.get("outcomes_included") is not False:
        raise ResearchV2Error("reasoning review task is not blind")
    bundle = _read_model(review_dir / "drafts.json", ReasoningReviewDraftBundleV2)
    run_id = task.get("run_id")
    if bundle.run_id != run_id:
        raise ResearchV2Error("reasoning drafts do not bind the review task")
    input_by_signal = {
        item.signal_id: item
        for item in (
            ReasoningReviewInputV2.model_validate(value) for value in task.get("reviews", [])
        )
    }
    if {item.signal_id for item in bundle.reviews} != set(input_by_signal):
        raise ResearchV2Error("reasoning drafts must cover every blind review input")
    now = datetime.now(ZoneInfo(settings.timezone))
    rows: list[ReasoningReviewV2] = []
    trace_mode = "demo"
    signal_artifacts: list[tuple[str, str]] = []
    with database.session_factory() as session:
        run = session.scalar(
            select(ResearchRunV2).options(selectinload(ResearchRunV2.signals)).where(
                ResearchRunV2.id == run_id
            )
        )
        if run is None:
            raise ResearchV2Error("research run was not found")
        trace_mode = run.mode
        signals = {item.id: item for item in run.signals}
        existing_reviews = {
            item.signal_id: item
            for signal in run.signals
            for item in signal.reasoning_reviews
        }
        if existing_reviews:
            if set(existing_reviews) == set(input_by_signal):
                return [existing_reviews[key] for key in sorted(existing_reviews)]
            raise ResearchV2Error("reasoning review set is only partially finalized")
        for draft in bundle.reviews:
            review_input = input_by_signal[draft.signal_id]
            if review_input.review_input_hash != draft.review_input_hash:
                raise ResearchV2Error("reasoning review input hash mismatch")
            signal = signals.get(draft.signal_id)
            if signal is None:
                raise ResearchV2Error("reasoning review references another run")
            checks = deterministic_review_checks(
                AgentSignalEnvelopeV2.model_validate(signal.envelope).draft
            )
            required = requires_human_review(
                review_input_hash=draft.review_input_hash,
                deterministic_checks=checks,
                rubric=draft.rubric,
            )
            body = {
                "schema_version": REASONING_REVIEW_SCHEMA_V2,
                "signal_id": signal.id,
                "review_input_hash": draft.review_input_hash,
                "deterministic_checks": checks,
                "rubric": draft.rubric.model_dump(),
                "total_score": draft.rubric.total_score,
                "human_review_required": required,
                "human_review_status": "pending" if required else "not_required",
                "created_at": now,
            }
            row = ReasoningReviewV2(
                id=str(uuid4()),
                content_hash=content_hash(body),
                **body,
            )
            session.add(row)
            rows.append(row)
        session.commit()
        for row in rows:
            session.refresh(row)
        signal_artifacts = [
            (signals[row.signal_id].id, signals[row.signal_id].content_hash)
            for row in rows
        ]
    _trace_reasoning_review_finalize(
        database,
        settings,
        run_id=str(run_id),
        mode=trace_mode,
        input_hash=content_hash(task),
        rows=rows,
        signal_artifacts=signal_artifacts,
        occurred_at=now,
    )
    return rows


def review_reasoning(
    database: Database,
    settings: Settings,
    *,
    review_id: str,
    decision: Literal["approved", "rejected"],
    reviewer: str,
    notes: str = "",
) -> ReasoningReviewV2:
    now = datetime.now(ZoneInfo(settings.timezone))
    with database.session_factory() as session:
        row = session.scalar(
            select(ReasoningReviewV2)
            .options(selectinload(ReasoningReviewV2.human_events))
            .where(ReasoningReviewV2.id == review_id)
        )
        if row is None:
            raise ResearchV2Error("reasoning review was not found")
        if not row.human_review_required:
            raise ResearchV2Error("reasoning review does not require a human decision")
        if row.human_events:
            event = row.human_events[0]
            if event.decision == decision and event.reviewer == reviewer and event.notes == notes:
                return row
            raise ResearchV2Error("reasoning review already has an immutable human decision")
        body = {
            "review_id": row.id,
            "decision": decision,
            "reviewer": reviewer,
            "notes": notes,
            "occurred_at": now,
        }
        session.add(
            ReasoningReviewHumanEventV2(
                id=str(uuid4()),
                content_hash=content_hash(body),
                **body,
            )
        )
        session.commit()
        return row


def evaluate_research_target(
    database: Database,
    settings: Settings,
    *,
    observation_path: Path,
) -> list[SignalEvaluationV2]:
    observation = _read_model(observation_path, OutcomeObservationV2)
    if observation.mode == "live":
        _validate_live_outcome_sources(observation)
    timezone = ZoneInfo(settings.timezone)
    observed_at = observation.observed_at.astimezone(timezone)
    if observed_at < datetime.combine(observation.target_date, time(15, 0), timezone):
        raise ResearchV2Error("outcome was observed before the target session closed")
    created_artifacts = False
    with database.session_factory() as session:
        signal_rows = session.scalars(
            select(AgentSignalV2Record)
            .join(ResearchRunV2, ResearchRunV2.id == AgentSignalV2Record.run_id)
            .where(
                AgentSignalV2Record.program_hash == observation.program_hash,
                AgentSignalV2Record.target_id == observation.target_id,
                AgentSignalV2Record.anchor_date == observation.anchor_date,
                AgentSignalV2Record.target_date == observation.target_date,
                AgentSignalV2Record.signal_kind.in_(
                    [
                        SignalKindV2.NATURAL_VIEW.value,
                        SignalKindV2.STRATEGY_FORECAST.value,
                        SignalKindV2.DECISION_FORECAST.value,
                    ]
                ),
                ResearchRunV2.mode == observation.mode,
            )
        ).all()
        if not signal_rows:
            raise ResearchV2Error("no scoreable v2 signal matches the outcome observation")
        thresholds = {item.threshold for item in signal_rows}
        if len(thresholds) != 1 or None in thresholds:
            raise ResearchV2Error("matching signals disagree on the frozen neutral threshold")
        threshold = float(next(iter(thresholds)))
        actual_value = realized_outcome(observation)
        actual_label = classify_outcome(actual_value, threshold)
        existing_observation = session.scalar(
            select(OutcomeObservationV2Record).where(
                OutcomeObservationV2Record.program_hash == observation.program_hash,
                OutcomeObservationV2Record.target_id == observation.target_id,
                OutcomeObservationV2Record.anchor_date == observation.anchor_date,
                OutcomeObservationV2Record.target_date == observation.target_date,
                OutcomeObservationV2Record.mode == observation.mode,
            )
        )
        if (
            existing_observation is not None
            and existing_observation.content_hash != observation.content_hash
        ):
            raise ResearchV2Error("a different immutable outcome already exists for this episode")
        if existing_observation is None:
            created_artifacts = True
            existing_observation = OutcomeObservationV2Record(
                id=str(uuid4()),
                schema_version=observation.schema_version,
                program_hash=observation.program_hash,
                mode=observation.mode,
                target_id=observation.target_id,
                anchor_date=observation.anchor_date,
                target_date=observation.target_date,
                actual_value=actual_value,
                actual_label=actual_label,
                threshold=threshold,
                observed_at=observation.observed_at,
                content_hash=observation.content_hash,
                observation=observation.model_dump(mode="json"),
            )
            session.add(existing_observation)
            session.flush()
        signal_ids = [item.id for item in signal_rows]
        forecasts = session.scalars(
            select(ForecastV2)
            .options(selectinload(ForecastV2.evaluation))
            .where(ForecastV2.source_signal_id.in_(signal_ids))
        ).all()
        now = datetime.now(ZoneInfo(settings.timezone))
        rows: list[SignalEvaluationV2] = []
        for signal in signal_rows:
            existing = session.scalar(
                select(SignalEvaluationV2).where(SignalEvaluationV2.signal_id == signal.id)
            )
            if existing is not None:
                rows.append(existing)
                continue
            envelope = AgentSignalEnvelopeV2.model_validate(signal.envelope)
            probabilities = envelope.draft.probabilities
            if probabilities is None:
                continue
            baseline = envelope.baseline_probabilities or ProbabilitiesV2(
                up=1 / 3, neutral=1 / 3, down=1 / 3
            )
            direction_correct = envelope.draft.direction == actual_label
            body = {
                "signal_id": signal.id,
                "observation_id": existing_observation.id,
                "actual_label": actual_label,
                "brier_score": multiclass_brier(probabilities, actual_label),
                "baseline_brier_score": multiclass_brier(baseline, actual_label),
                "direction_correct": direction_correct,
                "evaluator_version": EVALUATOR_VERSION_V2,
                "evaluated_at": now,
            }
            row = SignalEvaluationV2(
                id=str(uuid4()),
                content_hash=content_hash(body),
                **body,
            )
            session.add(row)
            rows.append(row)
            created_artifacts = True
        session.flush()
        by_signal = {row.signal_id: row for row in rows}
        forecast_evaluation_rows: list[ForecastEvaluationV2] = []
        for forecast in forecasts:
            if forecast.evaluation is not None:
                forecast_evaluation_rows.append(forecast.evaluation)
                continue
            signal_eval = by_signal.get(forecast.source_signal_id)
            if signal_eval is None:
                raise ResearchV2Error("decision signal was not evaluated")
            body = {
                "forecast_id": forecast.id,
                "signal_evaluation_id": signal_eval.id,
                "actual_value": actual_value,
                "actual_label": actual_label,
                "brier_score": signal_eval.brier_score,
                "baseline_brier_score": signal_eval.baseline_brier_score,
                "direction_correct": signal_eval.direction_correct,
                "evaluated_at": now,
            }
            forecast_evaluation = ForecastEvaluationV2(
                id=str(uuid4()),
                content_hash=content_hash(body),
                **body,
            )
            session.add(forecast_evaluation)
            forecast_evaluation_rows.append(forecast_evaluation)
            created_artifacts = True
        session.commit()
    if created_artifacts:
        _trace_outcome_evaluation(
            database,
            settings,
            observation=observation,
            observation_id=existing_observation.id,
            rows=rows,
            forecast_evaluations=forecast_evaluation_rows,
            occurred_at=now,
        )
    return rows


def create_reflection_v2(
    database: Database,
    settings: Settings,
    *,
    draft: ReflectionDraftV2,
) -> ReflectionV2:
    now = datetime.now(ZoneInfo(settings.timezone))
    with database.session_factory() as session:
        forecast = session.scalar(
            select(ForecastV2)
            .options(selectinload(ForecastV2.evaluation), selectinload(ForecastV2.run))
            .where(ForecastV2.id == draft.forecast_id)
        )
        if forecast is None or forecast.evaluation is None:
            raise ResearchV2Error("v2 reflection requires an evaluated forecast")
        evaluation = forecast.evaluation
        expected_identity = {
            "forecast_id": forecast.id,
            "forecast_hash": forecast.content_hash,
            "evaluation_id": evaluation.id,
            "evaluation_hash": evaluation.content_hash,
            "target_id": forecast.target_id,
            "anchor_date": forecast.anchor_date,
            "target_date": forecast.target_date,
            "actual_label": evaluation.actual_label,
        }
        supplied_identity = {
            "forecast_id": draft.forecast_id,
            "forecast_hash": draft.forecast_hash,
            "evaluation_id": draft.evaluation_id,
            "evaluation_hash": draft.evaluation_hash,
            "target_id": draft.target_id,
            "anchor_date": draft.anchor_date,
            "target_date": draft.target_date,
            "actual_label": draft.actual_label,
        }
        if supplied_identity != expected_identity:
            raise ResearchV2Error(
                "v2 reflection identity does not match the evaluated forecast"
            )
        envelope = draft.model_dump(mode="json")
        existing = session.scalar(
            select(ReflectionV2).where(ReflectionV2.forecast_id == forecast.id)
        )
        if existing is not None:
            try:
                stored = ReflectionDraftV2.model_validate(existing.envelope)
            except ValueError as exc:
                raise ResearchV2Error(
                    "stored v2 reflection envelope failed validation"
                ) from exc
            persisted_identity = {
                "forecast_id": existing.forecast_id,
                "forecast_hash": existing.forecast_hash,
                "evaluation_id": existing.evaluation_id,
                "evaluation_hash": existing.evaluation_hash,
                "target_id": existing.target_id,
                "anchor_date": existing.anchor_date,
                "target_date": existing.target_date,
                "actual_label": existing.actual_label,
            }
            if (
                stored.content_hash != existing.content_hash
                or existing.content_hash != draft.content_hash
                or existing.envelope != envelope
                or persisted_identity != expected_identity
            ):
                raise ResearchV2Error(
                    "v2 reflection already exists with conflicting content"
                )
            return existing
        body = {
            "forecast_id": forecast.id,
            "forecast_hash": forecast.content_hash,
            "evaluation_id": evaluation.id,
            "evaluation_hash": evaluation.content_hash,
            "schema_version": REFLECTION_SCHEMA_V2,
            "target_id": forecast.target_id,
            "anchor_date": forecast.anchor_date,
            "target_date": forecast.target_date,
            "actual_label": evaluation.actual_label,
            "status": "completed",
            "verdict": draft.verdict,
            "findings": draft.findings,
            "envelope": envelope,
            "created_at": now,
        }
        row = ReflectionV2(id=str(uuid4()), content_hash=draft.content_hash, **body)
        session.add(row)
        session.commit()
        session.refresh(row)
        trace_mode = forecast.run.mode
        forecast_hash = forecast.content_hash
        evaluation_id = forecast.evaluation.id
        evaluation_hash = forecast.evaluation.content_hash
    _trace_reflection_create(
        database,
        settings,
        row=row,
        mode=trace_mode,
        forecast_id=draft.forecast_id,
        forecast_hash=forecast_hash,
        evaluation_id=evaluation_id,
        evaluation_hash=evaluation_hash,
        occurred_at=now,
    )
    return row


def review_reflection_v2(
    database: Database,
    settings: Settings,
    *,
    reflection_id: str,
    decision: Literal["approved", "rejected"],
    reviewer: str,
    notes: str = "",
) -> ReflectionV2:
    now = datetime.now(ZoneInfo(settings.timezone))
    with database.session_factory() as session:
        row = session.scalar(
            select(ReflectionV2)
            .options(selectinload(ReflectionV2.review_events))
            .where(ReflectionV2.id == reflection_id)
        )
        if row is None:
            raise ResearchV2Error("v2 reflection was not found")
        if row.review_events:
            event = row.review_events[0]
            if event.decision == decision and event.reviewer == reviewer and event.notes == notes:
                return row
            raise ResearchV2Error("v2 reflection already has an immutable review")
        body = {
            "reflection_id": row.id,
            "decision": decision,
            "reviewer": reviewer,
            "notes": notes,
            "occurred_at": now,
        }
        session.add(
            ReflectionReviewEventV2(
                id=str(uuid4()), content_hash=content_hash(body), **body
            )
        )
        session.commit()
        return row


def activate_d1_v2(
    database: Database,
    settings: Settings,
    *,
    actor: str,
    agent_eval_report_path: Path,
) -> ResearchActivationEventV2:
    """Activate only D1 after immutable forward and human gates are satisfied."""

    agent_eval_report_hash = _validated_agent_eval_pass_report(
        settings,
        agent_eval_report_path,
    )
    now = datetime.now(ZoneInfo(settings.timezone))
    with database.session_factory() as session:
        eligible_forecasts = session.scalars(
            select(ForecastV2)
            .join(ResearchRunV2, ResearchRunV2.id == ForecastV2.run_id)
            .where(
                ForecastV2.program_hash == DEFAULT_RESEARCH_PROGRAM_V2.content_hash,
                ForecastV2.target_id == CSI1000_D1_TARGET,
                ResearchRunV2.mode == "live",
                ForecastV2.evaluation.has(),
            )
            .order_by(
                ForecastV2.target_date.asc(),
                ForecastV2.created_at.asc(),
                ForecastV2.id.asc(),
            )
        ).all()
        first_by_target_date: dict[date, ForecastV2] = {}
        for forecast in eligible_forecasts:
            first_by_target_date.setdefault(forecast.target_date, forecast)
        completed_dates = len(first_by_target_date)
        earliest_ten = list(first_by_target_date.values())[:10]
        if completed_dates < 20 or len(earliest_ten) < 10:
            raise ResearchV2Error("D1 activation requires 20 forward dates and 10 approvals")
        reflection_rows = session.execute(
            select(ReflectionV2, ReflectionReviewEventV2)
            .outerjoin(
                ReflectionReviewEventV2,
                ReflectionReviewEventV2.reflection_id == ReflectionV2.id,
            )
            .where(ReflectionV2.forecast_id.in_([item.id for item in earliest_ten]))
        ).all()
        approved_by_forecast = {
            reflection.forecast_id: (reflection, review)
            for reflection, review in reflection_rows
            if review is not None and review.decision == "approved"
        }
        if set(approved_by_forecast) != {item.id for item in earliest_ten}:
            raise ResearchV2Error(
                "D1 activation requires approval of every one of the earliest 10 live reflections"
            )
        previous = session.scalar(
            select(ResearchActivationEventV2)
            .where(
                ResearchActivationEventV2.program_hash
                == DEFAULT_RESEARCH_PROGRAM_V2.content_hash,
                ResearchActivationEventV2.target_id == CSI1000_D1_TARGET,
            )
            .order_by(ResearchActivationEventV2.occurred_at.desc())
        )
        if previous is not None and previous.event_type == "activated":
            return previous
        evidence = {
            "forward_target_dates": completed_dates,
            "approved_reflections": len(approved_by_forecast),
            "earliest_10": [
                {
                    "target_date": forecast.target_date.isoformat(),
                    "forecast_hash": forecast.content_hash,
                    "reflection_hash": approved_by_forecast[forecast.id][0].content_hash,
                    "review_event_hash": approved_by_forecast[forecast.id][1].content_hash,
                }
                for forecast in earliest_ten
            ],
            "agent_eval_report_hash": agent_eval_report_hash,
        }
        body = {
            "schema_version": ACTIVATION_SCHEMA_V2,
            "program_hash": DEFAULT_RESEARCH_PROGRAM_V2.content_hash,
            "target_id": CSI1000_D1_TARGET,
            "event_type": "activated",
            "policy_version": "2.0.0",
            "evidence": evidence,
            "actor": actor,
            "occurred_at": now,
            "previous_event_hash": previous.content_hash if previous else None,
        }
        row = ResearchActivationEventV2(
            id=str(uuid4()), content_hash=content_hash(body), **body
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def latest_forecasts_v2(session) -> dict[str, Any]:
    """Return the newest D1 card and the independently anchored W1 shadow card."""

    rows = session.scalars(
        select(ForecastV2)
        .options(selectinload(ForecastV2.evaluation))
        .join(ResearchRunV2, ResearchRunV2.id == ForecastV2.run_id)
        .where(
            ForecastV2.program_hash == DEFAULT_RESEARCH_PROGRAM_V2.content_hash,
            ResearchRunV2.mode == "live",
        )
        .order_by(ForecastV2.created_at.desc(), ForecastV2.id.desc())
    ).all()
    latest: dict[str, ForecastV2] = {}
    for row in rows:
        latest.setdefault(row.target_id, row)
    return {
        "program_hash": DEFAULT_RESEARCH_PROGRAM_V2.content_hash,
        "formal": _forecast_api_payload(latest.get(CSI1000_D1_TARGET)),
        "shadow": _forecast_api_payload(latest.get(CSI1000_RELATIVE_W1_TARGET)),
    }


def reasoning_reviews_v2(
    session,
    *,
    limit: int,
    cursor: str | None = None,
) -> dict[str, Any]:
    statement = (
        select(ReasoningReviewV2)
        .join(AgentSignalV2Record, AgentSignalV2Record.id == ReasoningReviewV2.signal_id)
        .join(ResearchRunV2, ResearchRunV2.id == AgentSignalV2Record.run_id)
        .options(
            selectinload(ReasoningReviewV2.signal),
            selectinload(ReasoningReviewV2.human_events),
        )
        .where(ResearchRunV2.mode == "live")
        .order_by(ReasoningReviewV2.created_at.desc(), ReasoningReviewV2.id.desc())
    )
    if cursor:
        cursor_created_at, cursor_id = _decode_review_cursor(cursor)
        statement = statement.where(
            (ReasoningReviewV2.created_at < cursor_created_at)
            | (
                (ReasoningReviewV2.created_at == cursor_created_at)
                & (ReasoningReviewV2.id < cursor_id)
            )
        )
    rows = session.scalars(statement.limit(limit + 1)).all()
    page = rows[:limit]
    items = [
        {
            "id": row.id,
            "signal_id": row.signal_id,
            "agent_id": row.signal.agent_id,
            "target_id": row.signal.target_id,
            "signal_kind": row.signal.signal_kind,
            "horizon": row.signal.decision_horizon or row.signal.natural_horizon,
            "status": _effective_reasoning_status(row),
            "total_score": row.total_score,
            "human_review_required": row.human_review_required,
            "human_review_status": _effective_reasoning_status(row),
            "deterministic_checks": row.deterministic_checks,
            "rubric": row.rubric,
            "created_at": row.created_at,
        }
        for row in page
    ]
    return {
        "items": items,
        "next_cursor": (
            _encode_review_cursor(page[-1]) if len(rows) > limit and page else None
        ),
    }


def _effective_reasoning_status(row: ReasoningReviewV2) -> str:
    if row.human_events:
        return row.human_events[0].decision
    return row.human_review_status


def agent_scorecards_v2(
    session,
    *,
    generated_at: datetime,
    ablation_values: dict[tuple[str, str, str, str, str], float] | None = None,
) -> dict[str, Any]:
    """Build five diagnostic sections without manufacturing a cross-role rank."""

    signal_rows = session.scalars(
        select(AgentSignalV2Record)
        .join(ResearchRunV2, ResearchRunV2.id == AgentSignalV2Record.run_id)
        .options(
            selectinload(AgentSignalV2Record.evaluations),
            selectinload(AgentSignalV2Record.reasoning_reviews),
        )
        .where(
            AgentSignalV2Record.program_hash == DEFAULT_RESEARCH_PROGRAM_V2.content_hash,
            ResearchRunV2.mode == "live",
        )
        .order_by(AgentSignalV2Record.created_at)
    ).all()
    forecast_by_episode: dict[tuple[str, str], ForecastV2] = {}
    if signal_rows:
        forecasts = session.scalars(
            select(ForecastV2)
            .options(selectinload(ForecastV2.evaluation))
            .where(ForecastV2.run_id.in_({row.run_id for row in signal_rows}))
        ).all()
        forecast_by_episode = {
            (forecast.run_id, forecast.target_id): forecast for forecast in forecasts
        }
    grouped: dict[tuple[str, str, str, str, str, str, str], list[AgentSignalV2Record]] = {}
    for row in signal_rows:
        identity = (
            row.agent_id,
            row.agent_version,
            row.model_name,
            AgentSignalEnvelopeV2.model_validate(row.envelope).prompt_version,
            row.target_id,
            row.signal_kind,
            row.decision_horizon or row.natural_horizon,
        )
        grouped.setdefault(identity, []).append(row)

    sections: dict[str, list[dict[str, Any]]] = {
        "final_system": [],
        "natural_horizon": [],
        "d1_impact": [],
        "reasoning": [],
        "incremental_value": [],
    }
    for identity, rows in sorted(grouped.items()):
        (
            agent_id,
            agent_version,
            _model_name,
            _prompt_version,
            target_id,
            signal_kind,
            horizon,
        ) = identity
        item = _scorecard_item(rows, identity, forecast_by_episode)
        if agent_id in {"user_judgment_agent", "quant_agent"}:
            item["note"] = (
                "Shadow-only D1 benchmark; it never enters Strategy, CIO, or "
                "the formal forecast projection."
            )
        if signal_kind in {
            SignalKindV2.STRATEGY_FORECAST.value,
            SignalKindV2.DECISION_FORECAST.value,
        }:
            sections["final_system"].append(item)
        if signal_kind == SignalKindV2.NATURAL_VIEW.value:
            sections["natural_horizon"].append(item)
        if signal_kind == SignalKindV2.D1_IMPACT.value:
            impact_item = dict(item)
            impact_item["average_brier"] = None
            impact_item["baseline_brier"] = None
            impact_item["brier_skill"] = None
            impact_item["classwise_ece"] = None
            impact_item["direction_accuracy"] = None
            impact_item["note"] = (
                "D1 impact is evaluated structurally; it does not cast a market vote."
            )
            sections["d1_impact"].append(impact_item)
        if (
            any(row.reasoning_reviews for row in rows)
            or signal_kind == SignalKindV2.RISK_CRITIQUE.value
        ):
            reasoning_item = dict(item)
            reasoning_item["average_brier"] = None
            reasoning_item["baseline_brier"] = None
            reasoning_item["brier_skill"] = None
            reasoning_item["classwise_ece"] = None
            reasoning_item["direction_accuracy"] = None
            reasoning_item["note"] = (
                "Risk Critic is evaluated on coverage, invalidation conditions, "
                "missed system errors, and blind review; it never receives a "
                "direction score."
                if signal_kind == SignalKindV2.RISK_CRITIQUE.value
                else "Blind LLM rubric is advisory and separately human-audited."
            )
            sections["reasoning"].append(reasoning_item)
        incremental = dict(item)
        incremental["average_brier"] = None
        incremental["baseline_brier"] = None
        incremental["brier_skill"] = None
        incremental["classwise_ece"] = None
        incremental["direction_accuracy"] = None
        incremental["ablation_brier_delta"] = (ablation_values or {}).get(
            (target_id, agent_id, agent_version, _model_name, _prompt_version)
        )
        incremental["note"] = (
            "Latest exact-version private replay; diagnostic only and never changes weights."
            if incremental["ablation_brier_delta"] is not None
            else "No exact-version private ablation is available; no automatic weight changes."
        )
        sections["incremental_value"].append(incremental)

    titles = {
        "final_system": "最终系统",
        "natural_horizon": "自然周期",
        "d1_impact": "D1 边际影响",
        "reasoning": "推理质量",
        "incremental_value": "增量贡献",
    }
    return {
        "program_hash": DEFAULT_RESEARCH_PROGRAM_V2.content_hash,
        "generated_at": generated_at,
        "sections": [
            {"axis": axis, "title": titles[axis], "items": sections[axis]}
            for axis in (
                "final_system",
                "natural_horizon",
                "d1_impact",
                "reasoning",
                "incremental_value",
            )
        ],
    }


def _forecast_api_payload(row: ForecastV2 | None) -> dict[str, Any] | None:
    if row is None:
        return None
    evaluation = row.evaluation
    return {
        "id": row.id,
        "run_id": row.run_id,
        "target_id": row.target_id,
        "horizon": row.horizon,
        "lane": row.effective_lane,
        "configured_lane": row.configured_lane,
        "anchor_date": row.anchor_date,
        "target_date": row.target_date,
        "probabilities": {
            "up": row.probability_up,
            "neutral": row.probability_neutral,
            "down": row.probability_down,
        },
        "baseline_probabilities": row.baseline_probabilities,
        "neutral_threshold": row.threshold,
        "rationale": row.rationale,
        "counter_evidence": row.counter_evidence,
        "invalidation_conditions": row.invalidation_conditions,
        "created_at": row.created_at,
        "evaluation": (
            {
                "actual_value": evaluation.actual_value,
                "actual_label": evaluation.actual_label,
                "brier_score": evaluation.brier_score,
                "baseline_brier_score": evaluation.baseline_brier_score,
                "brier_improvement": evaluation.baseline_brier_score
                - evaluation.brier_score,
                "direction_correct": bool(evaluation.direction_correct),
                "evaluated_at": evaluation.evaluated_at,
            }
            if evaluation is not None
            else None
        ),
    }


def _scorecard_item(
    rows: list[AgentSignalV2Record],
    identity: tuple[str, str, str, str, str, str, str],
    forecast_by_episode: dict[tuple[str, str], ForecastV2],
) -> dict[str, Any]:
    (
        agent_id,
        agent_version,
        model_name,
        prompt_version,
        target_id,
        signal_kind,
        horizon,
    ) = identity
    evaluations = [
        evaluation
        for row in rows
        for evaluation in row.evaluations
    ]
    reasoning = [
        review.total_score
        for row in rows
        for review in row.reasoning_reviews
    ]
    average_brier = _mean([item.brier_score for item in evaluations])
    baseline_brier = _mean([item.baseline_brier_score for item in evaluations])
    classwise_ece = _classwise_ece(rows) if evaluations else None
    risk_diagnostics = (
        _risk_critic_diagnostics(rows, forecast_by_episode)
        if signal_kind == SignalKindV2.RISK_CRITIQUE.value
        else None
    )
    return {
        "agent_id": agent_id,
        "agent_name": AGENT_BY_ID.get(agent_id).name if agent_id in AGENT_BY_ID else agent_id,
        "agent_version": agent_version,
        "model_name": model_name,
        "prompt_version": prompt_version,
        "target_id": target_id,
        "signal_kind": signal_kind,
        "horizon": horizon,
        "sample_size": len(evaluations),
        "independent_episodes": len({
            (row.anchor_date, row.target_date) for row in rows if row.evaluations
        }),
        "average_brier": average_brier,
        "baseline_brier": baseline_brier,
        "brier_skill": _relative_brier_skill(average_brier, baseline_brier),
        "classwise_ece": classwise_ece,
        "direction_accuracy": _mean(
            [1.0 if item.direction_correct else 0.0 for item in evaluations]
        ),
        "reasoning_average": _mean([float(value) for value in reasoning]),
        "ablation_brier_delta": None,
        "risk_diagnostics": risk_diagnostics,
        "note": "",
    }


def _relative_brier_skill(
    average_brier: float | None,
    baseline_brier: float | None,
) -> float | None:
    if average_brier is None or baseline_brier is None or baseline_brier <= 0:
        return None
    return (baseline_brier - average_brier) / baseline_brier


def _risk_critic_diagnostics(
    rows: list[AgentSignalV2Record],
    forecast_by_episode: dict[tuple[str, str], ForecastV2],
) -> dict[str, float | int | None]:
    """Score critic coverage without manufacturing a directional forecast.

    A missed risk is a realized CIO forecast error for which the pre-outcome
    critic declared only ``none`` or ``low`` severity.  The metric is explicitly
    diagnostic: it does not alter aggregation or any historical weight.
    """

    critique_count = len(rows)
    counter_coverage = 0
    invalidation_coverage = 0
    risk_flags = 0
    evaluated_errors = 0
    missed_risks = 0
    for row in rows:
        envelope = AgentSignalEnvelopeV2.model_validate(row.envelope)
        draft = envelope.draft
        if draft.counter_evidence:
            counter_coverage += 1
        if draft.invalidation_conditions:
            invalidation_coverage += 1
        if draft.risk_severity in {"medium", "high"}:
            risk_flags += 1
        forecast = forecast_by_episode.get((row.run_id, row.target_id))
        if forecast is None or forecast.evaluation is None:
            continue
        probabilities = {
            "up": forecast.probability_up,
            "neutral": forecast.probability_neutral,
            "down": forecast.probability_down,
        }
        predicted = max(probabilities, key=probabilities.__getitem__)
        if predicted == forecast.evaluation.actual_label:
            continue
        evaluated_errors += 1
        if draft.risk_severity in {"none", "low"}:
            missed_risks += 1
    return {
        "critique_count": critique_count,
        "counter_evidence_coverage_rate": (
            counter_coverage / critique_count if critique_count else 0.0
        ),
        "invalidation_coverage_rate": (
            invalidation_coverage / critique_count if critique_count else 0.0
        ),
        "risk_flag_rate": risk_flags / critique_count if critique_count else 0.0,
        "evaluated_system_errors": evaluated_errors,
        "missed_risk_count": missed_risks,
        "missed_risk_rate": (
            missed_risks / evaluated_errors if evaluated_errors else None
        ),
    }


def _classwise_ece(rows: list[AgentSignalV2Record]) -> dict[str, float]:
    pairs: dict[str, list[tuple[float, float]]] = {
        label: [] for label in ("up", "neutral", "down")
    }
    for row in rows:
        if not row.evaluations:
            continue
        envelope = AgentSignalEnvelopeV2.model_validate(row.envelope)
        probabilities = envelope.draft.probabilities
        if probabilities is None:
            continue
        for evaluation in row.evaluations:
            for label, probability in probabilities.as_dict().items():
                pairs[label].append(
                    (probability, 1.0 if evaluation.actual_label == label else 0.0)
                )
    return {label: _ece(values) for label, values in pairs.items()}


def _ece(values: list[tuple[float, float]]) -> float:
    if not values:
        return 0.0
    total = len(values)
    result = 0.0
    for index in range(SCORECARD_ECE_BINS_V2):
        lower = index / SCORECARD_ECE_BINS_V2
        upper = (index + 1) / SCORECARD_ECE_BINS_V2
        bucket = [
            item
            for item in values
            if item[0] >= lower
            and (item[0] < upper or (index == SCORECARD_ECE_BINS_V2 - 1 and item[0] <= upper))
        ]
        if not bucket:
            continue
        confidence = sum(item[0] for item in bucket) / len(bucket)
        frequency = sum(item[1] for item in bucket) / len(bucket)
        result += len(bucket) / total * abs(confidence - frequency)
    return result


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    result = sum(values) / len(values)
    return result if math.isfinite(result) else None


def _encode_review_cursor(row: ReasoningReviewV2) -> str:
    import base64

    raw = f"{row.created_at.isoformat()}|{row.id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_review_cursor(cursor: str) -> tuple[datetime, str]:
    import base64

    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode((cursor + padding).encode()).decode()
        created_at_text, row_id = decoded.split("|", 1)
        created_at = datetime.fromisoformat(created_at_text)
        if created_at.tzinfo is None or not row_id:
            raise ValueError
        return created_at, row_id
    except (ValueError, UnicodeDecodeError) as exc:
        raise ResearchV2Error("invalid reasoning review cursor") from exc


def _build_assignments(
    database: Database,
    snapshot: EvidenceSnapshotV2,
    wiki: FrozenWikiCatalog,
    *,
    context_signals: dict[tuple[str, str], AgentSignalV2Record],
    latest_signals: dict[tuple[str, str], AgentSignalV2Record],
    baselines: dict[str, ProbabilitiesV2],
    mode: Literal["demo", "live"],
) -> list[HandoffAssignmentV3]:
    assignments: list[HandoffAssignmentV3] = []
    evidence_ids = [item.item_id for item in snapshot.items]
    active = context_signals
    previous_session = snapshot.instruments[CSI1000].returns[-2].trade_date
    for agent_id in RESEARCH_AGENT_IDS:
        if agent_id == "market_news_agent":
            natural_targets = [(CSI1000_D1_TARGET, V2Horizon.D1)]
        elif agent_id == "ai_storage_industry_agent":
            latest = latest_signals.get((agent_id, CSI1000_RELATIVE_W1_TARGET))
            natural_targets = (
                [
                    (
                        CSI1000_RELATIVE_W1_TARGET,
                        V2Horizon.W1,
                        "bootstrap" if latest is None else "scheduled",
                    )
                ]
                if _natural_view_due(
                    active.get((agent_id, CSI1000_RELATIVE_W1_TARGET)),
                    latest,
                    snapshot.base_session,
                    previous_session,
                    cadence="weekly",
                )
                else []
            )
        else:
            latest = latest_signals.get((agent_id, CSI1000_D20_RESEARCH_TARGET))
            natural_targets = (
                [
                    (
                        CSI1000_D20_RESEARCH_TARGET,
                        V2Horizon.D20,
                        "bootstrap" if latest is None else "scheduled",
                    )
                ]
                if _natural_view_due(
                    active.get((agent_id, CSI1000_D20_RESEARCH_TARGET)),
                    latest,
                    snapshot.base_session,
                    previous_session,
                    cadence="monthly",
                )
                else []
            )
        if agent_id == "market_news_agent":
            natural_targets = [(CSI1000_D1_TARGET, V2Horizon.D1, "daily")]
        for target_id, horizon, generation_reason in natural_targets:
            assignments.append(
                _assignment(
                    agent_id,
                    SignalKindV2.NATURAL_VIEW,
                    target_id,
                    horizon,
                    None,
                    snapshot,
                    wiki,
                    evidence_ids,
                    state_available=True,
                    baseline=baselines[target_id],
                    generation_reason=generation_reason,
                )
            )
        if agent_id in {"macro_policy_agent", "ai_storage_industry_agent"}:
            natural_target = (
                CSI1000_D20_RESEARCH_TARGET
                if agent_id == "macro_policy_agent"
                else CSI1000_RELATIVE_W1_TARGET
            )
            prior = active.get((agent_id, natural_target))
            current_natural_id = next(
                (
                    item.assignment_id
                    for item in assignments
                    if item.agent_id == agent_id
                    and item.signal_kind == SignalKindV2.NATURAL_VIEW
                    and item.target_id == natural_target
                ),
                None,
            )
            state_available = prior is not None or any(
                item.agent_id == agent_id
                and item.signal_kind == SignalKindV2.NATURAL_VIEW
                for item in assignments
            )
            assignments.append(
                _assignment(
                    agent_id,
                    SignalKindV2.D1_IMPACT,
                    CSI1000_D1_TARGET,
                    V2Horizon.D20
                    if agent_id == "macro_policy_agent"
                    else V2Horizon.W1,
                    V2Horizon.D1,
                    snapshot,
                    wiki,
                    evidence_ids,
                    state_available=state_available,
                    prior_signal_id=prior.id if prior else None,
                    context_signal_ids=[prior.id] if prior else [],
                    depends_on_assignment_ids=(
                        [current_natural_id] if current_natural_id else []
                    ),
                    generation_reason="daily",
                )
            )
    research_assignment_ids = [item.assignment_id for item in assignments]
    context_ids = [item.id for item in active.values()]
    for target in DEFAULT_RESEARCH_PROGRAM_V2.decision_targets:
        if not _decision_target_due(
            database, target.target_id, snapshot, previous_session, mode=mode
        ):
            continue
        strategy = _assignment(
            STRATEGY_AGENT_ID,
            SignalKindV2.STRATEGY_FORECAST,
            target.target_id,
            target.horizon,
            target.horizon,
            snapshot,
            wiki,
            evidence_ids,
            state_available=True,
            context_signal_ids=context_ids,
            depends_on_assignment_ids=research_assignment_ids,
            baseline=baselines[target.target_id],
            generation_reason="daily" if target.horizon is V2Horizon.D1 else "scheduled",
        )
        critic = _assignment(
            CRITIC_AGENT_ID,
            SignalKindV2.RISK_CRITIQUE,
            target.target_id,
            target.horizon,
            target.horizon,
            snapshot,
            wiki,
            evidence_ids,
            state_available=True,
            context_signal_ids=context_ids,
            depends_on_assignment_ids=[strategy.assignment_id],
            generation_reason="daily" if target.horizon is V2Horizon.D1 else "scheduled",
        )
        cio = _assignment(
            CIO_AGENT_ID,
            SignalKindV2.DECISION_FORECAST,
            target.target_id,
            target.horizon,
            target.horizon,
            snapshot,
            wiki,
            evidence_ids,
            state_available=True,
            depends_on_assignment_ids=[strategy.assignment_id, critic.assignment_id],
            baseline=baselines[target.target_id],
            producer="deterministic",
            generation_reason="daily" if target.horizon is V2Horizon.D1 else "scheduled",
        )
        assignments.extend((strategy, critic, cio))
    return assignments


def _assignment(
    agent_id: str,
    kind: SignalKindV2,
    target_id: str,
    natural_horizon: V2Horizon,
    decision_horizon: V2Horizon | None,
    snapshot: EvidenceSnapshotV2,
    wiki: FrozenWikiCatalog,
    evidence_ids: list[str],
    *,
    state_available: bool,
    prior_signal_id: str | None = None,
    context_signal_ids: list[str] | None = None,
    depends_on_assignment_ids: list[str] | None = None,
    baseline: ProbabilitiesV2 | None = None,
    producer: Literal["codex", "deterministic"] = "codex",
    generation_reason: Literal["daily", "scheduled", "bootstrap"] = "daily",
) -> HandoffAssignmentV3:
    entry = wiki.select_for_agent(agent_id, index_code=CSI1000)
    section = entry.sections[0]
    role = AGENT_BY_ID[agent_id].role
    return HandoffAssignmentV3(
        assignment_id=_assignment_id(
            agent_id, kind, target_id, natural_horizon, decision_horizon
        ),
        agent_id=agent_id,
        agent_version=AGENT_BY_ID[agent_id].version,
        model_name=(
            FROZEN_CODEX_MODEL
            if producer == "codex"
            else "forecast-loop-deterministic-cio"
        ),
        prompt_version=(
            CODEX_HANDOFF_SCHEMA_V3 if producer == "codex" else "deterministic-cio/v2"
        ),
        producer=producer,
        signal_kind=kind,
        target_id=target_id,
        natural_horizon=natural_horizon,
        decision_horizon=decision_horizon,
        generation_reason=generation_reason,
        anchor_date=snapshot.base_session,
        target_date=target_date(
            snapshot,
            decision_horizon or natural_horizon,
        ),
        state_available=state_available,
        prior_signal_id=prior_signal_id,
        context_signal_ids=context_signal_ids or [],
        depends_on_assignment_ids=depends_on_assignment_ids or [],
        baseline_probabilities=baseline,
        allowed_evidence_item_ids=evidence_ids,
        wiki_entry_id=entry.id,
        wiki_version=entry.version,
        wiki_section=section.slug,
        wiki_content_hash=entry.content_hash,
        role=(
            f"{role} Use Wiki {entry.id}@{entry.version} section {section.slug}. "
            f"Do not cite evidence outside allowed_evidence_item_ids."
        ),
    )


def _assignment_id(
    agent_id: str,
    kind: SignalKindV2,
    target_id: str,
    natural_horizon: V2Horizon,
    decision_horizon: V2Horizon | None,
) -> str:
    return ":".join(
        [
            agent_id,
            kind.value,
            target_id,
            natural_horizon.value,
            decision_horizon.value if decision_horizon else "none",
        ]
    )


def _seal_signal(
    request: CodexHandoffRequestV3,
    assignment: HandoffAssignmentV3,
    draft: AgentSignalDraftV2,
    *,
    created_at: datetime,
) -> AgentSignalEnvelopeV2:
    baseline = assignment.baseline_probabilities
    threshold = (
        threshold_for_target(request.snapshot, assignment.target_id)
        if draft.probabilities is not None
        else None
    )
    signal_id = str(uuid4())
    body = {
        "schema_version": AGENT_SIGNAL_SCHEMA_V2,
        "signal_id": signal_id,
        "run_id": request.run_id,
        "agent_id": assignment.agent_id,
        "agent_version": assignment.agent_version,
        "model_name": assignment.model_name,
        "prompt_version": assignment.prompt_version,
        "target_id": assignment.target_id,
        "signal_kind": assignment.signal_kind,
        "natural_horizon": assignment.natural_horizon,
        "decision_horizon": assignment.decision_horizon,
        "generation_reason": assignment.generation_reason,
        "anchor_date": assignment.anchor_date,
        "target_date": assignment.target_date,
        "evidence_cutoff": request.snapshot.data_cutoff,
        "program_hash": request.program.content_hash,
        "input_hash": request.input_hash,
        "threshold": threshold,
        "baseline_probabilities": baseline,
        "draft": draft,
        "created_at": created_at,
    }
    return AgentSignalEnvelopeV2(**body, content_hash=content_hash(body))


def _signal_record(signal: AgentSignalEnvelopeV2) -> AgentSignalV2Record:
    return AgentSignalV2Record(
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
        decision_horizon=(signal.decision_horizon.value if signal.decision_horizon else None),
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
        state_available=signal.draft.state_available,
        abstain=signal.draft.abstain,
        content_hash=signal.content_hash,
        envelope=signal.model_dump(mode="json"),
        created_at=signal.created_at,
    )


def _frozen_baselines(
    database: Database,
    cutoff: datetime,
    *,
    mode: Literal["demo", "live"],
) -> dict[str, ProbabilitiesV2]:
    """Freeze class frequencies from cutoff-safe outcomes in the same trust lane.

    Live observations are additionally required to have produced at least one
    immutable evaluation of a signal belonging to a completed live run.  This
    keeps imported or demo-only outcome rows out of formal baselines.
    """

    targets = {
        CSI1000_D1_TARGET,
        CSI1000_RELATIVE_W1_TARGET,
        CSI1000_D20_RESEARCH_TARGET,
    }
    labels: dict[str, list[str]] = {target: [] for target in targets}
    with database.session_factory() as session:
        rows = session.scalars(
            select(OutcomeObservationV2Record)
            .where(
                OutcomeObservationV2Record.observed_at <= cutoff,
                OutcomeObservationV2Record.mode == mode,
            )
            .order_by(
                OutcomeObservationV2Record.target_date,
                OutcomeObservationV2Record.id,
            )
        ).all()
    seen: set[tuple[str, date, date]] = set()
    for row in rows:
        if mode == "live" and not _is_live_outcome_bound(database, row.id):
            continue
        identity = (row.target_id, row.anchor_date, row.target_date)
        if row.target_id not in labels or identity in seen:
            continue
        seen.add(identity)
        labels[row.target_id].append(row.actual_label)
    return {target: smoothed_baseline(values) for target, values in labels.items()}


def _is_live_outcome_bound(database: Database, observation_id: str) -> bool:
    with database.session_factory() as session:
        return (
            session.scalar(
                select(SignalEvaluationV2.id)
                .join(
                    AgentSignalV2Record,
                    AgentSignalV2Record.id == SignalEvaluationV2.signal_id,
                )
                .join(ResearchRunV2, ResearchRunV2.id == AgentSignalV2Record.run_id)
                .where(
                    SignalEvaluationV2.observation_id == observation_id,
                    ResearchRunV2.mode == "live",
                    ResearchRunV2.status == "completed",
                )
                .limit(1)
            )
            is not None
        )


def _validate_live_snapshot_sources(snapshot: EvidenceSnapshotV2) -> None:
    """Enforce the reviewed provenance allowlist at the live v2 trust boundary."""

    validate_trusted_source_url(
        snapshot.calendar_source.source_url, label="v2 trading calendar"
    )
    for instrument in snapshot.instruments.values():
        for row in instrument.returns:
            validate_trusted_source_url(
                row.source.source_url,
                label=f"v2 {instrument.code} return on {row.trade_date.isoformat()}",
            )
    for item in snapshot.items:
        validate_trusted_source_url(item.source_url, label=f"v2 evidence {item.item_id}")


def _validate_live_outcome_sources(observation: OutcomeObservationV2) -> None:
    """Enforce the reviewed source allowlist after structural stamp validation."""

    if observation.primary_source is None or observation.calendar_source is None:
        raise ResearchV2Error("live outcome is missing required trusted source stamps")
    validate_trusted_source_url(
        observation.primary_source.source_url,
        label=f"v2 outcome {CSI1000}",
    )
    validate_trusted_source_url(
        observation.calendar_source.source_url,
        label="v2 outcome trading calendar",
    )
    if observation.comparison_source is not None:
        validate_trusted_source_url(
            observation.comparison_source.source_url,
            label=f"v2 outcome {CSI300}",
        )


def _deterministic_cio_draft(
    strategy: AgentSignalEnvelopeV2,
    critic: AgentSignalEnvelopeV2,
    assignment: HandoffAssignmentV3,
) -> AgentSignalDraftV2:
    """Apply a fixed Risk-Critic uncertainty discount to Strategy probabilities."""

    probabilities = strategy.draft.probabilities
    baseline = assignment.baseline_probabilities
    if probabilities is None or baseline is None:
        raise ResearchV2Error("deterministic CIO requires Strategy and frozen baseline")
    severity = critic.draft.risk_severity
    if severity is None:
        raise ResearchV2Error("deterministic CIO requires explicit critic risk severity")
    discount = {"none": 0.0, "low": 0.05, "medium": 0.12, "high": 0.20}[severity]
    values = {
        label: (1.0 - discount) * getattr(probabilities, label)
        + discount * getattr(baseline, label)
        for label in ("up", "neutral", "down")
    }
    winners = [label for label, value in values.items() if value == max(values.values())]
    if len(winners) != 1:
        # A tie is made explicitly more uncertain instead of casting an arbitrary vote.
        epsilon = 1e-8
        values["neutral"] += epsilon
        values["up"] -= epsilon / 2
        values["down"] -= epsilon / 2
    final = ProbabilitiesV2(**values)
    final_values = final.as_dict()
    direction = max(final_values, key=lambda label: final_values[label])
    return AgentSignalDraftV2(
        signal_kind=SignalKindV2.DECISION_FORECAST,
        target_id=assignment.target_id,
        natural_horizon=assignment.natural_horizon,
        decision_horizon=assignment.decision_horizon,
        direction=direction,
        probabilities=final,
        state_available=True,
        rationale=(
            "Deterministic CIO policy retained the Strategy distribution and applied "
            f"the frozen {severity} Risk-Critic discount ({discount:.0%}) toward the "
            "cutoff-bound historical baseline."
        ),
        transmission_chain=strategy.draft.transmission_chain,
        counter_evidence=critic.draft.counter_evidence,
        invalidation_conditions=critic.draft.invalidation_conditions,
        evidence_item_ids=sorted(
            set(strategy.draft.evidence_item_ids).union(critic.draft.evidence_item_ids)
        ),
        wiki_entry_id=assignment.wiki_entry_id,
        wiki_version=assignment.wiki_version,
        wiki_section=assignment.wiki_section,
        wiki_content_hash=assignment.wiki_content_hash,
    )


def _latest_natural_views(
    database: Database,
    *,
    mode: Literal["demo", "live"],
) -> dict[tuple[str, str], AgentSignalV2Record]:
    with database.session_factory() as session:
        rows = session.scalars(
            select(AgentSignalV2Record)
            .join(ResearchRunV2, ResearchRunV2.id == AgentSignalV2Record.run_id)
            .where(
                AgentSignalV2Record.signal_kind == SignalKindV2.NATURAL_VIEW.value,
                ResearchRunV2.mode == mode,
            )
            .order_by(AgentSignalV2Record.created_at.desc())
        ).all()
        result: dict[tuple[str, str], AgentSignalV2Record] = {}
        for row in rows:
            result.setdefault((row.agent_id, row.target_id), row)
        return result


def _natural_view_due(
    active: AgentSignalV2Record | None,
    latest: AgentSignalV2Record | None,
    anchor_date: date,
    previous_session: date,
    *,
    cadence: Literal["weekly", "monthly"],
) -> bool:
    if active is not None:
        return False
    if latest is None:
        return True
    if cadence == "weekly":
        return anchor_date.isocalendar()[:2] != previous_session.isocalendar()[:2]
    return (anchor_date.year, anchor_date.month) != (
        previous_session.year,
        previous_session.month,
    )


def _decision_target_due(
    database: Database,
    target_id: str,
    snapshot: EvidenceSnapshotV2,
    previous_session: date,
    *,
    mode: Literal["demo", "live"],
) -> bool:
    if target_id == CSI1000_D1_TARGET:
        return True
    if target_id != CSI1000_RELATIVE_W1_TARGET:
        return False
    with database.session_factory() as session:
        latest = session.scalar(
            select(ForecastV2)
            .join(ResearchRunV2, ResearchRunV2.id == ForecastV2.run_id)
            .where(
                ForecastV2.program_hash == DEFAULT_RESEARCH_PROGRAM_V2.content_hash,
                ForecastV2.target_id == target_id,
                ResearchRunV2.mode == mode,
            )
            .order_by(ForecastV2.anchor_date.desc(), ForecastV2.created_at.desc())
        )
    if latest is None:
        return True
    if latest.target_date > snapshot.base_session:
        return False
    return snapshot.base_session.isocalendar()[:2] != previous_session.isocalendar()[:2]


def _request_has_decision_target(
    request: CodexHandoffRequestV3,
    target_id: str,
) -> bool:
    return any(
        item.target_id == target_id
        and item.signal_kind == SignalKindV2.DECISION_FORECAST
        for item in request.assignments
    )


def _prediction_finalize_deadline(
    request: CodexHandoffRequestV3,
    settings: Settings,
) -> datetime:
    """The earliest target session midnight is the no-lookahead acceptance fence."""

    earliest_target = min(item.target_date for item in request.assignments)
    return datetime.combine(
        earliest_target,
        time.min,
        tzinfo=ZoneInfo(settings.timezone),
    )


def _effective_lane(
    session,
    target_id: str,
    configured_lane: str,
    *,
    mode: Literal["demo", "live"],
) -> str:
    if mode != "live" or configured_lane == "shadow":
        return "shadow"
    latest = session.scalar(
        select(ResearchActivationEventV2)
        .where(
            ResearchActivationEventV2.program_hash
            == DEFAULT_RESEARCH_PROGRAM_V2.content_hash,
            ResearchActivationEventV2.target_id == target_id,
        )
        .order_by(ResearchActivationEventV2.occurred_at.desc())
    )
    return "formal" if latest is not None and latest.event_type == "activated" else "shadow"


def _prediction_instructions(request: CodexHandoffRequestV3) -> str:
    return f"""# Focused research v2 drafts

Read only `input.json`. Copy `drafts.template.json` to `drafts.json` and fill every
Codex-produced assignment in dependency order. Do not draft assignments whose
producer is `deterministic`, and do not add or remove assignments. Use only frozen
evidence IDs, context signals, dependency drafts and the exact Wiki identity named
in each assignment. Risk Critic must not cast a direction vote and must declare
risk_severity. An unavailable natural state must produce an explicit no-impact
abstention.

The request is sealed by `{request.request_hash}`. Codex may write only
`drafts.json`; Python validates, aggregates, scores and persists every result.
"""


def _reasoning_instructions() -> str:
    return """# Blind reasoning review v2

Read only `input.json`. It contains no realized outcome or post-outcome material.
Copy `drafts.template.json` to `drafts.json` and score all five rubric dimensions
0-2. Declare exactly `gpt-5.6-sol` with `high` reasoning effort. The advisory
score cannot release a candidate or change a formal forecast.
"""


def _validated_job_dir(settings: Settings, run_id: str, *, must_exist: bool) -> Path:
    try:
        from uuid import UUID

        UUID(run_id)
    except ValueError as exc:
        raise ResearchV2Error("run ID is not a UUID") from exc
    root = (settings.handoff_root / "v2").resolve()
    candidate = root / run_id
    if must_exist and (not candidate.is_dir() or candidate.is_symlink()):
        raise ResearchV2Error("research job directory does not exist or is unsafe")
    if candidate.parent != root:
        raise ResearchV2Error("research job escaped the v2 handoff root")
    return candidate


def _validated_explicit_job_dir(settings: Settings, path: Path) -> Path:
    resolved = path.resolve(strict=True)
    root = (settings.handoff_root / "v2").resolve()
    if path.is_symlink() or resolved.parent != root:
        raise ResearchV2Error("research job must be a direct non-symlink child of the v2 root")
    return resolved


def _read_model(path: Path, model_type):
    return model_type.model_validate_json(_safe_read(path))


def _safe_read(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ResearchV2Error(f"required regular file is missing: {path.name}")
    size = path.stat().st_size
    if size <= 0 or size > MAX_JSON_BYTES:
        raise ResearchV2Error(f"file size is invalid: {path.name}")
    return path.read_bytes()


def _safe_read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_safe_read(path))
    except json.JSONDecodeError as exc:
        raise ResearchV2Error(f"invalid JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise ResearchV2Error(f"JSON root must be an object: {path.name}")
    return value


def _validated_agent_eval_pass_report(settings: Settings, report_path: Path) -> str:
    """Accept only a fully re-verified finalized private v2 job."""

    from .agent_evaluation_v2 import (
        AgentEvalV2Error,
        verify_finalized_agent_eval_v2_job,
    )

    try:
        report, report_hash = verify_finalized_agent_eval_v2_job(settings, report_path)
    except (AgentEvalV2Error, OSError, ValueError) as exc:
        raise ResearchV2Error(f"invalid finalized Agent Eval v2 job: {exc}") from exc
    if report.release_decision != "pass":
        raise ResearchV2Error("Agent Eval must pass before D1 activation")
    target = report.targets.get(CSI1000_D1_TARGET)
    if (
        target is None
        or not target.release_gate
        or target.decision != "pass"
        or not target.hard_gate_pass
        or target.metric_gate_pass is not True
    ):
        raise ResearchV2Error("Agent Eval D1 target gate did not pass")
    if target.episode_count < 20:
        raise ResearchV2Error("Agent Eval D1 target has fewer than 20 independent episodes")
    return report_hash


def _trace_span_snapshot(
    recorder: TraceRecorder,
    *,
    trace_id: str | None,
    workflow_kind: Literal["prediction", "reflection", "agent_eval"],
    subject_id: str,
    node_id: str,
    name: str,
    span_kind: Literal["workflow", "agent", "llm", "validator", "persistence", "external"],
    started_at: datetime,
    completed_at: datetime,
    parent_span_id: str | None = None,
    agent_id: str | None = None,
    agent_version: str | None = None,
    model_name: str | None = None,
    prompt_version: str | None = None,
    input_value: Any | None = None,
    output_value: Any | None = None,
    attributes: dict[str, Any] | None = None,
) -> str | None:
    if trace_id is None:
        return None
    return recorder.record_span_snapshot(
        workflow_kind=workflow_kind,
        subject_id=subject_id,
        trace_id=trace_id,
        node_id=node_id,
        name=name,
        span_kind=span_kind,
        started_at=started_at,
        completed_at=completed_at,
        parent_span_id=parent_span_id,
        agent_id=agent_id,
        agent_version=agent_version,
        model_name=model_name,
        prompt_version=prompt_version,
        input_value=input_value,
        output_value=output_value,
        attributes=attributes,
    )


def _record_finalized_research_trace(
    recorder: TraceRecorder,
    *,
    trace_id: str | None,
    request: CodexHandoffRequestV3,
    assignments: dict[str, HandoffAssignmentV3],
    codex_assignments: list[HandoffAssignmentV3],
    signals: dict[str, AgentSignalV2Record],
    forecasts: list[ForecastV2],
    external_span: str | None,
    started_at: datetime,
    completed_at: datetime,
) -> None:
    """Write best-effort telemetry only after the authoritative DB commit.

    TraceRecorder intentionally uses an independent session.  Keeping these
    writes outside the research transaction prevents SQLite telemetry locks
    from delaying or blocking an otherwise valid forecast.
    """

    subject_id = request.run_id
    target_spans: dict[str, str | None] = {}
    for target_id in sorted({item.target_id for item in assignments.values()}):
        target_assignment = next(
            item for item in assignments.values() if item.target_id == target_id
        )
        horizon = (
            target_assignment.decision_horizon or target_assignment.natural_horizon
        ).value
        target_spans[target_id] = _trace_span_snapshot(
            recorder,
            trace_id=trace_id,
            workflow_kind="prediction",
            subject_id=subject_id,
            node_id=f"target.{target_id}",
            name=f"target {target_id}",
            span_kind="workflow",
            parent_span_id=external_span,
            started_at=started_at,
            completed_at=completed_at,
            attributes={"horizon": horizon},
        )

    for assignment in codex_assignments:
        signal = signals[assignment.assignment_id]
        agent_span = _trace_span_snapshot(
            recorder,
            trace_id=trace_id,
            workflow_kind="prediction",
            subject_id=subject_id,
            node_id=f"agent.{assignment.assignment_id}",
            name=f"call {assignment.agent_id}",
            span_kind="agent",
            parent_span_id=target_spans[assignment.target_id],
            started_at=started_at,
            completed_at=completed_at,
            agent_id=assignment.agent_id,
            agent_version=assignment.agent_version,
            model_name=assignment.model_name,
            prompt_version=assignment.prompt_version,
            input_value={"assignment_id": assignment.assignment_id},
            output_value={"signal_hash": signal.content_hash},
            attributes={
                "horizon": (
                    assignment.decision_horizon or assignment.natural_horizon
                ).value
            },
        )
        validator_span = _trace_span_snapshot(
            recorder,
            trace_id=trace_id,
            workflow_kind="prediction",
            subject_id=subject_id,
            node_id=f"validator.{assignment.assignment_id}",
            name=f"validate {assignment.agent_id} draft",
            span_kind="validator",
            parent_span_id=agent_span,
            started_at=completed_at,
            completed_at=completed_at,
            agent_id=assignment.agent_id,
            agent_version=assignment.agent_version,
            output_value={"signal_hash": signal.content_hash},
        )
        _trace_link(
            recorder,
            trace_id=trace_id,
            workflow_kind="prediction",
            subject_id=subject_id,
            span_id=validator_span,
            artifact_kind="signal",
            artifact_id=signal.id,
            relation="output",
            content_hash=signal.content_hash,
        )

    for assignment_id, signal in signals.items():
        assignment = assignments[assignment_id]
        if assignment.producer != "deterministic":
            continue
        aggregation_span = _trace_span_snapshot(
            recorder,
            trace_id=trace_id,
            workflow_kind="prediction",
            subject_id=subject_id,
            node_id=f"aggregation.{assignment.target_id}",
            name=f"aggregate {assignment.target_id}",
            span_kind="workflow",
            parent_span_id=target_spans[assignment.target_id],
            started_at=completed_at,
            completed_at=completed_at,
            agent_id=assignment.agent_id,
            agent_version=assignment.agent_version,
            output_value={"signal_hash": signal.content_hash},
            attributes={
                "horizon": (
                    assignment.decision_horizon or assignment.natural_horizon
                ).value
            },
        )
        _trace_link(
            recorder,
            trace_id=trace_id,
            workflow_kind="prediction",
            subject_id=subject_id,
            span_id=aggregation_span,
            artifact_kind="signal",
            artifact_id=signal.id,
            relation="output",
            content_hash=signal.content_hash,
        )

    persistence_span = _trace_span_snapshot(
        recorder,
        trace_id=trace_id,
        workflow_kind="prediction",
        subject_id=subject_id,
        node_id="run.persistence",
        name="persist sealed v2 artifacts",
        span_kind="persistence",
        parent_span_id=external_span,
        started_at=completed_at,
        completed_at=completed_at,
        output_value={"forecast_count": len(forecasts), "signal_count": len(signals)},
        attributes={"artifact_count": len(signals) + len(forecasts)},
    )
    for forecast in forecasts:
        _trace_link(
            recorder,
            trace_id=trace_id,
            workflow_kind="prediction",
            subject_id=subject_id,
            span_id=persistence_span,
            artifact_kind="forecast",
            artifact_id=forecast.id,
            relation="output",
            content_hash=forecast.content_hash,
        )


def _trace_link(
    recorder: TraceRecorder,
    *,
    trace_id: str | None,
    workflow_kind: Literal["prediction", "reflection", "agent_eval"],
    subject_id: str,
    artifact_kind: Literal[
        "signal", "forecast", "evaluation", "reasoning_review", "reflection", "bad_case"
    ],
    artifact_id: str,
    relation: Literal["input", "output", "reused", "diagnostic"],
    span_id: str | None = None,
    content_hash: str | None = None,
) -> None:
    if trace_id is None:
        return
    recorder.link_artifact(
        workflow_kind=workflow_kind,
        subject_id=subject_id,
        trace_id=trace_id,
        span_id=span_id,
        artifact_kind=artifact_kind,
        artifact_id=artifact_id,
        relation=relation,
        content_hash=content_hash,
    )


def _trace_reasoning_review_finalize(
    database: Database,
    settings: Settings,
    *,
    run_id: str,
    mode: str,
    input_hash: str,
    rows: list[ReasoningReviewV2],
    signal_artifacts: list[tuple[str, str]],
    occurred_at: datetime,
) -> None:
    subject_id = f"reasoning:{run_id}"
    recorder = TraceRecorder(database, settings)
    trace_id = recorder.start_trace(
        workflow_kind="agent_eval",
        subject_id=subject_id,
        mode=mode,
        input_hash=input_hash,
        attributes={"artifact_count": len(rows), "handoff_stage": "finalize"},
        started_at=occurred_at,
    )
    validator = _trace_span_snapshot(
        recorder,
        trace_id=trace_id,
        workflow_kind="agent_eval",
        subject_id=subject_id,
        node_id="reasoning.validator",
        name="validate blind reasoning reviews",
        span_kind="validator",
        started_at=occurred_at,
        completed_at=occurred_at,
        model_name=FROZEN_CODEX_MODEL,
        prompt_version="reasoning-review-v2",
        input_value={"run_id": run_id, "review_count": len(rows)},
        output_value={"review_hashes": sorted(row.content_hash for row in rows)},
    )
    persistence = _trace_span_snapshot(
        recorder,
        trace_id=trace_id,
        workflow_kind="agent_eval",
        subject_id=subject_id,
        node_id="reasoning.persistence",
        name="persist sealed reasoning reviews",
        span_kind="persistence",
        parent_span_id=validator,
        started_at=occurred_at,
        completed_at=occurred_at,
        output_value={"review_count": len(rows)},
        attributes={"artifact_count": len(rows)},
    )
    for signal_id, signal_hash in signal_artifacts:
        _trace_link(
            recorder,
            trace_id=trace_id,
            workflow_kind="agent_eval",
            subject_id=subject_id,
            span_id=validator,
            artifact_kind="signal",
            artifact_id=signal_id,
            relation="input",
            content_hash=signal_hash,
        )
    for row in rows:
        _trace_link(
            recorder,
            trace_id=trace_id,
            workflow_kind="agent_eval",
            subject_id=subject_id,
            span_id=persistence,
            artifact_kind="reasoning_review",
            artifact_id=row.id,
            relation="output",
            content_hash=row.content_hash,
        )
    _finish_artifact_trace(
        recorder,
        database,
        workflow_kind="agent_eval",
        subject_id=subject_id,
        trace_id=trace_id,
        expected_artifacts=len(rows),
    )


def _trace_outcome_evaluation(
    database: Database,
    settings: Settings,
    *,
    observation: OutcomeObservationV2,
    observation_id: str,
    rows: list[SignalEvaluationV2],
    forecast_evaluations: list[ForecastEvaluationV2],
    occurred_at: datetime,
) -> None:
    subject_id = f"evaluation:{observation_id}"
    recorder = TraceRecorder(database, settings)
    trace_id = recorder.start_trace(
        workflow_kind="agent_eval",
        subject_id=subject_id,
        mode=observation.mode,
        input_hash=observation.content_hash,
        target_id=observation.target_id,
        attributes={
            "artifact_count": len(rows) + len(forecast_evaluations),
            "target_date": observation.target_date.isoformat(),
        },
        started_at=occurred_at,
    )
    validator = _trace_span_snapshot(
        recorder,
        trace_id=trace_id,
        workflow_kind="agent_eval",
        subject_id=subject_id,
        node_id="evaluation.validator",
        name="validate outcome and scoreability",
        span_kind="validator",
        started_at=occurred_at,
        completed_at=occurred_at,
        input_value={
            "observation_id": observation_id,
            "observation_hash": observation.content_hash,
        },
        output_value={"signal_evaluation_count": len(rows)},
        attributes={"target_date": observation.target_date.isoformat()},
    )
    persistence = _trace_span_snapshot(
        recorder,
        trace_id=trace_id,
        workflow_kind="agent_eval",
        subject_id=subject_id,
        node_id="evaluation.persistence",
        name="persist immutable evaluation artifacts",
        span_kind="persistence",
        parent_span_id=validator,
        started_at=occurred_at,
        completed_at=occurred_at,
        output_value={
            "signal_evaluations": len(rows),
            "forecast_evaluations": len(forecast_evaluations),
        },
        attributes={"artifact_count": len(rows) + len(forecast_evaluations)},
    )
    for row in [*rows, *forecast_evaluations]:
        _trace_link(
            recorder,
            trace_id=trace_id,
            workflow_kind="agent_eval",
            subject_id=subject_id,
            span_id=persistence,
            artifact_kind="evaluation",
            artifact_id=row.id,
            relation="output",
            content_hash=row.content_hash,
        )
    _finish_artifact_trace(
        recorder,
        database,
        workflow_kind="agent_eval",
        subject_id=subject_id,
        trace_id=trace_id,
        expected_artifacts=len(rows) + len(forecast_evaluations),
    )


def _trace_reflection_create(
    database: Database,
    settings: Settings,
    *,
    row: ReflectionV2,
    mode: str,
    forecast_id: str,
    forecast_hash: str,
    evaluation_id: str,
    evaluation_hash: str,
    occurred_at: datetime,
) -> None:
    subject_id = f"reflection:{row.id}"
    recorder = TraceRecorder(database, settings)
    trace_id = recorder.start_trace(
        workflow_kind="reflection",
        subject_id=subject_id,
        mode=mode,
        input_hash=content_hash(
            {"forecast_hash": forecast_hash, "evaluation_hash": evaluation_hash}
        ),
        target_id=row.target_id,
        attributes={
            "artifact_count": 1,
            "target_date": row.target_date.isoformat(),
        },
        started_at=occurred_at,
    )
    validator = _trace_span_snapshot(
        recorder,
        trace_id=trace_id,
        workflow_kind="reflection",
        subject_id=subject_id,
        node_id="reflection.validator",
        name="validate evaluated forecast binding",
        span_kind="validator",
        started_at=occurred_at,
        completed_at=occurred_at,
        input_value={"forecast_id": forecast_id, "evaluation_id": evaluation_id},
        output_value={"reflection_hash": row.content_hash},
    )
    persistence = _trace_span_snapshot(
        recorder,
        trace_id=trace_id,
        workflow_kind="reflection",
        subject_id=subject_id,
        node_id="reflection.persistence",
        name="persist target-scoped reflection",
        span_kind="persistence",
        parent_span_id=validator,
        started_at=occurred_at,
        completed_at=occurred_at,
        output_value={"reflection_hash": row.content_hash},
        attributes={"artifact_count": 1},
    )
    for artifact_kind, artifact_id, relation, artifact_hash, span_id in (
        ("forecast", forecast_id, "input", forecast_hash, validator),
        ("evaluation", evaluation_id, "input", evaluation_hash, validator),
        ("reflection", row.id, "output", row.content_hash, persistence),
    ):
        _trace_link(
            recorder,
            trace_id=trace_id,
            workflow_kind="reflection",
            subject_id=subject_id,
            span_id=span_id,
            artifact_kind=artifact_kind,  # type: ignore[arg-type]
            artifact_id=artifact_id,
            relation=relation,  # type: ignore[arg-type]
            content_hash=artifact_hash,
        )
    _finish_artifact_trace(
        recorder,
        database,
        workflow_kind="reflection",
        subject_id=subject_id,
        trace_id=trace_id,
        expected_artifacts=1,
    )


def _finish_artifact_trace(
    recorder: TraceRecorder,
    database: Database,
    *,
    workflow_kind: Literal["prediction", "reflection", "agent_eval"],
    subject_id: str,
    trace_id: str | None,
    expected_artifacts: int,
) -> None:
    if trace_id is None:
        return
    telemetry_complete = True
    try:
        with database.session_factory() as session:
            trace = session.get(AgentTrace, trace_id)
            linked = (
                len(
                    session.scalars(
                        select(AgentTraceArtifactLink).where(
                            AgentTraceArtifactLink.trace_id == trace_id
                        )
                    ).all()
                )
                if trace is not None
                else 0
            )
            telemetry_complete = (
                trace is not None
                and trace.telemetry_complete
                and linked >= expected_artifacts
            )
    except Exception:
        telemetry_complete = False
    if not telemetry_complete:
        recorder.mark_degraded(
            workflow_kind=workflow_kind,
            subject_id=subject_id,
            trace_id=trace_id,
            note="artifact trace was incomplete after authoritative commit",
        )
    recorder.finish_trace(
        workflow_kind=workflow_kind,
        subject_id=subject_id,
        trace_id=trace_id,
        status="completed" if telemetry_complete else "degraded",
        attributes={"artifact_count": expected_artifacts},
    )


def _recover_completed_research_run(
    database: Database,
    settings: Settings,
    *,
    job_dir: Path,
    request: CodexHandoffRequestV3,
    raw_drafts: bytes,
    run: ResearchRunV2,
) -> ResearchRunV2:
    """Restore a missing local receipt from the immutable database receipt."""

    if (
        run.input_hash != request.input_hash
        or run.request_hash != request.request_hash
        or run.snapshot_hash != request.snapshot.content_hash
        or run.program_hash != request.program.content_hash
    ):
        raise ResearchV2Error("completed database seals no longer match the handoff request")
    receipt = ReceiptV2.model_validate(run.receipt)
    drafts_raw_hash = __import__("hashlib").sha256(raw_drafts).hexdigest()
    if receipt.run_id != run.id or receipt.drafts_raw_hash != drafts_raw_hash:
        raise ResearchV2Error("completed receipt no longer binds drafts.json")
    receipt_path = job_dir / "receipt.json"
    if receipt_path.exists():
        file_receipt = _read_model(receipt_path, ReceiptV2)
        if file_receipt != receipt:
            raise ResearchV2Error("receipt.json conflicts with the immutable database receipt")
    else:
        _atomic_write(receipt_path, canonical_json(receipt))
    _prepare_reasoning_review_best_effort(
        database,
        settings,
        run_id=run.id,
        job_dir=job_dir,
    )
    return run


def _prepare_reasoning_review_best_effort(
    database: Database,
    settings: Settings,
    *,
    run_id: str,
    job_dir: Path,
) -> Exception | None:
    """Keep advisory reasoning-task I/O outside the authoritative forecast seal."""

    try:
        prepare_reasoning_review(database, settings, run_id=run_id, job_dir=job_dir)
    except Exception as exc:
        return exc
    return None


def _close_failed_finalize_trace(
    database: Database,
    settings: Settings,
    *,
    job_dir: Path,
    error: Exception,
) -> None:
    """Best-effort failure seal; a subsequent retry always gets a new attempt."""

    try:
        resolved = _validated_explicit_job_dir(settings, job_dir)
        request = _read_model(resolved / "input.json", CodexHandoffRequestV3)
    except Exception:
        return
    recorder = TraceRecorder(database, settings)
    recorder.finish_trace(
        workflow_kind="prediction",
        subject_id=request.run_id,
        trace_id=recorder.trace_id_for(
            "prediction", request.run_id, running_only=True
        ),
        status="failed",
        error=error,
        attributes={"handoff_stage": "finalize"},
    )


def validate_research_draft_bundle(
    settings: Settings,
    *,
    job_dir: Path,
    raw_drafts: bytes,
) -> tuple[CodexHandoffRequestV3, CodexDraftBundleV3]:
    """Validate an external v2 draft before an operator publishes it.

    The check is intentionally persistence-free so a dispatcher can validate a
    candidate in private staging, then create the sole authorized
    ``drafts.json`` file without leaving a rejected bundle in the handoff.
    Finalize repeats this exact check from the published bytes.
    """

    job_dir = _validated_explicit_job_dir(settings, job_dir)
    request = _read_model(job_dir / "input.json", CodexHandoffRequestV3)
    bundle = CodexDraftBundleV3.model_validate_json(raw_drafts)
    if bundle.run_id != request.run_id or bundle.request_hash != request.request_hash:
        raise ResearchV2Error("draft bundle does not bind the frozen request")
    by_assignment = {item.assignment_id: item for item in bundle.drafts}
    codex_assignments = [
        item for item in request.assignments if item.producer == "codex"
    ]
    expected = {item.assignment_id for item in codex_assignments}
    if set(by_assignment) != expected:
        raise ResearchV2Error("draft bundle must contain exactly every assignment")
    wiki = FrozenWikiCatalog(request.frozen_wiki)
    evidence_ids = {item.item_id for item in request.snapshot.items}
    assignments = {item.assignment_id: item for item in request.assignments}
    for record in bundle.drafts:
        assignment = assignments[record.assignment_id]
        draft = record.draft
        if (
            draft.target_id != assignment.target_id
            or draft.signal_kind != assignment.signal_kind
            or draft.natural_horizon != assignment.natural_horizon
            or draft.decision_horizon != assignment.decision_horizon
        ):
            raise ResearchV2Error(f"draft identity mismatch for {assignment.assignment_id}")
        if not set(draft.evidence_item_ids).issubset(evidence_ids):
            raise ResearchV2Error("draft references evidence outside the frozen snapshot")
        if draft.state_available != assignment.state_available:
            raise ResearchV2Error("draft changed the host-derived natural-state availability")
        entry = wiki.get(assignment.wiki_entry_id, include_body=False)
        if (
            entry is None
            or draft.wiki_entry_id != assignment.wiki_entry_id
            or draft.wiki_version != assignment.wiki_version
            or draft.wiki_section != assignment.wiki_section
            or draft.wiki_content_hash != assignment.wiki_content_hash
            or draft.wiki_version != entry.version
            or draft.wiki_content_hash != entry.content_hash
        ):
            raise ResearchV2Error("draft changed the frozen Wiki identity")
    return request, bundle


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
