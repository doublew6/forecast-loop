"""Audited file handoff between a Codex task and the committee workflow.

The database and the frozen ``input.json`` package are the trust anchors.  Codex
may only fill ``drafts.json``; it cannot select new evidence, change the Wiki
snapshot, aggregate the committee result, or finalize a run through HTTP.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator
from sqlalchemy import select

from ..adapters import LocalJsonQuantSignalSource
from ..agent_contracts import (
    SignalEnvelope,
    SignalInputBinding,
    SignalTarget,
    agent_spec,
)
from ..config import Settings
from ..db import Database
from ..domain import AGENT_BY_ID, Horizon, RunStatus
from ..market_universe import (
    DEFAULT_MARKET_UNIVERSE,
    MarketUniverseSpec,
    load_market_universe,
)
from ..models import AgentOpinion, Forecast, WorkflowRun
from ..ports import EvidenceSnapshotSource, QuantSignalCandidate
from ..schemas import AgentDraft, APIModel, FrozenEvidenceSnapshot
from ..serializers import forecast_read, opinion_read
from ..workflow import (
    EFFECTIVE_RESEARCH_AGENT_IDS,
    STRATEGY_AGENT_ID,
    CommitteeState,
    CommitteeWorkflow,
    PreparedRun,
    WorkflowRuntimeMode,
    workflow_runtime_versions,
)
from .provider import (
    CODEX_FILE_PROVIDER_NAME,
    LEGACY_CODEX_FILE_PROVIDER_NAME,
    PREVIOUS_CODEX_FILE_PROVIDER_NAME,
)
from .quant_signal import accept_quant_candidate
from .schema_readiness import require_schema_current
from .signal_contract import persist_signal_envelope
from .snapshot import (
    load_evidence_snapshot,
    validate_live_snapshot,
    validate_snapshot_content_hash,
)
from .wiki import FrozenWikiCatalog, WikiCatalog

LEGACY_HANDOFF_PROTOCOL_VERSION = "1.0.0"
PREVIOUS_HANDOFF_PROTOCOL_VERSION = "2.0.0"
HANDOFF_PROTOCOL_VERSION = "3.0.0"
LEGACY_HANDOFF_PROMPT_VERSION = LEGACY_CODEX_FILE_PROVIDER_NAME
PREVIOUS_HANDOFF_PROMPT_VERSION = PREVIOUS_CODEX_FILE_PROVIDER_NAME
HANDOFF_PROMPT_VERSION = CODEX_FILE_PROVIDER_NAME
HandoffProtocolVersion = Literal["1.0.0", "2.0.0", "3.0.0"]
HandoffProviderName = Literal[
    "codex-file-handoff-v1",
    "codex-file-handoff-v2",
    "codex-file-handoff-v3",
]
HANDOFF_WINDOW = timedelta(hours=8)
MAX_JSON_BYTES = 25 * 1024 * 1024
RESEARCH_AGENT_IDS = tuple(EFFECTIVE_RESEARCH_AGENT_IDS)
CRITIC_AGENT_ID = "risk_critic_agent"
DRAFT_AGENT_IDS = (*RESEARCH_AGENT_IDS, STRATEGY_AGENT_ID, CRITIC_AGENT_ID)
QUANT_AGENT_ID = "quant_agent"
QUANT_EXTERNAL_INPUT_SCHEMA = "forecast-loop.quant-run-input-binding/v1"
QUANT_SOURCE_RECORD_TYPE = "quant_bundle_signal"


@dataclass(frozen=True, slots=True)
class _PreparedQuantInput:
    snapshot: FrozenEvidenceSnapshot
    candidates: tuple[QuantSignalCandidate, ...]
    external_binding: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _PinnedEvidenceSnapshotSource:
    snapshot: FrozenEvidenceSnapshot

    def load_snapshot(self, *, as_of: datetime) -> FrozenEvidenceSnapshot:
        if self.snapshot.as_of != as_of:
            raise ValueError("pinned evidence snapshot does not match requested as_of")
        return self.snapshot.model_copy(deep=True)


class HandoffAssignment(APIModel):
    """One exact draft slot that Codex is allowed to fill."""

    agent_id: str
    index_code: str
    index_name: str
    horizon: Horizon
    target_date: str
    role: Literal["research", "strategy", "critic"]
    agent_brief: str | None = Field(
        default=None,
        min_length=1,
        max_length=2000,
        exclude_if=lambda value: value is None,
    )
    wiki_entry_id: str
    wiki_title: str
    wiki_version: str
    wiki_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    wiki_sections: list[str] = Field(min_length=1)
    allowed_evidence_item_ids: list[str] = Field(default_factory=list)

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.agent_id, self.index_code, self.horizon.value


class HandoffRequest(APIModel):
    protocol_version: HandoffProtocolVersion = HANDOFF_PROTOCOL_VERSION
    run_id: UUID
    mode: Literal["demo", "live"]
    provider: HandoffProviderName = CODEX_FILE_PROVIDER_NAME
    prepared_at: datetime
    finalize_deadline: datetime
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    workflow_version: str
    decision_schema_version: str
    initial_state: dict[str, Any]
    assignments: list[HandoffAssignment] = Field(min_length=1, max_length=1000)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("prepared_at", "finalize_deadline")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("handoff timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_request(self) -> HandoffRequest:
        if self.finalize_deadline <= self.prepared_at:
            raise ValueError("finalize_deadline must be later than prepared_at")
        identities = [assignment.identity for assignment in self.assignments]
        if len(identities) != len(set(identities)):
            raise ValueError("handoff assignments must have unique identities")
        expected_provider = _provider_for_protocol(self.protocol_version)
        if self.provider != expected_provider:
            raise ValueError("handoff protocol_version and provider must use the same version")
        if self.protocol_version != LEGACY_HANDOFF_PROTOCOL_VERSION:
            missing_briefs = [
                assignment.identity
                for assignment in self.assignments
                if assignment.agent_brief is None
            ]
            if missing_briefs:
                raise ValueError(
                    "v2/v3 handoff assignments must freeze an agent_brief; "
                    f"missing={missing_briefs}"
                )
        _validate_handoff_runtime_versions(
            protocol_version=self.protocol_version,
            initial_state=self.initial_state,
            workflow_version=self.workflow_version,
            decision_schema_version=self.decision_schema_version,
        )
        return self


class HandoffGeneratorIdentity(APIModel):
    """Untrusted audit label supplied by the operator/Codex task."""

    surface: Literal["codex"] = "codex"
    task_id: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=200)


class HandoffDraftRecord(APIModel):
    agent_id: str
    index_code: str
    horizon: Horizon
    agent_brief: str | None = Field(
        default=None,
        min_length=1,
        max_length=2000,
        exclude_if=lambda value: value is None,
    )
    draft: AgentDraft

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.agent_id, self.index_code, self.horizon.value


class HandoffDraftBundle(APIModel):
    protocol_version: HandoffProtocolVersion = HANDOFF_PROTOCOL_VERSION
    run_id: UUID
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    generated_by: HandoffGeneratorIdentity
    drafts: list[HandoffDraftRecord] = Field(min_length=1, max_length=1000)

    @field_validator("generated_at")
    @classmethod
    def generated_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def draft_identities_are_unique(self) -> HandoffDraftBundle:
        identities = [record.identity for record in self.drafts]
        if len(identities) != len(set(identities)):
            raise ValueError("draft identities must be unique")
        return self


class HandoffReceipt(APIModel):
    protocol_version: HandoffProtocolVersion = HANDOFF_PROTOCOL_VERSION
    run_id: UUID
    status: Literal["completed", "failed"]
    finalized_at: datetime
    provider: HandoffProviderName = CODEX_FILE_PROVIDER_NAME
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_raw_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    drafts_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    drafts_raw_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    opinion_count: int = Field(ge=0)
    forecast_count: int = Field(ge=0)
    generated_by: HandoffGeneratorIdentity
    error: str | None = None
    receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("finalized_at")
    @classmethod
    def finalized_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("finalized_at must be timezone-aware")
        return value


class FileHandoffProvider:
    """Replay prevalidated Codex drafts through the normal committee graph."""

    def __init__(
        self,
        records: list[HandoffDraftRecord] | None = None,
        *,
        provider_name: HandoffProviderName = CODEX_FILE_PROVIDER_NAME,
    ) -> None:
        self.name = provider_name
        self.prompt_version = provider_name
        self._drafts = {
            record.identity: record.draft.model_copy(deep=True) for record in (records or [])
        }

    def model_name_for_agent(self, agent_id: str) -> str:
        del agent_id
        return self.name

    def research(self, *, agent_id: str, index, horizon: Horizon, **_) -> AgentDraft:
        return self._get(agent_id, index.code, horizon)

    def strategize(self, *, index, horizon: Horizon, **_) -> AgentDraft:
        return self._get(STRATEGY_AGENT_ID, index.code, horizon)

    def criticize(self, *, index, horizon: Horizon, **_) -> AgentDraft:
        return self._get(CRITIC_AGENT_ID, index.code, horizon)

    def _get(self, agent_id: str, index_code: str, horizon: Horizon) -> AgentDraft:
        identity = (agent_id, index_code, horizon.value)
        try:
            return self._drafts[identity].model_copy(deep=True)
        except KeyError as exc:  # pragma: no cover - guarded by bundle validation
            raise RuntimeError(f"missing handoff draft for {identity}") from exc


def prepare_handoff(
    settings: Settings,
    *,
    as_of: datetime | None = None,
    now: datetime | None = None,
    handoff_root: Path | None = None,
    quant_manifest_path: Path | None = None,
    evidence_source: EvidenceSnapshotSource | None = None,
    protocol_version: HandoffProtocolVersion = HANDOFF_PROTOCOL_VERSION,
) -> Path:
    """Freeze a committee input package and persist its database-side seals."""

    _validate_execution_mode(settings)
    zone = ZoneInfo(settings.timezone)
    prepared_at = _normalize_now(now, zone)
    as_of = _validate_live_prepare_time(
        settings,
        requested_as_of=as_of,
        prepared_at=prepared_at,
        evidence_source=evidence_source,
    )
    if not settings.use_demo_provider and as_of is not None:
        pinned_snapshot = load_evidence_snapshot(
            settings,
            as_of=as_of,
            source=evidence_source,
        )
        if pinned_snapshot.created_at > prepared_at:
            raise ValueError(
                "live evidence snapshot created_at cannot be later than prepare_time"
            )
        evidence_source = _PinnedEvidenceSnapshotSource(pinned_snapshot)
    if quant_manifest_path is not None and as_of is None:
        # Demo prepare historically lets the workflow choose the local 15:00
        # cutoff. Make that choice explicit only for Quant-bound runs so the
        # bundle and the subsequently frozen workflow snapshot cannot diverge.
        universe = load_market_universe(settings.market_universe_path)
        close_hour, close_minute = (
            int(part) for part in universe.session_close.split(":", maxsplit=1)
        )
        as_of = prepared_at.replace(
            hour=close_hour,
            minute=close_minute,
            second=0,
            microsecond=0,
        )
    root = _prepare_root(handoff_root or settings.handoff_root)
    database = Database(settings.database_url)
    workflow: CommitteeWorkflow | None = None
    prepared: PreparedRun | None = None
    job_dir: Path | None = None
    try:
        require_schema_current(database.engine)
        runtime_mode = _runtime_mode_for_protocol(protocol_version)
        forecast_horizons = _forecast_horizons_for_protocol(protocol_version)
        quant_input = (
            _prepare_quant_input(
                settings,
                manifest_path=quant_manifest_path,
                as_of=as_of,
                accepted_at=prepared_at,
                evidence_source=evidence_source,
                forecast_horizons=forecast_horizons,
            )
            if quant_manifest_path is not None
            else None
        )
        provider_name = _provider_for_protocol(protocol_version)
        provider = FileHandoffProvider(provider_name=provider_name)
        workflow = CommitteeWorkflow(
            settings=settings,
            database=database,
            provider=provider,
            wiki=WikiCatalog.from_settings(settings),
            evidence_source=evidence_source,
            runtime_mode=runtime_mode,
        )
        prepared = workflow.prepare_run(
            as_of=as_of,
            initial_status=RunStatus.AWAITING_DRAFT,
            external_input_bindings=(
                {QUANT_AGENT_ID: quant_input.external_binding} if quant_input is not None else None
            ),
        )
        snapshot = FrozenEvidenceSnapshot.model_validate(prepared.initial["evidence_snapshot"])
        if quant_input is not None and snapshot != quant_input.snapshot:
            raise ValueError("frozen workflow evidence snapshot changed after Quant validation")
        d1_boundary = datetime.combine(
            snapshot.target_sessions[0],
            time.min,
            tzinfo=zone,
        )
        deadline = min(
            prepared_at + HANDOFF_WINDOW,
            d1_boundary - timedelta(microseconds=1),
        )
        if deadline <= prepared_at:
            raise ValueError("the D1 freeze boundary has passed; prepare a new run")
        quant_signals = (
            _accept_quant_signals(
                quant_input,
                run_id=prepared.row.id,
                run_input_hash=prepared.row.input_hash,
                accepted_at=prepared_at,
                submission_deadline=deadline,
                mode=prepared.row.mode,
            )
            if quant_input is not None
            else ()
        )
        quant_audit = (
            _quant_audit(
                quant_input,
                accepted_at=prepared_at,
            )
            if quant_input is not None
            else None
        )
        if quant_audit is not None:
            prepared.initial["data_quality"] = {
                **prepared.initial.get("data_quality", {}),
                "quant": quant_audit,
            }
        assignments = _expected_assignments(
            prepared.initial,
            protocol_version=protocol_version,
        )
        unsigned = HandoffRequest(
            protocol_version=protocol_version,
            run_id=prepared.row.id,
            mode=prepared.row.mode,
            provider=provider_name,
            prepared_at=prepared_at,
            finalize_deadline=deadline,
            input_hash=prepared.row.input_hash,
            workflow_version=workflow.workflow_version,
            decision_schema_version=workflow.decision_schema_version,
            initial_state=dict(prepared.initial),
            assignments=assignments,
            request_hash="0" * 64,
        )
        request_hash = _canonical_hash(unsigned.model_dump(mode="json", exclude={"request_hash"}))
        request = unsigned.model_copy(update={"request_hash": request_hash})
        request_bytes = _json_bytes(request.model_dump(mode="json"))
        request_raw_hash = _sha256(request_bytes)

        job_dir = root / str(request.run_id)
        job_dir.mkdir(mode=0o700)
        os.chmod(job_dir, 0o700)
        _atomic_write(job_dir / "input.json", request_bytes, mode=0o400)
        _atomic_write(
            job_dir / "INSTRUCTIONS.md",
            render_handoff_instructions(request).encode("utf-8"),
            mode=0o400,
        )
        _atomic_write(
            job_dir / "drafts.template.json",
            _json_bytes(build_handoff_draft_template(request)),
            mode=0o600,
        )

        quality = {
            "handoff": {
                "protocol_version": request.protocol_version,
                "provider": request.provider,
                "status": RunStatus.AWAITING_DRAFT.value,
                "job_id": str(request.run_id),
                "prepared_at": prepared_at.isoformat(),
                "finalize_deadline": deadline.isoformat(),
                "request_hash": request_hash,
                "request_raw_hash": request_raw_hash,
            }
        }
        if quant_audit is not None:
            quality["quant"] = quant_audit
        with database.session_factory() as session:
            row = session.get(WorkflowRun, prepared.row.id)
            if row is None or row.status != RunStatus.AWAITING_DRAFT.value:
                raise RuntimeError("prepared handoff run is no longer awaiting a draft")
            for candidate, signal in zip(
                quant_input.candidates if quant_input is not None else (),
                quant_signals,
                strict=True,
            ):
                _, created = persist_signal_envelope(
                    session,
                    signal=signal,
                    authoritative_target=candidate.target,
                    authoritative_accepted_at=prepared_at,
                    authoritative_submission_deadline=deadline,
                    authoritative_provenance=candidate.provenance,
                    run_timezone=settings.timezone,
                    source_record_type=QUANT_SOURCE_RECORD_TYPE,
                    source_record_id=_quant_source_record_id(candidate),
                )
                if not created:
                    raise ValueError(
                        "Quant signal bundle was already admitted for this or another run"
                    )
            row.data_quality = {
                **(row.data_quality or {}),
                **quality,
            }
            session.commit()
        return job_dir
    except Exception as exc:
        if prepared is not None:
            _mark_prepare_failed(database, prepared.row.id, str(exc), zone)
        raise
    finally:
        if workflow is not None:
            workflow.close()
        database.dispose()


def finalize_handoff(
    settings: Settings,
    job_dir: str | Path,
    *,
    now: datetime | None = None,
    handoff_root: Path | None = None,
) -> HandoffReceipt:
    """Validate a Codex draft bundle, execute the graph once, and write a receipt."""

    _validate_execution_mode(settings)
    zone = ZoneInfo(settings.timezone)
    finalized_at = _normalize_now(now, zone)
    directory = _resolve_job_dir(handoff_root or settings.handoff_root, job_dir)
    input_bytes, input_payload = _secure_read_json(directory / "input.json")
    request = HandoffRequest.model_validate(input_payload)
    if str(request.run_id) != directory.name:
        raise ValueError("job directory UUID does not match input.json run_id")
    computed_request_hash = _canonical_hash(
        request.model_dump(mode="json", exclude={"request_hash"})
    )
    if computed_request_hash != request.request_hash:
        raise ValueError("input.json canonical request hash is invalid")
    request_raw_hash = _sha256(input_bytes)

    database = Database(settings.database_url)
    try:
        require_schema_current(database.engine)
        row = _load_and_verify_database_seal(
            database,
            request=request,
            request_raw_hash=request_raw_hash,
            settings=settings,
        )
        if finalized_at > request.finalize_deadline:
            raise ValueError("handoff finalize deadline has passed; prepare a new run")

        snapshot = FrozenEvidenceSnapshot.model_validate(request.initial_state["evidence_snapshot"])
        validate_snapshot_content_hash(snapshot)
        as_of = datetime.fromisoformat(str(request.initial_state["as_of"]))
        universe = _universe_from_state(request.initial_state)
        if request.mode == "live":
            validate_live_snapshot(
                snapshot,
                as_of=as_of,
                instrument_codes=universe.codes,
            )
        expected_assignments = _expected_assignments(
            request.initial_state,
            protocol_version=request.protocol_version,
        )
        if [item.model_dump(mode="json") for item in request.assignments] != [
            item.model_dump(mode="json") for item in expected_assignments
        ]:
            raise ValueError("handoff assignments do not match the frozen input state")

        draft_bytes, draft_payload = _secure_read_json(directory / "drafts.json")
        bundle = HandoffDraftBundle.model_validate(draft_payload)
        drafts_raw_hash = _sha256(draft_bytes)
        drafts_hash = _canonical_hash(bundle.model_dump(mode="json"))
        _validate_bundle(
            bundle,
            request=request,
            finalized_at=finalized_at,
            assignments=expected_assignments,
        )

        audit = {
            "protocol_version": request.protocol_version,
            "provider": request.provider,
            "status": "validating",
            "job_id": str(request.run_id),
            "prepared_at": request.prepared_at.isoformat(),
            "finalize_deadline": request.finalize_deadline.isoformat(),
            "request_hash": request.request_hash,
            "request_raw_hash": request_raw_hash,
            "drafts_hash": drafts_hash,
            "drafts_raw_hash": drafts_raw_hash,
            "generated_at": bundle.generated_at.isoformat(),
            "generated_by": bundle.generated_by.model_dump(mode="json"),
        }
        initial: CommitteeState = dict(request.initial_state)  # type: ignore[assignment]
        initial_quality = initial.get("data_quality", {})
        if not isinstance(initial_quality, dict):
            raise ValueError("frozen data_quality must be a JSON object")
        initial["data_quality"] = {
            **initial_quality,
            "handoff": audit,
        }
        provider = FileHandoffProvider(
            bundle.drafts,
            provider_name=request.provider,
        )
        runtime_mode = _runtime_mode_for_protocol(request.protocol_version)
        workflow = CommitteeWorkflow(
            settings=settings,
            database=database,
            provider=provider,
            wiki=FrozenWikiCatalog(initial["wiki_snapshot"]),
            universe=universe,
            runtime_mode=runtime_mode,
        )
        if (
            request.workflow_version != workflow.workflow_version
            or request.decision_schema_version != workflow.decision_schema_version
        ):
            workflow.close()
            raise ValueError(
                "handoff runtime versions changed after prepare; "
                "restore the prepared workflow and decision schema versions"
            )
        try:
            completed = workflow.execute_prepared(
                PreparedRun(row=row, initial=initial),
            )
        except Exception as exc:
            receipt = _failed_receipt(
                request=request,
                bundle=bundle,
                request_raw_hash=request_raw_hash,
                drafts_hash=drafts_hash,
                drafts_raw_hash=drafts_raw_hash,
                finalized_at=finalized_at,
                error=str(exc),
            )
            _atomic_write(
                directory / "receipt.json",
                _json_bytes(receipt.model_dump(mode="json")),
                mode=0o400,
            )
            os.chmod(directory / "drafts.json", 0o400)
            raise
        finally:
            workflow.close()

        output_hash, opinion_count, forecast_count = _seal_output(
            database,
            run_id=completed.id,
            audit=audit,
        )
        receipt = _build_receipt(
            request=request,
            bundle=bundle,
            status="completed",
            request_raw_hash=request_raw_hash,
            drafts_hash=drafts_hash,
            drafts_raw_hash=drafts_raw_hash,
            output_hash=output_hash,
            opinion_count=opinion_count,
            forecast_count=forecast_count,
            finalized_at=finalized_at,
        )
        _atomic_write(
            directory / "receipt.json",
            _json_bytes(receipt.model_dump(mode="json")),
            mode=0o400,
        )
        os.chmod(directory / "drafts.json", 0o400)
        return receipt
    finally:
        database.dispose()


def _expected_assignments(
    state: dict[str, Any],
    *,
    protocol_version: HandoffProtocolVersion = HANDOFF_PROTOCOL_VERSION,
) -> list[HandoffAssignment]:
    snapshot = FrozenEvidenceSnapshot.model_validate(state["evidence_snapshot"])
    wiki = FrozenWikiCatalog(state["wiki_snapshot"])
    universe = _universe_from_state(state)
    instruments = universe.definitions()
    forecast_horizons = _forecast_horizons_for_protocol(protocol_version)
    target_dates = {
        Horizon.D1: snapshot.target_sessions[0].isoformat(),
        Horizon.D2: snapshot.target_sessions[1].isoformat(),
    }
    assignments: list[HandoffAssignment] = []
    for agent_id in DRAFT_AGENT_IDS:
        role: Literal["research", "strategy", "critic"]
        if agent_id in RESEARCH_AGENT_IDS:
            role = "research"
        elif agent_id == STRATEGY_AGENT_ID:
            role = "strategy"
        else:
            role = "critic"
        for index in instruments:
            entry = wiki.select_for_agent(
                agent_id,
                index_code=index.code,
                preferred_entry_id=index.wiki_entry_id_for(agent_id),
            )
            relevant_evidence = [
                item.id
                for item in snapshot.items
                if not item.entities or index.code in item.entities
            ]
            for horizon in forecast_horizons:
                assignments.append(
                    HandoffAssignment(
                        agent_id=agent_id,
                        index_code=index.code,
                        index_name=index.name,
                        horizon=horizon,
                        target_date=target_dates[horizon],
                        role=role,
                        agent_brief=(
                            (
                                index.agent_brief_for(agent_id)
                                or AGENT_BY_ID[agent_id].role
                            )
                            if protocol_version != LEGACY_HANDOFF_PROTOCOL_VERSION
                            else None
                        ),
                        wiki_entry_id=entry.id,
                        wiki_title=entry.title,
                        wiki_version=entry.version,
                        wiki_content_hash=entry.content_hash,
                        wiki_sections=[section.slug for section in entry.sections],
                        allowed_evidence_item_ids=([] if role == "critic" else relevant_evidence),
                    )
                )
    expected_count = (
        len(DRAFT_AGENT_IDS) * len(instruments) * len(forecast_horizons)
    )
    if len(assignments) != expected_count:  # pragma: no cover - domain shape assertion
        raise RuntimeError(
            f"expected {expected_count} Codex assignments, got {len(assignments)}"
        )
    return assignments


def _universe_from_state(state: dict[str, Any]) -> MarketUniverseSpec:
    raw = state.get("market_universe")
    if raw is None:
        return DEFAULT_MARKET_UNIVERSE
    return MarketUniverseSpec.model_validate(raw)


def _prepare_quant_input(
    settings: Settings,
    *,
    manifest_path: Path,
    as_of: datetime | None,
    accepted_at: datetime,
    evidence_source: EvidenceSnapshotSource | None = None,
    forecast_horizons: tuple[Horizon, ...],
) -> _PreparedQuantInput:
    """Verify one complete Quant bundle against the frozen committee target matrix."""

    if as_of is None:  # pragma: no cover - prepare_handoff normalizes this.
        raise ValueError("Quant handoff requires an explicit as_of")
    universe = load_market_universe(settings.market_universe_path)
    instruments = universe.definitions()
    snapshot = load_evidence_snapshot(
        settings,
        as_of=as_of,
        source=evidence_source,
        universe=universe,
    )
    validate_snapshot_content_hash(snapshot)
    if not settings.use_demo_provider:
        validate_live_snapshot(
            snapshot,
            as_of=as_of,
            instrument_codes=universe.codes,
        )
    if len(snapshot.target_sessions) != 2:
        raise ValueError("Quant handoff requires exactly two frozen D1/D2 sessions")

    configured_manifest = manifest_path.expanduser()
    source = LocalJsonQuantSignalSource(
        root=configured_manifest.parent,
        manifest_path=Path(configured_manifest.name),
    )
    candidates = source.load_candidates(as_of=snapshot.as_of)
    expected_targets = {
        (index.code, horizon.value): SignalTarget(
            index_code=index.code,
            horizon=horizon,
            base_trade_date=snapshot.base_session,
            target_date=snapshot.target_sessions[0 if horizon is Horizon.D1 else 1],
            as_of=snapshot.as_of,
            data_cutoff=snapshot.data_cutoff,
        )
        for index in instruments
        for horizon in forecast_horizons
    }
    by_identity: dict[tuple[str, str], QuantSignalCandidate] = {}
    for candidate in candidates:
        identity = (
            candidate.target.index_code,
            candidate.target.horizon.value,
        )
        if identity in by_identity:
            raise ValueError(f"Quant target matrix contains duplicate identity {identity}")
        by_identity[identity] = candidate

    if set(by_identity) != set(expected_targets):
        missing = sorted(set(expected_targets) - set(by_identity))
        extra = sorted(set(by_identity) - set(expected_targets))
        expected_matrix = (
            (
                "exactly 5 indexes x "
                + "/".join(horizon.value for horizon in forecast_horizons)
            )
            if universe.content_hash == DEFAULT_MARKET_UNIVERSE.content_hash
            else "the configured instrument matrix"
        )
        raise ValueError(
            f"Quant target matrix must contain {expected_matrix}; "
            f"missing={missing}, extra={extra}"
        )
    for identity, expected in expected_targets.items():
        actual = by_identity[identity].target
        if actual != expected:
            raise ValueError(
                "Quant target does not match the frozen evidence sessions for "
                f"{identity}: expected={expected.model_dump(mode='json')}, "
                f"actual={actual.model_dump(mode='json')}"
            )

    ordered = tuple(
        by_identity[(index.code, horizon.value)]
        for index in instruments
        for horizon in forecast_horizons
    )
    expected_count = len(instruments) * len(forecast_horizons)
    if len(ordered) != expected_count:  # pragma: no cover - domain shape assertion.
        raise RuntimeError(f"expected {expected_count} Quant targets, got {len(ordered)}")
    if any(candidate.draft.submitted_at > accepted_at for candidate in ordered):
        raise ValueError("Quant bundle generated_at may not be after prepare time")
    first = ordered[0]
    bundle_ids = {_quant_source_value(candidate, "bundle_id") for candidate in ordered}
    snapshot_content_hashes = {
        _quant_source_value(candidate, "input_snapshot_content_hash") for candidate in ordered
    }
    bundle_hashes = {candidate.bundle_content_hash for candidate in ordered}
    manifest_hashes = {candidate.manifest_sha256 for candidate in ordered}
    evidence_snapshot_hashes = {
        candidate.evidence_snapshot_hash for candidate in ordered
    }
    market_universe_hashes = {
        candidate.market_universe_hash for candidate in ordered
    }
    input_snapshot_hashes = {
        candidate.provenance.artifact_hashes.get("input_snapshot") for candidate in ordered
    }
    if (
        len(bundle_ids) != 1
        or len(snapshot_content_hashes) != 1
        or len(bundle_hashes) != 1
        or len(manifest_hashes) != 1
        or len(evidence_snapshot_hashes) != 1
        or None in evidence_snapshot_hashes
        or len(market_universe_hashes) != 1
        or None in market_universe_hashes
        or len(input_snapshot_hashes) != 1
        or None in input_snapshot_hashes
    ):
        raise ValueError("Quant candidates do not share one immutable bundle binding")
    evidence_snapshot_hash = next(iter(evidence_snapshot_hashes))
    if evidence_snapshot_hash != snapshot.content_hash:
        raise ValueError(
            "Quant bundle is bound to a different Evidence Snapshot"
        )
    market_universe_hash = next(iter(market_universe_hashes))
    if market_universe_hash != universe.content_hash:
        raise ValueError("Quant bundle is bound to a different market universe")

    spec = agent_spec(QUANT_AGENT_ID)
    if spec.agent_version != "0.3.0" or spec.participation.mode.value != "shadow":
        raise ValueError("active Quant AgentSpec must be version 0.3.0 shadow")
    external_binding = {
        "schema_version": QUANT_EXTERNAL_INPUT_SCHEMA,
        "agent_id": QUANT_AGENT_ID,
        "agent_version": spec.agent_version,
        "agent_spec_hash": spec.content_hash,
        "participation_policy_id": spec.participation.policy_id,
        "participation_policy_version": spec.participation.policy_version,
        "participation_mode": spec.participation.mode.value,
        "bundle_id": next(iter(bundle_ids)),
        "bundle_content_hash": first.bundle_content_hash,
        "manifest_sha256": first.manifest_sha256,
        "input_snapshot_sha256": first.provenance.artifact_hashes["input_snapshot"],
        "input_snapshot_content_hash": next(iter(snapshot_content_hashes)),
        "evidence_snapshot_content_hash": evidence_snapshot_hash,
        "market_universe_content_hash": market_universe_hash,
        "as_of": snapshot.as_of.isoformat(),
        "data_cutoff": snapshot.data_cutoff.isoformat(),
        "generated_at": first.draft.submitted_at.isoformat(),
        "signal_count": len(ordered),
        "signal_ids": [candidate.draft.signal_id for candidate in ordered],
        "target_matrix_hash": _canonical_hash(
            [candidate.target.model_dump(mode="json") for candidate in ordered]
        ),
        "decision_weight_total": 0,
        "activation_status": "shadow_locked",
    }
    return _PreparedQuantInput(
        snapshot=snapshot,
        candidates=ordered,
        external_binding=external_binding,
    )


def _accept_quant_signals(
    prepared: _PreparedQuantInput,
    *,
    run_id: str,
    run_input_hash: str,
    accepted_at: datetime,
    submission_deadline: datetime,
    mode: Literal["demo", "live"],
) -> tuple[SignalEnvelope, ...]:
    spec = agent_spec(QUANT_AGENT_ID)
    return tuple(
        accept_quant_candidate(
            candidate=candidate,
            mode=mode,
            target=candidate.target,
            accepted_at=accepted_at,
            submission_deadline=submission_deadline,
            input_binding=SignalInputBinding(
                run_id=run_id,
                run_input_hash=run_input_hash,
                agent_spec_hash=spec.content_hash,
                evidence_snapshot_hash=prepared.snapshot.content_hash,
            ),
        )
        for candidate in prepared.candidates
    )


def _quant_audit(
    prepared: _PreparedQuantInput,
    *,
    accepted_at: datetime,
) -> dict[str, Any]:
    binding = prepared.external_binding
    return {
        "status": "accepted",
        "agent_id": binding["agent_id"],
        "agent_version": binding["agent_version"],
        "agent_spec_hash": binding["agent_spec_hash"],
        "participation_mode": binding["participation_mode"],
        "routing_lane": "shadow_benchmark",
        "activation_status": binding["activation_status"],
        "signal_count": binding["signal_count"],
        "bundle_id": binding["bundle_id"],
        "bundle_content_hash": binding["bundle_content_hash"],
        "manifest_sha256": binding["manifest_sha256"],
        "input_snapshot_sha256": binding["input_snapshot_sha256"],
        "input_snapshot_content_hash": binding["input_snapshot_content_hash"],
        "evidence_snapshot_content_hash": binding["evidence_snapshot_content_hash"],
        "market_universe_content_hash": binding["market_universe_content_hash"],
        "decision_weight_total": binding["decision_weight_total"],
        "accepted_at": accepted_at.isoformat(),
    }


def _quant_source_value(
    candidate: QuantSignalCandidate,
    key: str,
) -> str:
    value = candidate.draft.source_payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Quant candidate source_payload is missing {key}")
    return value


def _quant_source_record_id(candidate: QuantSignalCandidate) -> str:
    return _canonical_hash(
        {
            "manifest_sha256": candidate.manifest_sha256,
            "signal_id": candidate.draft.signal_id,
        }
    )


def _validate_bundle(
    bundle: HandoffDraftBundle,
    *,
    request: HandoffRequest,
    finalized_at: datetime,
    assignments: list[HandoffAssignment],
) -> None:
    if bundle.run_id != request.run_id:
        raise ValueError("drafts.json run_id does not match input.json")
    if bundle.protocol_version != request.protocol_version:
        raise ValueError("drafts.json protocol_version does not match input.json")
    if bundle.input_hash != request.input_hash:
        raise ValueError("drafts.json input_hash does not match the frozen run")
    if bundle.request_hash != request.request_hash:
        raise ValueError("drafts.json request_hash does not match input.json")
    if bundle.generated_at < request.prepared_at:
        raise ValueError("drafts.json was generated before the handoff was prepared")
    if bundle.generated_at > request.finalize_deadline:
        raise ValueError("drafts.json was generated after the finalize deadline")
    if bundle.generated_at > finalized_at + timedelta(minutes=5):
        raise ValueError("drafts.json generated_at is implausibly in the future")

    by_identity = {assignment.identity: assignment for assignment in assignments}
    actual_identities = {record.identity for record in bundle.drafts}
    if actual_identities != set(by_identity):
        missing = sorted(set(by_identity) - actual_identities)
        extra = sorted(actual_identities - set(by_identity))
        raise ValueError(f"draft identity matrix is incomplete; missing={missing}, extra={extra}")
    for record in bundle.drafts:
        assignment = by_identity[record.identity]
        draft = record.draft
        if record.agent_brief != assignment.agent_brief:
            raise ValueError(f"{record.identity} changed the assigned agent brief")
        if draft.wiki_entry_id != assignment.wiki_entry_id:
            raise ValueError(f"{record.identity} changed the assigned Wiki entry")
        if draft.wiki_section not in assignment.wiki_sections:
            raise ValueError(f"{record.identity} referenced an unavailable Wiki section")
        evidence_ids = set(draft.evidence_item_ids)
        allowed_ids = set(assignment.allowed_evidence_item_ids)
        if not evidence_ids.issubset(allowed_ids):
            raise ValueError(f"{record.identity} introduced evidence outside the snapshot")
        if request.mode == "live" and assignment.role != "critic" and not evidence_ids:
            raise ValueError(f"live {record.identity} must cite frozen evidence IDs")
        if assignment.role == "critic" and evidence_ids:
            raise ValueError("risk critic cannot introduce evidence_item_ids")
        _reject_placeholders(record)


def _reject_placeholders(record: HandoffDraftRecord) -> None:
    draft = record.draft
    values = [
        draft.summary,
        *draft.evidence,
        *draft.counter_evidence,
        *draft.invalidation_conditions,
    ]
    markers = ("todo", "placeholder", "待填写", "在此填写")
    if any(marker in value.lower() for value in values for marker in markers):
        raise ValueError(f"{record.identity} still contains template placeholders")


def _load_and_verify_database_seal(
    database: Database,
    *,
    request: HandoffRequest,
    request_raw_hash: str,
    settings: Settings,
) -> WorkflowRun:
    expected_mode = "demo" if settings.use_demo_provider else "live"
    if request.mode != expected_mode:
        raise ValueError(
            f"handoff mode is {request.mode}, but runtime is configured for {expected_mode}"
        )
    with database.session_factory() as session:
        row = session.get(WorkflowRun, str(request.run_id))
        if row is None:
            raise RuntimeError("handoff run is missing from the database")
        if row.status != RunStatus.AWAITING_DRAFT.value:
            raise RuntimeError(f"handoff run cannot finalize from status {row.status}")
        if row.mode != request.mode or row.input_hash != request.input_hash:
            raise ValueError("database run identity does not match input.json")
        request_universe = _universe_from_state(request.initial_state)
        if row.market_universe_hash != request_universe.content_hash:
            raise ValueError(
                "database run market universe does not match input.json"
            )
        if _aware(row.as_of, settings.timezone) != datetime.fromisoformat(
            str(request.initial_state["as_of"])
        ):
            raise ValueError("database as_of does not match the frozen state")
        if _aware(row.data_cutoff, settings.timezone) != datetime.fromisoformat(
            str(request.initial_state["data_cutoff"])
        ):
            raise ValueError("database data_cutoff does not match the frozen state")
        handoff = (row.data_quality or {}).get("handoff", {})
        expected = {
            "protocol_version": request.protocol_version,
            "provider": request.provider,
            "job_id": str(request.run_id),
            "request_hash": request.request_hash,
            "request_raw_hash": request_raw_hash,
        }
        if any(handoff.get(key) != value for key, value in expected.items()):
            raise ValueError("input.json does not match the database-side handoff seal")
        request_quality = request.initial_state.get("data_quality", {})
        if not isinstance(request_quality, dict):
            raise ValueError("input.json data_quality must be a JSON object")
        request_quant = request_quality.get("quant")
        database_quant = (row.data_quality or {}).get("quant")
        if request_quant is not None or database_quant is not None:
            if request_quant != database_quant:
                raise ValueError("input.json Quant audit does not match the database-side seal")
        session.expunge(row)
        return row


def _seal_output(
    database: Database,
    *,
    run_id: str,
    audit: dict[str, Any],
) -> tuple[str, int, int]:
    with database.session_factory() as session:
        row = session.get(WorkflowRun, run_id)
        if row is None or row.status != RunStatus.COMPLETED.value:
            raise RuntimeError("committee output was not completed")
        opinions = session.scalars(
            select(AgentOpinion)
            .where(AgentOpinion.run_id == run_id)
            .order_by(AgentOpinion.agent_id, AgentOpinion.index_code, AgentOpinion.horizon)
        ).all()
        forecasts = session.scalars(
            select(Forecast)
            .where(Forecast.run_id == run_id)
            .order_by(Forecast.index_code, Forecast.horizon)
        ).all()
        output = {
            "run_id": run_id,
            "input_hash": row.input_hash,
            "opinions": [opinion_read(item).model_dump(mode="json") for item in opinions],
            "forecasts": [forecast_read(item).model_dump(mode="json") for item in forecasts],
        }
        output_hash = _canonical_hash(output)
        row.data_quality = {
            **(row.data_quality or {}),
            "handoff": {
                **audit,
                "status": "completed",
                "output_hash": output_hash,
                "opinion_count": len(opinions),
                "forecast_count": len(forecasts),
            },
        }
        session.commit()
        return output_hash, len(opinions), len(forecasts)


def _failed_receipt(
    *,
    request: HandoffRequest,
    bundle: HandoffDraftBundle,
    request_raw_hash: str,
    drafts_hash: str,
    drafts_raw_hash: str,
    finalized_at: datetime,
    error: str,
) -> HandoffReceipt:
    return _build_receipt(
        request=request,
        bundle=bundle,
        status="failed",
        request_raw_hash=request_raw_hash,
        drafts_hash=drafts_hash,
        drafts_raw_hash=drafts_raw_hash,
        output_hash=None,
        opinion_count=0,
        forecast_count=0,
        finalized_at=finalized_at,
        error=error,
    )


def _build_receipt(
    *,
    request: HandoffRequest,
    bundle: HandoffDraftBundle,
    status: Literal["completed", "failed"],
    request_raw_hash: str,
    drafts_hash: str,
    drafts_raw_hash: str,
    output_hash: str | None,
    opinion_count: int,
    forecast_count: int,
    finalized_at: datetime,
    error: str | None = None,
) -> HandoffReceipt:
    unsigned = HandoffReceipt(
        protocol_version=request.protocol_version,
        run_id=request.run_id,
        status=status,
        finalized_at=finalized_at,
        provider=request.provider,
        input_hash=request.input_hash,
        request_hash=request.request_hash,
        request_raw_hash=request_raw_hash,
        drafts_hash=drafts_hash,
        drafts_raw_hash=drafts_raw_hash,
        output_hash=output_hash,
        opinion_count=opinion_count,
        forecast_count=forecast_count,
        generated_by=bundle.generated_by,
        error=error,
        receipt_hash="0" * 64,
    )
    receipt_hash = _canonical_hash(unsigned.model_dump(mode="json", exclude={"receipt_hash"}))
    return unsigned.model_copy(update={"receipt_hash": receipt_hash})


def build_handoff_draft_template(request: HandoffRequest) -> dict[str, Any]:
    """Build the only supported draft template for a frozen request."""

    return {
        "protocol_version": request.protocol_version,
        "run_id": str(request.run_id),
        "input_hash": request.input_hash,
        "request_hash": request.request_hash,
        "generated_at": "REPLACE_WITH_TIMEZONE_AWARE_ISO8601",
        "generated_by": {"surface": "codex", "task_id": None, "model": None},
        "drafts": [
            {
                "agent_id": assignment.agent_id,
                "index_code": assignment.index_code,
                "horizon": assignment.horizon.value,
                **(
                    {"agent_brief": assignment.agent_brief}
                    if assignment.agent_brief is not None
                    else {}
                ),
                "draft": {
                    "direction": "REPLACE_WITH_up_OR_down",
                    "probabilities": {
                        "up": "REPLACE_WITH_NUMBER_0_TO_1",
                        "neutral": "REPLACE_WITH_NUMBER_0_TO_1",
                        "down": "REPLACE_WITH_NUMBER_0_TO_1",
                    },
                    "summary": "REPLACE_WITH_FROZEN_INPUT_SYNTHESIS",
                    "evidence": ["REPLACE_WITH_AT_LEAST_ONE_EVIDENCE_STATEMENT"],
                    "counter_evidence": ["REPLACE_WITH_COUNTER_EVIDENCE"],
                    "invalidation_conditions": ["REPLACE_WITH_INVALIDATION_CONDITION"],
                    "evidence_item_ids": assignment.allowed_evidence_item_ids,
                    "wiki_entry_id": assignment.wiki_entry_id,
                    "wiki_section": assignment.wiki_sections[0],
                },
            }
            for assignment in request.assignments
        ],
    }


def render_handoff_instructions(request: HandoffRequest) -> str:
    """Render the immutable draft-stage instructions for a frozen request."""

    if request.protocol_version == LEGACY_HANDOFF_PROTOCOL_VERSION:
        return _render_handoff_instructions_v1(request)
    return _render_handoff_instructions_v2(request)


def _render_handoff_instructions_v1(request: HandoffRequest) -> str:
    """Render the byte-stable instructions used by historical v1 packages."""

    return f"""# VeriCouncil Codex 文件交接任务

