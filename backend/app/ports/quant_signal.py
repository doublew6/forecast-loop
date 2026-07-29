"""Stable port for verified Quant candidates awaiting host admission."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from ..agent_contracts import (
    AgentSignalDraft,
    SignalProvenance,
    SignalTarget,
)
from .agent_signal import AgentSignalSource


@dataclass(frozen=True, slots=True)
class QuantSignalCandidate:
    """A validated source draft plus verified context awaiting host admission."""

    draft: AgentSignalDraft
    target: SignalTarget
    provenance: SignalProvenance
    bundle_content_hash: str
    manifest_sha256: str
    evidence_snapshot_hash: str | None
    market_universe_hash: str | None


@runtime_checkable
class QuantSignalSource(AgentSignalSource, Protocol):
    """Load Quant drafts and resolve one exact host target without side effects."""

    def load_candidates(
        self,
        *,
        as_of: datetime,
    ) -> tuple[QuantSignalCandidate, ...]:
        """Return every candidate from one all-or-nothing verified bundle read."""

    def load_candidate(self, *, target: SignalTarget) -> QuantSignalCandidate:
        """Return the unique verified candidate for ``target`` or fail closed."""
