"""Export and verify portable, immutable forecast run bundles."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..db import Database
from ..domain import AGENTS
from ..models import Forecast, WorkflowRun
from ..schemas import AgentOpinionRead, ForecastRead, WorkflowRunRead
from ..serializers import forecast_read, opinion_read, run_read
from .believability import (
    believability_run_binding_hash,
    validate_believability_snapshot,
)

RUN_BUNDLE_SCHEMA = "vericouncil.run-bundle/v2"
LEGACY_RUN_BUNDLE_SCHEMA = "vericouncil.run-bundle/v1"
SUPPORTED_RUN_BUNDLE_SCHEMAS = frozenset(
    {RUN_BUNDLE_SCHEMA, LEGACY_RUN_BUNDLE_SCHEMA}
)
MANIFEST_NAME = "manifest.json"
ARTIFACT_NAMES = ("run.json", "opinions.json", "forecasts.json")
HASH_PATTERN = r"^[0-9a-f]{64}$"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024


class RunBundleError(ValueError):
    """The requested bundle operation failed an integrity or safety check."""


class RunBundleArtifact(BaseModel):
    """One content-addressed JSON artifact in a run bundle."""

    model_config = ConfigDict(extra="forbid")

    path: str
    media_type: str = "application/json"
    sha256: str = Field(pattern=HASH_PATTERN)
    size: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)


class RunBundleManifest(BaseModel):
    """Portable manifest binding all exported run artifacts."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    run_id: str
    mode: str
    status: str
    input_hash: str = Field(pattern=HASH_PATTERN)
    exported_at: datetime
    artifacts: list[RunBundleArtifact]
    bundle_hash: str = Field(pattern=HASH_PATTERN)


def export_run_bundle(
    database: Database,
    *,
    run_id: str,
    output_root: Path,
    exported_at: datetime | None = None,
) -> Path:
    """Export one completed run without mutating its database records."""

    root = _prepare_output_root(output_root)
    destination = root / run_id
    if destination.exists() or destination.is_symlink():
        raise RunBundleError(f"Run bundle destination already exists: {destination}")

    with database.session_factory() as session:
        row = session.scalar(
            select(WorkflowRun)
            .options(
                selectinload(WorkflowRun.opinions),
                selectinload(WorkflowRun.forecasts).selectinload(Forecast.evaluation),
            )
            .where(WorkflowRun.id == run_id)
        )
        if row is None:
            raise RunBundleError(f"Workflow run not found: {run_id}")
        if row.status != "completed":
            raise RunBundleError(
                f"Only completed runs can be exported; {run_id} is {row.status}"
            )
        payloads = _run_payloads(row)
        try:
            run_payload = WorkflowRunRead.model_validate(payloads["run.json"])
        except ValidationError as exc:
            raise RunBundleError(
                f"Run bundle payload schema validation failed: {exc}"
            ) from exc
        # Never publish a nominal v2 directory that its own verifier must
        # reject. Existing v1 directories remain readable, but exporting from
        # the live database always requires the current run-bound seal.
        _validate_believability_payload(run_payload, required=True)
        manifest_fields = {
            "schema_version": RUN_BUNDLE_SCHEMA,
            "run_id": row.id,
            "mode": row.mode,
            "status": row.status,
            "input_hash": row.input_hash,
            "exported_at": _aware_utc(exported_at),
        }

    temporary = root / f".{run_id}.{uuid4().hex}.tmp"
    try:
        temporary.mkdir(mode=0o700)
        artifacts = []
        for name in ARTIFACT_NAMES:
            body = _canonical_json_bytes(payloads[name])
            _write_new_file(temporary / name, body)
            artifacts.append(
                RunBundleArtifact(
                    path=name,
                    sha256=_sha256(body),
                    size=len(body),
                )
            )
        provisional_manifest = RunBundleManifest(
            **manifest_fields,
            artifacts=artifacts,
            bundle_hash="0" * 64,
        )
        unsigned_manifest = provisional_manifest.model_dump(
            mode="json", exclude={"bundle_hash"}
        )
        manifest = provisional_manifest.model_copy(
            update={"bundle_hash": _canonical_hash(unsigned_manifest)}
        )
        _write_new_file(
            temporary / MANIFEST_NAME,
            _canonical_json_bytes(manifest.model_dump(mode="json")),
        )
        os.replace(temporary, destination)
    except Exception:
        if temporary.exists() and not temporary.is_symlink():
            shutil.rmtree(temporary)
        raise
    return destination


