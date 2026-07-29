"""Deterministic replay metrics and append-only Lesson lifecycle governance."""

from __future__ import annotations

import calendar
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    ForecastDiagnostic,
    LessonEpisode,
    LessonLifecycleEvent,
    LessonProposal,
    LessonReplayBatch,
    MarketSessionSnapshot,
)
from .reflection_governance import (
    IMMEDIATE_EXTREME_TYPES,
    LESSON_REVALIDATION_EPISODES,
    MIN_INDEPENDENT_EPISODES,
    MIN_REPLAY_TARGET_DATES,
    assess_lesson_policy,
    completed_live_target_date_count,
)

LESSON_REPLAY_PROTOCOL_VERSION = "1.0.0"
CALIBRATION_BIN_COUNT = 10
_LABELS = ("up", "neutral", "down")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReplayProbabilities(StrictModel):
    up: float = Field(ge=0, le=1)
    neutral: float = Field(ge=0, le=1)
    down: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def probabilities_sum_to_one(self) -> ReplayProbabilities:
        if abs(self.up + self.neutral + self.down - 1.0) > 1e-9:
            raise ValueError("replay probabilities must sum to 1")
        return self


class ReplayObservation(StrictModel):
    target_date: date
    index_code: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,23}$")
    horizon: Literal["D1", "D2"]
    actual_label: Literal["up", "neutral", "down"]
    forecast_id: str = Field(min_length=1, max_length=36)
    forecast_diagnostic_id: str = Field(min_length=1, max_length=36)
    outcome_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    market_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_probabilities: ReplayProbabilities
    candidate_probabilities: ReplayProbabilities
    important_subgroups: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("important_subgroups")
    @classmethod
    def validate_subgroups(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > 80 for value in normalized):
            raise ValueError("important subgroup labels must contain 1-80 characters")
        if len(normalized) != len(set(normalized)):
            raise ValueError("important subgroup labels must be unique")
        return sorted(normalized)

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.target_date.isoformat(), self.index_code, self.horizon)


class ClassLogitBias(StrictModel):
    up: float = Field(ge=-10, le=10)
    neutral: float = Field(ge=-10, le=10)
    down: float = Field(ge=-10, le=10)


class CandidateTransform(StrictModel):
    transform_type: Literal["temperature_class_bias_v1"]
    temperature: float = Field(gt=0, le=10)
    class_logit_bias: ClassLogitBias


class LessonReplayBundle(StrictModel):
    protocol_version: Literal["1.0.0"] = LESSON_REPLAY_PROTOCOL_VERSION
    lesson_id: str = Field(min_length=1, max_length=36)
    baseline_rule_version: Literal["forecast-probabilities-v1"]
    candidate_rule_version: str = Field(min_length=1, max_length=120)
    wiki_version: str = Field(min_length=1, max_length=120)
    threshold_policy_version: str = Field(min_length=1, max_length=120)
    replay_generator: Literal["deterministic_rule_engine"]
    candidate_transform: CandidateTransform
    replay_input_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    observations: list[ReplayObservation] = Field(min_length=1, max_length=25_000)

    @model_validator(mode="after")
    def observation_identities_are_unique(self) -> LessonReplayBundle:
        identities = [item.identity for item in self.observations]
        if len(identities) != len(set(identities)):
            raise ValueError(
                "replay observation identities must be unique by "
                "target_date/index_code/horizon"
            )
        return self


@dataclass(frozen=True, slots=True)
class ReplayRecordResult:
    batch: LessonReplayBatch
    event: LessonLifecycleEvent
    idempotent: bool


@dataclass(frozen=True, slots=True)
class LessonTransitionResult:
    lesson: LessonProposal
    event: LessonLifecycleEvent
    idempotent: bool


@dataclass(frozen=True, slots=True)
class DueLessonReview:
    lesson_id: str
    status: str
    reasons: tuple[str, ...]
    latest_replay_hash: str | None


@dataclass(frozen=True, slots=True)
class LessonAuditReport:
    lesson_id: str
    status: str
    replay_batch_count: int
    lifecycle_event_count: int
    latest_replay_hash: str | None
    audit_root_hash: str


def parse_replay_bundle(payload: dict[str, Any]) -> LessonReplayBundle:
    """Validate an untrusted JSON object before any database mutation."""

    return LessonReplayBundle.model_validate(payload)