任务 ID：`{request.run_id}`

最终提交截止：`{request.finalize_deadline.isoformat()}`

1. 只读取本目录的 `input.json` 与 `drafts.template.json`。
   不得联网补充事实，也不得修改冻结证据、Wiki、指数、周期或身份矩阵。
2. 将模板复制为 `drafts.json`，填写全部 50 个 `draft`：30 份研究、
   10 份策略、10 份风险反证；不要创建 Quant 或 CIO 草稿。
3. 每份草稿必须符合 `AgentDraft`。`direction` 只能是 `up`/`down`，
   且必须对应 `probabilities.up` 与 `probabilities.down` 中更大的一侧；
   三项概率之和为 1，涨跌两项不得相等。
4. Wiki 字段与 `evidence_item_ids` 只能从对应 assignment 中选择。
   Live 模式下研究与策略草稿必须引用冻结证据；风险反证官不得新增 evidence ID。
5. `generated_at` 必须是带时区的 ISO 8601 时间；`generated_by` 仅用于审计，不会被当作可信模型身份。
6. 保存 `drafts.json` 后，由操作员运行 finalize。不要改数据库、`input.json` 或任何服务端文件。

最终入库仍会执行确定性 schema、哈希、时间截面、证据、Wiki、方向与唯一性校验；
CIO 结论由 VeriCouncil 本地聚合器生成。
"""


def _render_handoff_instructions_v2(request: HandoffRequest) -> str:
    research_count = sum(item.role == "research" for item in request.assignments)
    strategy_count = sum(item.role == "strategy" for item in request.assignments)
    critic_count = sum(item.role == "critic" for item in request.assignments)
    return f"""# forecast-loop Codex 文件交接任务