def verify_run_bundle(bundle_path: Path) -> RunBundleManifest:
    """Verify file membership, hashes, schemas and cross-file run identity."""

    bundle = bundle_path.expanduser()
    if bundle.is_symlink() or not bundle.is_dir():
        raise RunBundleError(f"Run bundle must be a real directory: {bundle}")
    bundle = bundle.resolve()
    manifest_path = bundle / MANIFEST_NAME
    manifest_body = _read_bounded_regular_file(
        manifest_path,
        max_bytes=MAX_MANIFEST_BYTES,
        label="Run bundle manifest",
    )
    try:
        manifest = RunBundleManifest.model_validate_json(manifest_body)
    except ValidationError as exc:
        raise RunBundleError(f"Invalid run bundle manifest: {exc}") from exc
    if manifest.schema_version not in SUPPORTED_RUN_BUNDLE_SCHEMAS:
        raise RunBundleError(
            f"Unsupported run bundle schema: {manifest.schema_version}"
        )

    artifact_paths = [item.path for item in manifest.artifacts]
    if artifact_paths != list(ARTIFACT_NAMES):
        raise RunBundleError(
            f"Run bundle artifacts must be exactly: {', '.join(ARTIFACT_NAMES)}"
        )
    expected_members = {MANIFEST_NAME, *ARTIFACT_NAMES}
    actual_members = {item.name for item in bundle.iterdir()}
    if actual_members != expected_members:
        raise RunBundleError("Run bundle contains missing or unexpected files")

    payloads: dict[str, Any] = {}
    for artifact in manifest.artifacts:
        relative = PurePosixPath(artifact.path)
        if relative.is_absolute() or len(relative.parts) != 1:
            raise RunBundleError(f"Unsafe run bundle artifact path: {artifact.path}")
        path = bundle / artifact.path
        body = _read_bounded_regular_file(
            path,
            max_bytes=MAX_ARTIFACT_BYTES,
            expected_size=artifact.size,
            label=f"Run bundle artifact {artifact.path}",
        )
        if _sha256(body) != artifact.sha256:
            raise RunBundleError(f"Run bundle artifact hash mismatch: {artifact.path}")
        try:
            payloads[artifact.path] = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RunBundleError(
                f"Run bundle artifact is not valid JSON: {artifact.path}"
            ) from exc

    unsigned_manifest = manifest.model_dump(mode="json", exclude={"bundle_hash"})
    if _canonical_hash(unsigned_manifest) != manifest.bundle_hash:
        raise RunBundleError("Run bundle manifest hash mismatch")
    _validate_payloads(payloads, manifest=manifest)
    return manifest


def _run_payloads(row: WorkflowRun) -> dict[str, Any]:
    agent_order = {agent.id: position for position, agent in enumerate(AGENTS)}
    opinions = sorted(
        row.opinions,
        key=lambda item: (
            item.index_code,
            item.horizon,
            agent_order.get(item.agent_id, len(agent_order)),
        ),
    )
    forecasts = sorted(row.forecasts, key=lambda item: (item.index_code, item.horizon))
    return {
        # Queue state is operational metadata, not part of the immutable v1/v2
        # result bundle projection. Keep historical bundle hashes stable.
        "run.json": run_read(row, forecasts_count=len(forecasts)).model_dump(
            mode="json",
            exclude={"task"},
        ),
        "opinions.json": [
            opinion_read(item).model_dump(mode="json") for item in opinions
        ],
        "forecasts.json": [
            forecast_read(item).model_dump(mode="json") for item in forecasts
        ],
    }


