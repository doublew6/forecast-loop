"""Read-only adapter for frozen, content-addressed Quant signal bundles."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from ..agent_contracts import (
    AgentSignalDraft,
    SignalProvenance,
    SignalTarget,
)
from ..domain import AgentSourceType
from ..ports import (
    AgentSignalAccessError,
    AgentSignalFormatError,
    AgentSignalValidationError,
    QuantSignalCandidate,
)
from ..quant_contracts import (
    QuantArtifactRef,
    QuantInputSnapshot,
    QuantSignalBundle,
    QuantSignalOutput,
)

DEFAULT_MAX_QUANT_MANIFEST_BYTES = 1024 * 1024
DEFAULT_MAX_QUANT_ARTIFACT_BYTES = 25 * 1024 * 1024
QUANT_ADAPTER_NAME = "local-json-quant-signal"
QUANT_ADAPTER_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class _VerifiedBundle:
    bundle: QuantSignalBundle
    input_snapshot: QuantInputSnapshot
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class LocalJsonQuantSignalSource:
    """Load a Quant bundle without executing code or opening an upstream database.

    Every file is opened with read-only flags below a configured trusted root.
    The adapter validates the manifest seal, all five artifact digests, and the
    time semantics of the input snapshot before returning any draft.
    """

    root: Path
    manifest_path: Path
    producer: str = "forecast-loop-local-quant"
    max_manifest_bytes: int = DEFAULT_MAX_QUANT_MANIFEST_BYTES
    max_artifact_bytes: int = DEFAULT_MAX_QUANT_ARTIFACT_BYTES

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        object.__setattr__(self, "manifest_path", Path(self.manifest_path))
        if not self.producer.strip():
            raise ValueError("producer may not be blank")
        if self.max_manifest_bytes <= 0 or self.max_artifact_bytes <= 0:
            raise ValueError("Quant adapter byte limits must be greater than zero")

    def load_signal_drafts(self, *, as_of: datetime) -> tuple[AgentSignalDraft, ...]:
        """Return all-or-nothing drafts for one exact, timezone-aware as_of."""

        return tuple(candidate.draft for candidate in self.load_candidates(as_of=as_of))

    def load_candidates(
        self,
        *,
        as_of: datetime,
    ) -> tuple[QuantSignalCandidate, ...]:
        """Return every candidate after one immutable all-or-nothing bundle read."""

        verified = self._load_verified_bundle(as_of=as_of)
        return tuple(
            self._candidate(verified, output)
            for output in verified.bundle.signals
        )

    def load_candidate(self, *, target: SignalTarget) -> QuantSignalCandidate:
        """Return the unique source result bound to an exact host target."""

        candidates = self.load_candidates(as_of=target.as_of)
        matches = [
            candidate
            for candidate in candidates
            if candidate.target == target
        ]
        if len(matches) != 1:
            raise AgentSignalValidationError(
                "Quant bundle must contain exactly one signal for the host target"
            )
        return matches[0]

    def _load_verified_bundle(self, *, as_of: datetime) -> _VerifiedBundle:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise AgentSignalValidationError(
                "requested Quant as_of must include a timezone"
            )
        trusted_root = self._trusted_root()
        manifest = self._resolve_file(
            trusted_root,
            self.manifest_path,
            label="Quant manifest",
        )
        raw_manifest = self._read_only(
            manifest,
            label="Quant manifest",
            maximum_bytes=self.max_manifest_bytes,
        )
        manifest_sha256 = hashlib.sha256(raw_manifest).hexdigest()
        try:
            payload = json.loads(raw_manifest.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentSignalFormatError(
                f"Quant manifest is not valid UTF-8 JSON: {manifest}"
            ) from exc
        try:
            bundle = QuantSignalBundle.model_validate(payload)
        except ValidationError as exc:
            raise AgentSignalValidationError(
                f"Quant manifest failed schema or content-hash validation: {manifest}: {exc}"
            ) from exc
        if bundle.as_of != as_of:
            raise AgentSignalValidationError(
                "Quant bundle as_of does not exactly match the requested as_of"
            )

        artifact_bytes: dict[str, bytes] = {}
        for name, artifact in bundle.artifacts.as_dict().items():
            artifact_path = self._resolve_file(
                trusted_root,
                Path(artifact.path),
                label=f"Quant {name} artifact",
            )
            raw = self._read_only(
                artifact_path,
                label=f"Quant {name} artifact",
                maximum_bytes=self.max_artifact_bytes,
            )
            actual_hash = hashlib.sha256(raw).hexdigest()
            if actual_hash != artifact.sha256:
                raise AgentSignalValidationError(
                    f"Quant {name} artifact SHA-256 does not match the manifest"
                )
            artifact_bytes[name] = raw

        input_snapshot = self._validate_input_snapshot(
            artifact_bytes["input_snapshot"],
            bundle=bundle,
        )
        return _VerifiedBundle(
            bundle=bundle,
            input_snapshot=input_snapshot,
            manifest_sha256=manifest_sha256,
        )

    def _validate_input_snapshot(
        self,
        raw: bytes,
        *,
        bundle: QuantSignalBundle,
    ) -> QuantInputSnapshot:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentSignalFormatError(
                "Quant input snapshot is not valid UTF-8 JSON"
            ) from exc
        try:
            snapshot = QuantInputSnapshot.model_validate(payload)
        except ValidationError as exc:
            raise AgentSignalValidationError(
                f"Quant input snapshot failed schema or content-hash validation: {exc}"
            ) from exc
        expected_version = bundle.artifacts.feature_set.version
        if snapshot.feature_set_version != expected_version:
            raise AgentSignalValidationError(
                "Quant input snapshot feature_set_version does not match the artifact"
            )
        if (
            snapshot.as_of != bundle.as_of
            or snapshot.data_cutoff != bundle.data_cutoff
        ):
            raise AgentSignalValidationError(
                "Quant input snapshot time binding does not match the bundle"
            )
        if snapshot.created_at > bundle.generated_at:
            raise AgentSignalValidationError(
                "Quant input snapshot was created after the model output"
            )
        required_indexes = {signal.target.index_code for signal in bundle.signals}
        snapshot_indexes = {row.index_code for row in snapshot.rows}
        if required_indexes != snapshot_indexes:
            raise AgentSignalValidationError(
                "Quant input snapshot rows must exactly match bundle target indexes"
            )
        return snapshot

    def _candidate(
        self,
        verified: _VerifiedBundle,
        output: QuantSignalOutput,
    ) -> QuantSignalCandidate:
        bundle = verified.bundle
        artifacts = bundle.artifacts.as_dict()
        artifact_metadata = {
            name: {
                "artifact_id": artifact.artifact_id,
                "version": artifact.version,
                "sha256": artifact.sha256,
            }
            for name, artifact in artifacts.items()
        }
        source_payload = {
            "bundle_id": bundle.bundle_id,
            "bundle_content_hash": bundle.content_hash,
            "manifest_sha256": verified.manifest_sha256,
            "input_snapshot_content_hash": verified.input_snapshot.content_hash,
            "quant_target_binding": output.target.model_dump(mode="json"),
            "artifact_manifest": artifact_metadata,
        }
        if bundle.evidence_snapshot_hash is not None:
            source_payload["evidence_snapshot_hash"] = bundle.evidence_snapshot_hash
        if bundle.market_universe_hash is not None:
            source_payload["market_universe_hash"] = bundle.market_universe_hash
        draft = AgentSignalDraft(
            signal_id=output.signal_id,
            submitted_at=bundle.generated_at,
            direction=output.direction,
            probabilities=output.probabilities,
            rationale=output.rationale,
            counter_evidence=output.counter_evidence,
            invalidation_conditions=output.invalidation_conditions,
            payload_schema="forecast-loop.quant-signal/v1",
            source_payload=source_payload,
        )
        provenance = self._provenance(
            artifacts=artifacts,
            manifest_sha256=verified.manifest_sha256,
        )
        return QuantSignalCandidate(
            draft=draft,
            target=output.target,
            provenance=provenance,
            bundle_content_hash=bundle.content_hash,
            manifest_sha256=verified.manifest_sha256,
            evidence_snapshot_hash=bundle.evidence_snapshot_hash,
            market_universe_hash=bundle.market_universe_hash,
        )

    def _provenance(
        self,
        *,
        artifacts: dict[str, QuantArtifactRef],
        manifest_sha256: str,
    ) -> SignalProvenance:
        code = artifacts["code"]
        model = artifacts["model"]
        return SignalProvenance(
            source_type=AgentSourceType.QUANT,
            producer=self.producer.strip(),
            adapter=QUANT_ADAPTER_NAME,
            adapter_version=QUANT_ADAPTER_VERSION,
            model_name=model.artifact_id,
            model_version=model.version,
            code_version=code.version,
            code_hash=code.sha256,
            artifact_hashes={
                "parameters": artifacts["parameters"].sha256,
                "feature_set": artifacts["feature_set"].sha256,
                "model": model.sha256,
                "input_snapshot": artifacts["input_snapshot"].sha256,
                "bundle_manifest": manifest_sha256,
            },
        )

    def _trusted_root(self) -> Path:
        configured = self.root.expanduser()
        if configured.is_symlink():
            raise AgentSignalAccessError(
                f"Quant adapter root may not be a symlink: {configured}"
            )
        try:
            resolved = configured.resolve(strict=True)
        except OSError as exc:
            raise AgentSignalAccessError(
                f"Quant adapter root is unavailable: {configured}: {exc}"
            ) from exc
        if not resolved.is_dir():
            raise AgentSignalAccessError(
                f"Quant adapter root must be a directory: {resolved}"
            )
        return resolved

    def _resolve_file(
        self,
        trusted_root: Path,
        configured_path: Path,
        *,
        label: str,
    ) -> Path:
        configured = configured_path.expanduser()
        if not configured.is_absolute():
            configured = trusted_root / configured
        source = Path(os.path.abspath(configured))
        try:
            relative = source.relative_to(trusted_root)
        except ValueError as exc:
            raise AgentSignalAccessError(
                f"{label} escaped the configured root: {source}"
            ) from exc
        current = trusted_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise AgentSignalAccessError(
                    f"{label} path may not contain symlinks: {current}"
                )
        try:
            resolved = source.resolve(strict=True)
        except OSError as exc:
            raise AgentSignalAccessError(
                f"{label} is missing or unavailable: {source}: {exc}"
            ) from exc
        if not resolved.is_relative_to(trusted_root):
            raise AgentSignalAccessError(
                f"{label} escaped the configured root: {source}"
            )
        if not resolved.is_file():
            raise AgentSignalAccessError(
                f"{label} must be a regular file: {resolved}"
            )
        return resolved

    @staticmethod
    def _read_only(
        source: Path,
        *,
        label: str,
        maximum_bytes: int,
    ) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(source, flags)
        except OSError as exc:
            raise AgentSignalAccessError(
                f"{label} cannot be opened safely: {source}: {exc}"
            ) from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise AgentSignalAccessError(
                    f"{label} must be a regular file: {source}"
                )
            if before.st_size <= 0 or before.st_size > maximum_bytes:
                raise AgentSignalAccessError(
                    f"{label} size must be between 1 and {maximum_bytes} bytes"
                )
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 1024 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(descriptor)
            if (
                len(raw) != before.st_size
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or after.st_ino != before.st_ino
                or after.st_dev != before.st_dev
            ):
                raise AgentSignalAccessError(
                    f"{label} changed while being read: {source}"
                )
            return raw
        except OSError as exc:
            raise AgentSignalAccessError(
                f"{label} could not be read: {source}: {exc}"
            ) from exc
        finally:
            os.close(descriptor)
