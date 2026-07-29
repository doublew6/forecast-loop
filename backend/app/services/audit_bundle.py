"""Export and verify content-addressed audit bundles for completed file handoffs.

The bundle preserves the frozen request, evidence snapshot, handoff instructions,
draft template, Codex drafts, deterministic receipt, and the separately verified
result bundle. SHA-256 hashes provide integrity and cross-artifact linkage only.
They do not authenticate a publisher, and the bundle does not capture external
Codex orchestration, the Python runtime, installed dependencies, or repository
revision needed for bit-for-bit replay.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..schemas import FrozenEvidenceSnapshot
from .handoff import (
    HandoffDraftBundle,
    HandoffReceipt,
    HandoffRequest,
    build_handoff_draft_template,
    render_handoff_instructions,
)
from .run_bundle import (
    MANIFEST_NAME as RUN_MANIFEST_NAME,
)
from .run_bundle import (
    MAX_ARTIFACT_BYTES as MAX_RESULT_ARTIFACT_BYTES,
)
from .run_bundle import (
    MAX_MANIFEST_BYTES as MAX_RESULT_MANIFEST_BYTES,
)
from .run_bundle import (
    RUN_BUNDLE_SCHEMA,
    RunBundleError,
    RunBundleManifest,
    verify_run_bundle,
)
from .snapshot import (
    LiveEvidenceRequiredError,
    validate_live_snapshot,
    validate_snapshot_content_hash,
)

AUDIT_BUNDLE_SCHEMA = "vericouncil.audit-bundle/v1"
AUDIT_MANIFEST_NAME = "manifest.json"
HANDOFF_DIRECTORY = "handoff"
RESULTS_DIRECTORY = "results"
HANDOFF_ARTIFACT_NAMES = (
    "input.json",
    "evidence_snapshot.json",
    "INSTRUCTIONS.md",
    "drafts.template.json",
    "drafts.json",
    "receipt.json",
)
RESULT_ARTIFACT_NAMES = (
    RUN_MANIFEST_NAME,
    "run.json",
    "opinions.json",
    "forecasts.json",
)
AUDIT_ARTIFACT_PATHS = tuple(
    f"{HANDOFF_DIRECTORY}/{name}" for name in HANDOFF_ARTIFACT_NAMES
) + tuple(f"{RESULTS_DIRECTORY}/{name}" for name in RESULT_ARTIFACT_NAMES)
HASH_PATTERN = r"^[0-9a-f]{64}$"
MAX_AUDIT_MANIFEST_BYTES = 1024 * 1024
MAX_INSTRUCTIONS_BYTES = 1024 * 1024
MAX_HANDOFF_JSON_BYTES = 25 * 1024 * 1024
MAX_AUDIT_ARTIFACT_BYTES = MAX_RESULT_ARTIFACT_BYTES


class AuditBundleError(ValueError):
    """An audit bundle failed a safety, schema, or integrity check."""


class AuditBundleArtifact(BaseModel):
    """One content-addressed file in an audit bundle."""

    model_config = ConfigDict(extra="forbid")

    path: str
    media_type: Literal["application/json", "text/markdown; charset=utf-8"]
    sha256: str = Field(pattern=HASH_PATTERN)
    size: int = Field(ge=1, le=MAX_AUDIT_ARTIFACT_BYTES)


class AuditBundleManifest(BaseModel):
    """Integrity metadata and honest reproducibility boundaries for an audit bundle."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    run_id: str
    mode: Literal["demo", "live"]
    status: Literal["completed"]
    input_hash: str = Field(pattern=HASH_PATTERN)
    request_hash: str = Field(pattern=HASH_PATTERN)
    receipt_hash: str = Field(pattern=HASH_PATTERN)
    output_hash: str = Field(pattern=HASH_PATTERN)
    evidence_content_hash: str = Field(pattern=HASH_PATTERN)
    run_bundle_hash: str = Field(pattern=HASH_PATTERN)
    exported_at: datetime
    integrity_algorithm: Literal["sha256"]
    publisher_authentication: Literal["none"]
    reproducibility_scope: Literal["frozen-inputs-and-output-linkage"]
    external_orchestration_captured: Literal[False]
    runtime_environment_captured: Literal[False]
    input_hash_verification: Literal["cross-artifact-linkage"]
    output_hash_verification: Literal["recomputed"]
    artifacts: list[AuditBundleArtifact]
    bundle_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("exported_at")
    @classmethod
    def exported_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("exported_at must include a timezone")
        return value