def _validate_payloads(
    payloads: dict[str, Any],
    *,
    manifest: RunBundleManifest,
) -> None:
    try:
        run = WorkflowRunRead.model_validate(payloads["run.json"])
        opinions = [
            AgentOpinionRead.model_validate(item)
            for item in payloads["opinions.json"]
        ]
        forecasts = [
            ForecastRead.model_validate(item)
            for item in payloads["forecasts.json"]
        ]
    except (KeyError, TypeError, ValidationError) as exc:
        raise RunBundleError(f"Run bundle payload schema validation failed: {exc}") from exc
    if run.id != manifest.run_id or run.mode != manifest.mode:
        raise RunBundleError("Run bundle manifest does not match run.json")
    if run.status != "completed" or manifest.status != "completed":
        raise RunBundleError("Run bundle must contain a completed run")
    if run.input_hash != manifest.input_hash:
        raise RunBundleError("Run bundle input hash does not match manifest")
    if run.forecasts_count != len(forecasts):
        raise RunBundleError("Run bundle forecast count does not match run.json")
    if any(item.run_id != run.id for item in [*opinions, *forecasts]):
        raise RunBundleError("Run bundle contains records from another run")
    _validate_believability_payload(
        run,
        required=manifest.schema_version == RUN_BUNDLE_SCHEMA,
    )


def _validate_believability_payload(
    run: WorkflowRunRead,
    *,
    required: bool,
) -> None:
    payload = run.data_quality.get("believability_snapshot")
    runtime = run.data_quality.get("believability")
    if payload is None and runtime is None:
        if not required:
            # Read-only compatibility is confined to an explicitly legacy v1
            # manifest; newly exported v2 bundles always require the seal.
            return
        raise RunBundleError("Run bundle is missing its believability seal")
    if not isinstance(payload, dict) or not isinstance(runtime, dict):
        raise RunBundleError("Run bundle has an incomplete believability seal")
    try:
        snapshot = validate_believability_snapshot(payload)
    except ValueError as exc:
        raise RunBundleError(str(exc)) from exc
    if (
        runtime.get("policy_version") != snapshot.policy_version
        or runtime.get("snapshot_hash") != snapshot.content_hash
        or runtime.get("run_binding_hash")
        != believability_run_binding_hash(run.id, snapshot.content_hash)
        or runtime.get("mode") != "shadow_only"
        or runtime.get("applied_to_decision") is not False
    ):
        raise RunBundleError("Run bundle believability runtime seal does not match")
    if (
        snapshot.mode != run.mode
        or snapshot.as_of != run.as_of
        or snapshot.data_cutoff != run.data_cutoff
    ):
        raise RunBundleError("Run bundle believability snapshot belongs to another run")


def _prepare_output_root(output_root: Path) -> Path:
    root = output_root.expanduser()
    if root.is_symlink():
        raise RunBundleError(f"Run bundle output root may not be a symlink: {root}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not root.is_dir():
        raise RunBundleError(f"Run bundle output root is not a directory: {root}")
    return root.resolve()


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
        raise RunBundleError(f"{label} may not be a symlink: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RunBundleError(f"{label} cannot be opened safely: {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RunBundleError(f"{label} must be a regular file: {path}")
        if metadata.st_size > max_bytes:
            raise RunBundleError(f"{label} exceeds the size limit")
        if expected_size is not None and metadata.st_size != expected_size:
            raise RunBundleError(f"{label} hash mismatch")

        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining:
            raise RunBundleError(f"{label} changed while being read")
        final_metadata = os.fstat(descriptor)
        if (
            final_metadata.st_size != metadata.st_size
            or final_metadata.st_mtime_ns != metadata.st_mtime_ns
        ):
            raise RunBundleError(f"{label} changed while being read")
        return b"".join(chunks)
    except OSError as exc:
        raise RunBundleError(f"{label} could not be read: {path}: {exc}") from exc
    finally:
        os.close(descriptor)


def _aware_utc(value: datetime | None) -> datetime:
    resolved = value or datetime.now(UTC)
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise RunBundleError("exported_at must include a timezone")
    return resolved.astimezone(UTC)


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _canonical_hash(value: Any) -> str:
    return _sha256(_canonical_json_bytes(value))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
