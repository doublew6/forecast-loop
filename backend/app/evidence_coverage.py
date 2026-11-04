"""Versioned, provider-neutral policy coverage gates for sealed handoffs.

Collectors own discovery and semantic verification. This contract binds their
receipts and required events to immutable evidence and assignment permissions.
Citations establish structural coverage, never the direction of market impact.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CoverageModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CollectionReceiptV1(CoverageModel):
    group_id: str = Field(min_length=1, max_length=160)
    status: Literal["complete", "failed"]
    window_start: datetime
    observed_through: datetime
    captured_at: datetime
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("window_start", "observed_through", "captured_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("collection receipt requires timezone-aware times")
        return value

    @model_validator(mode="after")
    def order(self) -> CollectionReceiptV1:
        if not self.window_start <= self.observed_through <= self.captured_at:
            raise ValueError("invalid collection receipt time order")
        return self


class PolicyEventRequirementV1(CoverageModel):
    event_id: str = Field(min_length=1, max_length=160)
    status: Literal["verified", "unresolved"]
    evidence_item_ids: list[str] = Field(max_length=100)
    source_hashes: dict[str, str]
    required_agent_ids: list[str] = Field(min_length=2, max_length=20)
    missing_reason: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def shape(self) -> PolicyEventRequirementV1:
        if len(set(self.evidence_item_ids)) != len(self.evidence_item_ids):
            raise ValueError("duplicate policy evidence IDs")
        if len(set(self.required_agent_ids)) != len(self.required_agent_ids):
            raise ValueError("duplicate policy coverage agents")
        if not {"macro_policy_agent", "strategy_agent"}.issubset(self.required_agent_ids):
            raise ValueError("policy events require macro and Strategy coverage")
        if set(self.source_hashes) != set(self.evidence_item_ids):
            raise ValueError("policy hashes must bind exactly the supporting evidence")
        if any(
            len(h) != 64 or any(c not in "0123456789abcdef" for c in h)
            for h in self.source_hashes.values()
        ):
            raise ValueError("invalid policy source hash")
        if self.status == "verified" and (not self.evidence_item_ids or self.missing_reason):
            raise ValueError("verified policy requires evidence and no missing reason")
        if self.status == "unresolved" and not self.missing_reason:
            raise ValueError("unresolved policy requires an explicit missing reason")
        return self


class EvidenceCoverageV1(CoverageModel):
    schema_version: Literal["forecast-loop.evidence-coverage/v1"] = (
        "forecast-loop.evidence-coverage/v1"
    )
    window_start: datetime
    evidence_cutoff: datetime
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_source_groups: list[str] = Field(min_length=1, max_length=100)
    receipts: list[CollectionReceiptV1] = Field(max_length=100)
    events: list[PolicyEventRequirementV1] = Field(max_length=1000)

    @field_validator("window_start", "evidence_cutoff")
    @classmethod
    def aware_window(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("coverage window requires timezone-aware times")
        return value

    @model_validator(mode="after")
    def unique(self) -> EvidenceCoverageV1:
        if self.window_start >= self.evidence_cutoff:
            raise ValueError("coverage window is empty")
        for values in (
            self.required_source_groups,
            [r.group_id for r in self.receipts],
            [e.event_id for e in self.events],
        ):
            if len(set(values)) != len(values):
                raise ValueError("coverage identities must be unique")
        return self


DEGRADED_WARNING = "证据不完整：降级概率预测"


class EvidenceCoverageV3(EvidenceCoverageV1):
    """Explicit partial-coverage policy for new premarket tasks only."""

    schema_version: Literal["forecast-loop.evidence-coverage/v3"] = (
        "forecast-loop.evidence-coverage/v3"
    )
    availability_policy: Literal["partial-evidence/v1"] = "partial-evidence/v1"


def coverage_gaps(coverage: EvidenceCoverageV1) -> list[str]:
    receipts = {r.group_id: r for r in coverage.receipts}
    gaps = []
    for group in coverage.required_source_groups:
        receipt = receipts.get(group)
        if (
            receipt is None
            or receipt.status != "complete"
            or receipt.observed_through < coverage.evidence_cutoff - timedelta(minutes=15)
            or receipt.window_start > coverage.window_start
        ):
            gaps.append(f"source:{group}")
    gaps.extend(f"unresolved:{e.event_id}" for e in coverage.events if e.status == "unresolved")
    return gaps


def validate_coverage(coverage: EvidenceCoverageV1, snapshot, assignments, drafts=None) -> None:
    """Fail closed before publication, including callers bypassing dispatchers."""
    from .evidence_coverage_v2 import MEDIA_WARNING, EvidenceCoverageV2

    if coverage.snapshot_hash != snapshot.content_hash:
        raise ValueError("input_incomplete: coverage snapshot hash mismatch")
    cutoff = getattr(snapshot, "evidence_cutoff", None) or snapshot.data_cutoff
    start = getattr(snapshot, "evidence_start", None) or cutoff.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if coverage.evidence_cutoff != cutoff or coverage.window_start > start:
        raise ValueError("input_incomplete: coverage requested window mismatch")
    partial = isinstance(coverage, EvidenceCoverageV3)
    if any(r.captured_at > cutoff for r in coverage.receipts):
        raise ValueError("input_incomplete: future collection receipt")
    receipts = {r.group_id: r for r in coverage.receipts}
    for group in coverage.required_source_groups:
        receipt = receipts.get(group)
        if (
            receipt is None
            or receipt.status != "complete"
            or receipt.captured_at > cutoff
            or receipt.observed_through < cutoff - timedelta(minutes=15)
            or receipt.window_start > coverage.window_start
        ):
            if not partial:
                raise ValueError(f"input_incomplete: required source group {group}")
    items = {item.item_id: item for item in snapshot.items}
    citations = (
        None
        if drafts is None
        else {d.assignment_id: set(getattr(d, "draft", d).evidence_item_ids) for d in drafts}
    )
    for event in coverage.events:
        reported = isinstance(coverage, EvidenceCoverageV2) and event.status == "media_reported"
        if event.status != "verified" and not reported and not partial:
            raise ValueError(f"input_incomplete: unresolved policy event {event.event_id}")
        for item_id in event.evidence_item_ids:
            item = items.get(item_id)
            if (
                item is None
                or item.source_hash != event.source_hashes[item_id]
                or item.published_at > cutoff
                or item.ingested_at > cutoff
                or getattr(item, "observed_at", item.ingested_at) > cutoff
            ):
                raise ValueError(f"input_incomplete: policy evidence {event.event_id}/{item_id}")
            if reported and MEDIA_WARNING not in item.summary:
                raise ValueError(f"input_incomplete: unmarked media evidence {event.event_id}")
        if partial and event.status == "unresolved":
            continue
        for agent_id in event.required_agent_ids:
            required = [a for a in assignments if a.agent_id == agent_id]
            if not required:
                raise ValueError(f"input_incomplete: policy assignment {event.event_id}/{agent_id}")
            for assignment in required:
                permitted = set(event.evidence_item_ids) & set(assignment.allowed_evidence_item_ids)
                if not permitted:
                    raise ValueError(
                        f"input_incomplete: policy permission {event.event_id}/"
                        f"{assignment.assignment_id}"
                    )
                if citations is not None and not permitted & citations.get(
                    assignment.assignment_id, set()
                ):
                    raise ValueError(
                        f"input_incomplete: missing policy citation {event.event_id}/"
                        f"{assignment.assignment_id}"
                    )
                if reported and drafts is not None:
                    record = next(
                        (d for d in drafts if d.assignment_id == assignment.assignment_id), None
                    )
                    rationale = getattr(getattr(record, "draft", record), "rationale", "")
                    if MEDIA_WARNING not in rationale:
                        raise ValueError(
                            f"input_incomplete: missing media warning {event.event_id}/"
                            f"{assignment.assignment_id}"
                        )
    if isinstance(coverage, EvidenceCoverageV2) and drafts is not None:
        reported_ids = {
            item_id for event in coverage.events if event.status == "media_reported"
            for item_id in event.evidence_item_ids
        }
        for record in drafts:
            draft = getattr(record, "draft", record)
            if (
                reported_ids.intersection(draft.evidence_item_ids)
                and MEDIA_WARNING not in draft.rationale
            ):
                raise ValueError(f"input_incomplete: missing media warning {record.assignment_id}")
