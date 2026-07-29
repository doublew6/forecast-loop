"""Append-only governance state for externally operated job manifests.

This module deliberately does not execute commands, start a model, or depend on
FastAPI.  It records the trusted side of a two-phase dispatcher protocol:

1. an operator opens an idempotent execution and runs the allowlisted prepare
   operation through a supported interface;
2. the operator acknowledges the resulting handoff directory;
3. an external Codex task receives one narrowly scoped draft instruction;
4. the operator acknowledges the draft and runs deterministic finalize;
5. the final handoff receipt is verified and copied into the private state root.

The state root must never be granted to the draft runner.  The external
instruction exposes the required read paths and exactly one declared write
path, ``drafts.json``; the external orchestrator must enforce that scope.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..services.handoff import (
    MAX_JSON_BYTES,
    HandoffDraftBundle,
    HandoffReceipt,
    HandoffRequest,
    build_handoff_draft_template,
    render_handoff_instructions,
)
from .manifest import JobManifest, load_job_manifest

JOB_EXECUTION_SCHEMA = "vericouncil.job-execution/v1"
EXTERNAL_DRAFT_INSTRUCTION_SCHEMA = "vericouncil.external-draft-instruction/v1"
MAX_STATE_BYTES = 1024 * 1024
MAX_PROMPT_BYTES = 1024 * 1024

_EXECUTION_ID = re.compile(r"^[0-9a-f]{64}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
_REVISION_FILE = re.compile(r"^([0-9]{6})\.json$")

ExecutionPhase = Literal[
    "prepare_pending",
    "awaiting_draft",
    "finalize_pending",
    "completed",
    "failed",
]


class JobExecutionError(ValueError):
    """Base error for a rejected or inconsistent execution transition."""


class JobExecutionConflictError(JobExecutionError):
    """Raised when another writer won an incompatible state transition."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class JobExecutionState(_StrictModel):
    """One sealed revision in an append-only job execution chain."""

    schema_id: Literal["vericouncil.job-execution/v1"] = Field(alias="schema")
    execution_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_name: str
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str
    phase: ExecutionPhase
    revision: int = Field(ge=0)
    previous_state_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    updated_at: datetime
    job_id: str | None = None
    input_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    request_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    request_raw_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    instructions_raw_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    template_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    template_raw_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    drafts_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    drafts_raw_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    receipt_status: Literal["completed", "failed"] | None = None
    receipt_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    receipt_raw_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("idempotency_key")
    @classmethod
    def idempotency_key_is_portable(cls, value: str) -> str:
        if not _IDEMPOTENCY_KEY.fullmatch(value):
            raise ValueError(
                "idempotency_key must be a portable 1-128 character identifier"
            )
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("execution timestamps must be timezone-aware")
        return value

    @field_validator("job_id")
    @classmethod
    def job_id_is_a_canonical_uuid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = UUID(value)
        except ValueError as error:
            raise ValueError("job_id must be a UUID") from error
        if str(parsed) != value:
            raise ValueError("job_id must use canonical lowercase UUID syntax")
        return value

    @model_validator(mode="after")
    def phase_has_exact_seals(self) -> JobExecutionState:
        prepared = (
            self.job_id,
            self.input_hash,
            self.request_hash,
            self.request_raw_hash,
            self.instructions_raw_hash,
            self.template_hash,
            self.template_raw_hash,
        )
        drafted = (self.drafts_hash, self.drafts_raw_hash)
        finalized = (
            self.receipt_status,
            self.receipt_hash,
            self.receipt_raw_hash,
        )
        if self.phase == "prepare_pending":
            if any(value is not None for value in (*prepared, *drafted, *finalized)):
                raise ValueError("prepare_pending state may not contain later-stage seals")
        else:
            if any(value is None for value in prepared):
                raise ValueError("prepared execution state is missing handoff seals")
        if self.phase == "awaiting_draft":
            if any(value is not None for value in (*drafted, *finalized)):
                raise ValueError("awaiting_draft state may not contain later-stage seals")
        elif self.phase in {"finalize_pending", "completed", "failed"}:
            if any(value is None for value in drafted):
                raise ValueError("finalize state is missing draft seals")
        if self.phase == "finalize_pending":
            if any(value is not None for value in finalized):
                raise ValueError("finalize_pending state may not contain receipt seals")
        elif self.phase in {"completed", "failed"}:
            if any(value is None for value in finalized):
                raise ValueError("terminal state is missing receipt seals")
            if self.receipt_status != self.phase:
                raise ValueError("terminal phase must match receipt_status")
        if self.revision == 0 and self.previous_state_hash is not None:
            raise ValueError("initial revision may not have previous_state_hash")
        if self.revision > 0 and self.previous_state_hash is None:
            raise ValueError("non-initial revision requires previous_state_hash")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at may not precede created_at")
        return self


