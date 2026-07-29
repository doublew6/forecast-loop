"""Read-only local JSON adapter for frozen evidence snapshots."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..ports import (
    EvidenceSnapshotAccessError,
    EvidenceSnapshotFormatError,
    EvidenceSnapshotValidationError,
)
from ..schemas import FrozenEvidenceSnapshot
from ..services.snapshot import (
    LiveEvidenceRequiredError,
    validate_live_snapshot,
    validate_snapshot_content_hash,
)

DEFAULT_MAX_SNAPSHOT_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class LocalJsonEvidenceSnapshotSource:
    """Load a hash-sealed snapshot from a file below a trusted local root."""

    root: Path
    snapshot_path: Path
    max_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES
    instrument_codes: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        object.__setattr__(self, "snapshot_path", Path(self.snapshot_path))
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be greater than zero")

    def load_snapshot(self, *, as_of: datetime) -> FrozenEvidenceSnapshot:
        """Read and validate the snapshot for the exact requested cutoff."""

        snapshot = self._read_snapshot()
        source = self._resolve_source_path()
        try:
            validate_live_snapshot(
                snapshot,
                as_of=as_of,
                instrument_codes=self.instrument_codes,
            )
            validate_snapshot_content_hash(snapshot)
        except LiveEvidenceRequiredError as exc:
            raise EvidenceSnapshotValidationError(
                f"Local evidence snapshot failed freshness, provenance, or integrity "
                f"validation: {source}: {exc}"
            ) from exc
        return snapshot

    def peek_as_of(self) -> datetime:
        """Read the schema-validated timestamp without weakening later validation."""

        return self._read_snapshot().as_of

    def _read_snapshot(self) -> FrozenEvidenceSnapshot:
        source = self._resolve_source_path()
        raw = self._secure_read(source)
        try:
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise EvidenceSnapshotFormatError(
                f"Local evidence snapshot is not valid UTF-8 JSON: {source}: {exc}"
            ) from exc
        try:
            snapshot = FrozenEvidenceSnapshot.model_validate(payload)
        except ValidationError as exc:
            raise EvidenceSnapshotFormatError(
                f"Local evidence snapshot does not match FrozenEvidenceSnapshot: {source}: {exc}"
            ) from exc
        return snapshot

    def _resolve_source_path(self) -> Path:
        configured_root = self.root.expanduser()
        if configured_root.is_symlink():
            raise EvidenceSnapshotAccessError(
                f"Local evidence snapshot root may not be a symlink: {configured_root}"
            )
        try:
            trusted_root = configured_root.resolve(strict=True)
        except OSError as exc:
            raise EvidenceSnapshotAccessError(
                f"Local evidence snapshot root is unavailable: {configured_root}: {exc}"
            ) from exc
        if not trusted_root.is_dir():
            raise EvidenceSnapshotAccessError(
                f"Local evidence snapshot root is not a directory: {trusted_root}"
            )

        configured_source = self.snapshot_path.expanduser()
        if not configured_source.is_absolute():
            configured_source = trusted_root / configured_source
        source = Path(os.path.abspath(configured_source))
        try:
            relative = source.relative_to(trusted_root)
        except ValueError as exc:
            raise EvidenceSnapshotAccessError(
                f"Local evidence snapshot escaped its configured root: {source}"
            ) from exc

        current = trusted_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise EvidenceSnapshotAccessError(
                    f"Local evidence snapshot path may not contain symlinks: {current}"
                )
        try:
            resolved = source.resolve(strict=True)
        except OSError as exc:
            raise EvidenceSnapshotAccessError(
                f"Local evidence snapshot is unavailable: {source}: {exc}"
            ) from exc
        if not resolved.is_relative_to(trusted_root):
            raise EvidenceSnapshotAccessError(
                f"Local evidence snapshot escaped its configured root: {source}"
            )
        if not resolved.is_file():
            raise EvidenceSnapshotAccessError(
                f"Local evidence snapshot must be a regular file: {resolved}"
            )
        return resolved

    def _secure_read(self, source: Path) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(source, flags)
        except OSError as exc:
            raise EvidenceSnapshotAccessError(
                f"Local evidence snapshot cannot be opened safely: {source}: {exc}"
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise EvidenceSnapshotAccessError(
                    f"Local evidence snapshot must be a regular file: {source}"
                )
            if metadata.st_size <= 0 or metadata.st_size > self.max_bytes:
                raise EvidenceSnapshotAccessError(
                    "Local evidence snapshot size must be between 1 and "
                    f"{self.max_bytes} bytes: {source}"
                )
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
                raise EvidenceSnapshotAccessError(
                    f"Local evidence snapshot changed while being read: {source}"
                )
            return raw
        except OSError as exc:
            raise EvidenceSnapshotAccessError(
                f"Local evidence snapshot could not be read: {source}: {exc}"
            ) from exc
        finally:
            os.close(descriptor)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")
