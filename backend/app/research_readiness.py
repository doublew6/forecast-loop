"""Versioned evidence admission for new focused research tasks, not old snapshots."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from .research_v2 import (
    CSI300,
    CSI1000,
    ContractModel,
    EvidenceSnapshotV2,
    HandoffAssignmentV3,
    SignalKindV2,
    content_hash,
)

EvidenceCategory = Literal[
    "index_exposure",
    "market_context",
    "macro_context",
    "expectations_context",
    "industry_context",
    "company_index_mapping",
]


class EvidenceBindingV1(ContractModel):
    """A read-only adapter's attestation, still subject to model/semantic review."""

    category: EvidenceCategory
    evidence_item_id: str = Field(min_length=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    instruments: list[Literal[CSI1000, CSI300]] = Field(min_length=1, max_length=2)
    effective_from: date
    effective_through: date

    @model_validator(mode="after")
    def validate_interval(self) -> EvidenceBindingV1:
        if self.effective_from > self.effective_through:
            raise ValueError("evidence admission validity interval is reversed")
        if len(set(self.instruments)) != len(self.instruments):
            raise ValueError("evidence admission instruments must be unique")
        return self


class EvidenceAdmissionV1(ContractModel):
    schema_version: Literal["forecast-loop.evidence-admission/v1"] = (
        "forecast-loop.evidence-admission/v1"
    )
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    bindings: list[EvidenceBindingV1] = Field(max_length=1000)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_seal(self) -> EvidenceAdmissionV1:
        if self.content_hash != content_hash(self):
            raise ValueError("evidence admission content hash mismatch")
        keys = [(b.category, b.evidence_item_id) for b in self.bindings]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate evidence admission binding")
        return self


class ReadinessIssueV1(ContractModel):
    assignment_id: str = Field(min_length=1)
    code: Literal["missing_evidence", "insufficient_evidence", "rule_conflict", "dependency"]
    requirement: str = Field(min_length=1, max_length=200)
    explanation: str = Field(min_length=1, max_length=1600)
    evidence_item_ids: list[str] = Field(default_factory=list, max_length=100)


class EvidenceReadinessV1(ContractModel):
    schema_version: Literal["forecast-loop.evidence-readiness/v1"] = (
        "forecast-loop.evidence-readiness/v1"
    )
    policy: Literal["focused-evidence/v1"] = "focused-evidence/v1"
    snapshot_hash: str
    admission_hash: str | None
    status: Literal["ready", "not_ready", "demo_only"]
    issues: list[ReadinessIssueV1]


def assess_evidence_readiness(
    snapshot: EvidenceSnapshotV2,
    assignments: list[HandoffAssignmentV3],
    admission: EvidenceAdmissionV1 | None,
    *,
    mode: Literal["demo", "live"],
) -> EvidenceReadinessV1:
    """Check references, validity and assignment coverage; never infer facts from titles."""

    items = {item.item_id: item for item in snapshot.items}
    if len(items) != len(snapshot.items):
        raise ValueError("readiness requires unique snapshot evidence IDs")
    bindings = admission.bindings if admission is not None else []
    if admission is not None and admission.snapshot_hash != snapshot.content_hash:
        raise ValueError("evidence admission binds another snapshot")
    for binding in bindings:
        item = items.get(binding.evidence_item_id)
        if item is None or item.source_hash != binding.source_hash:
            raise ValueError("evidence admission source identity mismatch")
        reference_session = (
            snapshot.future_sessions[0]
            if binding.category in {"index_exposure", "company_index_mapping"}
            else snapshot.base_session
        )
        if not binding.effective_from <= reference_session <= binding.effective_through:
            raise ValueError("evidence admission is stale or not yet effective for its session")
    issues = []
    for assignment in assignments:
        if mode == "demo" or assignment.producer == "deterministic":
            continue
        required: dict[str, set[str]] = {"index_exposure": {CSI1000, CSI300}}
        if assignment.signal_kind is SignalKindV2.NATURAL_VIEW:
            required.update(
                {
                    "market_news_agent": {"market_context": {CSI1000}},
                    "macro_policy_agent": {
                        "macro_context": {CSI1000},
                        "expectations_context": {CSI1000},
                    },
                    "ai_storage_industry_agent": {
                        "industry_context": {CSI1000, CSI300},
                        "company_index_mapping": {CSI1000, CSI300},
                    },
                }.get(assignment.agent_id, {})
            )
        for category, instruments in required.items():
            covered = {
                instrument
                for binding in bindings
                if binding.category == category
                and binding.evidence_item_id in assignment.allowed_evidence_item_ids
                for instrument in binding.instruments
            }
            missing = instruments - covered
            if missing:
                issues.append(
                    ReadinessIssueV1(
                        assignment_id=assignment.assignment_id,
                        code="missing_evidence",
                        requirement=category,
                        explanation="Missing admitted evidence for: " + ", ".join(sorted(missing)),
                    )
                )
    return EvidenceReadinessV1(
        snapshot_hash=snapshot.content_hash,
        admission_hash=admission.content_hash if admission else None,
        status="demo_only" if mode == "demo" else "not_ready" if issues else "ready",
        issues=issues,
    )