def export_audit_bundle(
    *,
    handoff_root: Path,
    job_dir: str | Path,
    run_bundle_path: Path,
    output_root: Path,
    exported_at: datetime | None = None,
) -> Path:
    """Export a completed handoff and its result bundle without database access."""

    handoff_directory = _resolve_handoff_job(handoff_root, job_dir)
    run_directory = _resolve_result_bundle(run_bundle_path)
    handoff_raw, handoff_payloads = _load_handoff_source(handoff_directory)
    result_manifest, result_raw, result_payloads = _load_result_source(run_directory)
    request, receipt, snapshot = _validate_artifact_relationships(
        handoff_raw=handoff_raw,
        handoff_payloads=handoff_payloads,
        result_manifest=result_manifest,
        result_payloads=result_payloads,
    )

    root = _prepare_output_root(
        output_root,
        forbidden_directories=(handoff_directory, run_directory),
    )
    destination = root / str(request.run_id)
    if destination.exists() or destination.is_symlink():
        raise AuditBundleError(f"Audit bundle destination already exists: {destination}")

    evidence_body = _canonical_json_bytes(snapshot.model_dump(mode="json"))
    artifact_bodies = {
        f"{HANDOFF_DIRECTORY}/input.json": handoff_raw["input.json"],
        f"{HANDOFF_DIRECTORY}/evidence_snapshot.json": evidence_body,
        f"{HANDOFF_DIRECTORY}/INSTRUCTIONS.md": handoff_raw["INSTRUCTIONS.md"],
        f"{HANDOFF_DIRECTORY}/drafts.template.json": handoff_raw["drafts.template.json"],
        f"{HANDOFF_DIRECTORY}/drafts.json": handoff_raw["drafts.json"],
        f"{HANDOFF_DIRECTORY}/receipt.json": handoff_raw["receipt.json"],
        **{f"{RESULTS_DIRECTORY}/{name}": result_raw[name] for name in RESULT_ARTIFACT_NAMES},
    }
    temporary = root / f".{request.run_id}.{uuid4().hex}.tmp"
    try:
        temporary.mkdir(mode=0o700)
        (temporary / HANDOFF_DIRECTORY).mkdir(mode=0o700)
        (temporary / RESULTS_DIRECTORY).mkdir(mode=0o700)
        artifacts: list[AuditBundleArtifact] = []
        for relative_path in AUDIT_ARTIFACT_PATHS:
            body = artifact_bodies[relative_path]
            _write_new_file(temporary / relative_path, body)
            artifacts.append(
                AuditBundleArtifact(
                    path=relative_path,
                    media_type=_media_type(relative_path),
                    sha256=_sha256(body),
                    size=len(body),
                )
            )

        provisional = AuditBundleManifest(
            schema_version=AUDIT_BUNDLE_SCHEMA,
            run_id=str(request.run_id),
            mode=request.mode,
            status="completed",
            input_hash=request.input_hash,
            request_hash=request.request_hash,
            receipt_hash=receipt.receipt_hash,
            output_hash=receipt.output_hash or "",
            evidence_content_hash=snapshot.content_hash,
            run_bundle_hash=result_manifest.bundle_hash,
            exported_at=_aware_utc(exported_at),
            integrity_algorithm="sha256",
            publisher_authentication="none",
            reproducibility_scope="frozen-inputs-and-output-linkage",
            external_orchestration_captured=False,
            runtime_environment_captured=False,
            input_hash_verification="cross-artifact-linkage",
            output_hash_verification="recomputed",
            artifacts=artifacts,
            bundle_hash="0" * 64,
        )
        manifest = provisional.model_copy(
            update={
                "bundle_hash": _canonical_hash(
                    provisional.model_dump(mode="json", exclude={"bundle_hash"})
                )
            }
        )
        _write_new_file(
            temporary / AUDIT_MANIFEST_NAME,
            _canonical_json_bytes(manifest.model_dump(mode="json")),
        )
        if destination.exists() or destination.is_symlink():
            raise AuditBundleError(f"Audit bundle destination already exists: {destination}")
        os.rename(temporary, destination)
    except Exception:
        if temporary.exists() and not temporary.is_symlink():
            shutil.rmtree(temporary)
        raise
    return destination