def record_lesson_replay(
    session: Session,
    *,
    bundle: LessonReplayBundle,
    submitted_by: str,
    recorded_at: datetime,
    required_shadow_target_dates: int = 20,
) -> ReplayRecordResult:
    """Append one semantic replay batch and refresh the deterministic projection."""

    _validate_actor_and_time(submitted_by, recorded_at)
    lesson = session.get(LessonProposal, bundle.lesson_id)
    if lesson is None:
        raise ValueError("lesson proposal was not found")
    if lesson.status in {"retired", "superseded"}:
        raise ValueError("retired or superseded lessons cannot accept replay observations")
    diagnostics = [
        _validate_observation_against_frozen_outcome(
            session,
            observation.model_dump(mode="json"),
        )
        for observation in bundle.observations
    ]
    _validate_bundle_artifacts(bundle, diagnostics)

    normalized = _normalized_bundle(bundle)
    content_hash = _canonical_hash(normalized)
    existing_batch = session.scalar(
        select(LessonReplayBatch).where(
            LessonReplayBatch.lesson_proposal_id == lesson.id,
            LessonReplayBatch.content_hash == content_hash,
        )
    )
    if existing_batch is not None:
        event = session.scalar(
            select(LessonLifecycleEvent).where(
                LessonLifecycleEvent.lesson_proposal_id == lesson.id,
                LessonLifecycleEvent.event_key == f"replay:{content_hash}",
            )
        )
        if event is None:
            raise RuntimeError("replay batch exists without its immutable lifecycle event")
        return ReplayRecordResult(batch=existing_batch, event=event, idempotent=True)

    existing_batches = list(
        session.scalars(
            select(LessonReplayBatch)
            .where(LessonReplayBatch.lesson_proposal_id == lesson.id)
            .order_by(LessonReplayBatch.created_at, LessonReplayBatch.id)
        ).all()
    )
    manifest = {
        key: value for key, value in normalized.items() if key != "observations"
    }
    mismatched_manifest = next(
        (
            batch
            for batch in existing_batches
            if batch.manifest != manifest
        ),
        None,
    )
    if mismatched_manifest is not None:
        raise ValueError(
            "replay manifest changed; create a successor Lesson instead of "
            "mixing rule, Wiki, threshold, or protocol versions"
        )
    prior_observations = [
        observation
        for batch in existing_batches
        for observation in batch.observations
    ]
    prior_identities = {_observation_identity(item) for item in prior_observations}
    overlap = prior_identities & {
        observation.identity for observation in bundle.observations
    }
    if overlap:
        raise ValueError(
            "replay observations were already recorded: "
            + ", ".join("/".join(item) for item in sorted(overlap))
        )

    observations = [
        *prior_observations,
        *(item.model_dump(mode="json") for item in bundle.observations),
    ]
    observations.sort(key=_observation_identity)
    aggregate_metrics = compute_replay_metrics(observations)
    independent_episode_count = max(
        lesson.independent_episode_count,
        int(
            session.scalar(
                select(func.count())
                .select_from(LessonEpisode)
                .where(LessonEpisode.cluster_key == lesson.cluster_key)
            )
            or 0
        ),
    )
    assessment = _assess_current_policy(
        session,
        lesson=lesson,
        independent_episode_count=independent_episode_count,
        aggregate_metrics=aggregate_metrics,
        required_shadow_target_dates=required_shadow_target_dates,
    )
    projection = {
        **(lesson.replay_metrics or {}),
        **aggregate_metrics,
        **assessment,
        "completed_shadow_target_dates": completed_live_target_date_count(session),
        "revalidation_due_after_independent_episode_count": (
            independent_episode_count + LESSON_REVALIDATION_EPISODES
        ),
        "revalidation_due_after_sessions": lesson.half_life_sessions,
        "automatic_wiki_promotion": False,
        "wiki_promotion_status": "not_promoted",
    }
    lesson.independent_episode_count = independent_episode_count
    lesson.replay_target_dates = int(aggregate_metrics["distinct_target_dates"])
    lesson.replay_metrics = projection
    if lesson.status in {"active", "challenged"}:
        due_reasons = lesson_revalidation_due_reasons(lesson, as_of=recorded_at)
        lesson.replay_metrics = {
            **lesson.replay_metrics,
            "revalidation_due": bool(due_reasons),
            "revalidation_due_reasons": due_reasons,
        }

    batch = LessonReplayBatch(
        id=str(uuid4()),
        lesson_proposal_id=lesson.id,
        content_hash=content_hash,
        manifest=manifest,
        observations=[item.model_dump(mode="json") for item in bundle.observations],
        observation_count=len(bundle.observations),
        distinct_target_dates=len(
            {item.target_date for item in bundle.observations}
        ),
        aggregate_metrics=aggregate_metrics,
        submitted_by=submitted_by.strip(),
        created_at=recorded_at,
    )
    session.add(batch)
    session.flush()
    event = _append_event(
        session,
        lesson=lesson,
        event_type="replay_recorded",
        event_key=f"replay:{content_hash}",
        from_status=lesson.status,
        to_status=lesson.status,
        actor=submitted_by,
        reason="deterministic replay observations recorded",
        payload={
            "batch_id": batch.id,
            "batch_content_hash": content_hash,
            "manifest": batch.manifest,
            "batch_observation_count": batch.observation_count,
            "aggregate_replay_set_hash": aggregate_metrics["replay_set_hash"],
            "aggregate_metrics": aggregate_metrics,
        },
        occurred_at=recorded_at,
    )
    session.flush()
    return ReplayRecordResult(batch=batch, event=event, idempotent=False)


