"""Deterministic, audit-only evidence for future believability policies.

The v1 snapshot deliberately does not calculate or apply a dynamic weight.  It
freezes the immutable evidence that a later, separately governed policy may
use.  This keeps the first shadow period observational and prevents an LLM from
grading its own reasoning or silently changing the committee graph.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import Field
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..models import (
    AgentOpinion,
    EvaluationBatch,
    EvaluationResult,
    OpinionEvaluation,
    ReflectionFinding,
    ReflectionHumanReview,
    ReflectionRun,
    WorkflowRun,
)
from ..schemas import APIModel
from .reflection_governance import reflection_review_gate_state

BELIEVABILITY_SCHEMA_ID = "vericouncil.believability-shadow/v1"
BELIEVABILITY_POLICY_VERSION = "1.0.0-shadow"


@dataclass(frozen=True, slots=True)
class BelievabilityAgentScope:
    """The exact current identity whose historical evidence may be considered."""

    agent_id: str
    agent_version: str
    model_name: str
    role_domain: Literal["research", "strategy"]
    stage: Literal["research_to_strategy", "strategy_to_cio"]
    current_stage_weight_metadata: float


class BelievabilityGate(APIModel):
    completed_live_evaluation_target_dates: int = Field(ge=0)
    completed_live_reflection_target_dates: int = Field(ge=0)
    approved_initial_reflection_prefix: int = Field(ge=0)
    required_live_target_dates: int = Field(ge=20)
    required_approved_reflections: int = Field(ge=10)
    reflection_gate_evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    blockers: list[str] = Field(default_factory=list)


class BelievabilityDomainEvidence(APIModel):
    """Two evidence axes for one exact Agent/model/index/horizon scope."""

    agent_id: str
    agent_version: str
    model_name: str
    role_domain: Literal["research", "strategy"]
    stage: Literal["research_to_strategy", "strategy_to_cio"]
    index_code: str
    horizon: Literal["D1", "D2"]

    evaluation_sample_size: int = Field(ge=0)
    independent_evaluation_target_dates: int = Field(ge=0)
    average_brier: float | None = Field(default=None, ge=0)
    sign_sample_size: int = Field(ge=0)
    sign_accuracy: float | None = Field(default=None, ge=0, le=1)
    material_sample_size: int = Field(ge=0)
    material_accuracy: float | None = Field(default=None, ge=0, le=1)
    performance_evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    approved_reflection_count: int = Field(ge=0)
    independent_reasoning_target_dates: int = Field(ge=0)
    human_reviewed_right_reason_proxy_count: int = Field(ge=0)
    right_reason_verified_count: int = Field(ge=0)
    right_reason_supported_count: int = Field(ge=0)
    lucky_correct_count: int = Field(ge=0)
    wrong_count: int = Field(ge=0)
    reasoning_or_weighting_failure_count: int = Field(ge=0)
    unresolved_count: int = Field(ge=0)
    reasoning_proxy_evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    evidence_status: Literal[
        "demo_excluded",
        "insufficient_history",
        "insufficient_reasoning_proxy",
        "shadow_observation",
        "governance_review_required",
    ]
    current_stage_weight_metadata: float = Field(ge=0)
    proposed_stage_multiplier: None = None
    applied_stage_multiplier: float = Field(default=1.0, ge=1.0, le=1.0)


class BelievabilitySnapshot(APIModel):
    """A canonical run-time seal; it is evidence, never an activation event."""

    schema_id: Literal["vericouncil.believability-shadow/v1"] = (
        BELIEVABILITY_SCHEMA_ID
    )
    policy_version: Literal["1.0.0-shadow"] = BELIEVABILITY_POLICY_VERSION
    mode: Literal["demo", "live"]
    as_of: datetime
    data_cutoff: datetime
    phase: Literal["demo_excluded", "shadow", "governance_review_required"]
    applied_to_decision: Literal[False] = False
    activation_supported: Literal[False] = False
    reasoning_axis: Literal["human_reviewed_right_reason_proxy"] = (
        "human_reviewed_right_reason_proxy"
    )
    gate: BelievabilityGate
    profiles: list[BelievabilityDomainEvidence]
    limitations: list[str]
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def build_believability_snapshot(
    session: Session,
    *,
    mode: Literal["demo", "live"],
    as_of: datetime,
    data_cutoff: datetime,
    agent_scopes: tuple[BelievabilityAgentScope, ...],
    index_codes: tuple[str, ...],
    horizons: tuple[str, ...],
    market_universe_hash: str,
    required_live_target_dates: int,
    required_approved_reflections: int,
) -> BelievabilitySnapshot:
    """Freeze historical evidence available strictly by ``data_cutoff``.

    Demo creates the same schema with empty formal evidence.  Live data is
    partitioned by exact Agent version and model identity; older identities are
    never inherited by a new deployment.
    """

    _require_aware(as_of, "as_of")
    _require_aware(data_cutoff, "data_cutoff")
    if data_cutoff > as_of:
        raise ValueError("believability data_cutoff must not be later than as_of")
    if required_live_target_dates < 20:
        raise ValueError("believability shadow period cannot be shorter than 20 dates")
    if required_approved_reflections < 10:
        raise ValueError("believability review gate cannot be lower than 10 reflections")
    if len({scope.agent_id for scope in agent_scopes}) != len(agent_scopes):
        raise ValueError("believability agent scopes must have unique agent IDs")

    performance_by_scope: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    reasoning_by_scope: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    evaluation_dates: set[str] = set()
    reflection_dates: set[str] = set()
    approved_reflection_ids: set[str] = set()
    reflection_lineage_conflict_ids: tuple[str, ...] = ()
    reflection_gate_evidence_hash = _canonical_hash([])

    if mode == "live":
        performance_by_scope, evaluation_dates = _performance_evidence(
            session,
            data_cutoff=data_cutoff,
            agent_scopes=agent_scopes,
            index_codes=index_codes,
            horizons=horizons,
            market_universe_hash=market_universe_hash,
        )
        (
            reasoning_by_scope,
            reflection_dates,
            approved_reflection_ids,
            reflection_gate_evidence_hash,
            reflection_lineage_conflict_ids,
        ) = _reasoning_proxy_evidence(
            session,
            data_cutoff=data_cutoff,
            agent_scopes=agent_scopes,
            index_codes=index_codes,
            horizons=horizons,
            market_universe_hash=market_universe_hash,
        )

    blockers: list[str] = []
    if len(reflection_dates) < required_live_target_dates:
        blockers.append("live_reflection_target_dates_below_minimum")
    if len(approved_reflection_ids) < required_approved_reflections:
        blockers.append("approved_initial_reflection_prefix_below_minimum")
    if reflection_lineage_conflict_ids:
        blockers.append("reflection_lineage_conflict")
    exact_keys = [
        (scope.agent_id, index_code, horizon)
        for scope in agent_scopes
        for index_code in index_codes
        for horizon in horizons
    ]
    if any(
        len({item["target_date"] for item in performance_by_scope.get(key, [])})
        < required_live_target_dates
        for key in exact_keys
    ):
        blockers.append("exact_identity_history_below_minimum")
    if any(
        len({item["target_date"] for item in reasoning_by_scope.get(key, [])})
        < required_approved_reflections
        for key in exact_keys
    ):
        blockers.append("exact_identity_reasoning_proxy_below_minimum")
    phase: Literal["demo_excluded", "shadow", "governance_review_required"]
    if mode == "demo":
        phase = "demo_excluded"
        blockers = ["demo_never_enters_formal_believability"]
    elif blockers:
        phase = "shadow"
    else:
        # Reaching the observation gates authorizes only a separate policy
        # review.  This schema has no activation path by design.
        phase = "governance_review_required"

    profiles: list[BelievabilityDomainEvidence] = []
    for scope in sorted(agent_scopes, key=lambda item: item.agent_id):
        for index_code in sorted(index_codes):
            for horizon in sorted(horizons):
                key = (scope.agent_id, index_code, horizon)
                performance = performance_by_scope.get(key, [])
                reasoning = reasoning_by_scope.get(key, [])
                profiles.append(
                    _build_profile(
                        mode=mode,
                        phase=phase,
                        scope=scope,
                        index_code=index_code,
                        horizon=horizon,
                        performance=performance,
                        reasoning=reasoning,
                        required_live_target_dates=required_live_target_dates,
                        required_approved_reflections=(
                            required_approved_reflections
                        ),
                    )
                )

    unsigned = {
        "schema_id": BELIEVABILITY_SCHEMA_ID,
        "policy_version": BELIEVABILITY_POLICY_VERSION,
        "mode": mode,
        "as_of": as_of.isoformat(),
        "data_cutoff": data_cutoff.isoformat(),
        "phase": phase,
        "applied_to_decision": False,
        "activation_supported": False,
        "reasoning_axis": "human_reviewed_right_reason_proxy",
        "gate": {
            "completed_live_evaluation_target_dates": len(evaluation_dates),
            "completed_live_reflection_target_dates": len(reflection_dates),
            "approved_initial_reflection_prefix": len(approved_reflection_ids),
            "required_live_target_dates": required_live_target_dates,
            "required_approved_reflections": required_approved_reflections,
            "reflection_gate_evidence_hash": reflection_gate_evidence_hash,
            "blockers": blockers,
        },
        "profiles": [item.model_dump(mode="json") for item in profiles],
        "limitations": [
            "no_dynamic_weight_is_computed_or_applied",
            "approved_reflection_findings_are_an_outcome_attribution_proxy_not_a_full_reasoning_rubric",
            "codex_file_model_name_is_a_handoff_identity_not_verified_base_model_attestation",
            "human_reviewed_at_is_a_declared_record_time_not_an_external_trusted_timestamp",
            "risk_critic_and_unavailable_quant_are_outside_directional_believability",
            "activation_requires_a_separate_versioned_replay_and_human_policy_event",
        ],
    }
    return BelievabilitySnapshot.model_validate(
        {
            **unsigned,
            "content_hash": _canonical_hash(unsigned),
        }
    )


def validate_believability_snapshot(
    value: BelievabilitySnapshot | dict[str, Any],
) -> BelievabilitySnapshot:
    """Validate schema and recompute the canonical content seal."""

    snapshot = (
        value
        if isinstance(value, BelievabilitySnapshot)
        else BelievabilitySnapshot.model_validate(value)
    )
    unsigned = snapshot.model_dump(mode="json", exclude={"content_hash"})
    computed = _canonical_hash(unsigned)
    if not hmac.compare_digest(computed, snapshot.content_hash):
        raise ValueError("believability snapshot content hash is invalid")
    return snapshot


def believability_run_binding_hash(run_id: str, snapshot_hash: str) -> str:
    """Bind reusable historical evidence to one immutable run identity."""

    return hashlib.sha256(
        f"vericouncil.believability-run-binding/v1:{run_id}:{snapshot_hash}".encode()
    ).hexdigest()


def _performance_evidence(
    session: Session,
    *,
    data_cutoff: datetime,
    agent_scopes: tuple[BelievabilityAgentScope, ...],
    index_codes: tuple[str, ...],
    horizons: tuple[str, ...],
    market_universe_hash: str,
) -> tuple[dict[tuple[str, str, str], list[dict[str, Any]]], set[str]]:
    scope_by_agent = {scope.agent_id: scope for scope in agent_scopes}
    if not scope_by_agent:
        return {}, set()
    statement = (
        select(
            AgentOpinion.id.label("opinion_id"),
            AgentOpinion.agent_id,
            AgentOpinion.agent_version,
            AgentOpinion.model_name,
            AgentOpinion.index_code,
            AgentOpinion.horizon,
            AgentOpinion.target_date,
            OpinionEvaluation.id.label("opinion_evaluation_id"),
            OpinionEvaluation.sign_correct,
            OpinionEvaluation.material_direction_correct,
            OpinionEvaluation.brier_score,
            OpinionEvaluation.evaluated_at,
            EvaluationBatch.id.label("batch_id"),
            EvaluationBatch.evaluation_set_hash,
            EvaluationBatch.completed_at.label("batch_completed_at"),
            EvaluationResult.observation_hash,
            EvaluationResult.evaluated_at.label("result_evaluated_at"),
            WorkflowRun.as_of.label("run_as_of"),
            WorkflowRun.completed_at.label("run_completed_at"),
        )
        .join(
            OpinionEvaluation,
            OpinionEvaluation.agent_opinion_id == AgentOpinion.id,
        )
        .join(EvaluationBatch, EvaluationBatch.id == OpinionEvaluation.batch_id)
        .join(
            EvaluationResult,
            EvaluationResult.id == OpinionEvaluation.evaluation_result_id,
        )
        .join(WorkflowRun, WorkflowRun.id == AgentOpinion.run_id)
        .where(
            AgentOpinion.agent_id.in_(tuple(scope_by_agent)),
            AgentOpinion.index_code.in_(index_codes),
            AgentOpinion.horizon.in_(horizons),
            AgentOpinion.direction.in_(("up", "down")),
            AgentOpinion.target_date <= data_cutoff.date(),
            OpinionEvaluation.included_in_direction_score.is_(True),
            EvaluationBatch.status == "completed",
            WorkflowRun.mode == "live",
            WorkflowRun.status == "completed",
            WorkflowRun.market_universe_hash == market_universe_hash,
        )
    )
    latest_by_opinion: dict[str, dict[str, Any]] = {}
    for raw in session.execute(statement).mappings():
        row = dict(raw)
        scope = scope_by_agent.get(str(row["agent_id"]))
        if scope is None:
            continue
        if (
            row["agent_version"] != scope.agent_version
            or row["model_name"] != scope.model_name
            or not _at_or_before(row["evaluated_at"], data_cutoff)
            or not _at_or_before(row["result_evaluated_at"], data_cutoff)
            or not _at_or_before(row["batch_completed_at"], data_cutoff)
            or not _at_or_before(row["run_as_of"], data_cutoff)
            or not _at_or_before(row["run_completed_at"], data_cutoff)
        ):
            continue
        existing = latest_by_opinion.get(str(row["opinion_id"]))
        if existing is None or _performance_priority(
            row, data_cutoff
        ) > _performance_priority(existing, data_cutoff):
            latest_by_opinion[str(row["opinion_id"])] = row

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    dates: set[str] = set()
    for row in latest_by_opinion.values():
        target_date = row["target_date"].isoformat()
        dates.add(target_date)
        key = (
            str(row["agent_id"]),
            str(row["index_code"]),
            str(row["horizon"]),
        )
        grouped.setdefault(key, []).append(
            {
                "opinion_id": str(row["opinion_id"]),
                "opinion_evaluation_id": str(row["opinion_evaluation_id"]),
                "batch_id": str(row["batch_id"]),
                "evaluation_set_hash": str(row["evaluation_set_hash"]),
                "observation_hash": str(row["observation_hash"]),
                "target_date": target_date,
                "brier_score": float(row["brier_score"]),
                "sign_correct": row["sign_correct"],
                "material_direction_correct": row["material_direction_correct"],
                "evaluated_at": _instant(row["evaluated_at"], data_cutoff).isoformat(),
                "result_evaluated_at": _instant(
                    row["result_evaluated_at"], data_cutoff
                ).isoformat(),
                "batch_completed_at": _instant(
                    row["batch_completed_at"], data_cutoff
                ).isoformat(),
                "run_completed_at": _instant(
                    row["run_completed_at"], data_cutoff
                ).isoformat(),
            }
        )
    for rows in grouped.values():
        rows.sort(key=lambda item: (item["target_date"], item["opinion_id"]))
    return grouped, dates


def _reasoning_proxy_evidence(
    session: Session,
    *,
    data_cutoff: datetime,
    agent_scopes: tuple[BelievabilityAgentScope, ...],
    index_codes: tuple[str, ...],
    horizons: tuple[str, ...],
    market_universe_hash: str,
) -> tuple[
    dict[tuple[str, str, str], list[dict[str, Any]]],
    set[str],
    set[str],
    str,
    tuple[str, ...],
]:
    scope_by_agent = {scope.agent_id: scope for scope in agent_scopes}
    if not scope_by_agent:
        return {}, set(), set(), _canonical_hash([]), ()

    gate_state = reflection_review_gate_state(
        session,
        cutoff=data_cutoff,
        market_universe_hash=market_universe_hash,
    )
    reflection_dates = {
        target_date.isoformat()
        for target_date in gate_state.completed_target_dates
    }
    approved_current_ids = set(gate_state.approved_current_ids)
    approved_gate_ids = set(gate_state.approved_prefix_ids)
    if not approved_current_ids:
        return (
            {},
            reflection_dates,
            approved_gate_ids,
            gate_state.evidence_hash,
            gate_state.lineage_conflict_ids,
        )

    finding_statement = (
        select(
            ReflectionFinding.id.label("finding_id"),
            ReflectionFinding.reflection_run_id,
            ReflectionFinding.subject_id.label("agent_id"),
            ReflectionFinding.index_code,
            ReflectionFinding.horizon,
            ReflectionFinding.verdict,
            ReflectionFinding.primary_error_type,
            ReflectionFinding.availability_class,
            ReflectionFinding.causal_status,
            ReflectionFinding.evidence_ids,
            ReflectionFinding.created_at.label("finding_created_at"),
            ReflectionRun.target_date,
            ReflectionRun.output_hash,
            ReflectionHumanReview.notes_hash,
            AgentOpinion.agent_version,
            AgentOpinion.model_name,
        )
        .join(
            ReflectionRun,
            ReflectionRun.id == ReflectionFinding.reflection_run_id,
        )
        .join(
            ReflectionHumanReview,
            ReflectionHumanReview.reflection_run_id == ReflectionRun.id,
        )
        .join(WorkflowRun, WorkflowRun.id == ReflectionRun.source_run_id)
        .join(
            AgentOpinion,
            and_(
                AgentOpinion.run_id == ReflectionRun.source_run_id,
                AgentOpinion.agent_id == ReflectionFinding.subject_id,
                AgentOpinion.index_code == ReflectionFinding.index_code,
                AgentOpinion.horizon == ReflectionFinding.horizon,
            ),
        )
        .where(
            ReflectionFinding.scope_type == "agent",
            ReflectionFinding.subject_id.in_(tuple(scope_by_agent)),
            ReflectionFinding.index_code.in_(index_codes),
            ReflectionFinding.horizon.in_(horizons),
            ReflectionRun.id.in_(tuple(approved_current_ids)),
            ReflectionHumanReview.decision == "approved",
            WorkflowRun.mode == "live",
            WorkflowRun.status == "completed",
            WorkflowRun.market_universe_hash == market_universe_hash,
        )
    )
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    seen_findings: set[str] = set()
    for raw in session.execute(finding_statement).mappings():
        row = dict(raw)
        finding_id = str(row["finding_id"])
        if finding_id in seen_findings:
            continue
        scope = scope_by_agent.get(str(row["agent_id"]))
        if scope is None or (
            row["agent_version"] != scope.agent_version
            or row["model_name"] != scope.model_name
            or not _at_or_before(row["finding_created_at"], data_cutoff)
        ):
            continue
        seen_findings.add(finding_id)
        key = (
            str(row["agent_id"]),
            str(row["index_code"]),
            str(row["horizon"]),
        )
        grouped.setdefault(key, []).append(
            {
                "finding_id": finding_id,
                "reflection_run_id": str(row["reflection_run_id"]),
                "target_date": row["target_date"].isoformat(),
                "verdict": str(row["verdict"]),
                "primary_error_type": str(row["primary_error_type"]),
                "availability_class": str(row["availability_class"]),
                "causal_status": str(row["causal_status"]),
                "evidence_ids": sorted(str(item) for item in (row["evidence_ids"] or [])),
                "reflection_output_hash": row["output_hash"],
                "human_review_notes_hash": str(row["notes_hash"]),
            }
        )
    for rows in grouped.values():
        rows.sort(
            key=lambda item: (
                item["target_date"],
                item["reflection_run_id"],
                item["finding_id"],
            )
        )
    return (
        grouped,
        reflection_dates,
        approved_gate_ids,
        gate_state.evidence_hash,
        gate_state.lineage_conflict_ids,
    )


def _build_profile(
    *,
    mode: Literal["demo", "live"],
    phase: Literal["demo_excluded", "shadow", "governance_review_required"],
    scope: BelievabilityAgentScope,
    index_code: str,
    horizon: str,
    performance: list[dict[str, Any]],
    reasoning: list[dict[str, Any]],
    required_live_target_dates: int,
    required_approved_reflections: int,
) -> BelievabilityDomainEvidence:
    brier_scores = [float(item["brier_score"]) for item in performance]
    sign_values = [
        bool(item["sign_correct"])
        for item in performance
        if item["sign_correct"] is not None
    ]
    material_values = [
        bool(item["material_direction_correct"])
        for item in performance
        if item["material_direction_correct"] is not None
    ]
    right_verified = sum(
        item["verdict"] == "right_reason" and item["causal_status"] == "verified"
        for item in reasoning
    )
    right_supported = sum(
        item["verdict"] == "right_reason" and item["causal_status"] == "supported"
        for item in reasoning
    )
    approved_reflections = {item["reflection_run_id"] for item in reasoning}
    independent_dates = {item["target_date"] for item in performance}
    reasoning_dates = {item["target_date"] for item in reasoning}
    if mode == "demo":
        evidence_status = "demo_excluded"
    elif len(independent_dates) < required_live_target_dates:
        evidence_status = "insufficient_history"
    elif len(reasoning_dates) < required_approved_reflections:
        evidence_status = "insufficient_reasoning_proxy"
    elif phase == "governance_review_required":
        evidence_status = "governance_review_required"
    else:
        evidence_status = "shadow_observation"
    return BelievabilityDomainEvidence(
        agent_id=scope.agent_id,
        agent_version=scope.agent_version,
        model_name=scope.model_name,
        role_domain=scope.role_domain,
        stage=scope.stage,
        index_code=index_code,
        horizon=horizon,  # type: ignore[arg-type]
        evaluation_sample_size=len(performance),
        independent_evaluation_target_dates=len(independent_dates),
        average_brier=(
            sum(brier_scores) / len(brier_scores) if brier_scores else None
        ),
        sign_sample_size=len(sign_values),
        sign_accuracy=(
            sum(sign_values) / len(sign_values) if sign_values else None
        ),
        material_sample_size=len(material_values),
        material_accuracy=(
            sum(material_values) / len(material_values)
            if material_values
            else None
        ),
        performance_evidence_hash=_canonical_hash(performance),
        approved_reflection_count=len(approved_reflections),
        independent_reasoning_target_dates=len(reasoning_dates),
        human_reviewed_right_reason_proxy_count=right_verified + right_supported,
        right_reason_verified_count=right_verified,
        right_reason_supported_count=right_supported,
        lucky_correct_count=sum(
            item["verdict"] == "lucky_correct" for item in reasoning
        ),
        wrong_count=sum(item["verdict"] == "wrong" for item in reasoning),
        reasoning_or_weighting_failure_count=sum(
            item["primary_error_type"] == "reasoning_or_weighting_failure"
            for item in reasoning
        ),
        unresolved_count=sum(
            item["verdict"] == "unresolved"
            or item["causal_status"] == "unresolved"
            for item in reasoning
        ),
        reasoning_proxy_evidence_hash=_canonical_hash(reasoning),
        evidence_status=evidence_status,
        current_stage_weight_metadata=scope.current_stage_weight_metadata,
        proposed_stage_multiplier=None,
        applied_stage_multiplier=1.0,
    )


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _instant(value: datetime, cutoff: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=cutoff.tzinfo)
    return value.astimezone(cutoff.tzinfo)


def _at_or_before(value: datetime | None, cutoff: datetime) -> bool:
    return value is not None and _instant(value, cutoff) <= cutoff


def _performance_priority(
    row: dict[str, Any],
    cutoff: datetime,
) -> tuple[datetime, datetime, str, str, str]:
    """Choose one correction deterministically when timestamps collide."""

    return (
        _instant(row["evaluated_at"], cutoff),
        _instant(row["batch_completed_at"], cutoff),
        str(row["evaluation_set_hash"]),
        str(row["batch_id"]),
        str(row["opinion_evaluation_id"]),
    )


def _canonical_hash(value: Any) -> str:
    body = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()