def verify_audit_bundle(bundle_path: Path) -> AuditBundleManifest:
    """Verify bundle membership, content hashes, schemas, and all handoff links."""

    bundle = _resolve_real_directory(bundle_path, label="Audit bundle")
    expected_top_level = {
        AUDIT_MANIFEST_NAME,
        HANDOFF_DIRECTORY,
        RESULTS_DIRECTORY,
    }
    if {item.name for item in bundle.iterdir()} != expected_top_level:
        raise AuditBundleError("Audit bundle contains missing or unexpected files")
    handoff_directory = _resolve_child_directory(
        bundle, HANDOFF_DIRECTORY, label="Audit handoff directory"
    )
    results_directory = _resolve_child_directory(
        bundle, RESULTS_DIRECTORY, label="Audit results directory"
    )
    if {item.name for item in handoff_directory.iterdir()} != set(HANDOFF_ARTIFACT_NAMES):
        raise AuditBundleError("Audit handoff directory contains missing or unexpected files")
    if {item.name for item in results_directory.iterdir()} != set(RESULT_ARTIFACT_NAMES):
        raise AuditBundleError("Audit results directory contains missing or unexpected files")

    manifest_body = _read_bounded_regular_file(
        bundle / AUDIT_MANIFEST_NAME,
        max_bytes=MAX_AUDIT_MANIFEST_BYTES,
        label="Audit bundle manifest",
    )
    manifest_payload = _parse_json(manifest_body, label="Audit bundle manifest")
    try:
        manifest = AuditBundleManifest.model_validate(manifest_payload)
    except ValidationError as exc:
        raise AuditBundleError(f"Invalid audit bundle manifest: {exc}") from exc
    if manifest.schema_version != AUDIT_BUNDLE_SCHEMA:
        raise AuditBundleError(f"Unsupported audit bundle schema: {manifest.schema_version}")
    if [artifact.path for artifact in manifest.artifacts] != list(AUDIT_ARTIFACT_PATHS):
        raise AuditBundleError("Audit bundle artifact list is invalid")

    raw_by_path: dict[str, bytes] = {}
    payload_by_path: dict[str, Any] = {}
    for artifact in manifest.artifacts:
        relative = PurePosixPath(artifact.path)
        if relative.is_absolute() or len(relative.parts) != 2:
            raise AuditBundleError(f"Unsafe audit bundle artifact path: {artifact.path}")
        if artifact.media_type != _media_type(artifact.path):
            raise AuditBundleError(f"Audit bundle artifact media type is invalid: {artifact.path}")
        path = bundle.joinpath(*relative.parts)
        limit = (
            _handoff_limit(relative.parts[1])
            if relative.parts[0] == HANDOFF_DIRECTORY
            else _result_limit(relative.parts[1])
        )
        body = _read_bounded_regular_file(
            path,
            max_bytes=limit,
            expected_size=artifact.size,
            label=f"Audit bundle artifact {artifact.path}",
        )
        if _sha256(body) != artifact.sha256:
            raise AuditBundleError(f"Audit bundle artifact hash mismatch: {artifact.path}")
        raw_by_path[artifact.path] = body
        if relative.suffix == ".json":
            payload_by_path[artifact.path] = _parse_json(
                body, label=f"Audit bundle artifact {artifact.path}"
            )

    if (
        _canonical_hash(manifest.model_dump(mode="json", exclude={"bundle_hash"}))
        != manifest.bundle_hash
    ):
        raise AuditBundleError("Audit bundle manifest hash mismatch")

    result_manifest = _verify_result_bundle(results_directory)
    handoff_raw = {
        name: raw_by_path[f"{HANDOFF_DIRECTORY}/{name}"]
        for name in HANDOFF_ARTIFACT_NAMES
        if name != "evidence_snapshot.json"
    }
    handoff_payloads = {
        name: payload_by_path[f"{HANDOFF_DIRECTORY}/{name}"]
        for name in HANDOFF_ARTIFACT_NAMES
        if name.endswith(".json")
    }
    result_payloads = {
        name: payload_by_path[f"{RESULTS_DIRECTORY}/{name}"]
        for name in RESULT_ARTIFACT_NAMES
        if name != RUN_MANIFEST_NAME
    }
    request, receipt, snapshot = _validate_artifact_relationships(
        handoff_raw=handoff_raw,
        handoff_payloads=handoff_payloads,
        result_manifest=result_manifest,
        result_payloads=result_payloads,
    )
    expected_manifest_values = {
        "run_id": str(request.run_id),
        "mode": request.mode,
        "status": receipt.status,
        "input_hash": request.input_hash,
        "request_hash": request.request_hash,
        "receipt_hash": receipt.receipt_hash,
        "output_hash": receipt.output_hash,
        "evidence_content_hash": snapshot.content_hash,
        "run_bundle_hash": result_manifest.bundle_hash,
    }
    actual_manifest_values = {key: getattr(manifest, key) for key in expected_manifest_values}
    if actual_manifest_values != expected_manifest_values:
        raise AuditBundleError(
            "Audit bundle manifest does not match its handoff and result artifacts"
        )
    if bundle.name != manifest.run_id:
        raise AuditBundleError("Audit bundle directory name does not match run_id")
    return manifest


