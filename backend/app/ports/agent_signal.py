"""Port for read-only producers of untrusted, structurally sealed signals."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from ..agent_contracts import AgentSignalDraft


class AgentSignalSourceError(RuntimeError):
    """Base error raised when an Agent adapter cannot return signal proposals."""


class AgentSignalAccessError(AgentSignalSourceError):
    """The configured signal source cannot be accessed through a safe read path."""


class AgentSignalFormatError(AgentSignalSourceError):
    """The source bytes are not a supported signal document."""


class AgentSignalValidationError(AgentSignalSourceError):
    """The adapter output fails its AgentSpec or SignalEnvelope contract."""


@runtime_checkable
class AgentSignalSource(Protocol):
    """Read untrusted drafts that still require host-owned acceptance checks."""

    def load_signal_drafts(self, *, as_of: datetime) -> Sequence[AgentSignalDraft]:
        """Return source drafts or fail closed without partial results.

        Before persistence, deterministic host code must independently bind the
        Agent identity/spec, target, receipt time, deadline, provenance and run,
        then seal the resulting SignalEnvelope.
        """