def approve_lesson(
    session: Session,
    *,
    lesson_id: str,
    reviewer: str,
    notes: str,
    approved_at: datetime,
    supersedes_id: str | None = None,
) -> LessonTransitionResult:
    """Human-approve an eligible candidate as active; never write to Wiki."""

    _validate_actor_and_time(reviewer, approved_at)
    _validate_reason(notes)
    lesson = session.get(LessonProposal, lesson_id)
    if lesson is None:
        raise ValueError("lesson proposal was not found")
    event_key = "approve:" + _canonical_hash(
        {
            "lesson_id": lesson_id,
            "reviewer": reviewer.strip(),
            "notes": notes,
            "supersedes_id": supersedes_id,
        }
    )
    existing_event = _event_by_key(session, lesson_id, event_key)
    if existing_event is not None:
        return LessonTransitionResult(
            lesson=lesson,
            event=existing_event,
            idempotent=True,
        )
    if lesson.status != "candidate":
        raise ValueError("only a candidate lesson may be approved")
    _validate_activation_eligibility(session, lesson)

    active_heads = list(
        session.scalars(
            select(LessonProposal).where(
                LessonProposal.cluster_key == lesson.cluster_key,
                LessonProposal.id != lesson.id,
                LessonProposal.status.in_(("active", "challenged")),
            )
        ).all()
    )
    if len(active_heads) > 1:
        raise ValueError("lesson cluster has multiple active heads and must be repaired")
    superseded = active_heads[0] if active_heads else None
    if superseded is not None and supersedes_id != superseded.id:
        raise ValueError(
            "an active cluster head exists; approval must explicitly supersede it"
        )
    if superseded is None and supersedes_id is not None:
        raise ValueError("supersedes must point to the current active cluster head")

    activated_target_dates = lesson.replay_target_dates
    previous_superseded_status: str | None = None
    if superseded is not None:
        previous_superseded_status = superseded.status
        superseded.status = "superseded"
        superseded.reviewed_at = approved_at
        superseded.replay_metrics = {
            **(superseded.replay_metrics or {}),
            "superseded_by_id": lesson.id,
            "superseded_at": approved_at.isoformat(),
            "revalidation_due": False,
            "revalidation_due_reasons": [],
            "automatic_wiki_promotion": False,
            "wiki_promotion_status": "not_promoted",
        }
    lesson.status = "active"
    lesson.reviewed_at = approved_at
    lesson.supersedes_id = superseded.id if superseded else None
    lesson.replay_metrics = {
        **(lesson.replay_metrics or {}),
        "activated_at": approved_at.isoformat(),
        "activated_replay_target_dates": activated_target_dates,
        "last_revalidated_at": approved_at.isoformat(),
        "last_revalidated_target_dates": activated_target_dates,
        "last_revalidated_replay_set_hash": lesson.replay_metrics.get(
            "replay_set_hash"
        ),
        "next_monthly_revalidation_at": _add_calendar_month(approved_at).isoformat(),
        "next_replay_revalidation_target_dates": (
            activated_target_dates + LESSON_REVALIDATION_EPISODES
        ),
        "consecutive_failed_revalidations": 0,
        "revalidation_due": False,
        "revalidation_due_reasons": [],
        "human_activation_reviewer": reviewer.strip(),
        "automatic_wiki_promotion": False,
        "wiki_promotion_status": "not_promoted",
    }
    event = _append_event(
        session,
        lesson=lesson,
        event_type="approved",
        event_key=event_key,
        from_status="candidate",
        to_status="active",
        actor=reviewer,
        reason=notes,
        payload={
            "supersedes_id": superseded.id if superseded else None,
            "replay_set_hash": lesson.replay_metrics.get("replay_set_hash"),
            "policy_version": lesson.replay_metrics.get("policy_version"),
            "wiki_promotion_performed": False,
        },
        occurred_at=approved_at,
    )
    if superseded is not None:
        _append_event(
            session,
            lesson=superseded,
            event_type="superseded",
            event_key=f"superseded-by:{lesson.id}",
            from_status=previous_superseded_status or "active",
            to_status="superseded",
            actor=reviewer,
            reason=notes,
            payload={
                "successor_id": lesson.id,
                "successor_approval_event_id": event.id,
                "wiki_promotion_performed": False,
            },
            occurred_at=approved_at,
        )
    session.flush()
    return LessonTransitionResult(lesson=lesson, event=event, idempotent=False)


def revalidate_lesson(
    session: Session,
    *,
    lesson_id: str,
    reviewer: str,
    notes: str,
    reviewed_at: datetime,
    required_shadow_target_dates: int = 20,
    checklist_valid: bool | None = None,
) -> LessonTransitionResult:
    """Run a due review and deterministically project active/challenged/retired."""

    _validate_actor_and_time(reviewer, reviewed_at)
    _validate_reason(notes)
    lesson = session.get(LessonProposal, lesson_id)
    if lesson is None:
        raise ValueError("lesson proposal was not found")
    event_key = "revalidate:" + _canonical_hash(
        {
            "lesson_id": lesson.id,
            "reviewer": reviewer.strip(),
            "notes": notes,
            "reviewed_at": reviewed_at.isoformat(),
            "replay_set_hash": (lesson.replay_metrics or {}).get("replay_set_hash"),
            "checklist_valid": checklist_valid,
        }
    )
    existing_event = _event_by_key(session, lesson.id, event_key)
    if existing_event is not None:
        return LessonTransitionResult(
            lesson=lesson,
            event=existing_event,
            idempotent=True,
        )
    if lesson.status not in {"active", "challenged"}:
        raise ValueError("only an active or challenged lesson may be revalidated")
    due_reasons = lesson_revalidation_due_reasons(lesson, as_of=reviewed_at)
    if not due_reasons:
        raise ValueError("lesson revalidation is not due")

    metrics = lesson.replay_metrics or {}
    immediate = metrics.get("immediate_extreme_checklist") is True
    if immediate:
        if checklist_valid is None:
            raise ValueError(
                "extreme-event checklist revalidation requires checklist_valid"
            )
        passed = checklist_valid
        assessment = {
            key: metrics.get(key)
            for key in (
                "policy_version",
                "evidence_threshold_met",
                "wiki_review_ready",
                "automatic_promotion_allowed",
                "blockers",
                "immediate_extreme_checklist",
                "requirements",
            )
        }
    else:
        assessment = _assess_current_policy(
            session,
            lesson=lesson,
            independent_episode_count=lesson.independent_episode_count,
            aggregate_metrics=metrics,
            required_shadow_target_dates=required_shadow_target_dates,
        )
        last_target_dates = int(metrics.get("last_revalidated_target_dates", 0))
        last_replay_hash = metrics.get("last_revalidated_replay_set_hash")
        current_replay_hash = metrics.get("replay_set_hash")
        has_fresh_replay = (
            lesson.replay_target_dates > last_target_dates
            and isinstance(current_replay_hash, str)
            and current_replay_hash != last_replay_hash
        )
        if not has_fresh_replay:
            assessment = {
                **assessment,
                "wiki_review_ready": False,
                "blockers": [
                    *assessment.get("blockers", []),
                    "no_new_replay_evidence_since_last_validation",
                ],
            }
        passed = assessment["wiki_review_ready"] is True

    previous_status = lesson.status
    prior_failures = int(metrics.get("consecutive_failed_revalidations", 0))
    if passed:
        next_status = "active"
        failures = 0
        event_type = "revalidated"
    else:
        failures = prior_failures + 1
        if previous_status == "challenged" or failures >= 2:
            next_status = "retired"
            event_type = "retired"
        else:
            next_status = "challenged"
            event_type = "challenged"

    lesson.status = next_status
    lesson.reviewed_at = reviewed_at
    lifecycle_projection = {
        **metrics,
        **assessment,
        "last_revalidated_at": reviewed_at.isoformat(),
        "last_revalidated_target_dates": lesson.replay_target_dates,
        "last_revalidated_replay_set_hash": metrics.get("replay_set_hash"),
        "last_revalidation_result": "passed" if passed else "failed",
        "last_revalidation_due_reasons": due_reasons,
        "consecutive_failed_revalidations": failures,
        "automatic_wiki_promotion": False,
        "wiki_promotion_status": "not_promoted",
    }
    if next_status == "retired":
        lifecycle_projection.update(
            {
                "retired_at": reviewed_at.isoformat(),
                "revalidation_due": False,
                "revalidation_due_reasons": [],
                "next_monthly_revalidation_at": None,
                "next_replay_revalidation_target_dates": None,
            }
        )
    else:
        lifecycle_projection.update(
            {
                "next_monthly_revalidation_at": _add_calendar_month(
                    reviewed_at
                ).isoformat(),
                "next_replay_revalidation_target_dates": (
                    lesson.replay_target_dates + LESSON_REVALIDATION_EPISODES
                ),
                "revalidation_due": False,
                "revalidation_due_reasons": [],
            }
        )
    lesson.replay_metrics = lifecycle_projection
    event = _append_event(
        session,
        lesson=lesson,
        event_type=event_type,
        event_key=event_key,
        from_status=previous_status,
        to_status=next_status,
        actor=reviewer,
        reason=notes,
        payload={
            "passed": passed,
            "due_reasons": due_reasons,
            "checklist_valid": checklist_valid,
            "consecutive_failed_revalidations": failures,
            "replay_set_hash": metrics.get("replay_set_hash"),
            "policy_assessment": assessment,
            "wiki_promotion_performed": False,
        },
        occurred_at=reviewed_at,
    )
    session.flush()
    return LessonTransitionResult(lesson=lesson, event=event, idempotent=False)


