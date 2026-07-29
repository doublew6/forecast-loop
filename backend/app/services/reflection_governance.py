"""Deterministic human-review and Lesson promotion gates."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..market_universe import DEFAULT_MARKET_UNIVERSE
from ..models import ReflectionHumanReview, ReflectionRun, WorkflowRun

LESSON_POLICY_VERSION = "1.0.0"
MIN_INDEPENDENT_EPISODES = 5
MIN_REPLAY_TARGET_DATES = 20
LESSON_HALF_LIFE_SESSIONS = 60
LESSON_REVALIDATION_EPISODES = 20
IMMEDIATE_EXTREME_TYPES = frozenset({"data_coverage", "risk_check", "workflow"})


@dataclass(frozen=True, slots=True)
class LessonPolicyAssessment:
    """Machine-checkable eligibility; publication still requires a human."""

    evidence_threshold_met: bool
    wiki_review_ready: bool
    automatic_promotion_allowed: bool
    blockers: tuple[str, ...]
    immediate_extreme_checklist: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_version": LESSON_POLICY_VERSION,
            "evidence_threshold_met": self.evidence_threshold_met,
            "wiki_review_ready": self.wiki_review_ready,
            "automatic_promotion_allowed": self.automatic_promotion_allowed,
            "blockers": list(self.blockers),
            "immediate_extreme_checklist": self.immediate_extreme_checklist,
            "requirements": {
                "independent_episode_count": MIN_INDEPENDENT_EPISODES,
                "replay_target_dates": MIN_REPLAY_TARGET_DATES,
                "average_brier_improvement": "positive",
                "calibration_improvement": "positive",
                "important_subgroups_non_degrading": True,
                "human_wiki_review": True,
            },
        }


@dataclass(frozen=True, slots=True)
class ReflectionReviewGateState:
    """Cutoff-bound current reflection heads and their approved leading prefix."""

    current_reflection_ids: tuple[str, ...]
    approved_current_ids: tuple[str, ...]
    approved_prefix_ids: tuple[str, ...]
    lineage_conflict_ids: tuple[str, ...]
    completed_target_dates: tuple[date, ...]
    evidence_hash: str


def record_reflection_human_review(
    session: Session,
    *,
    reflection_id: str,
    decision: str,
    reviewer: str,
    notes: str,
    reviewed_at: datetime,
) -> ReflectionHumanReview:
    """Append one immutable review decision for a completed live reflection."""

    if decision not in {"approved", "rejected"}:
        raise ValueError("review decision must be approved or rejected")
    if reviewed_at.tzinfo is None or reviewed_at.utcoffset() is None:
        raise ValueError("reviewed_at must be timezone-aware")
    reviewer = reviewer.strip()
    if not reviewer or len(reviewer) > 120:
        raise ValueError("reviewer must contain 1-120 characters")
    if len(notes) > 20_000:
        raise ValueError("review notes are too long")
    reflection = session.get(ReflectionRun, reflection_id)
    if reflection is None:
        raise ValueError("reflection was not found")
    if reflection.status != "completed" or reflection.source_run.mode != "live":
        raise ValueError("only completed live reflections may be human-reviewed")
    if (
        reflection.completed_at is None
        or _sort_instant(reflection.completed_at, reviewed_at)
        > reviewed_at.astimezone(UTC)
    ):
        raise ValueError("reviewed_at cannot be earlier than reflection completion")
    existing = session.scalar(
        select(ReflectionHumanReview).where(
            ReflectionHumanReview.reflection_run_id == reflection_id
        )
    )
    notes_hash = hashlib.sha256(notes.encode("utf-8")).hexdigest()
    if existing is not None:
        if (
            existing.decision == decision
            and existing.reviewer == reviewer
            and existing.notes_hash == notes_hash
        ):
            return existing
        raise ValueError("reflection already has a different immutable human review")
    row = ReflectionHumanReview(
        id=str(uuid4()),
        reflection_run_id=reflection_id,
        decision=decision,
        reviewer=reviewer,
        notes=notes,
        notes_hash=notes_hash,
        reviewed_at=reviewed_at,
    )
    session.add(row)
    session.flush()
    return row


def approved_reflection_review_count(
    session: Session,
    *,
    cutoff: datetime | None = None,
    market_universe_hash: str | None = DEFAULT_MARKET_UNIVERSE.content_hash,
) -> int:
    """Count the approved leading prefix of current completed Live reflections."""

    return len(
        reflection_review_gate_state(
            session,
            cutoff=cutoff,
            market_universe_hash=market_universe_hash,
        ).approved_prefix_ids
    )


def reflection_review_gate_state(
    session: Session,
    *,
    cutoff: datetime | None = None,
    market_universe_hash: str | None = DEFAULT_MARKET_UNIVERSE.content_hash,
) -> ReflectionReviewGateState:
    """Resolve supersession and enforce review of the earliest reflections first.

    A later approved reflection cannot bypass an earlier unreviewed or rejected
    current head.  A completed successor removes the corrected predecessor from
    the ordered gate as of the supplied cutoff.
    """

    if cutoff is not None and (
        cutoff.tzinfo is None or cutoff.utcoffset() is None
    ):
        raise ValueError("reflection review gate cutoff must be timezone-aware")
    statement = (
        select(
            ReflectionRun.id,
            ReflectionRun.target_date,
            ReflectionRun.horizon,
            ReflectionRun.created_at,
            ReflectionRun.completed_at,
            ReflectionRun.supersedes_id,
            ReflectionRun.schema_version,
            ReflectionRun.source_run_id,
            ReflectionRun.evaluation_set_hash,
            ReflectionRun.output_hash,
            ReflectionRun.receipt_hash,
            ReflectionHumanReview.id.label("review_id"),
            ReflectionHumanReview.decision,
            ReflectionHumanReview.reviewer,
            ReflectionHumanReview.reviewed_at,
            ReflectionHumanReview.notes_hash,
            WorkflowRun.completed_at.label("source_run_completed_at"),
        )
        .join(WorkflowRun, WorkflowRun.id == ReflectionRun.source_run_id)
        .outerjoin(
            ReflectionHumanReview,
            ReflectionHumanReview.reflection_run_id == ReflectionRun.id,
        )
        .where(
            ReflectionRun.status == "completed",
            WorkflowRun.mode == "live",
            WorkflowRun.status == "completed",
        )
    )
    if market_universe_hash is not None:
        statement = statement.where(
            WorkflowRun.market_universe_hash == market_universe_hash
        )
    rows = []
    for raw in session.execute(statement).mappings():
        row = dict(raw)
        if (
            row["completed_at"] is None
            or row["source_run_completed_at"] is None
            or not _within_cutoff(row["completed_at"], cutoff)
            or not _within_cutoff(row["source_run_completed_at"], cutoff)
            or (
                cutoff is not None
                and row["target_date"] > cutoff.date()
            )
        ):
            continue
        if row["reviewed_at"] is not None and not _within_cutoff(
            row["reviewed_at"], cutoff
        ):
            row["review_id"] = None
            row["decision"] = None
            row["reviewer"] = None
            row["reviewed_at"] = None
            row["notes_hash"] = None
        rows.append(row)
    superseded_ids = {
        str(row["supersedes_id"])
        for row in rows
        if row["supersedes_id"] is not None
    }
    current = sorted(
        (row for row in rows if str(row["id"]) not in superseded_ids),
        key=lambda row: (
            row["target_date"],
            str(row["horizon"]),
            _sort_instant(row["created_at"], cutoff),
            str(row["id"]),
        ),
    )
    current_by_lineage: dict[
        tuple[str, str, date, str],
        list[dict[str, object]],
    ] = {}
    for row in current:
        lineage_key = (
            str(row["source_run_id"]),
            str(row["horizon"]),
            row["target_date"],
            str(row["evaluation_set_hash"]),
        )
        current_by_lineage.setdefault(lineage_key, []).append(row)
    lineage_conflict_ids = tuple(
        sorted(
            str(row["id"])
            for heads in current_by_lineage.values()
            if len(heads) > 1
            for row in heads
        )
    )
    approved_current = (
        set()
        if lineage_conflict_ids
        else {
            str(row["id"])
            for row in current
            if row["decision"] == "approved"
            and row["reviewed_at"] is not None
            and _within_cutoff(row["reviewed_at"], cutoff)
        }
    )
    approved_prefix: list[str] = []
    for row in current:
        reflection_id = str(row["id"])
        if reflection_id not in approved_current:
            break
        approved_prefix.append(reflection_id)
    evidence_payload = [
        {
            "reflection_id": str(row["id"]),
            "target_date": row["target_date"].isoformat(),
            "horizon": str(row["horizon"]),
            "created_at": _sort_instant(
                row["created_at"], cutoff
            ).isoformat(),
            "completed_at": _sort_instant(
                row["completed_at"], cutoff
            ).isoformat(),
            "supersedes_id": row["supersedes_id"],
            "schema_version": str(row["schema_version"]),
            "source_run_id": str(row["source_run_id"]),
            "evaluation_set_hash": str(row["evaluation_set_hash"]),
            "output_hash": row["output_hash"],
            "receipt_hash": row["receipt_hash"],
            "source_run_completed_at": _sort_instant(
                row["source_run_completed_at"], cutoff
            ).isoformat(),
            "review_id": row["review_id"],
            "review_decision": row["decision"],
            "reviewer": row["reviewer"],
            "reviewed_at": (
                None
                if row["reviewed_at"] is None
                else _sort_instant(row["reviewed_at"], cutoff).isoformat()
            ),
            "review_notes_hash": row["notes_hash"],
        }
        for row in current
    ]
    hashed_evidence = {
        "current_heads": evidence_payload,
        "lineage_conflict_ids": list(lineage_conflict_ids),
    }
    return ReflectionReviewGateState(
        current_reflection_ids=tuple(str(row["id"]) for row in current),
        approved_current_ids=tuple(
            str(row["id"]) for row in current if str(row["id"]) in approved_current
        ),
        approved_prefix_ids=tuple(approved_prefix),
        lineage_conflict_ids=lineage_conflict_ids,
        completed_target_dates=tuple(
            sorted({row["target_date"] for row in current})
        ),
        evidence_hash=hashlib.sha256(
            json.dumps(
                hashed_evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    )


def _within_cutoff(value: datetime, cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    return _sort_instant(value, cutoff) <= cutoff


def _sort_instant(value: datetime, cutoff: datetime | None) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(
            tzinfo=cutoff.tzinfo if cutoff is not None else UTC
        )
    return value.astimezone(UTC)


def completed_live_target_date_count(
    session: Session,
    *,
    include_target_date=None,
) -> int:
    """Count independent market episodes; D1/D2 and five indexes share one date."""

    dates = set(
        session.scalars(
            select(ReflectionRun.target_date)
            .join(WorkflowRun, WorkflowRun.id == ReflectionRun.source_run_id)
            .where(
                ReflectionRun.status == "completed",
                WorkflowRun.mode == "live",
                WorkflowRun.status == "completed",
                WorkflowRun.market_universe_hash
                == DEFAULT_MARKET_UNIVERSE.content_hash,
            )
        ).all()
    )
    if include_target_date is not None:
        dates.add(include_target_date)
    return len(dates)


def assess_lesson_policy(
    *,
    proposal_type: str,
    overall_severity: str,
    independent_episode_count: int,
    replay_target_dates: int,
    average_brier_improvement: float | None,
    calibration_improvement: float | None,
    important_subgroups_non_degrading: bool | None,
    completed_shadow_target_dates: int,
    required_shadow_target_dates: int,
) -> LessonPolicyAssessment:
    """Apply the v1 evidence and shadow-run thresholds without model judgment."""

    is_extreme = overall_severity in {"extreme", "systemic_extreme_down"}
    immediate = is_extreme and proposal_type in IMMEDIATE_EXTREME_TYPES
    blockers: list[str] = []
    if completed_shadow_target_dates < required_shadow_target_dates:
        blockers.append("shadow_target_dates_below_minimum")
    if not immediate:
        if independent_episode_count < MIN_INDEPENDENT_EPISODES:
            blockers.append("independent_episodes_below_5")
        if replay_target_dates < MIN_REPLAY_TARGET_DATES:
            blockers.append("replay_target_dates_below_20")
        if average_brier_improvement is None or average_brier_improvement <= 0:
            blockers.append("average_brier_not_improved")
        if calibration_improvement is None or calibration_improvement <= 0:
            blockers.append("calibration_not_improved")
        if important_subgroups_non_degrading is not True:
            blockers.append("important_subgroup_regression_not_cleared")
    evidence_threshold_met = not [
        blocker
        for blocker in blockers
        if blocker != "shadow_target_dates_below_minimum"
    ]
    return LessonPolicyAssessment(
        evidence_threshold_met=evidence_threshold_met,
        wiki_review_ready=not blockers,
        automatic_promotion_allowed=False,
        blockers=tuple(blockers),
        immediate_extreme_checklist=immediate,
    )