任务 ID：`{request.run_id}`

最终提交截止：`{request.finalize_deadline.isoformat()}`

1. 只读取本目录的 `input.json` 与 `drafts.template.json`。
   不得联网补充事实，也不得修改冻结证据、Wiki、指数、周期或身份矩阵。
2. 将模板复制为 `drafts.json`，填写全部 {len(request.assignments)} 个 `draft`：
   {research_count} 份研究、{strategy_count} 份策略、{critic_count} 份风险反证；
   不要创建 Quant 或 CIO 草稿。
3. 每份草稿必须符合 `AgentDraft`。`direction` 只能是 `up`/`down`，
   且必须对应 `probabilities.up` 与 `probabilities.down` 中更大的一侧；
   三项概率之和为 1，涨跌两项不得相等。
4. 每份草稿必须严格履行对应 assignment 冻结的 `agent_brief`，并原样保留模板中的
   `agent_brief`。Wiki 字段与 `evidence_item_ids` 只能从对应 assignment 中选择。
   Live 模式下研究与策略草稿必须引用冻结证据；风险反证官不得新增 evidence ID。
5. `generated_at` 必须是带时区的 ISO 8601 时间；`generated_by` 仅用于审计，不会被当作可信模型身份。
6. 保存 `drafts.json` 后，由操作员运行 finalize。不要改数据库、`input.json` 或任何服务端文件。