def lesson_revalidation_due_reasons(
    lesson: LessonProposal,
    *,
    as_of: datetime,
) -> list[str]:
    """Return stable due reasons for monthly, +20-date, and 60-session checks."""

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if lesson.status not in {"active", "challenged"}:
        return []
    metrics = lesson.replay_metrics or {}
    last_raw = metrics.get("last_revalidated_at") or metrics.get("activated_at")
    if not isinstance(last_raw, str):
        raise ValueError("active lesson is missing its last validation timestamp")
    last_at = datetime.fromisoformat(last_raw)
    if last_at.tzinfo is None or last_at.utcoffset() is None:
        raise ValueError("lesson validation timestamp must be timezone-aware")
    last_count = int(
        metrics.get(
            "last_revalidated_target_dates",
            metrics.get("activated_replay_target_dates", 0),
        )
    )
    added = lesson.replay_target_dates - last_count
    reasons: list[str] = []
    if as_of >= _add_calendar_month(last_at):
        reasons.append("monthly")
    if added >= LESSON_REVALIDATION_EPISODES:
        reasons.append("new_20_target_dates")
    if added >= lesson.half_life_sessions:
        reasons.append("half_life_60_sessions")
    return reasons


def due_lesson_reviews(
    session: Session,
    *,
    as_of: datetime,
) -> list[DueLessonReview]:
    """List active/challenged lessons requiring a deterministic review."""

    rows = session.scalars(
        select(LessonProposal)
        .where(LessonProposal.status.in_(("active", "challenged")))
        .order_by(LessonProposal.created_at, LessonProposal.id)
    ).all()
    due: list[DueLessonReview] = []
    for row in rows:
        reasons = lesson_revalidation_due_reasons(row, as_of=as_of)
        if reasons:
            due.append(
                DueLessonReview(
                    lesson_id=row.id,
                    status=row.status,
                    reasons=tuple(reasons),
                    latest_replay_hash=(row.replay_metrics or {}).get(
                        "replay_set_hash"
                    ),
                )
            )
    return due