def _load_handoff_source(
    directory: Path,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    names = (
        "input.json",
        "INSTRUCTIONS.md",
        "drafts.template.json",
        "drafts.json",
        "receipt.json",
    )
    raw: dict[str, bytes] = {}
    payloads: dict[str, Any] = {}
    for name in names:
        body = _read_bounded_regular_file(
            directory / name,
            max_bytes=_handoff_limit(name),
            label=f"Handoff artifact {name}",
        )
        raw[name] = body
        if name.endswith(".json"):
            payloads[name] = _parse_json(body, label=f"Handoff artifact {name}")
    return raw, payloads


def _load_result_source(
    directory: Path,
) -> tuple[RunBundleManifest, dict[str, bytes], dict[str, Any]]:
    verified = _verify_result_bundle(directory)
    if verified.schema_version != RUN_BUNDLE_SCHEMA:
        raise AuditBundleError(
            "New audit bundles require a vericouncil.run-bundle/v2 result bundle"
        )
    raw: dict[str, bytes] = {}
    payloads: dict[str, Any] = {}
    for name in RESULT_ARTIFACT_NAMES:
        body = _read_bounded_regular_file(
            directory / name,
            max_bytes=_result_limit(name),
            label=f"Result bundle artifact {name}",
        )
        raw[name] = body
        payloads[name] = _parse_json(body, label=f"Result bundle artifact {name}")
    try:
        reloaded = RunBundleManifest.model_validate(payloads[RUN_MANIFEST_NAME])
    except ValidationError as exc:  # pragma: no cover - verified above
        raise AuditBundleError(f"Invalid result bundle manifest: {exc}") from exc
    if reloaded != verified:
        raise AuditBundleError("Result bundle changed while being exported")
    by_name = {artifact.path: artifact for artifact in verified.artifacts}
    for name in RESULT_ARTIFACT_NAMES[1:]:
        artifact = by_name[name]
        if len(raw[name]) != artifact.size or _sha256(raw[name]) != artifact.sha256:
            raise AuditBundleError(f"Result bundle changed while being exported: {name}")
    return verified, raw, {name: payloads[name] for name in RESULT_ARTIFACT_NAMES[1:]}


def _validate_artifact_relationships(
    *,
    handoff_raw: dict[str, bytes],
    handoff_payloads: dict[str, Any],
    result_manifest: RunBundleManifest,
    result_payloads: dict[str, Any],
) -> tuple[HandoffRequest, HandoffReceipt, FrozenEvidenceSnapshot]:
    try:
        request = HandoffRequest.model_validate(handoff_payloads["input.json"])
        drafts = HandoffDraftBundle.model_validate(handoff_payloads["drafts.json"])
        receipt = HandoffReceipt.model_validate(handoff_payloads["receipt.json"])
        snapshot = FrozenEvidenceSnapshot.model_validate(request.initial_state["evidence_snapshot"])
    except (KeyError, TypeError, ValidationError) as exc:
        raise AuditBundleError(f"Audit handoff artifact schema validation failed: {exc}") from exc

    if request.request_hash != _canonical_hash(
        request.model_dump(mode="json", exclude={"request_hash"})
    ):
        raise AuditBundleError("input.json canonical request hash is invalid")
    _validate_initial_state(request, snapshot=snapshot)
    _validate_handoff_instructions(
        handoff_raw["INSTRUCTIONS.md"],
        request=request,
    )
    if _sha256(handoff_raw["input.json"]) != receipt.request_raw_hash:
        raise AuditBundleError("input.json raw hash does not match receipt.json")
    if drafts.protocol_version != request.protocol_version:
        raise AuditBundleError("drafts.json protocol_version does not match input.json")
    if drafts.run_id != request.run_id:
        raise AuditBundleError("drafts.json run_id does not match input.json")
    if drafts.input_hash != request.input_hash:
        raise AuditBundleError("drafts.json input_hash does not match input.json")
    if drafts.request_hash != request.request_hash:
        raise AuditBundleError("drafts.json request_hash does not match input.json")
    if _canonical_hash(drafts.model_dump(mode="json")) != receipt.drafts_hash:
        raise AuditBundleError("drafts.json canonical hash does not match receipt.json")
    if _sha256(handoff_raw["drafts.json"]) != receipt.drafts_raw_hash:
        raise AuditBundleError("drafts.json raw hash does not match receipt.json")
    if receipt.receipt_hash != _canonical_hash(
        receipt.model_dump(mode="json", exclude={"receipt_hash"})
    ):
        raise AuditBundleError("receipt.json canonical receipt hash is invalid")
    if receipt.status != "completed" or receipt.output_hash is None:
        raise AuditBundleError("Audit bundles require a completed handoff receipt")
    if receipt.error is not None:
        raise AuditBundleError("Completed handoff receipt may not contain an error")
    if (
        receipt.protocol_version != request.protocol_version
        or receipt.provider != request.provider
        or str(receipt.run_id) != str(request.run_id)
        or receipt.input_hash != request.input_hash
        or receipt.request_hash != request.request_hash
        or receipt.generated_by != drafts.generated_by
    ):
        raise AuditBundleError("receipt.json does not match input.json and drafts.json")

    _validate_draft_template(handoff_payloads["drafts.template.json"], request=request)
    try:
        validate_snapshot_content_hash(snapshot)
        if request.mode == "live":
            validate_live_snapshot(
                snapshot,
                as_of=datetime.fromisoformat(str(request.initial_state["as_of"])),
            )
    except LiveEvidenceRequiredError as exc:
        raise AuditBundleError(str(exc)) from exc
    explicit_evidence = handoff_payloads.get("evidence_snapshot.json")
    if explicit_evidence is not None:
        try:
            exported_snapshot = FrozenEvidenceSnapshot.model_validate(explicit_evidence)
        except ValidationError as exc:
            raise AuditBundleError(f"Invalid evidence_snapshot.json: {exc}") from exc
        if exported_snapshot.model_dump(mode="json") != snapshot.model_dump(mode="json"):
            raise AuditBundleError(
                "evidence_snapshot.json does not match input.json frozen evidence"
            )

    if (
        result_manifest.run_id != str(request.run_id)
        or result_manifest.mode != request.mode
        or result_manifest.status != "completed"
        or result_manifest.input_hash != request.input_hash
    ):
        raise AuditBundleError("Result bundle does not match the completed handoff identity")
    opinions = result_payloads.get("opinions.json")
    forecasts = result_payloads.get("forecasts.json")
    if not isinstance(opinions, list) or not isinstance(forecasts, list):
        raise AuditBundleError("Result bundle output artifacts must be JSON arrays")
    if receipt.opinion_count != len(opinions) or receipt.forecast_count != len(forecasts):
        raise AuditBundleError("Receipt output counts do not match result bundle")
    if (
        _sealed_output_hash(
            run_id=str(request.run_id),
            input_hash=request.input_hash,
            opinions=opinions,
            forecasts=forecasts,
        )
        != receipt.output_hash
    ):
        raise AuditBundleError("Receipt output hash does not match result bundle")
    return request, receipt, snapshot


def _validate_initial_state(
    request: HandoffRequest,
    *,
    snapshot: FrozenEvidenceSnapshot,
) -> None:
    state = request.initial_state
    try:
        state_run_id = str(state["run_id"])
        state_input_hash = str(state["input_hash"])
        state_as_of = datetime.fromisoformat(str(state["as_of"]))
        state_data_cutoff = datetime.fromisoformat(str(state["data_cutoff"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise AuditBundleError(f"input.json initial_state identity is invalid: {exc}") from exc
    if state_run_id != str(request.run_id):
        raise AuditBundleError("initial_state run_id does not match input.json")
    if state_input_hash != request.input_hash:
        raise AuditBundleError("initial_state input_hash does not match input.json")
    if state_as_of != snapshot.as_of:
        raise AuditBundleError("initial_state as_of does not match its frozen evidence snapshot")
    if state_data_cutoff != snapshot.data_cutoff:
        raise AuditBundleError(
            "initial_state data_cutoff does not match its frozen evidence snapshot"
        )


def _validate_handoff_instructions(
    body: bytes,
    *,
    request: HandoffRequest,
) -> None:
    try:
        actual = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AuditBundleError("INSTRUCTIONS.md is not valid UTF-8") from exc
    if actual != render_handoff_instructions(request):
        raise AuditBundleError("INSTRUCTIONS.md does not match the frozen handoff request")


def _validate_draft_template(payload: Any, *, request: HandoffRequest) -> None:
    expected = build_handoff_draft_template(request)
    if payload != expected:
        raise AuditBundleError("drafts.template.json does not match the frozen handoff request")


def _sealed_output_hash(
    *,
    run_id: str,
    input_hash: str,
    opinions: list[Any],
    forecasts: list[Any],
) -> str:
    try:
        ordered_opinions = sorted(
            opinions,
            key=lambda item: (
                item["agent_id"],
                item["index_code"],
                item["horizon"],
            ),
        )
        ordered_forecasts = sorted(
            forecasts,
            key=lambda item: (item["index_code"], item["horizon"]),
        )
    except (KeyError, TypeError) as exc:
        raise AuditBundleError(
            "Result bundle cannot be ordered for output hash verification"
        ) from exc
    # Evaluations are attached after the handoff output seal is created.  Reset
    # only that later field to reconstruct the exact completion-time payload.
    completion_forecasts = [{**forecast, "evaluation": None} for forecast in ordered_forecasts]
    return _canonical_hash(
        {
            "run_id": run_id,
            "input_hash": input_hash,
            "opinions": ordered_opinions,
            "forecasts": completion_forecasts,
        }
    )


def _resolve_handoff_job(root: Path, job_dir: str | Path) -> Path:
    resolved_root = _resolve_real_directory(root, label="Handoff root")
    candidate = Path(job_dir).expanduser()
    if not candidate.is_absolute():
        if candidate.parent != Path("."):
            raise AuditBundleError("Handoff job must be a UUID name or an absolute path")
        candidate = resolved_root / candidate
    if candidate.is_symlink():
        raise AuditBundleError("Handoff job directory may not be a symlink")
    try:
        UUID(candidate.name)
    except ValueError as exc:
        raise AuditBundleError("Handoff job directory name must be a UUID") from exc
    resolved = _resolve_real_directory(candidate, label="Handoff job directory")
    if resolved.parent != resolved_root:
        raise AuditBundleError("Handoff job directory must be a direct child of handoff_root")
    return resolved


def _resolve_result_bundle(path: Path) -> Path:
    resolved = _resolve_real_directory(path, label="Result bundle")
    manifest = _verify_result_bundle(resolved)
    if manifest.schema_version != RUN_BUNDLE_SCHEMA:
        raise AuditBundleError(
            "New audit bundles require a vericouncil.run-bundle/v2 result bundle"
        )
    return resolved


def _verify_result_bundle(path: Path) -> RunBundleManifest:
    try:
        return verify_run_bundle(path)
    except (RunBundleError, OSError) as exc:
        raise AuditBundleError(f"Invalid result bundle: {exc}") from exc


def _resolve_real_directory(path: Path, *, label: str) -> Path:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise AuditBundleError(f"{label} may not be a symlink: {candidate}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise AuditBundleError(f"{label} does not exist: {candidate}") from exc
    if not resolved.is_dir():
        raise AuditBundleError(f"{label} must be a directory: {candidate}")
    return resolved


def _resolve_child_directory(root: Path, name: str, *, label: str) -> Path:
    candidate = root / name
    if candidate.is_symlink():
        raise AuditBundleError(f"{label} may not be a symlink")
    resolved = _resolve_real_directory(candidate, label=label)
    if resolved.parent != root:
        raise AuditBundleError(f"{label} escaped the audit bundle")
    return resolved


def _prepare_output_root(
    output_root: Path,
    *,
    forbidden_directories: tuple[Path, ...],
) -> Path:
    candidate = Path(os.path.abspath(output_root.expanduser()))
    _reject_existing_symlink_components(candidate)
    for forbidden in forbidden_directories:
        if candidate == forbidden or forbidden in candidate.parents:
            raise AuditBundleError("Audit output root may not be inside a source bundle")
    candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
    if candidate.is_symlink() or not candidate.is_dir():
        raise AuditBundleError(f"Audit bundle output root is not a real directory: {candidate}")
    resolved = candidate.resolve()
    if stat.S_IMODE(resolved.stat().st_mode) & 0o022:
        raise AuditBundleError(
            "Audit bundle output root must not be group/world writable"
        )
    for forbidden in forbidden_directories:
        if resolved == forbidden or forbidden in resolved.parents:
            raise AuditBundleError("Audit output root may not be inside a source bundle")
    return resolved


def _reject_existing_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise AuditBundleError(f"Audit output path may not traverse a symlink: {current}")
        if not current.exists():
            break


def _write_new_file(path: Path, body: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _read_bounded_regular_file(
    path: Path,
    *,
    max_bytes: int,
    label: str,
    expected_size: int | None = None,
) -> bytes:
    if path.is_symlink():
        raise AuditBundleError(f"{label} may not be a symlink: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AuditBundleError(f"{label} cannot be opened safely: {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AuditBundleError(f"{label} must be a regular file: {path}")
        if metadata.st_size <= 0 or metadata.st_size > max_bytes:
            raise AuditBundleError(f"{label} has an invalid size")
        if expected_size is not None and metadata.st_size != expected_size:
            raise AuditBundleError(f"{label} size does not match manifest")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining:
            raise AuditBundleError(f"{label} changed while being read")
        final_metadata = os.fstat(descriptor)
        if (
            final_metadata.st_size != metadata.st_size
            or final_metadata.st_mtime_ns != metadata.st_mtime_ns
        ):
            raise AuditBundleError(f"{label} changed while being read")
        return b"".join(chunks)
    except OSError as exc:
        raise AuditBundleError(f"{label} could not be read: {path}: {exc}") from exc
    finally:
        os.close(descriptor)


def _parse_json(body: bytes, *, label: str) -> Any:
    try:
        return json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise AuditBundleError(f"{label} is not strict UTF-8 JSON: {exc}") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _handoff_limit(name: str) -> int:
    return MAX_INSTRUCTIONS_BYTES if name == "INSTRUCTIONS.md" else MAX_HANDOFF_JSON_BYTES


def _result_limit(name: str) -> int:
    return MAX_RESULT_MANIFEST_BYTES if name == RUN_MANIFEST_NAME else MAX_RESULT_ARTIFACT_BYTES


def _media_type(path: str) -> str:
    return (
        "text/markdown; charset=utf-8"
        if PurePosixPath(path).suffix == ".md"
        else "application/json"
    )


def _aware_utc(value: datetime | None) -> datetime:
    resolved = value or datetime.now(UTC)
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise AuditBundleError("exported_at must include a timezone")
    return resolved.astimezone(UTC)


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _canonical_hash(value: Any) -> str:
    return _sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