class ExternalDraftInstruction(_StrictModel):
    """Narrow handoff metadata for a supported external model interface."""

    schema_id: Literal["vericouncil.external-draft-instruction/v1"] = Field(
        alias="schema"
    )
    status: Literal["external_action_required"] = "external_action_required"
    execution_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_name: str
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_raw_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    instructions_raw_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    template_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    template_raw_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner: str
    model: str | None = None
    reasoning_effort: (
        Literal["low", "medium", "high", "xhigh", "max", "ultra"] | None
    ) = None
    prompt_path: str
    job_dir: str
    input_path: str
    instructions_path: str
    template_path: str
    draft_path: str
    allowed_write_paths: tuple[str, ...] = Field(min_length=1, max_length=1)

    @field_validator("idempotency_key")
    @classmethod
    def idempotency_key_is_portable(cls, value: str) -> str:
        if not _IDEMPOTENCY_KEY.fullmatch(value):
            raise ValueError("instruction idempotency_key is invalid")
        return value

    @model_validator(mode="after")
    def only_draft_is_writable(self) -> ExternalDraftInstruction:
        if self.allowed_write_paths != (self.draft_path,):
            raise ValueError("external draft instruction may allow only draft_path")
        return self


class JobExecutionStore:
    """Private append-only store for a two-phase external dispatcher."""

    def __init__(
        self,
        *,
        state_root: str | Path,
        project_root: str | Path,
        handoff_root: str | Path,
    ) -> None:
        self.project_root = _resolve_existing_directory(
            Path(project_root),
            label="project_root",
        )
        self.handoff_root = _prepare_private_root(
            Path(handoff_root),
            label="handoff_root",
        )
        self.state_root = _prepare_private_root(
            Path(state_root),
            label="state_root",
        )
        if _contains(self.state_root, self.handoff_root) or _contains(
            self.handoff_root,
            self.state_root,
        ):
            raise JobExecutionError(
                "state_root and handoff_root must be separate non-nested directories"
            )

    def begin(
        self,
        manifest: JobManifest,
        *,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> JobExecutionState:
        """Open one execution, or resume the same manifest/key idempotently."""

        _validate_idempotency_key(idempotency_key)
        self._validate_manifest_runtime_binding(manifest)
        timestamp = _timestamp(now)
        manifest_payload = manifest.model_dump(mode="json", by_alias=True)
        manifest_hash = _canonical_hash(manifest_payload)
        manifest_bytes = _json_bytes(manifest_payload)
        prompt_path = _resolve_project_file(
            self.project_root,
            manifest.draft.prompt,
            label="draft prompt",
            maximum=MAX_PROMPT_BYTES,
        )
        prompt_hash = _sha256(
            _secure_read_bytes(prompt_path, maximum=MAX_PROMPT_BYTES)
        )
        execution_id = _execution_id(manifest.name, idempotency_key)
        target = self.state_root / execution_id

        if target.exists() or target.is_symlink():
            state = self.resume(execution_id)
            _require_same_execution(
                state,
                manifest_hash=manifest_hash,
                prompt_hash=prompt_hash,
                idempotency_key=idempotency_key,
            )
            return state

        unsigned = JobExecutionState(
            schema=JOB_EXECUTION_SCHEMA,
            execution_id=execution_id,
            manifest_name=manifest.name,
            manifest_hash=manifest_hash,
            prompt_hash=prompt_hash,
            idempotency_key=idempotency_key,
            phase="prepare_pending",
            revision=0,
            created_at=timestamp,
            updated_at=timestamp,
            state_hash="0" * 64,
        )
        initial = _seal_state(unsigned)

        temporary = Path(
            tempfile.mkdtemp(prefix=f".{execution_id}.", dir=self.state_root)
        )
        try:
            os.chmod(temporary, 0o700)
            revisions = temporary / "revisions"
            revisions.mkdir(mode=0o700)
            _write_exclusive(temporary / "manifest.json", manifest_bytes, mode=0o400)
            _write_exclusive(
                revisions / "000000.json",
                _json_bytes(initial.model_dump(mode="json", by_alias=True)),
                mode=0o400,
            )
            try:
                os.rename(temporary, target)
            except OSError as error:
                if error.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
        finally:
            if temporary.exists():
                _remove_owned_temporary(temporary)

        state = self.resume(execution_id)
        _require_same_execution(
            state,
            manifest_hash=manifest_hash,
            prompt_hash=prompt_hash,
            idempotency_key=idempotency_key,
        )
        return state

    def resume(self, execution_id: str) -> JobExecutionState:
        """Load and verify the full append-only revision chain."""

        directory = self._execution_directory(execution_id)
        manifest, manifest_hash = _load_bound_manifest(directory / "manifest.json")
        revisions = directory / "revisions"
        if revisions.is_symlink() or not revisions.is_dir():
            raise JobExecutionError("execution revisions must be a real directory")

        allowed = {"manifest.json", "revisions", "receipt.json"}
        unexpected = {item.name for item in directory.iterdir()} - allowed
        if unexpected:
            raise JobExecutionError(
                f"execution directory contains unexpected entries: {sorted(unexpected)}"
            )

        files = sorted(revisions.iterdir(), key=lambda item: item.name)
        if not files:
            raise JobExecutionError("execution has no state revisions")
        previous: JobExecutionState | None = None
        for expected_revision, path in enumerate(files):
            match = _REVISION_FILE.fullmatch(path.name)
            if (
                match is None
                or int(match.group(1)) != expected_revision
                or path.is_symlink()
            ):
                raise JobExecutionError("execution revisions are not contiguous regular files")
            raw, payload = _secure_read_json(path, maximum=MAX_STATE_BYTES)
            del raw
            state = JobExecutionState.model_validate(payload)
            _verify_state_revision(
                state,
                previous=previous,
                execution_id=execution_id,
                manifest=manifest,
                manifest_hash=manifest_hash,
            )
            previous = state
        if previous is None:  # pragma: no cover - guarded above
            raise JobExecutionError("execution has no state revisions")
        if previous.phase in {"completed", "failed"}:
            self._verify_trusted_receipt(directory, previous)
        return previous

    def record_prepared(
        self,
        execution_id: str,
        job_dir: str | Path,
        *,
        now: datetime | None = None,
    ) -> JobExecutionState:
        """Bind an externally prepared handoff without running prepare."""

        state = self.resume(execution_id)
        manifest = self._manifest(execution_id)
        directory = self._resolve_handoff_directory(job_dir)
        request, request_raw_hash = _load_handoff_request(directory)
        if str(request.run_id) != directory.name:
            raise JobExecutionError("handoff input run_id does not match its directory")
        if request.mode != _manifest_mode(manifest, stage="prepare"):
            raise JobExecutionError("handoff mode does not match manifest prepare mode")
        draft_input_seals = _load_and_validate_draft_inputs(
            directory,
            request=request,
        )

        seals = {
            "job_id": directory.name,
            "input_hash": request.input_hash,
            "request_hash": request.request_hash,
            "request_raw_hash": request_raw_hash,
            **draft_input_seals,
        }
        if state.phase != "prepare_pending":
            _require_matching_fields(state, seals)
            return state

        return self._append(
            state,
            phase="awaiting_draft",
            now=now,
            **seals,
        )

    def draft_instruction(self, execution_id: str) -> ExternalDraftInstruction:
        """Return explicit external work; this method never invokes a model."""

        state = self.resume(execution_id)
        if state.phase != "awaiting_draft":
            raise JobExecutionError(
                "draft instruction is available only while awaiting_draft"
            )
        manifest = self._manifest(execution_id)
        directory, request = self._verify_bound_request(state)
        _require_matching_fields(
            state,
            _load_and_validate_draft_inputs(directory, request=request),
        )
        prompt_path = _resolve_project_file(
            self.project_root,
            manifest.draft.prompt,
            label="draft prompt",
            maximum=MAX_PROMPT_BYTES,
        )
        if (
            _sha256(_secure_read_bytes(prompt_path, maximum=MAX_PROMPT_BYTES))
            != state.prompt_hash
        ):
            raise JobExecutionError("draft prompt changed after execution began")
        draft_path = directory / "drafts.json"
        _require_manifest_write_scope(
            project_root=self.project_root,
            draft_path=draft_path,
            declared=manifest.draft.writable,
        )
        return ExternalDraftInstruction(
            schema=EXTERNAL_DRAFT_INSTRUCTION_SCHEMA,
            execution_id=state.execution_id,
            manifest_name=state.manifest_name,
            manifest_hash=state.manifest_hash,
            prompt_hash=state.prompt_hash,
            idempotency_key=state.idempotency_key,
            input_hash=_required_seal(state.input_hash, label="input_hash"),
            request_hash=_required_seal(state.request_hash, label="request_hash"),
            request_raw_hash=_required_seal(
                state.request_raw_hash,
                label="request_raw_hash",
            ),
            instructions_raw_hash=_required_seal(
                state.instructions_raw_hash,
                label="instructions_raw_hash",
            ),
            template_hash=_required_seal(
                state.template_hash,
                label="template_hash",
            ),
            template_raw_hash=_required_seal(
                state.template_raw_hash,
                label="template_raw_hash",
            ),
            runner=manifest.draft.runner,
            model=manifest.draft.model,
            reasoning_effort=manifest.draft.reasoning_effort,
            prompt_path=str(prompt_path),
            job_dir=str(directory),
            input_path=str(directory / "input.json"),
            instructions_path=str(directory / "INSTRUCTIONS.md"),
            template_path=str(directory / "drafts.template.json"),
            draft_path=str(draft_path),
            allowed_write_paths=(str(draft_path),),
        )

    def record_draft_ready(
        self,
        execution_id: str,
        *,
        now: datetime | None = None,
    ) -> JobExecutionState:
        """Validate and seal drafts.json before deterministic finalize."""

        state = self.resume(execution_id)
        if state.phase == "prepare_pending":
            raise JobExecutionError("prepare must be acknowledged before the draft")
        directory, request = self._verify_bound_request(state)
        _require_matching_fields(
            state,
            _load_and_validate_draft_inputs(directory, request=request),
        )
        raw, payload = _secure_read_json(
            directory / "drafts.json",
            maximum=MAX_JSON_BYTES,
        )
        bundle = HandoffDraftBundle.model_validate(payload)
        if str(bundle.run_id) != state.job_id:
            raise JobExecutionError("draft run_id does not match the prepared handoff")
        if bundle.input_hash != request.input_hash:
            raise JobExecutionError("draft input_hash does not match the prepared handoff")
        if bundle.request_hash != request.request_hash:
            raise JobExecutionError("draft request_hash does not match the prepared handoff")
        seals = {
            "drafts_hash": _canonical_hash(bundle.model_dump(mode="json")),
            "drafts_raw_hash": _sha256(raw),
        }
        if state.phase != "awaiting_draft":
            _require_matching_fields(state, seals)
            return state
        return self._append(
            state,
            phase="finalize_pending",
            now=now,
            **seals,
        )

    def record_finalized(
        self,
        execution_id: str,
        *,
        now: datetime | None = None,
    ) -> JobExecutionState:
        """Verify the handoff receipt and copy it into trusted local state."""

        state = self.resume(execution_id)
        if state.phase in {"prepare_pending", "awaiting_draft"}:
            raise JobExecutionError("draft must be acknowledged before finalize")
        directory, request = self._verify_bound_request(state)
        state = self.record_draft_ready(execution_id)
        receipt_raw, payload = _secure_read_json(
            directory / "receipt.json",
            maximum=MAX_JSON_BYTES,
        )
        receipt = HandoffReceipt.model_validate(payload)
        receipt_hash = _canonical_hash(
            receipt.model_dump(mode="json", exclude={"receipt_hash"})
        )
        if receipt.receipt_hash != receipt_hash:
            raise JobExecutionError("handoff receipt canonical hash is invalid")
        if str(receipt.run_id) != state.job_id:
            raise JobExecutionError("handoff receipt run_id does not match execution")
        expected = {
            "input_hash": state.input_hash,
            "request_hash": state.request_hash,
            "request_raw_hash": state.request_raw_hash,
            "drafts_hash": state.drafts_hash,
            "drafts_raw_hash": state.drafts_raw_hash,
        }
        actual = {
            "input_hash": receipt.input_hash,
            "request_hash": receipt.request_hash,
            "request_raw_hash": receipt.request_raw_hash,
            "drafts_hash": receipt.drafts_hash,
            "drafts_raw_hash": receipt.drafts_raw_hash,
        }
        if actual != expected:
            raise JobExecutionError("handoff receipt seals do not match execution state")
        _validate_receipt_terminal_invariants(receipt, request=request)

        trusted_path = self._execution_directory(execution_id) / "receipt.json"
        _write_or_verify_exact(trusted_path, receipt_raw, mode=0o400)
        seals = {
            "receipt_status": receipt.status,
            "receipt_hash": receipt.receipt_hash,
            "receipt_raw_hash": _sha256(receipt_raw),
        }
        if state.phase in {"completed", "failed"}:
            _require_matching_fields(state, seals)
            self._verify_trusted_receipt(
                self._execution_directory(execution_id),
                state,
            )
            return state
        return self._append(
            state,
            phase=receipt.status,
            now=now,
            **seals,
        )

    def _append(
        self,
        current: JobExecutionState,
        *,
        phase: ExecutionPhase,
        now: datetime | None,
        **updates: Any,
    ) -> JobExecutionState:
        timestamp = _timestamp(now)
        if timestamp < current.updated_at.astimezone(UTC):
            raise JobExecutionError("state transition time may not move backwards")
        unsigned_payload = current.model_dump(mode="python")
        unsigned_payload.update(
            {
                **updates,
                "phase": phase,
                "revision": current.revision + 1,
                "previous_state_hash": current.state_hash,
                "updated_at": timestamp,
                "state_hash": "0" * 64,
            }
        )
        next_state = _seal_state(
            JobExecutionState.model_validate(unsigned_payload)
        )
        path = (
            self._execution_directory(current.execution_id)
            / "revisions"
            / f"{next_state.revision:06d}.json"
        )
        payload = _json_bytes(next_state.model_dump(mode="json", by_alias=True))
        try:
            _write_atomic_exclusive(path, payload, mode=0o400)
        except FileExistsError:
            winner = self.resume(current.execution_id)
            if _is_same_logical_transition(
                winner,
                requested_phase=phase,
                requested_updates=updates,
            ):
                return winner
            raise JobExecutionConflictError(
                "another writer completed an incompatible execution transition"
            ) from None
        return self.resume(current.execution_id)

    def _manifest(self, execution_id: str) -> JobManifest:
        directory = self._execution_directory(execution_id)
        manifest, _ = _load_bound_manifest(directory / "manifest.json")
        return manifest

    def _validate_manifest_runtime_binding(self, manifest: JobManifest) -> None:
        prepare_mode = _manifest_mode(manifest, stage="prepare")
        finalize_mode = _manifest_mode(manifest, stage="finalize")
        if prepare_mode != finalize_mode:
            raise JobExecutionError(
                "manifest prepare and finalize modes must match"
            )
        for stage, command in (
            ("prepare", manifest.prepare.command),
            ("finalize", manifest.finalize.command),
        ):
            output_root = _command_option(command, "--output-root")
            if output_root is None:
                continue
            candidate = Path(output_root).expanduser()
            if not candidate.is_absolute():
                candidate = self.project_root / candidate
            if candidate.resolve() != self.handoff_root:
                raise JobExecutionError(
                    f"manifest {stage} --output-root must match handoff_root"
                )
        sample = self.handoff_root / "00000000-0000-0000-0000-000000000000"
        sample = sample / "drafts.json"
        _require_manifest_write_scope(
            project_root=self.project_root,
            draft_path=sample,
            declared=manifest.draft.writable,
            require_parent=False,
        )

    def _verify_bound_request(
        self,
        state: JobExecutionState,
    ) -> tuple[Path, HandoffRequest]:
        if state.job_id is None:
            raise JobExecutionError("execution is not bound to a handoff")
        directory = self._resolve_handoff_directory(state.job_id)
        request, raw_hash = _load_handoff_request(directory)
        expected = {
            "job_id": str(request.run_id),
            "input_hash": request.input_hash,
            "request_hash": request.request_hash,
            "request_raw_hash": raw_hash,
        }
        _require_matching_fields(state, expected)
        return directory, request

    def _resolve_handoff_directory(self, value: str | Path) -> Path:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            if candidate.parent != Path("."):
                raise JobExecutionError(
                    "relative handoff directory must be a bare UUID"
                )
            candidate = self.handoff_root / candidate
        try:
            UUID(candidate.name)
        except ValueError as error:
            raise JobExecutionError("handoff directory name must be a UUID") from error
        if candidate.is_symlink():
            raise JobExecutionError("handoff directory may not be a symlink")
        try:
            parent = candidate.parent.resolve(strict=True)
        except FileNotFoundError as error:
            raise JobExecutionError("handoff directory parent does not exist") from error
        if parent != self.handoff_root:
            raise JobExecutionError(
                "handoff directory must be a direct child of handoff_root"
            )
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as error:
            raise JobExecutionError("handoff directory does not exist") from error
        if not resolved.is_dir() or resolved.parent != self.handoff_root:
            raise JobExecutionError("invalid handoff directory")
        return resolved

    def _execution_directory(self, execution_id: str) -> Path:
        if not _EXECUTION_ID.fullmatch(execution_id):
            raise JobExecutionError("execution_id must be a 64-character lowercase hash")
        candidate = self.state_root / execution_id
        if candidate.is_symlink():
            raise JobExecutionError("execution directory may not be a symlink")
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as error:
            raise JobExecutionError("execution does not exist") from error
        if not resolved.is_dir() or resolved.parent != self.state_root:
            raise JobExecutionError("execution escaped state_root")
        return resolved

    def _verify_trusted_receipt(
        self,
        directory: Path,
        state: JobExecutionState,
    ) -> None:
        raw, payload = _secure_read_json(
            directory / "receipt.json",
            maximum=MAX_JSON_BYTES,
        )
        receipt = HandoffReceipt.model_validate(payload)
        if (
            _sha256(raw) != state.receipt_raw_hash
            or receipt.receipt_hash != state.receipt_hash
            or receipt.status != state.receipt_status
        ):
            raise JobExecutionError("trusted execution receipt does not match terminal state")


def _verify_state_revision(
    state: JobExecutionState,
    *,
    previous: JobExecutionState | None,
    execution_id: str,
    manifest: JobManifest,
    manifest_hash: str,
) -> None:
    if state.execution_id != execution_id:
        raise JobExecutionError("state execution_id does not match its directory")
    if state.manifest_name != manifest.name or state.manifest_hash != manifest_hash:
        raise JobExecutionError("state manifest seal is invalid")
    expected_hash = _canonical_hash(
        state.model_dump(mode="json", by_alias=True, exclude={"state_hash"})
    )
    if state.state_hash != expected_hash:
        raise JobExecutionError("state revision canonical hash is invalid")
    if previous is None:
        if state.revision != 0 or state.phase != "prepare_pending":
            raise JobExecutionError("execution must start at prepare_pending revision zero")
        return
    if state.revision != previous.revision + 1:
        raise JobExecutionError("execution revision number is not contiguous")
    if state.previous_state_hash != previous.state_hash:
        raise JobExecutionError("execution state hash chain is invalid")
    constants = (
        "execution_id",
        "manifest_name",
        "manifest_hash",
        "prompt_hash",
        "idempotency_key",
        "created_at",
    )
    if any(getattr(state, field) != getattr(previous, field) for field in constants):
        raise JobExecutionError("immutable execution identity changed between revisions")
    if state.updated_at < previous.updated_at:
        raise JobExecutionError("execution revision time moved backwards")
    allowed = {
        "prepare_pending": {"awaiting_draft"},
        "awaiting_draft": {"finalize_pending"},
        "finalize_pending": {"completed", "failed"},
        "completed": set(),
        "failed": set(),
    }
    if state.phase not in allowed[previous.phase]:
        raise JobExecutionError(
            f"invalid execution phase transition: {previous.phase} -> {state.phase}"
        )
    sealed_fields = (
        "job_id",
        "input_hash",
        "request_hash",
        "request_raw_hash",
        "instructions_raw_hash",
        "template_hash",
        "template_raw_hash",
        "drafts_hash",
        "drafts_raw_hash",
        "receipt_status",
        "receipt_hash",
        "receipt_raw_hash",
    )
    for field in sealed_fields:
        old = getattr(previous, field)
        if old is not None and getattr(state, field) != old:
            raise JobExecutionError(f"sealed execution field changed: {field}")


def _load_handoff_request(directory: Path) -> tuple[HandoffRequest, str]:
    raw, payload = _secure_read_json(directory / "input.json", maximum=MAX_JSON_BYTES)
    request = HandoffRequest.model_validate(payload)
    request_hash = _canonical_hash(
        request.model_dump(mode="json", exclude={"request_hash"})
    )
    if request.request_hash != request_hash:
        raise JobExecutionError("handoff input canonical request hash is invalid")
    return request, _sha256(raw)


def _load_and_validate_draft_inputs(
    directory: Path,
    *,
    request: HandoffRequest,
) -> dict[str, str]:
    instructions_raw = _secure_read_bytes(
        directory / "INSTRUCTIONS.md",
        maximum=MAX_PROMPT_BYTES,
    )
    try:
        instructions = instructions_raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise JobExecutionError("INSTRUCTIONS.md must be UTF-8") from error
    if instructions != render_handoff_instructions(request):
        raise JobExecutionError(
            "INSTRUCTIONS.md does not match the frozen handoff request"
        )

    template_raw, template_payload = _secure_read_json(
        directory / "drafts.template.json",
        maximum=MAX_JSON_BYTES,
    )
    if template_payload != build_handoff_draft_template(request):
        raise JobExecutionError(
            "drafts.template.json does not match the frozen handoff request"
        )
    return {
        "instructions_raw_hash": _sha256(instructions_raw),
        "template_hash": _canonical_hash(template_payload),
        "template_raw_hash": _sha256(template_raw),
    }


def _required_seal(value: str | None, *, label: str) -> str:
    if value is None:  # pragma: no cover - guarded by JobExecutionState
        raise JobExecutionError(f"prepared execution is missing {label}")
    return value


def _validate_receipt_terminal_invariants(
    receipt: HandoffReceipt,
    *,
    request: HandoffRequest,
) -> None:
    target_count = len(
        {
            (assignment.index_code, assignment.horizon.value)
            for assignment in request.assignments
        }
    )
    expected_completed_counts = (
        len(request.assignments) + target_count,
        target_count,
    )
    actual_counts = (receipt.opinion_count, receipt.forecast_count)
    if not request.prepared_at <= receipt.finalized_at <= request.finalize_deadline:
        raise JobExecutionError(
            "handoff receipt finalized_at is outside the prepared handoff window"
        )
    if receipt.status == "completed":
        if receipt.output_hash is None:
            raise JobExecutionError("completed handoff receipt requires output_hash")
        if receipt.error is not None:
            raise JobExecutionError("completed handoff receipt may not contain an error")
        if actual_counts != expected_completed_counts:
            raise JobExecutionError(
                "completed handoff receipt has unexpected output counts"
            )
        return
    if receipt.output_hash is not None:
        raise JobExecutionError("failed handoff receipt may not contain output_hash")
    if actual_counts != (0, 0):
        raise JobExecutionError("failed handoff receipt output counts must be zero")
    if receipt.error is None or not receipt.error.strip():
        raise JobExecutionError("failed handoff receipt requires a non-empty error")


def _load_bound_manifest(path: Path) -> tuple[JobManifest, str]:
    try:
        manifest = load_job_manifest(path)
    except (OSError, ValueError) as error:
        raise JobExecutionError(f"could not load bound manifest: {error}") from error
    return manifest, _canonical_hash(
        manifest.model_dump(mode="json", by_alias=True)
    )


def _manifest_mode(
    manifest: JobManifest,
    *,
    stage: Literal["prepare", "finalize"],
) -> Literal["demo", "live"]:
    command = manifest.prepare.command if stage == "prepare" else manifest.finalize.command
    value = _command_option(command, "--mode")
    if value not in {"demo", "live"}:  # pragma: no cover - manifest validation
        raise JobExecutionError("manifest mode is invalid")
    return value


def _command_option(command: tuple[str, ...], option: str) -> str | None:
    try:
        position = command.index(option)
    except ValueError:
        return None
    return command[position + 1]


def _require_manifest_write_scope(
    *,
    project_root: Path,
    draft_path: Path,
    declared: tuple[str, ...],
    require_parent: bool = True,
) -> None:
    resolved_parent = draft_path.parent.resolve(strict=require_parent)
    resolved = resolved_parent / draft_path.name
    try:
        relative = resolved.relative_to(project_root).as_posix()
    except ValueError as error:
        raise JobExecutionError(
            "handoff draft path is outside project_root and manifest write scope"
        ) from error
    candidate_parts = tuple(relative.split("/"))
    for pattern in declared:
        pattern_parts = tuple(pattern.split("/"))
        if len(pattern_parts) == len(candidate_parts) and all(
            expected == "*" or expected == actual
            for expected, actual in zip(pattern_parts, candidate_parts, strict=True)
        ):
            return
    raise JobExecutionError("handoff draft path is not allowed by manifest draft.writable")


def _resolve_project_file(
    root: Path,
    relative: str,
    *,
    label: str,
    maximum: int,
) -> Path:
    candidate = root.joinpath(*relative.split("/"))
    current = root
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink():
            raise JobExecutionError(f"{label} may not traverse a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        raise JobExecutionError(f"{label} does not exist") from error
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise JobExecutionError(f"{label} escaped project_root") from error
    metadata = resolved.stat()
    if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= maximum:
        raise JobExecutionError(f"{label} must be a bounded regular file")
    return resolved


def _require_matching_fields(
    state: JobExecutionState,
    expected: dict[str, Any],
) -> None:
    for field, value in expected.items():
        if getattr(state, field) != value:
            raise JobExecutionConflictError(
                f"execution is already bound to a different {field}"
            )


def _is_same_logical_transition(
    winner: JobExecutionState,
    *,
    requested_phase: ExecutionPhase,
    requested_updates: dict[str, Any],
) -> bool:
    rank = {
        "prepare_pending": 0,
        "awaiting_draft": 1,
        "finalize_pending": 2,
        "completed": 3,
        "failed": 3,
    }
    if rank[winner.phase] < rank[requested_phase]:
        return False
    if requested_phase in {"completed", "failed"} and winner.phase != requested_phase:
        return False
    return all(getattr(winner, field) == value for field, value in requested_updates.items())


def _require_same_execution(
    state: JobExecutionState,
    *,
    manifest_hash: str,
    prompt_hash: str,
    idempotency_key: str,
) -> None:
    if (
        state.manifest_hash != manifest_hash
        or state.prompt_hash != prompt_hash
        or state.idempotency_key != idempotency_key
    ):
        raise JobExecutionConflictError(
            "idempotency key is already bound to a different manifest or prompt"
        )


def _execution_id(manifest_name: str, idempotency_key: str) -> str:
    return _sha256(
        (
            f"{JOB_EXECUTION_SCHEMA}\0{manifest_name}\0{idempotency_key}"
        ).encode()
    )


def _validate_idempotency_key(value: str) -> None:
    if not isinstance(value, str) or not _IDEMPOTENCY_KEY.fullmatch(value):
        raise JobExecutionError(
            "idempotency_key must match [A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}"
        )


def _timestamp(value: datetime | None) -> datetime:
    timestamp = value or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise JobExecutionError("execution time must be timezone-aware")
    return timestamp.astimezone(UTC)


def _seal_state(state: JobExecutionState) -> JobExecutionState:
    digest = _canonical_hash(
        state.model_dump(mode="json", by_alias=True, exclude={"state_hash"})
    )
    return state.model_copy(update={"state_hash": digest})


def _prepare_private_root(path: Path, *, label: str) -> Path:
    expanded = path.expanduser()
    expanded.mkdir(parents=True, exist_ok=True, mode=0o700)
    if expanded.is_symlink():
        raise JobExecutionError(f"{label} may not be a symlink")
    resolved = expanded.resolve(strict=True)
    if not resolved.is_dir():
        raise JobExecutionError(f"{label} must be a directory")
    os.chmod(resolved, 0o700)
    return resolved


def _resolve_existing_directory(path: Path, *, label: str) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise JobExecutionError(f"{label} may not be a symlink")
    try:
        resolved = expanded.resolve(strict=True)
    except FileNotFoundError as error:
        raise JobExecutionError(f"{label} does not exist") from error
    if not resolved.is_dir():
        raise JobExecutionError(f"{label} must be a directory")
    return resolved


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _secure_read_json(path: Path, *, maximum: int) -> tuple[bytes, Any]:
    raw = _secure_read_bytes(path, maximum=maximum)
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise JobExecutionError(f"{path.name} must be strict UTF-8 JSON") from error
    return raw, payload


def _secure_read_bytes(path: Path, *, maximum: int) -> bytes:
    if path.is_symlink():
        raise JobExecutionError(f"{path.name} may not be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise JobExecutionError(f"could not open {path.name}: {error}") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise JobExecutionError(f"{path.name} must be a regular file")
        if before.st_size <= 0 or before.st_size > maximum:
            raise JobExecutionError(f"{path.name} has an invalid size")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    if (
        len(raw) != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise JobExecutionError(f"{path.name} changed while being read")
    return raw


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JobExecutionError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise JobExecutionError(f"non-finite JSON number is forbidden: {value}")


def _write_exclusive(path: Path, payload: bytes, *, mode: int) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, mode)
    finally:
        os.close(descriptor)


def _write_atomic_exclusive(path: Path, payload: bytes, *, mode: int) -> None:
    """Publish complete bytes atomically without replacing another revision."""

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)
        os.link(temporary, path, follow_symlinks=False)
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _write_or_verify_exact(path: Path, payload: bytes, *, mode: int) -> None:
    try:
        _write_atomic_exclusive(path, payload, mode=mode)
    except FileExistsError:
        existing, _ = _secure_read_json(path, maximum=MAX_JSON_BYTES)
        if existing != payload:
            raise JobExecutionConflictError(
                "trusted receipt already exists with different bytes"
            ) from None


def _remove_owned_temporary(path: Path) -> None:
    """Remove only the known files in a freshly allocated private temp dir."""

    revisions = path / "revisions"
    if revisions.is_dir() and not revisions.is_symlink():
        for item in revisions.iterdir():
            if item.is_file() and not item.is_symlink():
                item.unlink()
        revisions.rmdir()
    manifest = path / "manifest.json"
    if manifest.is_file() and not manifest.is_symlink():
        manifest.unlink()
    path.rmdir()


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