def verify_lesson_audit(
    session: Session,
    *,
    lesson_id: str,
) -> LessonAuditReport:
    """Recompute the append-only chain and reject any stored tampering."""

    lesson = session.get(LessonProposal, lesson_id)
    if lesson is None:
        raise ValueError("lesson proposal was not found")
    batches = list(
        session.scalars(
            select(LessonReplayBatch)
            .where(LessonReplayBatch.lesson_proposal_id == lesson.id)
            .order_by(LessonReplayBatch.created_at, LessonReplayBatch.id)
        ).all()
    )
    events = list(
        session.scalars(
            select(LessonLifecycleEvent)
            .where(LessonLifecycleEvent.lesson_proposal_id == lesson.id)
            .order_by(LessonLifecycleEvent.sequence_number)
        ).all()
    )

    expected_manifest: dict[str, Any] | None = None
    aggregate_observations: list[dict[str, Any]] = []
    observation_identities: set[tuple[str, str, str]] = set()
    replay_event_keys: set[str] = set()
    for batch in batches:
        if batch.manifest.get("lesson_id") != lesson.id:
            raise ValueError("replay manifest lesson identity failed audit")
        if expected_manifest is None:
            expected_manifest = batch.manifest
        elif batch.manifest != expected_manifest:
            raise ValueError("replay manifests differ inside one Lesson")
        if batch.observation_count != len(batch.observations):
            raise ValueError("replay batch observation count failed audit")
        batch_dates = {
            str(item["target_date"]) for item in batch.observations
        }
        if batch.distinct_target_dates != len(batch_dates):
            raise ValueError("replay batch target-date count failed audit")
        normalized = {
            **batch.manifest,
            "observations": sorted(
                batch.observations,
                key=_observation_identity,
            ),
        }
        if _canonical_hash(normalized) != batch.content_hash:
            raise ValueError("replay batch content hash failed audit")
        replay_bundle = LessonReplayBundle.model_validate(normalized)
        diagnostics = [
            _validate_observation_against_frozen_outcome(
                session,
                observation.model_dump(mode="json"),
            )
            for observation in replay_bundle.observations
        ]
        _validate_bundle_artifacts(replay_bundle, diagnostics)
        batch_identities = {
            _observation_identity(item) for item in batch.observations
        }
        if len(batch_identities) != len(batch.observations):
            raise ValueError("duplicate observations exist inside a replay batch")
        if observation_identities & batch_identities:
            raise ValueError("replay observation identity was counted more than once")
        observation_identities.update(batch_identities)
        aggregate_observations.extend(batch.observations)
        aggregate_observations.sort(key=_observation_identity)
        expected_metrics = compute_replay_metrics(aggregate_observations)
        if batch.aggregate_metrics != expected_metrics:
            raise ValueError("replay aggregate metrics failed deterministic audit")
        replay_event_keys.add(f"replay:{batch.content_hash}")

    expected_status = "candidate"
    seen_replay_event_keys: set[str] = set()
    for expected_sequence, event in enumerate(events, start=1):
        if event.sequence_number != expected_sequence:
            raise ValueError("Lesson lifecycle event sequence is not contiguous")
        if (
            event.occurred_at.replace(tzinfo=None)
            != datetime.fromisoformat(event.occurred_at_canonical).replace(
                tzinfo=None
            )
        ):
            raise ValueError("Lesson lifecycle event timestamp failed audit")
        if _canonical_hash(_event_envelope(event)) != event.payload_hash:
            raise ValueError("Lesson lifecycle event envelope hash failed audit")
        if event.from_status != expected_status:
            raise ValueError("Lesson lifecycle status chain failed audit")
        _validate_event_transition(event)
        expected_status = event.to_status
        if event.event_type == "replay_recorded":
            seen_replay_event_keys.add(event.event_key)
            if event.payload.get("batch_content_hash") != event.event_key.removeprefix(
                "replay:"
            ):
                raise ValueError("replay lifecycle event does not match its batch hash")
    if seen_replay_event_keys != replay_event_keys:
        raise ValueError("replay batches and lifecycle events are not one-to-one")
    if lesson.status != expected_status:
        raise ValueError("Lesson status projection failed lifecycle audit")

    if batches:
        latest_metrics = batches[-1].aggregate_metrics
        for key, value in latest_metrics.items():
            if (lesson.replay_metrics or {}).get(key) != value:
                raise ValueError("Lesson replay metric projection failed audit")
        if lesson.replay_target_dates != latest_metrics["distinct_target_dates"]:
            raise ValueError("Lesson replay target-date projection failed audit")
    elif (
        lesson.status != "candidate"
        and (lesson.replay_metrics or {}).get("immediate_extreme_checklist") is not True
    ):
        raise ValueError("non-checklist activated Lesson has no replay batch")

    episode = session.scalar(
        select(LessonEpisode).where(
            LessonEpisode.cluster_key == lesson.cluster_key,
            LessonEpisode.episode_key == lesson.episode_key,
        )
    )
    if episode is None:
        raise ValueError("Lesson independent episode record is missing")
    if episode.evidence_set_hash != episode.first_reflection_run.evaluation_set_hash:
        raise ValueError("Lesson episode evidence-set identity failed audit")

    active_head_count = int(
        session.scalar(
            select(func.count())
            .select_from(LessonProposal)
            .where(
                LessonProposal.cluster_key == lesson.cluster_key,
                LessonProposal.status.in_(("active", "challenged")),
            )
        )
        or 0
    )
    if active_head_count > 1:
        raise ValueError("Lesson recurrence cluster has multiple active heads")
    _verify_lineage(session, lesson)

    audit_root_hash = _canonical_hash(
        {
            "lesson_id": lesson.id,
            "status": lesson.status,
            "replay_batch_hashes": [batch.content_hash for batch in batches],
            "event_envelope_hashes": [event.payload_hash for event in events],
            "replay_metrics": lesson.replay_metrics or {},
            "supersedes_id": lesson.supersedes_id,
        }
    )
    return LessonAuditReport(
        lesson_id=lesson.id,
        status=lesson.status,
        replay_batch_count=len(batches),
        lifecycle_event_count=len(events),
        latest_replay_hash=batches[-1].content_hash if batches else None,
        audit_root_hash=audit_root_hash,
    )


