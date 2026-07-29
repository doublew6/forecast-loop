"""Host-owned admission of verified Quant candidates into the shared contract."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from ..agent_contracts import (
    ParticipationMode,
    SignalEnvelope,
    SignalInputBinding,
    SignalTarget,
    agent_spec,
)
from ..ports import QuantSignalCandidate
from .signal_contract import accept_signal_draft

QUANT_AGENT_ID = "quant_agent"


def accept_quant_candidate(
    *,
    candidate: QuantSignalCandidate,
    mode: Literal["demo", "live"],
    target: SignalTarget,
    accepted_at: datetime,
    submission_deadline: datetime,
    input_binding: SignalInputBinding,
) -> SignalEnvelope:
    """Admit a read-only result using only the active host AgentSpec.

    The source target is checked before the generic acceptance boundary. The
    source cannot provide its own identity, policy, receipt time, run binding,
    or final envelope hash.
    """

    spec = agent_spec(QUANT_AGENT_ID)
    if spec.participation.mode is not ParticipationMode.SHADOW:
        raise ValueError("the active Quant AgentSpec must remain shadow-only")
    if candidate.target != target:
        raise ValueError("Quant candidate target does not match the host target")
    if input_binding.agent_spec_hash != spec.content_hash:
        raise ValueError("Quant input binding must reference the active AgentSpec")
    if (
        candidate.evidence_snapshot_hash is not None
        and input_binding.evidence_snapshot_hash != candidate.evidence_snapshot_hash
    ):
        raise ValueError(
            "Quant bundle Evidence Snapshot hash does not match the host input binding"
        )
    return accept_signal_draft(
        draft=candidate.draft,
        agent_id=QUANT_AGENT_ID,
        mode=mode,
        target=target,
        accepted_at=accepted_at,
        submission_deadline=submission_deadline,
        input_binding=input_binding,
        provenance=candidate.provenance,
    )
