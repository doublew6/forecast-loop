"""Port for loading a frozen market and evidence snapshot."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from ..schemas import FrozenEvidenceSnapshot


class EvidenceSnapshotSourceError(RuntimeError):
    """Base error raised when a source cannot return a trusted snapshot."""


class EvidenceSnapshotAccessError(EvidenceSnapshotSourceError):
    """The configured snapshot cannot be accessed safely."""


class EvidenceSnapshotFormatError(EvidenceSnapshotSourceError):
    """The source payload is not a valid frozen snapshot document."""


class EvidenceSnapshotValidationError(EvidenceSnapshotSourceError):
    """The snapshot fails freshness, provenance, or integrity validation."""


@runtime_checkable
class EvidenceSnapshotSource(Protocol):
    """Load one immutable snapshot for an exact research cutoff.

    Implementations may read local files, object storage, or another read-only
    upstream. They must return the existing canonical snapshot schema and fail
    closed when the requested cutoff cannot be validated.
    """

    def load_snapshot(self, *, as_of: datetime) -> FrozenEvidenceSnapshot:
        """Return the validated snapshot bound to ``as_of``."""