def compute_replay_metrics(
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute frozen three-class Brier, classwise ECE, and subgroup deltas."""

    if not observations:
        raise ValueError("at least one replay observation is required")
    normalized = sorted(observations, key=_observation_identity)
    baseline_briers = [
        _brier(item["baseline_probabilities"], item["actual_label"])
        for item in normalized
    ]
    candidate_briers = [
        _brier(item["candidate_probabilities"], item["actual_label"])
        for item in normalized
    ]
    baseline_brier = _mean(baseline_briers)
    candidate_brier = _mean(candidate_briers)
    baseline_ece = _classwise_ece(normalized, probability_key="baseline_probabilities")
    candidate_ece = _classwise_ece(
        normalized,
        probability_key="candidate_probabilities",
    )

    subgroup_indexes: dict[str, list[int]] = {}
    for index, observation in enumerate(normalized):
        groups = {
            f"index:{observation['index_code']}",
            f"horizon:{observation['horizon']}",
            *(
                f"tag:{tag}"
                for tag in observation.get("important_subgroups", [])
            ),
        }
        for group in groups:
            subgroup_indexes.setdefault(group, []).append(index)
    subgroup_metrics: dict[str, dict[str, Any]] = {}
    for group, indexes in sorted(subgroup_indexes.items()):
        group_baseline = _mean([baseline_briers[index] for index in indexes])
        group_candidate = _mean([candidate_briers[index] for index in indexes])
        subgroup_metrics[group] = {
            "observation_count": len(indexes),
            "baseline_average_brier": group_baseline,
            "candidate_average_brier": group_candidate,
            "average_brier_improvement": _rounded(
                group_baseline - group_candidate
            ),
            "non_degrading": group_candidate <= group_baseline + 1e-12,
        }
    target_dates = sorted({item["target_date"] for item in normalized})
    return {
        "metric_version": "three-class-brier-classwise-ece-v1",
        "observation_count": len(normalized),
        "distinct_target_dates": len(target_dates),
        "target_dates_hash": _canonical_hash(target_dates),
        "replay_set_hash": _canonical_hash(normalized),
        "baseline_average_brier": baseline_brier,
        "candidate_average_brier": candidate_brier,
        "average_brier_improvement": _rounded(
            baseline_brier - candidate_brier
        ),
        "baseline_calibration_ece": baseline_ece,
        "candidate_calibration_ece": candidate_ece,
        "calibration_improvement": _rounded(baseline_ece - candidate_ece),
        "important_subgroups_non_degrading": all(
            item["non_degrading"] for item in subgroup_metrics.values()
        ),
        "important_subgroups": subgroup_metrics,
        "first_target_date": target_dates[0],
        "last_target_date": target_dates[-1],
    }


def build_replay_manifest_hashes(
    *,
    lesson_id: str,
    candidate_rule_version: str,
    wiki_version: str,
    threshold_policy_version: str,
    candidate_transform: CandidateTransform,
) -> dict[str, str]:
    """Build the three deterministic artifact hashes required by replay input."""

    baseline_artifact_hash = _canonical_hash(
        {
            "baseline_rule_version": "forecast-probabilities-v1",
            "source": (
                "completed-live Forecast.probability_up/"
                "probability_neutral/probability_down"
            ),
        }
    )
    candidate_artifact_hash = _canonical_hash(
        {
            "candidate_rule_version": candidate_rule_version,
            "candidate_transform": candidate_transform.model_dump(mode="json"),
        }
    )
    replay_input_manifest_hash = _canonical_hash(
        {
            "protocol_version": LESSON_REPLAY_PROTOCOL_VERSION,
            "lesson_id": lesson_id,
            "baseline_rule_version": "forecast-probabilities-v1",
            "candidate_rule_version": candidate_rule_version,
            "wiki_version": wiki_version,
            "threshold_policy_version": threshold_policy_version,
            "replay_generator": "deterministic_rule_engine",
            "baseline_artifact_hash": baseline_artifact_hash,
            "candidate_artifact_hash": candidate_artifact_hash,
        }
    )
    return {
        "replay_input_manifest_hash": replay_input_manifest_hash,
        "baseline_artifact_hash": baseline_artifact_hash,
        "candidate_artifact_hash": candidate_artifact_hash,
    }


def _validate_observation_against_frozen_outcome(
    session: Session,
    observation: dict[str, Any],
) -> ForecastDiagnostic:
    diagnostic = session.get(
        ForecastDiagnostic,
        observation["forecast_diagnostic_id"],
    )
    if diagnostic is None:
        raise ValueError("replay forecast diagnostic was not found")
    batch = diagnostic.batch
    forecast = diagnostic.forecast
    evaluation = diagnostic.evaluation
    if evaluation.forecast_id != forecast.id:
        raise ValueError("replay diagnostic evaluation does not belong to its forecast")
    if batch.status != "completed":
        raise ValueError("replay diagnostic must belong to a completed evaluation batch")
    if forecast.run.mode != "live" or forecast.run.status != "completed":
        raise ValueError("replay diagnostic must come from a completed Live forecast")
    identity_pairs = (
        (forecast.id, observation["forecast_id"], "forecast_id"),
        (forecast.index_code, observation["index_code"], "index_code"),
        (forecast.horizon, observation["horizon"], "horizon"),
        (
            forecast.target_date.isoformat(),
            str(observation["target_date"]),
            "target_date",
        ),
        (batch.horizon, observation["horizon"], "batch horizon"),
        (
            batch.target_date.isoformat(),
            str(observation["target_date"]),
            "batch target_date",
        ),
        (evaluation.actual_label, observation["actual_label"], "actual_label"),
        (
            evaluation.observation_hash,
            observation["outcome_snapshot_hash"],
            "outcome_snapshot_hash",
        ),
    )
    for actual, claimed, label in identity_pairs:
        if actual != claimed:
            raise ValueError(f"replay {label} conflicts with frozen database outcome")
    snapshot = session.scalar(
        select(MarketSessionSnapshot).where(
            MarketSessionSnapshot.batch_id == batch.id,
            MarketSessionSnapshot.index_code == forecast.index_code,
        )
    )
    if snapshot is None:
        raise ValueError("replay market session snapshot was not found")
    if snapshot.target_date != forecast.target_date:
        raise ValueError("replay market snapshot target date is inconsistent")
    if not math.isclose(
        snapshot.actual_return,
        evaluation.actual_return,
        rel_tol=0,
        abs_tol=1e-12,
    ):
        raise ValueError("replay market snapshot return conflicts with evaluation")
    if snapshot.content_hash != observation["market_snapshot_hash"]:
        raise ValueError("replay market_snapshot_hash conflicts with frozen database outcome")
    baseline = observation["baseline_probabilities"]
    persisted_baseline = {
        "up": forecast.probability_up,
        "neutral": forecast.probability_neutral,
        "down": forecast.probability_down,
    }
    if any(
        not math.isclose(
            float(baseline[label]),
            float(persisted_baseline[label]),
            rel_tol=0,
            abs_tol=1e-12,
        )
        for label in _LABELS
    ):
        raise ValueError("replay baseline probabilities conflict with frozen forecast")
    return diagnostic


def _validate_bundle_artifacts(
    bundle: LessonReplayBundle,
    diagnostics: list[ForecastDiagnostic],
) -> None:
    expected_hashes = build_replay_manifest_hashes(
        lesson_id=bundle.lesson_id,
        candidate_rule_version=bundle.candidate_rule_version,
        wiki_version=bundle.wiki_version,
        threshold_policy_version=bundle.threshold_policy_version,
        candidate_transform=bundle.candidate_transform,
    )
    for field_name, expected in expected_hashes.items():
        if getattr(bundle, field_name) != expected:
            raise ValueError(f"replay {field_name} does not match its frozen manifest")
    for observation, diagnostic in zip(bundle.observations, diagnostics, strict=True):
        if diagnostic.policy_version != bundle.threshold_policy_version:
            raise ValueError(
                "replay threshold policy version conflicts with ForecastDiagnostic"
            )
        if diagnostic.forecast.wiki_version != bundle.wiki_version:
            raise ValueError("replay Wiki version conflicts with frozen forecast")
        expected_candidate = _apply_candidate_transform(
            observation.baseline_probabilities,
            bundle.candidate_transform,
        )
        claimed_candidate = observation.candidate_probabilities.model_dump()
        if any(
            not math.isclose(
                expected_candidate[label],
                claimed_candidate[label],
                rel_tol=0,
                abs_tol=1e-12,
            )
            for label in _LABELS
        ):
            raise ValueError(
                "replay candidate probabilities are not the registered "
                "deterministic transform"
            )


def _apply_candidate_transform(
    baseline: ReplayProbabilities,
    transform: CandidateTransform,
) -> dict[str, float]:
    probabilities = baseline.model_dump()
    biases = transform.class_logit_bias.model_dump()
    logits = {
        label: math.log(max(float(probabilities[label]), 1e-15))
        / transform.temperature
        + float(biases[label])
        for label in _LABELS
    }
    maximum = max(logits.values())
    exponentials = {
        label: math.exp(logits[label] - maximum) for label in _LABELS
    }
    total = sum(exponentials.values())
    return {
        label: exponentials[label] / total for label in _LABELS
    }


def _assess_current_policy(
    session: Session,
    *,
    lesson: LessonProposal,
    independent_episode_count: int,
    aggregate_metrics: dict[str, Any],
    required_shadow_target_dates: int,
) -> dict[str, Any]:
    immediate = (lesson.replay_metrics or {}).get("immediate_extreme_checklist") is True
    assessment = assess_lesson_policy(
        proposal_type=lesson.proposal_type,
        overall_severity="extreme" if immediate else "directional",
        independent_episode_count=independent_episode_count,
        replay_target_dates=int(aggregate_metrics.get("distinct_target_dates", 0)),
        average_brier_improvement=aggregate_metrics.get(
            "average_brier_improvement"
        ),
        calibration_improvement=aggregate_metrics.get(
            "calibration_improvement"
        ),
        important_subgroups_non_degrading=aggregate_metrics.get(
            "important_subgroups_non_degrading"
        ),
        completed_shadow_target_dates=completed_live_target_date_count(session),
        required_shadow_target_dates=required_shadow_target_dates,
    )
    return assessment.as_dict()


def _validate_event_transition(event: LessonLifecycleEvent) -> None:
    allowed = {
        "replay_recorded": {
            ("candidate", "candidate"),
            ("active", "active"),
            ("challenged", "challenged"),
        },
        "approved": {("candidate", "active")},
        "revalidated": {
            ("active", "active"),
            ("challenged", "active"),
        },
        "challenged": {("active", "challenged")},
        "retired": {("challenged", "retired")},
        "superseded": {
            ("active", "superseded"),
            ("challenged", "superseded"),
        },
    }
    if (event.from_status, event.to_status) not in allowed[event.event_type]:
        raise ValueError("illegal Lesson lifecycle event transition")


def _verify_lineage(session: Session, lesson: LessonProposal) -> None:
    metrics = lesson.replay_metrics or {}
    if lesson.supersedes_id is not None:
        predecessor = session.get(LessonProposal, lesson.supersedes_id)
        if predecessor is None or predecessor.status != "superseded":
            raise ValueError("Lesson predecessor lineage failed audit")
        if (predecessor.replay_metrics or {}).get("superseded_by_id") != lesson.id:
            raise ValueError("Lesson predecessor backlink failed audit")
    if lesson.status == "superseded":
        successor_id = metrics.get("superseded_by_id")
        successor = (
            session.get(LessonProposal, successor_id)
            if isinstance(successor_id, str)
            else None
        )
        if successor is None or successor.supersedes_id != lesson.id:
            raise ValueError("Lesson successor lineage failed audit")


def _validate_activation_eligibility(
    session: Session,
    lesson: LessonProposal,
) -> None:
    metrics = lesson.replay_metrics or {}
    if metrics.get("automatic_promotion_allowed") is not False:
        raise ValueError("lesson policy must explicitly prohibit automatic promotion")
    if metrics.get("wiki_review_ready") is not True:
        blockers = metrics.get("blockers", [])
        raise ValueError(
            "lesson is not eligible for human activation"
            + (f": {', '.join(blockers)}" if blockers else "")
        )
    immediate = metrics.get("immediate_extreme_checklist") is True
    if immediate:
        if lesson.proposal_type not in IMMEDIATE_EXTREME_TYPES:
            raise ValueError("an extreme singleton may only be a checklist proposal")
        return
    replay_batch_count = int(
        session.scalar(
            select(func.count())
            .select_from(LessonReplayBatch)
            .where(LessonReplayBatch.lesson_proposal_id == lesson.id)
        )
        or 0
    )
    if replay_batch_count == 0:
        raise ValueError("ordinary lesson requires its own frozen replay batch")
    if lesson.independent_episode_count < MIN_INDEPENDENT_EPISODES:
        raise ValueError("ordinary lesson needs at least 5 independent episodes")
    if lesson.replay_target_dates < MIN_REPLAY_TARGET_DATES:
        raise ValueError("ordinary lesson needs at least 20 replay target dates")
    if metrics.get("average_brier_improvement", 0) <= 0:
        raise ValueError("ordinary lesson must improve average Brier")
    if metrics.get("calibration_improvement", 0) <= 0:
        raise ValueError("ordinary lesson must improve calibration")
    if metrics.get("important_subgroups_non_degrading") is not True:
        raise ValueError("ordinary lesson regresses an important subgroup")


def _normalized_bundle(bundle: LessonReplayBundle) -> dict[str, Any]:
    observations = [
        item.model_dump(mode="json")
        for item in sorted(bundle.observations, key=lambda item: item.identity)
    ]
    return {
        "protocol_version": bundle.protocol_version,
        "lesson_id": bundle.lesson_id,
        "baseline_rule_version": bundle.baseline_rule_version,
        "candidate_rule_version": bundle.candidate_rule_version,
        "wiki_version": bundle.wiki_version,
        "threshold_policy_version": bundle.threshold_policy_version,
        "replay_generator": bundle.replay_generator,
        "candidate_transform": bundle.candidate_transform.model_dump(mode="json"),
        "replay_input_manifest_hash": bundle.replay_input_manifest_hash,
        "baseline_artifact_hash": bundle.baseline_artifact_hash,
        "candidate_artifact_hash": bundle.candidate_artifact_hash,
        "observations": observations,
    }


def _observation_identity(
    observation: dict[str, Any],
) -> tuple[str, str, str]:
    target_date = observation["target_date"]
    if isinstance(target_date, date):
        target_date = target_date.isoformat()
    return (str(target_date), observation["index_code"], observation["horizon"])


def _brier(probabilities: dict[str, float], actual_label: str) -> float:
    return sum(
        (float(probabilities[label]) - float(label == actual_label)) ** 2
        for label in _LABELS
    ) / len(_LABELS)


def _classwise_ece(
    observations: list[dict[str, Any]],
    *,
    probability_key: str,
) -> float:
    total = len(observations)
    class_scores: list[float] = []
    for label in _LABELS:
        bins: list[list[tuple[float, float]]] = [
            [] for _ in range(CALIBRATION_BIN_COUNT)
        ]
        for observation in observations:
            probability = float(observation[probability_key][label])
            bin_index = min(int(probability * CALIBRATION_BIN_COUNT), 9)
            bins[bin_index].append(
                (probability, float(observation["actual_label"] == label))
            )
        score = 0.0
        for entries in bins:
            if not entries:
                continue
            average_probability = _mean([item[0] for item in entries])
            observed_frequency = _mean([item[1] for item in entries])
            score += len(entries) / total * abs(
                average_probability - observed_frequency
            )
        class_scores.append(score)
    return _mean(class_scores)


def _mean(values: list[float]) -> float:
    return _rounded(sum(values) / len(values))


def _rounded(value: float) -> float:
    return round(float(value), 12)


def _append_event(
    session: Session,
    *,
    lesson: LessonProposal,
    event_type: str,
    event_key: str,
    from_status: str,
    to_status: str,
    actor: str,
    reason: str,
    payload: dict[str, Any],
    occurred_at: datetime,
) -> LessonLifecycleEvent:
    if len(event_key) > 160:
        raise ValueError("lesson lifecycle event key is too long")
    existing = _event_by_key(session, lesson.id, event_key)
    if existing is not None:
        return existing
    sequence_number = int(
        session.scalar(
            select(func.max(LessonLifecycleEvent.sequence_number)).where(
                LessonLifecycleEvent.lesson_proposal_id == lesson.id
            )
        )
        or 0
    ) + 1
    event_id = str(uuid4())
    occurred_at_canonical = occurred_at.isoformat()
    envelope = {
        "id": event_id,
        "lesson_proposal_id": lesson.id,
        "sequence_number": sequence_number,
        "event_type": event_type,
        "event_key": event_key,
        "from_status": from_status,
        "to_status": to_status,
        "actor": actor.strip(),
        "reason": reason,
        "payload": payload,
        "occurred_at": occurred_at_canonical,
    }
    event = LessonLifecycleEvent(
        id=event_id,
        lesson_proposal_id=lesson.id,
        sequence_number=sequence_number,
        event_type=event_type,
        event_key=event_key,
        from_status=from_status,
        to_status=to_status,
        actor=actor.strip(),
        reason=reason,
        payload=payload,
        payload_hash=_canonical_hash(envelope),
        occurred_at=occurred_at,
        occurred_at_canonical=occurred_at_canonical,
    )
    session.add(event)
    session.flush()
    return event


def _event_envelope(event: LessonLifecycleEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "lesson_proposal_id": event.lesson_proposal_id,
        "sequence_number": event.sequence_number,
        "event_type": event.event_type,
        "event_key": event.event_key,
        "from_status": event.from_status,
        "to_status": event.to_status,
        "actor": event.actor,
        "reason": event.reason,
        "payload": event.payload,
        "occurred_at": event.occurred_at_canonical,
    }


def _event_by_key(
    session: Session,
    lesson_id: str,
    event_key: str,
) -> LessonLifecycleEvent | None:
    return session.scalar(
        select(LessonLifecycleEvent).where(
            LessonLifecycleEvent.lesson_proposal_id == lesson_id,
            LessonLifecycleEvent.event_key == event_key,
        )
    )


def _validate_actor_and_time(actor: str, occurred_at: datetime) -> None:
    actor = actor.strip()
    if not actor or len(actor) > 120:
        raise ValueError("operator identity must contain 1-120 characters")
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise ValueError("lifecycle timestamps must be timezone-aware")


def _validate_reason(reason: str) -> None:
    if not reason.strip() or len(reason) > 20_000:
        raise ValueError("review notes must contain 1-20000 characters")


def _add_calendar_month(value: datetime) -> datetime:
    month_index = value.month
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _canonical_hash(payload: Any) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
