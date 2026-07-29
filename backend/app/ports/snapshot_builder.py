"""Ports for producing immutable input artifacts outside the public core."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..domain import Horizon


class SnapshotBuilderError(RuntimeError):
    """An external snapshot builder failed without publishing a trusted artifact."""


class NoApplicableSessionError(SnapshotBuilderError):
    """The source calendar reports that the requested session is not applicable."""


@runtime_checkable
class EvidenceSnapshotBuilder(Protocol):
    """Materialize one canonical evidence snapshot at an exact output path."""

    def build_snapshot(
        self,
        *,
        base_session: date,
        captured_at: datetime,
        output_path: Path,
    ) -> None:
        """Write a complete snapshot to ``output_path`` or fail closed."""


@runtime_checkable
class MarketOutcomeSnapshotBuilder(Protocol):
    """Materialize one canonical market-outcome snapshot."""

    def build_snapshot(
        self,
        *,
        target_date: date,
        horizon: Horizon,
        captured_at: datetime,
        output_path: Path,
    ) -> None:
        """Write a complete snapshot to ``output_path`` or fail closed."""