最终入库仍会执行确定性 schema、哈希、时间截面、证据、Wiki、方向与唯一性校验；
CIO 结论由 forecast-loop 本地聚合器生成。
"""


def _provider_for_protocol(
    protocol_version: HandoffProtocolVersion,
) -> HandoffProviderName:
    if protocol_version == LEGACY_HANDOFF_PROTOCOL_VERSION:
        return LEGACY_CODEX_FILE_PROVIDER_NAME
    if protocol_version == PREVIOUS_HANDOFF_PROTOCOL_VERSION:
        return PREVIOUS_CODEX_FILE_PROVIDER_NAME
    if protocol_version == HANDOFF_PROTOCOL_VERSION:
        return CODEX_FILE_PROVIDER_NAME
    raise ValueError(f"unsupported handoff protocol_version: {protocol_version}")


def _runtime_mode_for_protocol(
    protocol_version: HandoffProtocolVersion,
) -> WorkflowRuntimeMode:
    if protocol_version in {
        LEGACY_HANDOFF_PROTOCOL_VERSION,
        PREVIOUS_HANDOFF_PROTOCOL_VERSION,
    }:
        return "legacy_dual_horizon"
    if protocol_version == HANDOFF_PROTOCOL_VERSION:
        return "current"
    raise ValueError(f"unsupported handoff protocol_version: {protocol_version}")


def _forecast_horizons_for_protocol(
    protocol_version: HandoffProtocolVersion,
) -> tuple[Horizon, ...]:
    return (
        (Horizon.D1,)
        if _runtime_mode_for_protocol(protocol_version) == "current"
        else (Horizon.D1, Horizon.D2)
    )


def _validate_handoff_runtime_versions(
    *,
    protocol_version: HandoffProtocolVersion,
    initial_state: dict[str, Any],
    workflow_version: str,
    decision_schema_version: str,
) -> None:
    runtime_mode = _runtime_mode_for_protocol(protocol_version)
    frozen_horizons = initial_state.get("forecast_horizons")
    if runtime_mode == "current":
        if frozen_horizons != [Horizon.D1.value]:
            raise ValueError("v3 handoff must freeze the D1 forecast horizon")
    elif frozen_horizons not in (None, [Horizon.D1.value, Horizon.D2.value]):
        raise ValueError("v1/v2 handoff forecast horizon seal must remain D1/D2")
    expected = workflow_runtime_versions(
        uses_configurable_universe="market_universe" in initial_state,
        runtime_mode=runtime_mode,
    )
    if (workflow_version, decision_schema_version) != expected:
        raise ValueError(
            "handoff runtime versions changed after prepare; "
            "restore the prepared workflow and decision schema versions"
        )


def _validate_execution_mode(settings: Settings) -> None:
    if settings.execution_mode not in {"demo", "codex_file"}:
        raise ValueError(
            "file handoff requires VERICOUNCIL_EXECUTION_PROVIDER=codex_file "
            "(or demo for an explicitly labeled offline test)"
        )


def _validate_live_prepare_time(
    settings: Settings,
    *,
    requested_as_of: datetime | None,
    prepared_at: datetime,
    evidence_source: EvidenceSnapshotSource | None = None,
) -> datetime | None:
    """Bind live prepare to the snapshot's actual post-ingest timestamp."""

    if settings.use_demo_provider:
        return requested_as_of
    zone = ZoneInfo(settings.timezone)
    universe = load_market_universe(settings.market_universe_path)
    local_now = prepared_at.astimezone(zone)
    close_hour, close_minute = (
        int(part) for part in universe.session_close.split(":", maxsplit=1)
    )
    close_ready = datetime.combine(
        local_now.date(),
        time(close_hour, close_minute),
        tzinfo=zone,
    ) + timedelta(minutes=5)
    if local_now.weekday() >= 5 or local_now < close_ready:
        raise ValueError("live file handoffs may only be prepared after the trading-day close")
    if requested_as_of is None:
        if evidence_source is not None:
            raise ValueError(
                "live file handoff requires an explicit as_of when an evidence source "
                "is injected"
            )
        if settings.evidence_snapshot_path is None:
            raise ValueError("live file handoff requires a frozen evidence snapshot")
        try:
            payload = json.loads(settings.evidence_snapshot_path.read_text(encoding="utf-8"))
            normalized = FrozenEvidenceSnapshot.model_validate(payload).as_of.astimezone(zone)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"cannot read live evidence snapshot as_of: {exc}") from exc
    else:
        normalized = (
            requested_as_of.replace(tzinfo=zone)
            if requested_as_of.tzinfo is None or requested_as_of.utcoffset() is None
            else requested_as_of.astimezone(zone)
        )
    if normalized.date() != local_now.date():
        raise ValueError("live file handoff snapshot must be from today's trading session")
    if normalized > local_now:
        raise ValueError("live file handoff as_of cannot be later than prepare_time")
    snapshot = load_evidence_snapshot(
        settings,
        as_of=normalized,
        source=evidence_source,
        universe=universe,
    )
    if snapshot.created_at.astimezone(zone) > local_now:
        raise ValueError("live evidence snapshot created_at cannot be later than prepare_time")
    return normalized


