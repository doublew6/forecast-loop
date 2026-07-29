"""Infrastructure adapters implementing forecast-loop application ports."""

from ..ports import QuantSignalCandidate
from .external_snapshot_builder import (
    ExternalEvidenceSnapshotBuilder,
    ExternalMarketOutcomeSnapshotBuilder,
)
from .local_json_snapshot import LocalJsonEvidenceSnapshotSource
from .local_quant_signal import LocalJsonQuantSignalSource

__all__ = [
    "ExternalEvidenceSnapshotBuilder",
    "ExternalMarketOutcomeSnapshotBuilder",
    "LocalJsonEvidenceSnapshotSource",
    "LocalJsonQuantSignalSource",
    "QuantSignalCandidate",
]