def _prepare_root(root: Path) -> Path:
    path = root.expanduser()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("handoff root may not be a symlink")
    path = path.resolve()
    os.chmod(path, 0o700)
    return path


def _resolve_job_dir(root: Path, job_dir: str | Path) -> Path:
    resolved_root = _prepare_root(root)
    candidate = Path(job_dir).expanduser()
    if not candidate.is_absolute():
        candidate = (
            resolved_root / candidate if candidate.parent == Path(".") else candidate.resolve()
        )
    try:
        UUID(candidate.name)
    except ValueError as exc:
        raise ValueError("handoff job directory name must be a UUID") from exc
    if candidate.is_symlink():
        raise ValueError("handoff job directory may not be a symlink")
    if candidate.parent.resolve() != resolved_root:
        raise ValueError("handoff job directory must be a direct child of handoff_root")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_dir() or resolved.parent != resolved_root:
        raise ValueError("invalid handoff job directory")
    return resolved


def _secure_read_json(path: Path) -> tuple[bytes, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"handoff file is not regular: {path.name}")
        if metadata.st_size <= 0 or metadata.st_size > MAX_JSON_BYTES:
            raise ValueError(f"handoff file size is invalid: {path.name}")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != metadata.st_size:
            raise ValueError(f"handoff file changed while reading: {path.name}")
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid UTF-8 JSON in {path.name}: {exc}") from exc
    return raw, payload


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _atomic_write(path: Path, payload: bytes, *, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _mark_prepare_failed(database: Database, run_id: str, error: str, zone: ZoneInfo) -> None:
    with database.session_factory() as session:
        row = session.get(WorkflowRun, run_id)
        if row is None or row.status != RunStatus.AWAITING_DRAFT.value:
            return
        completed_at = datetime.now(zone)
        started_at = _aware(row.started_at, zone.key)
        row.status = RunStatus.FAILED.value
        row.completed_at = completed_at
        row.duration_seconds = max(0.0, (completed_at - started_at).total_seconds())
        row.error = f"Handoff preparation failed: {error}"
        session.commit()


def _normalize_now(value: datetime | None, zone: ZoneInfo) -> datetime:
    if value is None:
        return datetime.now(zone)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def _aware(value: datetime, timezone: str) -> datetime:
    zone = ZoneInfo(timezone)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _canonical_hash(value: Any) -> str:
    return _sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
