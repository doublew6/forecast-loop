"""Outcome-blind file handoff for private Agent evaluation v2 replays.

The v2 workflow deliberately does not use the database-backed v1 experiment
runner.  ``prepare`` publishes an outcome-free input package, an external Codex
task writes one draft per arm, and ``finalize`` reveals trusted outcomes only
after both drafts and every frozen binding have passed deterministic checks.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from ..config import Settings
from ..db import Database
from ..models import AgentTrace
from ..research_v2 import CSI1000_D1_TARGET, DEFAULT_RESEARCH_PROGRAM_V2
from .agent_evaluation import BadCaseCreate, create_bad_case
from .agent_tracing import TRACE_POLICY_VERSION, TraceRecorder, canonical_digest

SUITE_SCHEMA_VERSION_V2 = "forecast-loop.agent-eval-suite/v2"
REPORT_SCHEMA_VERSION_V2 = "forecast-loop.agent-eval-report/v2"
DRAFT_SCHEMA_VERSION_V2 = "forecast-loop.agent-eval-drafts/v2"
INPUT_SCHEMA_VERSION_V2 = "forecast-loop.agent-eval-input/v2"
MANIFEST_SCHEMA_VERSION_V2 = "forecast-loop.agent-eval-handoff/v2"
RECEIPT_SCHEMA_VERSION_V2 = "forecast-loop.agent-eval-receipt/v2"
REVIEW_INPUT_SCHEMA_VERSION_V2 = "forecast-loop.agent-eval-review-input/v2"
REVIEW_DRAFT_SCHEMA_VERSION_V2 = "forecast-loop.agent-eval-review-draft/v2"
ABLATION_INPUT_SCHEMA_VERSION_V2 = "forecast-loop.agent-eval-ablation-input/v2"
ABLATION_DRAFT_SCHEMA_VERSION_V2 = "forecast-loop.agent-eval-ablation-draft/v2"
RELEASE_POLICY_VERSION_V2 = "2.0.0"
EVALUATOR_VERSION_V2 = "2.0.0"

HASH_PATTERN = r"^[0-9a-f]{64}$"
MAX_JSON_BYTES = 25 * 1024 * 1024
LABELS = ("up", "neutral", "down")
REASONING_DIMENSIONS = (
    "evidence_relevance",
    "causal_chain",
    "target_horizon_mapping",
    "counterevidence_invalidation",
    "calibration_uncertainty",
)
_RESERVED_OUTCOME_KEYS = {
    "actual_direction",
    "actual_label",
    "actual_return",
    "evaluation_result",
    "outcome",
    "outcome_hash",
    "realized_class",
    "realized_label",
    "realized_return",
    "target_close",
}


class AgentEvalV2Error(RuntimeError):
    """Raised when a v2 suite or handoff cannot be trusted."""


class V2Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FrozenArtifactV2(V2Model):
    version: str = Field(min_length=1, max_length=120)
    content_hash: str = Field(pattern=HASH_PATTERN)


class FrozenModelV2(FrozenArtifactV2):
    name: str = Field(min_length=1, max_length=160)


class TargetVersionManifestV2(V2Model):
    target_id: str = Field(min_length=1, max_length=160)
    model: FrozenModelV2
    agents: dict[str, FrozenArtifactV2] = Field(min_length=1)
    prompts: dict[str, FrozenArtifactV2] = Field(min_length=1)
    workflow: FrozenArtifactV2
    research_program: FrozenArtifactV2
    aggregation: FrozenArtifactV2
    wiki: FrozenArtifactV2

    @model_validator(mode="after")
    def validate_component_names(self) -> TargetVersionManifestV2:
        for label, values in (("agent", self.agents), ("prompt", self.prompts)):
            for value in values:
                _validate_component(value, f"{label} ID")
        if set(self.agents) != set(self.prompts):
            raise ValueError("every frozen agent must have exactly one frozen prompt")
        return self


class EvalArmV2(V2Model):
    arm_id: str = Field(min_length=1, max_length=120)
    description: str = ""
    targets: list[TargetVersionManifestV2] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_target_ids(self) -> EvalArmV2:
        target_ids = [target.target_id for target in self.targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("arm target IDs must be unique")
        return self


class EvalTargetV2(V2Model):
    target_id: str = Field(min_length=1, max_length=160)
    horizon: str = Field(min_length=1, max_length=32)
    description: str = ""
    release_gate: bool = True


class FrozenEvidenceV2(V2Model):
    evidence_id: str = Field(min_length=1, max_length=200)
    observed_at: datetime
    content_hash: str = Field(pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def require_aware_time(self) -> FrozenEvidenceV2:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("evidence observed_at must be timezone-aware")
        return self


class TrustedOutcomeV2(V2Model):
    label: Literal["up", "neutral", "down"]
    observation_hash: str = Field(pattern=HASH_PATTERN)


class ProbabilityIntervalV2(V2Model):
    minimum: float = Field(default=0.0, ge=0, le=1)
    maximum: float = Field(default=1.0, ge=0, le=1)

    @model_validator(mode="after")
    def validate_interval(self) -> ProbabilityIntervalV2:
        if self.minimum > self.maximum:
            raise ValueError("probability interval minimum may not exceed maximum")
        return self


class MustPassInvariantV2(V2Model):
    expected_direction: Literal["up", "neutral", "down"] | None = None
    probability_bounds: dict[
        Literal["up", "neutral", "down"], ProbabilityIntervalV2
    ] = Field(default_factory=dict)
    required_evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_semantic_assertion(self) -> MustPassInvariantV2:
        if (
            self.expected_direction is None
            and not self.probability_bounds
            and not self.required_evidence_ids
        ):
            raise ValueError("must-pass invariant requires a semantic assertion")
        if len(self.required_evidence_ids) != len(set(self.required_evidence_ids)):
            raise ValueError("must-pass required evidence IDs must be unique")
        return self


class EvalEpisodeV2(V2Model):
    episode_id: str = Field(min_length=1, max_length=200)
    target_id: str = Field(min_length=1, max_length=160)
    independence_key: str = Field(min_length=1, max_length=200)
    anchor_date: date
    target_date: date
    evidence_cutoff: datetime
    input_payload: dict[str, Any]
    input_hash: str = Field(pattern=HASH_PATTERN)
    evidence: list[FrozenEvidenceV2] = Field(default_factory=list)
    expected_trajectory: list[str] = Field(min_length=1)
    must_pass: bool = False
    must_pass_invariant: MustPassInvariantV2 | None = None
    outcome: TrustedOutcomeV2

    @model_validator(mode="after")
    def validate_frozen_input(self) -> EvalEpisodeV2:
        if self.evidence_cutoff.tzinfo is None or self.evidence_cutoff.utcoffset() is None:
            raise ValueError("episode evidence_cutoff must be timezone-aware")
        if self.target_date <= self.anchor_date:
            raise ValueError("episode target_date must follow anchor_date")
        if _contains_reserved_outcome_key(self.input_payload):
            raise ValueError("episode input_payload must not contain realized outcomes")
        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("episode evidence IDs must be unique")
        if any(item.observed_at > self.evidence_cutoff for item in self.evidence):
            raise ValueError("episode evidence must not exceed evidence_cutoff")
        if self.must_pass != (self.must_pass_invariant is not None):
            raise ValueError("must_pass episodes must declare exactly one semantic invariant")
        if self.must_pass_invariant is not None and not set(
            self.must_pass_invariant.required_evidence_ids
        ).issubset(evidence_ids):
            raise ValueError("must-pass required evidence must exist in the frozen episode")
        if self.input_hash != episode_input_hash(self):
            raise ValueError("episode input_hash does not match its outcome-free input")
        return self


class ReleasePolicyV2(V2Model):
    version: str = RELEASE_POLICY_VERSION_V2
    min_metric_episodes: int = Field(default=20, ge=1)
    must_pass_rate: float = Field(default=1.0, ge=0, le=1)
    max_brier_delta: float = Field(default=0.01, ge=0)
    max_direction_drop: float = Field(default=0.02, ge=0, le=1)
    max_p95_latency_ratio: float = Field(default=1.2, ge=1)
    max_token_ratio: float = Field(default=1.15, ge=1)

    @model_validator(mode="after")
    def forbid_weaker_release_gates(self) -> ReleasePolicyV2:
        if self.version != RELEASE_POLICY_VERSION_V2:
            raise ValueError("Agent Eval v2 release policy version must be 2.0.0")
        if self.min_metric_episodes < 20:
            raise ValueError("Agent Eval v2 requires at least 20 independent episodes")
        if self.must_pass_rate != 1.0:
            raise ValueError("Agent Eval v2 must-pass rate must remain 100%")
        if self.max_brier_delta > 0.01:
            raise ValueError("Agent Eval v2 Brier regression limit may not exceed 0.01")
        if self.max_direction_drop > 0.02:
            raise ValueError("Agent Eval v2 direction drop may not exceed 0.02")
        if self.max_p95_latency_ratio > 1.2:
            raise ValueError("Agent Eval v2 P95 latency ratio may not exceed 1.20")
        if self.max_token_ratio > 1.15:
            raise ValueError("Agent Eval v2 mean token ratio may not exceed 1.15")
        return self


class AgentEvalSuiteV2(V2Model):
    schema_version: Literal["forecast-loop.agent-eval-suite/v2"]
    suite_id: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    synthetic: bool = False
    runner_kind: Literal["codex_file_replay"] = "codex_file_replay"
    targets: list[EvalTargetV2] = Field(min_length=1)
    arms: list[EvalArmV2] = Field(min_length=2)
    episodes: list[EvalEpisodeV2] = Field(min_length=1)
    release_policy: ReleasePolicyV2 = Field(default_factory=ReleasePolicyV2)

    @model_validator(mode="after")
    def validate_identities(self) -> AgentEvalSuiteV2:
        target_ids = [target.target_id for target in self.targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("suite target IDs must be unique")
        if not any(target.release_gate for target in self.targets):
            raise ValueError("suite must contain at least one release-gated target")
        required_targets = set(target_ids)
        arm_ids = [arm.arm_id for arm in self.arms]
        if len(arm_ids) != len(set(arm_ids)):
            raise ValueError("suite arm IDs must be unique")
        for arm in self.arms:
            if {target.target_id for target in arm.targets} != required_targets:
                raise ValueError(f"arm {arm.arm_id} must freeze every suite target exactly once")
        episode_ids = [episode.episode_id for episode in self.episodes]
        if len(episode_ids) != len(set(episode_ids)):
            raise ValueError("suite episode IDs must be unique")
        covered_targets = {episode.target_id for episode in self.episodes}
        if covered_targets != required_targets:
            raise ValueError("suite episodes must cover every target")
        horizons = {target.target_id: target.horizon.upper() for target in self.targets}
        independence: set[tuple[str, str]] = set()
        target_dates: set[tuple[str, date]] = set()
        for episode in self.episodes:
            identity = (episode.target_id, episode.independence_key)
            if identity in independence:
                raise ValueError("independence_key must be unique within a target")
            independence.add(identity)
            date_identity = (episode.target_id, episode.target_date)
            if date_identity in target_dates:
                raise ValueError("target_date must be unique within a target")
            target_dates.add(date_identity)
        for target_id, horizon in horizons.items():
            if horizon == "D1":
                continue
            ordered = sorted(
                (episode.anchor_date, episode.target_date)
                for episode in self.episodes
                if episode.target_id == target_id
            )
            for previous, current in zip(ordered, ordered[1:], strict=False):
                if current[0] < previous[1]:
                    raise ValueError(
                        f"non-D1 episodes must not overlap within target {target_id}"
                    )
        return self


class ProbabilitiesV2(V2Model):
    up: float = Field(ge=0, le=1)
    neutral: float = Field(ge=0, le=1)
    down: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def require_unit_sum(self) -> ProbabilitiesV2:
        if abs(self.up + self.neutral + self.down - 1.0) > 1e-9:
            raise ValueError("probabilities must sum to 1")
        return self


class ReasoningReviewV2(V2Model):
    evidence_relevance: int = Field(ge=0, le=2)
    causal_chain: int = Field(ge=0, le=2)
    target_horizon_mapping: int = Field(ge=0, le=2)
    counterevidence_invalidation: int = Field(ge=0, le=2)
    calibration_uncertainty: int = Field(ge=0, le=2)
    rule_passed: bool
    review_input_hash: str = Field(pattern=HASH_PATTERN)
    reviewer_model: Literal["gpt-5.6-sol"]
    reasoning_effort: Literal["high"]
    reviewer_id: str = Field(min_length=1, max_length=160)


class EvalCitationV2(V2Model):
    evidence_id: str = Field(min_length=1, max_length=200)


class StructuredReasoningV2(V2Model):
    rationale: str = Field(min_length=1, max_length=8000)
    causal_chain: list[str] = Field(min_length=1)
    counter_evidence: list[str] = Field(min_length=1)
    invalidation_conditions: list[str] = Field(min_length=1)


class SelfReportedAblationV2(V2Model):
    """Deprecated arm diagnostic retained only for compatibility and ignored."""

    agent_id: str = Field(min_length=1, max_length=160)
    replacement: Literal["no_impact"]
    probabilities: ProbabilitiesV2


class DraftEpisodeOutputV2(V2Model):
    episode_id: str = Field(min_length=1, max_length=200)
    target_id: str = Field(min_length=1, max_length=160)
    status: Literal["completed", "failed"]
    trajectory: list[str] = Field(min_length=1)
    citations: list[EvalCitationV2] = Field(default_factory=list)
    probabilities: ProbabilitiesV2 | None = None
    reasoning: StructuredReasoningV2 | None = None
    latency_ms: float = Field(ge=0)
    total_tokens: int = Field(ge=0)
    # These legacy fields are never used for scoring. Independent task drafts
    # under reviewer/ and ablation/ are authoritative.
    reasoning_review: ReasoningReviewV2 | None = None
    ablations: list[SelfReportedAblationV2] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_completed_output(self) -> DraftEpisodeOutputV2:
        if self.status == "completed" and (
            self.probabilities is None or self.reasoning is None
        ):
            raise ValueError("completed output requires probabilities and structured reasoning")
        if self.status == "failed" and (
            self.probabilities is not None
            or self.reasoning is not None
            or self.reasoning_review is not None
            or self.ablations
        ):
            raise ValueError("failed output cannot provide forecast artifacts")
        citation_ids = [item.evidence_id for item in self.citations]
        if len(citation_ids) != len(set(citation_ids)):
            raise ValueError("draft citation IDs must be unique")
        ablation_ids = [item.agent_id for item in self.ablations]
        if len(ablation_ids) != len(set(ablation_ids)):
            raise ValueError("self-reported ablation agent IDs must be unique")
        return self


class GeneratedByV2(V2Model):
    producer: str = Field(min_length=1, max_length=160)
    model: str = Field(min_length=1, max_length=160)
    reasoning_effort: str = Field(min_length=1, max_length=32)


class AgentEvalDraftV2(V2Model):
    schema_version: Literal["forecast-loop.agent-eval-drafts/v2"]
    job_id: str
    arm_id: str
    suite_hash: str = Field(pattern=HASH_PATTERN)
    input_hash: str = Field(pattern=HASH_PATTERN)
    arm_manifest_hash: str = Field(pattern=HASH_PATTERN)
    generated_by: GeneratedByV2
    outputs: list[DraftEpisodeOutputV2]

    @model_validator(mode="after")
    def validate_output_ids(self) -> AgentEvalDraftV2:
        output_ids = [output.episode_id for output in self.outputs]
        if len(output_ids) != len(set(output_ids)):
            raise ValueError("draft output episode IDs must be unique")
        return self


class EvalEpisodeInputV2(V2Model):
    episode_id: str
    target_id: str
    independence_key: str
    anchor_date: date
    target_date: date
    evidence_cutoff: datetime
    input_payload: dict[str, Any]
    input_hash: str = Field(pattern=HASH_PATTERN)
    evidence: list[FrozenEvidenceV2]
    expected_trajectory: list[str]
    must_pass: bool
    must_pass_invariant: MustPassInvariantV2 | None = None


class AgentEvalInputV2(V2Model):
    schema_version: Literal["forecast-loop.agent-eval-input/v2"]
    job_id: str
    suite_id: str
    suite_version: str
    suite_hash: str = Field(pattern=HASH_PATTERN)
    baseline_arm_id: str
    candidate_arm_id: str
    arm_manifests: dict[str, EvalArmV2]
    arm_manifest_hashes: dict[str, str]
    targets: list[EvalTargetV2]
    episodes: list[EvalEpisodeInputV2]
    prepared_at: datetime
    input_hash: str = Field(pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def validate_input_seal(self) -> AgentEvalInputV2:
        if self.input_hash != _model_hash_without(self, "input_hash"):
            raise ValueError("Agent Eval input_hash does not match input.json")
        return self


class AgentEvalReviewInputV2(V2Model):
    """Outcome-free material available to the independent reasoning reviewer."""

    schema_version: Literal["forecast-loop.agent-eval-review-input/v2"]
    job_id: str
    suite_hash: str = Field(pattern=HASH_PATTERN)
    eval_input_hash: str = Field(pattern=HASH_PATTERN)
    arm_ids: list[str] = Field(min_length=2)
    arm_manifest_hashes: dict[str, str]
    episodes: list[EvalEpisodeInputV2] = Field(min_length=1)
    rubric_dimensions: list[str]
    reviewer_model: Literal["gpt-5.6-sol"] = "gpt-5.6-sol"
    reasoning_effort: Literal["high"] = "high"
    input_hash: str = Field(pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def validate_review_input(self) -> AgentEvalReviewInputV2:
        if self.arm_ids != list(dict.fromkeys(self.arm_ids)):
            raise ValueError("review arm IDs must be unique")
        if set(self.arm_ids) != set(self.arm_manifest_hashes):
            raise ValueError("review input must bind every arm manifest")
        if self.rubric_dimensions != list(REASONING_DIMENSIONS):
            raise ValueError("review input must use the frozen five-dimension rubric")
        if self.input_hash != _model_hash_without(self, "input_hash"):
            raise ValueError("review input_hash does not match review input")
        return self


class ReasoningReviewItemV2(V2Model):
    arm_id: str = Field(min_length=1, max_length=120)
    episode_id: str = Field(min_length=1, max_length=200)
    target_id: str = Field(min_length=1, max_length=160)
    reviewed_output_hash: str = Field(pattern=HASH_PATTERN)
    review: ReasoningReviewV2


class AgentEvalReviewDraftV2(V2Model):
    schema_version: Literal["forecast-loop.agent-eval-review-draft/v2"]
    job_id: str
    suite_hash: str = Field(pattern=HASH_PATTERN)
    eval_input_hash: str = Field(pattern=HASH_PATTERN)
    review_input_hash: str = Field(pattern=HASH_PATTERN)
    generated_by: GeneratedByV2
    reviews: list[ReasoningReviewItemV2]

    @model_validator(mode="after")
    def validate_review_ids(self) -> AgentEvalReviewDraftV2:
        identities = [(item.arm_id, item.episode_id) for item in self.reviews]
        if len(identities) != len(set(identities)):
            raise ValueError("reasoning review identities must be unique")
        if self.generated_by.model != "gpt-5.6-sol":
            raise ValueError("reasoning reviewer must use gpt-5.6-sol")
        if self.generated_by.reasoning_effort != "high":
            raise ValueError("reasoning reviewer must use high reasoning effort")
        if any(item.review.reviewer_id != self.generated_by.producer for item in self.reviews):
            raise ValueError("reviewer_id must match the independent review producer")
        return self


class NoImpactOverrideV2(V2Model):
    replacement: Literal["no_impact"] = "no_impact"
    contribution_enabled: Literal[False] = False
    impact: Literal["none"] = "none"
    importance: Literal["none"] = "none"
    abstained: Literal[True] = True


class AblationAssignmentV2(V2Model):
    ablation_id: str = Field(min_length=1, max_length=360)
    target_id: str = Field(min_length=1, max_length=160)
    agent_id: str = Field(min_length=1, max_length=160)
    agent: FrozenArtifactV2
    prompt: FrozenArtifactV2
    override: NoImpactOverrideV2 = Field(default_factory=NoImpactOverrideV2)
    assignment_hash: str = Field(pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def validate_assignment_hash(self) -> AblationAssignmentV2:
        if self.assignment_hash != _model_hash_without(self, "assignment_hash"):
            raise ValueError("ablation assignment_hash does not match frozen override")
        return self


class AgentEvalAblationInputV2(V2Model):
    """Frozen candidate inputs plus explicit no-impact replacement assignments."""

    schema_version: Literal["forecast-loop.agent-eval-ablation-input/v2"]
    job_id: str
    suite_hash: str = Field(pattern=HASH_PATTERN)
    eval_input_hash: str = Field(pattern=HASH_PATTERN)
    candidate_arm_id: str = Field(min_length=1, max_length=120)
    candidate_manifest: EvalArmV2
    candidate_manifest_hash: str = Field(pattern=HASH_PATTERN)
    episodes: list[EvalEpisodeInputV2] = Field(min_length=1)
    assignments: list[AblationAssignmentV2] = Field(min_length=1)
    input_hash: str = Field(pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def validate_ablation_input(self) -> AgentEvalAblationInputV2:
        if self.candidate_manifest.arm_id != self.candidate_arm_id:
            raise ValueError("ablation candidate manifest arm does not match")
        if arm_manifest_hash_v2(self.candidate_manifest) != self.candidate_manifest_hash:
            raise ValueError("ablation candidate manifest hash does not match")
        identities = [item.ablation_id for item in self.assignments]
        if len(identities) != len(set(identities)):
            raise ValueError("ablation assignment IDs must be unique")
        if self.input_hash != _model_hash_without(self, "input_hash"):
            raise ValueError("ablation input_hash does not match ablation input")
        return self


class AblationOutputV2(V2Model):
    ablation_id: str = Field(min_length=1, max_length=360)
    episode_id: str = Field(min_length=1, max_length=200)
    target_id: str = Field(min_length=1, max_length=160)
    agent_id: str = Field(min_length=1, max_length=160)
    replacement: Literal["no_impact"]
    status: Literal["completed", "failed"]
    full_output_hash: str = Field(pattern=HASH_PATTERN)
    ablation_input_hash: str = Field(pattern=HASH_PATTERN)
    probabilities: ProbabilitiesV2 | None = None

    @model_validator(mode="after")
    def validate_output(self) -> AblationOutputV2:
        if (self.status == "completed") != (self.probabilities is not None):
            raise ValueError("completed ablation requires probabilities; failed forbids them")
        return self


class AgentEvalAblationDraftV2(V2Model):
    schema_version: Literal["forecast-loop.agent-eval-ablation-draft/v2"]
    job_id: str
    suite_hash: str = Field(pattern=HASH_PATTERN)
    eval_input_hash: str = Field(pattern=HASH_PATTERN)
    ablation_input_hash: str = Field(pattern=HASH_PATTERN)
    candidate_arm_id: str = Field(min_length=1, max_length=120)
    generated_by: GeneratedByV2
    outputs: list[AblationOutputV2]

    @model_validator(mode="after")
    def validate_output_ids(self) -> AgentEvalAblationDraftV2:
        identities = [(item.ablation_id, item.episode_id) for item in self.outputs]
        if len(identities) != len(set(identities)):
            raise ValueError("ablation output identities must be unique")
        return self


class AgentEvalManifestV2(V2Model):
    schema_version: Literal["forecast-loop.agent-eval-handoff/v2"]
    job_id: str
    suite_id: str
    suite_version: str
    suite_source: Literal["public", "private"]
    suite_hash: str = Field(pattern=HASH_PATTERN)
    baseline_arm_id: str
    candidate_arm_id: str
    input_hash: str = Field(pattern=HASH_PATTERN)
    input_file_hash: str = Field(pattern=HASH_PATTERN)
    review_input_hash: str = Field(pattern=HASH_PATTERN)
    review_input_file_hash: str = Field(pattern=HASH_PATTERN)
    ablation_input_hash: str = Field(pattern=HASH_PATTERN)
    ablation_input_file_hash: str = Field(pattern=HASH_PATTERN)
    prepared_at: datetime


class MetricAggregateV2(V2Model):
    episode_count: int = Field(ge=0)
    direction_accuracy: float | None = Field(default=None, ge=0, le=1)
    mean_brier: float | None = Field(default=None, ge=0, le=2 / 3)
    p95_latency_ms: float | None = Field(default=None, ge=0)
    mean_tokens: float | None = Field(default=None, ge=0)


class HardGateResultV2(V2Model):
    eligible_count: int = Field(ge=0)
    passed_count: int = Field(ge=0)
    rate: float = Field(ge=0, le=1)
    passed: bool


class ReasoningSummaryV2(V2Model):
    review_count: int = Field(ge=0)
    mean_total_score: float | None = Field(default=None, ge=0, le=10)
    rubric_means: dict[str, float | None]
    rule_pass_rate: float | None = Field(default=None, ge=0, le=1)
    human_confirmed_severe_count: int = Field(ge=0)
    advisory_only: Literal[True] = True


class AblationSummaryV2(V2Model):
    agent_id: str
    episode_count: int = Field(ge=0)
    mean_full_brier: float | None = Field(default=None, ge=0, le=2 / 3)
    mean_ablated_brier: float | None = Field(default=None, ge=0, le=2 / 3)
    mean_incremental_brier: float | None = Field(default=None, ge=-2 / 3, le=2 / 3)


class TargetEvalReportV2(V2Model):
    target_id: str
    horizon: str
    release_gate: bool
    decision: Literal["pass", "fail", "insufficient_sample"]
    episode_count: int = Field(ge=0)
    hard_gates: dict[str, HardGateResultV2]
    hard_gate_pass: bool
    metric_gates: dict[str, float | bool | None]
    metric_gate_pass: bool | None
    baseline: MetricAggregateV2
    candidate: MetricAggregateV2
    reasoning: dict[str, ReasoningSummaryV2]
    ablation: list[AblationSummaryV2]


class AgentEvalReportV2(V2Model):
    schema_version: Literal["forecast-loop.agent-eval-report/v2"]
    job_id: str
    suite_id: str
    suite_version: str
    suite_hash: str = Field(pattern=HASH_PATTERN)
    input_hash: str = Field(pattern=HASH_PATTERN)
    baseline_arm_id: str
    candidate_arm_id: str
    status: Literal["completed"]
    release_decision: Literal["pass", "fail", "insufficient_sample"]
    evaluator_version: str
    policy: ReleasePolicyV2
    targets: dict[str, TargetEvalReportV2]
    completed_at: datetime


class AgentEvalReceiptV2(V2Model):
    schema_version: Literal["forecast-loop.agent-eval-receipt/v2"]
    job_id: str
    input_hash: str = Field(pattern=HASH_PATTERN)
    baseline_draft_hash: str = Field(pattern=HASH_PATTERN)
    candidate_draft_hash: str = Field(pattern=HASH_PATTERN)
    review_draft_hash: str = Field(pattern=HASH_PATTERN)
    ablation_draft_hash: str = Field(pattern=HASH_PATTERN)
    report_hash: str = Field(pattern=HASH_PATTERN)
    release_decision: Literal["pass", "fail", "insufficient_sample"]
    finalized_at: datetime


class AgentEvalV2SuiteDescriptor(V2Model):
    schema_version: Literal["forecast-loop.agent-eval-suite/v2"]
    suite_id: str
    version: str
    title: str
    description: str
    synthetic: bool
    runner_kind: str
    episode_count: int
    target_ids: list[str]
    arm_ids: list[str]
    content_hash: str = Field(pattern=HASH_PATTERN)
    source: Literal["public", "private"]


class AgentEvalV2JobView(V2Model):
    """Sanitized read model for one private file-handoff evaluation job."""

    job_id: str
    suite_id: str
    suite_version: str
    suite_hash: str = Field(pattern=HASH_PATTERN)
    baseline_arm_id: str
    candidate_arm_id: str
    status: Literal["awaiting_draft", "ready_to_finalize", "completed"]
    release_decision: Literal["pending", "pass", "fail", "insufficient_sample"]
    policy_version: str
    prepared_at: datetime
    completed_at: datetime | None = None
    report_hash: str | None = Field(default=None, pattern=HASH_PATTERN)
    pending_arms: list[str] = Field(default_factory=list)
    pending_tasks: list[str] = Field(default_factory=list)
    targets: dict[str, TargetEvalReportV2] = Field(default_factory=dict)


class AgentEvalV2Store:
    """Load v2 suites while rejecting symlink and configured-root escapes."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def list_suites(self) -> list[AgentEvalV2SuiteDescriptor]:
        descriptors: list[AgentEvalV2SuiteDescriptor] = []
        for source, root in self._roots():
            if not root.exists():
                continue
            for path in sorted(root.glob("*/suite.json")):
                try:
                    suite = self._read_suite(path, root=root)
                except (OSError, ValueError, AgentEvalV2Error):
                    continue
                descriptors.append(self.describe(suite, source=source))
        return descriptors

    def load(
        self,
        suite_id: str,
        *,
        version: str | None,
        source: Literal["public", "private"],
    ) -> AgentEvalSuiteV2:
        _validate_component(suite_id, "suite_id")
        root = dict(self._roots())[source]
        if not root.exists():
            raise AgentEvalV2Error(f"Agent Eval {source} suite root does not exist")
        matches: list[AgentEvalSuiteV2] = []
        for path in sorted(root.glob("*/suite.json")):
            try:
                suite = self._read_suite(path, root=root)
            except (ValueError, AgentEvalV2Error):
                continue
            if suite.suite_id == suite_id and (version is None or suite.version == version):
                matches.append(suite)
        if not matches:
            suffix = f" version {version}" if version else ""
            raise AgentEvalV2Error(f"v2 suite {suite_id}{suffix} was not found")
        if len(matches) > 1:
            raise AgentEvalV2Error("v2 suite identity is ambiguous; specify suite_version")
        return matches[0]

    def describe(
        self,
        suite: AgentEvalSuiteV2,
        *,
        source: Literal["public", "private"],
    ) -> AgentEvalV2SuiteDescriptor:
        return AgentEvalV2SuiteDescriptor(
            schema_version=SUITE_SCHEMA_VERSION_V2,
            suite_id=suite.suite_id,
            version=suite.version,
            title=suite.title,
            description=suite.description,
            synthetic=suite.synthetic,
            runner_kind=suite.runner_kind,
            episode_count=len(suite.episodes),
            target_ids=[target.target_id for target in suite.targets],
            arm_ids=[arm.arm_id for arm in suite.arms],
            content_hash=suite_hash_v2(suite),
            source=source,
        )

    def _roots(self) -> tuple[tuple[Literal["public", "private"], Path], ...]:
        return (
            ("public", self.settings.agent_eval_public_root),
            ("private", self.settings.agent_eval_outcome_root),
        )

    @staticmethod
    def _read_suite(path: Path, *, root: Path) -> AgentEvalSuiteV2:
        resolved_root = root.resolve()
        if path.is_symlink():
            raise ValueError("suite path may not be a symlink")
        resolved_path = path.resolve(strict=True)
        if not resolved_path.is_relative_to(resolved_root):
            raise ValueError("suite path escapes configured root")
        raw, payload = _secure_read_json(resolved_path)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("suite exceeds the 4 MiB limit")
        return AgentEvalSuiteV2.model_validate(payload)


def suite_hash_v2(suite: AgentEvalSuiteV2) -> str:
    return canonical_digest(suite.model_dump(mode="json"))


def episode_input_hash(episode: EvalEpisodeV2 | dict[str, Any]) -> str:
    if isinstance(episode, EvalEpisodeV2):
        body = {
            "episode_id": episode.episode_id,
            "target_id": episode.target_id,
            "independence_key": episode.independence_key,
            "anchor_date": episode.anchor_date,
            "target_date": episode.target_date,
            "evidence_cutoff": episode.evidence_cutoff,
            "input_payload": episode.input_payload,
            "evidence": [item.model_dump(mode="json") for item in episode.evidence],
            "expected_trajectory": episode.expected_trajectory,
            "must_pass": episode.must_pass,
            "must_pass_invariant": (
                episode.must_pass_invariant.model_dump(mode="json")
                if episode.must_pass_invariant is not None
                else None
            ),
        }
    else:
        body = {
            key: episode[key]
            for key in (
                "episode_id",
                "target_id",
                "independence_key",
                "anchor_date",
                "target_date",
                "evidence_cutoff",
                "input_payload",
                "evidence",
                "expected_trajectory",
                "must_pass",
            )
        }
        body["must_pass_invariant"] = episode.get("must_pass_invariant")
    normalized = EvalEpisodeInputV2.model_validate(
        {**body, "input_hash": "0" * 64}
    ).model_dump(mode="json")
    normalized.pop("input_hash")
    return canonical_digest(normalized)


def arm_manifest_hash_v2(arm: EvalArmV2) -> str:
    return canonical_digest(arm.model_dump(mode="json"))


def prepare_agent_eval_v2(
    settings: Settings,
    *,
    suite_id: str,
    suite_version: str | None,
    source: Literal["public", "private"],
    baseline_arm_id: str,
    candidate_arm_id: str,
    output_root: Path | None = None,
    prepared_at: datetime | None = None,
) -> dict[str, Any]:
    """Create one sealed, outcome-free v2 handoff job."""

    if baseline_arm_id == candidate_arm_id:
        raise AgentEvalV2Error("baseline and candidate arms must be different")
    suite = AgentEvalV2Store(settings).load(suite_id, version=suite_version, source=source)
    arms = {arm.arm_id: arm for arm in suite.arms}
    try:
        baseline = arms[baseline_arm_id]
        candidate = arms[candidate_arm_id]
    except KeyError as exc:
        raise AgentEvalV2Error(f"unknown v2 eval arm: {exc.args[0]}") from exc
    root = _prepare_root(output_root or settings.agent_eval_private_root / "handoffs")
    job_id = str(uuid4())
    job_dir = root / job_id
    job_dir.mkdir(mode=0o700)
    for arm_id in (baseline_arm_id, candidate_arm_id, "reviewer", "ablation"):
        (job_dir / arm_id).mkdir(mode=0o700)
    now = prepared_at or datetime.now(ZoneInfo(settings.timezone))
    selected_arms = {baseline.arm_id: baseline, candidate.arm_id: candidate}
    input_body = {
        "schema_version": INPUT_SCHEMA_VERSION_V2,
        "job_id": job_id,
        "suite_id": suite.suite_id,
        "suite_version": suite.version,
        "suite_hash": suite_hash_v2(suite),
        "baseline_arm_id": baseline.arm_id,
        "candidate_arm_id": candidate.arm_id,
        "arm_manifests": {
            key: value.model_dump(mode="json") for key, value in selected_arms.items()
        },
        "arm_manifest_hashes": {
            key: arm_manifest_hash_v2(value) for key, value in selected_arms.items()
        },
        "targets": [target.model_dump(mode="json") for target in suite.targets],
        "episodes": [_outcome_free_episode(episode) for episode in suite.episodes],
        "prepared_at": now.isoformat(),
    }
    input_payload = {**input_body, "input_hash": canonical_digest(input_body)}
    validated_input = AgentEvalInputV2.model_validate(input_payload)
    input_bytes = _json_bytes(validated_input.model_dump(mode="json"))
    review_body = {
        "schema_version": REVIEW_INPUT_SCHEMA_VERSION_V2,
        "job_id": job_id,
        "suite_hash": suite_hash_v2(suite),
        "eval_input_hash": validated_input.input_hash,
        "arm_ids": [baseline.arm_id, candidate.arm_id],
        "arm_manifest_hashes": validated_input.arm_manifest_hashes,
        "episodes": [item.model_dump(mode="json") for item in validated_input.episodes],
        "rubric_dimensions": list(REASONING_DIMENSIONS),
        "reviewer_model": "gpt-5.6-sol",
        "reasoning_effort": "high",
    }
    review_input = AgentEvalReviewInputV2.model_validate(
        {**review_body, "input_hash": canonical_digest(review_body)}
    )
    review_input_bytes = _json_bytes(review_input.model_dump(mode="json"))
    assignments = _ablation_assignments(candidate)
    ablation_body = {
        "schema_version": ABLATION_INPUT_SCHEMA_VERSION_V2,
        "job_id": job_id,
        "suite_hash": suite_hash_v2(suite),
        "eval_input_hash": validated_input.input_hash,
        "candidate_arm_id": candidate.arm_id,
        "candidate_manifest": candidate.model_dump(mode="json"),
        "candidate_manifest_hash": arm_manifest_hash_v2(candidate),
        "episodes": [item.model_dump(mode="json") for item in validated_input.episodes],
        "assignments": [item.model_dump(mode="json") for item in assignments],
    }
    ablation_input = AgentEvalAblationInputV2.model_validate(
        {**ablation_body, "input_hash": canonical_digest(ablation_body)}
    )
    ablation_input_bytes = _json_bytes(ablation_input.model_dump(mode="json"))
    manifest = AgentEvalManifestV2(
        schema_version=MANIFEST_SCHEMA_VERSION_V2,
        job_id=job_id,
        suite_id=suite.suite_id,
        suite_version=suite.version,
        suite_source=source,
        suite_hash=suite_hash_v2(suite),
        baseline_arm_id=baseline.arm_id,
        candidate_arm_id=candidate.arm_id,
        input_hash=validated_input.input_hash,
        input_file_hash=_sha256(input_bytes),
        review_input_hash=review_input.input_hash,
        review_input_file_hash=_sha256(review_input_bytes),
        ablation_input_hash=ablation_input.input_hash,
        ablation_input_file_hash=_sha256(ablation_input_bytes),
        prepared_at=now,
    )
    _atomic_write_new(job_dir / "input.json", input_bytes, mode=0o400)
    _atomic_write_new(
        job_dir / "manifest.json",
        _json_bytes(manifest.model_dump(mode="json")),
        mode=0o400,
    )
    _atomic_write_new(job_dir / "reviewer/input.json", review_input_bytes, mode=0o400)
    _atomic_write_new(job_dir / "ablation/input.json", ablation_input_bytes, mode=0o400)
    for arm in (baseline, candidate):
        template = AgentEvalDraftV2(
            schema_version=DRAFT_SCHEMA_VERSION_V2,
            job_id=job_id,
            arm_id=arm.arm_id,
            suite_hash=manifest.suite_hash,
            input_hash=manifest.input_hash,
            arm_manifest_hash=arm_manifest_hash_v2(arm),
            generated_by=GeneratedByV2(
                producer="replace-with-external-task-id",
                model="replace-with-actual-model",
                reasoning_effort="replace-with-actual-effort",
            ),
            outputs=[],
        )
        _atomic_write_new(
            job_dir / arm.arm_id / "drafts.template.json",
            _json_bytes(template.model_dump(mode="json")),
            mode=0o400,
        )
    review_template = AgentEvalReviewDraftV2(
        schema_version=REVIEW_DRAFT_SCHEMA_VERSION_V2,
        job_id=job_id,
        suite_hash=manifest.suite_hash,
        eval_input_hash=manifest.input_hash,
        review_input_hash=manifest.review_input_hash,
        generated_by=GeneratedByV2(
            producer="replace-with-independent-review-task-id",
            model="gpt-5.6-sol",
            reasoning_effort="high",
        ),
        reviews=[],
    )
    _atomic_write_new(
        job_dir / "reviewer/drafts.template.json",
        _json_bytes(review_template.model_dump(mode="json")),
        mode=0o400,
    )
    ablation_template = AgentEvalAblationDraftV2(
        schema_version=ABLATION_DRAFT_SCHEMA_VERSION_V2,
        job_id=job_id,
        suite_hash=manifest.suite_hash,
        eval_input_hash=manifest.input_hash,
        ablation_input_hash=manifest.ablation_input_hash,
        candidate_arm_id=candidate.arm_id,
        generated_by=GeneratedByV2(
            producer="replace-with-independent-ablation-task-id",
            model="replace-with-actual-model",
            reasoning_effort="replace-with-actual-effort",
        ),
        outputs=[],
    )
    _atomic_write_new(
        job_dir / "ablation/drafts.template.json",
        _json_bytes(ablation_template.model_dump(mode="json")),
        mode=0o400,
    )
    _atomic_write_new(
        job_dir / "README.md",
        _instruction_text(baseline.arm_id, candidate.arm_id).encode("utf-8"),
        mode=0o400,
    )
    return {
        "status": "awaiting_draft",
        "job_id": job_id,
        "job_dir": str(job_dir),
        "input_hash": manifest.input_hash,
        "pending_arms": [baseline.arm_id, candidate.arm_id],
        "pending_tasks": ["reviewer", "ablation"],
        "draft_files": {
            arm_id: str(job_dir / arm_id / "drafts.json")
            for arm_id in (baseline.arm_id, candidate.arm_id, "reviewer", "ablation")
        },
    }


def agent_eval_v2_status(
    settings: Settings,
    job_dir: str | Path,
    *,
    output_root: Path | None = None,
) -> dict[str, Any]:
    directory = _resolve_job_dir(
        output_root or settings.agent_eval_private_root / "handoffs", job_dir
    )
    manifest, eval_input, review_input, ablation_input = _load_frozen_job(directory)
    receipt_path = directory / "receipt.json"
    report_path = directory / "report.json"
    if receipt_path.exists() or receipt_path.is_symlink():
        report, report_hash = verify_finalized_agent_eval_v2_job(
            settings, report_path, require_release_binding=False
        )
        return {
            "status": "completed",
            "job_id": manifest.job_id,
            "release_decision": report.release_decision,
            "report_hash": report_hash,
            "report_file": str(report_path),
        }
    pending: list[str] = []
    for arm_id in (manifest.baseline_arm_id, manifest.candidate_arm_id):
        path = directory / arm_id / "drafts.json"
        if not path.exists() and not path.is_symlink():
            pending.append(arm_id)
            continue
        _load_bound_draft(directory, arm_id, manifest=manifest, eval_input=eval_input)
    arm_pending = bool(pending)
    if not arm_pending:
        arm_drafts = {
            arm_id: _load_bound_draft(
                directory, arm_id, manifest=manifest, eval_input=eval_input
            )[0]
            for arm_id in (manifest.baseline_arm_id, manifest.candidate_arm_id)
        }
        _require_distinct_arm_producers(arm_drafts)
        review_path = directory / "reviewer/drafts.json"
        review_draft: AgentEvalReviewDraftV2 | None = None
        if not review_path.exists() and not review_path.is_symlink():
            pending.append("reviewer")
        else:
            review_draft, _ = _load_bound_review_draft(
                directory,
                manifest=manifest,
                eval_input=eval_input,
                review_input=review_input,
                arm_drafts=arm_drafts,
            )
        ablation_path = directory / "ablation/drafts.json"
        if not ablation_path.exists() and not ablation_path.is_symlink():
            pending.append("ablation")
        else:
            _load_bound_ablation_draft(
                directory,
                manifest=manifest,
                eval_input=eval_input,
                ablation_input=ablation_input,
                candidate=arm_drafts[manifest.candidate_arm_id],
                forbidden_producers={
                    draft.generated_by.producer for draft in arm_drafts.values()
                }
                | (
                    {review_draft.generated_by.producer}
                    if review_draft is not None
                    else set()
                ),
            )
    else:
        # Downstream tasks cannot be complete before both frozen arm outputs exist.
        pending.extend(("reviewer", "ablation"))
    return {
        "status": "awaiting_draft" if pending else "ready_to_finalize",
        "job_id": manifest.job_id,
        "input_hash": manifest.input_hash,
        "pending_arms": [
            item
            for item in pending
            if item in {manifest.baseline_arm_id, manifest.candidate_arm_id}
        ],
        "pending_tasks": [item for item in pending if item in {"reviewer", "ablation"}],
    }


def list_agent_eval_v2_jobs(
    settings: Settings,
    *,
    limit: int = 50,
) -> list[AgentEvalV2JobView]:
    """List private v2 jobs without exposing paths, inputs, drafts, or outcomes."""

    if not 1 <= limit <= 200:
        raise AgentEvalV2Error("Agent Eval v2 job limit must be between 1 and 200")
    root = settings.agent_eval_private_root / "handoffs"
    if not root.exists():
        return []
    if root.is_symlink():
        raise AgentEvalV2Error("Agent Eval handoff root may not be a symlink")
    resolved_root = root.resolve()
    jobs: list[AgentEvalV2JobView] = []
    for candidate in resolved_root.iterdir():
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        try:
            jobs.append(_agent_eval_v2_job_view(settings, candidate))
        except (OSError, ValueError, AgentEvalV2Error):
            # An incomplete, foreign, or corrupt directory is not safe to project.
            continue
    return sorted(
        jobs,
        key=lambda item: (item.prepared_at, item.job_id),
        reverse=True,
    )[:limit]


def latest_agent_eval_v2_ablation_values(
    settings: Settings,
) -> dict[tuple[str, str, str, str, str], float]:
    """Return the newest sealed ablation value for each exact Agent identity.

    Keys bind target, Agent version, model name, and prompt version so an
    offline replay can never be projected onto a different online scorecard
    identity.  Corrupt or incomplete jobs are ignored by the sanitized job
    listing boundary.
    """

    root = settings.agent_eval_private_root / "handoffs"
    values: dict[tuple[str, str, str, str, str], float] = {}
    for job in list_agent_eval_v2_jobs(settings, limit=200):
        if job.status != "completed":
            continue
        directory = root / job.job_id
        try:
            manifest, eval_input, _review_input, _ablation_input = _load_frozen_job(
                directory
            )
            candidate = eval_input.arm_manifests[manifest.candidate_arm_id]
        except (KeyError, OSError, ValueError, AgentEvalV2Error):
            continue
        target_manifests = {item.target_id: item for item in candidate.targets}
        for target_id, target_report in job.targets.items():
            target_manifest = target_manifests.get(target_id)
            if target_manifest is None:
                continue
            for ablation in target_report.ablation:
                if ablation.mean_incremental_brier is None:
                    continue
                agent = target_manifest.agents.get(ablation.agent_id)
                prompt = target_manifest.prompts.get(ablation.agent_id)
                if agent is None or prompt is None:
                    continue
                identity = (
                    target_id,
                    ablation.agent_id,
                    agent.version,
                    target_manifest.model.name,
                    prompt.version,
                )
                # Jobs are newest-first; never blend versions or replay runs.
                values.setdefault(identity, ablation.mean_incremental_brier)
    return values


def _agent_eval_v2_job_view(
    settings: Settings,
    directory: Path,
) -> AgentEvalV2JobView:
    manifest, _eval_input, _review_input, _ablation_input = _load_frozen_job(directory)
    status = agent_eval_v2_status(settings, directory)
    report: AgentEvalReportV2 | None = None
    report_hash: str | None = None
    report_path = directory / "report.json"
    if status["status"] == "completed":
        report_raw, report_payload = _secure_read_json(report_path)
        report = AgentEvalReportV2.model_validate(report_payload)
        report_hash = _sha256(report_raw)
    return AgentEvalV2JobView(
        job_id=manifest.job_id,
        suite_id=manifest.suite_id,
        suite_version=manifest.suite_version,
        suite_hash=manifest.suite_hash,
        baseline_arm_id=manifest.baseline_arm_id,
        candidate_arm_id=manifest.candidate_arm_id,
        status=status["status"],
        release_decision=(report.release_decision if report is not None else "pending"),
        policy_version=(
            report.policy.version if report is not None else RELEASE_POLICY_VERSION_V2
        ),
        prepared_at=manifest.prepared_at,
        completed_at=(report.completed_at if report is not None else None),
        report_hash=report_hash,
        pending_arms=list(status.get("pending_arms", [])),
        pending_tasks=list(status.get("pending_tasks", [])),
        targets=(report.targets if report is not None else {}),
    )


def finalize_agent_eval_v2(
    settings: Settings,
    job_dir: str | Path,
    *,
    output_root: Path | None = None,
    finalized_at: datetime | None = None,
    database: Database | None = None,
) -> AgentEvalReportV2:
    """Validate both drafts, reveal trusted outcomes, and seal a v2 report."""

    directory = _resolve_job_dir(
        output_root or settings.agent_eval_private_root / "handoffs", job_dir
    )
    manifest, eval_input, review_input, ablation_input = _load_frozen_job(directory)
    report_path = directory / "report.json"
    receipt_path = directory / "receipt.json"
    if receipt_path.exists() or receipt_path.is_symlink():
        sealed_report, report_hash = verify_finalized_agent_eval_v2_job(
            settings, report_path, require_release_binding=False
        )
        if database is not None:
            _feedback_failed_report(
                database,
                settings,
                report=sealed_report,
                report_hash=report_hash,
            )
        return sealed_report
    baseline, baseline_raw = _load_bound_draft(
        directory,
        manifest.baseline_arm_id,
        manifest=manifest,
        eval_input=eval_input,
    )
    candidate, candidate_raw = _load_bound_draft(
        directory,
        manifest.candidate_arm_id,
        manifest=manifest,
        eval_input=eval_input,
    )
    arm_drafts = {baseline.arm_id: baseline, candidate.arm_id: candidate}
    _require_distinct_arm_producers(arm_drafts)
    review_draft, review_raw = _load_bound_review_draft(
        directory,
        manifest=manifest,
        eval_input=eval_input,
        review_input=review_input,
        arm_drafts=arm_drafts,
    )
    ablation_draft, ablation_raw = _load_bound_ablation_draft(
        directory,
        manifest=manifest,
        eval_input=eval_input,
        ablation_input=ablation_input,
        candidate=candidate,
        forbidden_producers={
            baseline.generated_by.producer,
            candidate.generated_by.producer,
            review_draft.generated_by.producer,
        },
    )
    suite = AgentEvalV2Store(settings).load(
        manifest.suite_id,
        version=manifest.suite_version,
        source=manifest.suite_source,
    )
    if suite_hash_v2(suite) != manifest.suite_hash:
        raise AgentEvalV2Error("suite changed after Agent Eval v2 prepare")
    existing_report: AgentEvalReportV2 | None = None
    if report_path.exists() or report_path.is_symlink():
        _, existing_payload = _secure_read_json(_job_file(directory, "report.json"))
        existing_report = AgentEvalReportV2.model_validate(existing_payload)
        if (
            existing_report.job_id != manifest.job_id
            or existing_report.suite_hash != manifest.suite_hash
            or existing_report.input_hash != manifest.input_hash
            or existing_report.baseline_arm_id != manifest.baseline_arm_id
            or existing_report.candidate_arm_id != manifest.candidate_arm_id
        ):
            raise AgentEvalV2Error("unsealed report does not match the frozen handoff")
    completed_at = (
        existing_report.completed_at
        if existing_report is not None
        else finalized_at or datetime.now(ZoneInfo(settings.timezone))
    )
    report = _evaluate_v2(
        suite,
        eval_input=eval_input,
        baseline=baseline,
        candidate=candidate,
        review_draft=review_draft,
        ablation_draft=ablation_draft,
        completed_at=completed_at,
    )
    report_bytes = _json_bytes(report.model_dump(mode="json"))
    _atomic_publish_immutable(report_path, report_bytes, mode=0o400)
    receipt = AgentEvalReceiptV2(
        schema_version=RECEIPT_SCHEMA_VERSION_V2,
        job_id=manifest.job_id,
        input_hash=manifest.input_hash,
        baseline_draft_hash=_sha256(baseline_raw),
        candidate_draft_hash=_sha256(candidate_raw),
        review_draft_hash=_sha256(review_raw),
        ablation_draft_hash=_sha256(ablation_raw),
        report_hash=_sha256(report_bytes),
        release_decision=report.release_decision,
        finalized_at=completed_at,
    )
    _atomic_publish_immutable(
        receipt_path,
        _json_bytes(receipt.model_dump(mode="json")),
        mode=0o400,
    )
    os.chmod(directory / manifest.baseline_arm_id / "drafts.json", 0o400)
    os.chmod(directory / manifest.candidate_arm_id / "drafts.json", 0o400)
    os.chmod(directory / "reviewer/drafts.json", 0o400)
    os.chmod(directory / "ablation/drafts.json", 0o400)
    if database is not None:
        _feedback_failed_report(
            database,
            settings,
            report=report,
            report_hash=receipt.report_hash,
        )
    return report


def verify_finalized_agent_eval_v2_job(
    settings: Settings,
    report_path: str | Path,
    *,
    require_release_binding: bool = True,
) -> tuple[AgentEvalReportV2, str]:
    """Re-verify a sealed v2 job before another workflow trusts its report.

    A report and receipt are not sufficient on their own: this verifier walks
    back through the complete private handoff, validates every frozen input and
    draft binding, enforces four independent producers, and then checks that
    the receipt seals those exact draft and report bytes.
    """

    root = settings.agent_eval_private_root / "handoffs"
    candidate = Path(report_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    if candidate.name != "report.json" or candidate.is_symlink():
        raise AgentEvalV2Error("activation requires a regular finalized report.json")
    directory = _resolve_job_dir(root, candidate.parent)
    expected_report_path = _job_file(directory, "report.json")
    if candidate.resolve(strict=True) != expected_report_path:
        raise AgentEvalV2Error("Agent Eval report must be a direct job artifact")

    manifest, eval_input, review_input, ablation_input = _load_frozen_job(directory)
    if require_release_binding:
        _validate_release_candidate_binding(settings, manifest, eval_input)
    baseline, _ = _load_bound_draft(
        directory,
        manifest.baseline_arm_id,
        manifest=manifest,
        eval_input=eval_input,
    )
    candidate_draft, _ = _load_bound_draft(
        directory,
        manifest.candidate_arm_id,
        manifest=manifest,
        eval_input=eval_input,
    )
    arm_drafts = {
        baseline.arm_id: baseline,
        candidate_draft.arm_id: candidate_draft,
    }
    _require_distinct_arm_producers(arm_drafts)
    review_draft, _ = _load_bound_review_draft(
        directory,
        manifest=manifest,
        eval_input=eval_input,
        review_input=review_input,
        arm_drafts=arm_drafts,
    )
    ablation_draft, _ = _load_bound_ablation_draft(
        directory,
        manifest=manifest,
        eval_input=eval_input,
        ablation_input=ablation_input,
        candidate=candidate_draft,
        forbidden_producers={
            baseline.generated_by.producer,
            candidate_draft.generated_by.producer,
            review_draft.generated_by.producer,
        },
    )

    suite = AgentEvalV2Store(settings).load(
        manifest.suite_id,
        version=manifest.suite_version,
        source=manifest.suite_source,
    )
    trusted_suite_hash = suite_hash_v2(suite)
    if trusted_suite_hash != manifest.suite_hash:
        raise AgentEvalV2Error("suite changed after Agent Eval v2 prepare")
    if require_release_binding and suite.synthetic:
        raise AgentEvalV2Error("release activation requires a non-synthetic private suite")
    receipt = _load_receipt(directory, manifest, report_path=expected_report_path)
    report_raw, report_payload = _secure_read_json(expected_report_path)
    report = AgentEvalReportV2.model_validate(report_payload)
    if (
        report.job_id != manifest.job_id
        or report.suite_id != manifest.suite_id
        or report.suite_version != manifest.suite_version
        or report.suite_hash != manifest.suite_hash
        or report.input_hash != manifest.input_hash
        or report.baseline_arm_id != manifest.baseline_arm_id
        or report.candidate_arm_id != manifest.candidate_arm_id
        or report.policy != suite.release_policy
        or set(report.targets) != {target.target_id for target in suite.targets}
        or report.completed_at != receipt.finalized_at
    ):
        raise AgentEvalV2Error("Agent Eval report identity does not match its frozen job")
    report_hash = _sha256(report_raw)
    if report_hash != receipt.report_hash:
        raise AgentEvalV2Error("Agent Eval report hash does not match receipt")
    recomputed = _evaluate_v2(
        suite,
        eval_input=eval_input,
        baseline=baseline,
        candidate=candidate_draft,
        review_draft=review_draft,
        ablation_draft=ablation_draft,
        completed_at=receipt.finalized_at,
    )
    if _json_bytes(recomputed.model_dump(mode="json")) != report_raw:
        raise AgentEvalV2Error(
            "Agent Eval report does not match deterministic outcome-bound recomputation"
        )
    return report, report_hash


def _validate_release_candidate_binding(
    settings: Settings,
    manifest: AgentEvalManifestV2,
    eval_input: AgentEvalInputV2,
) -> None:
    """Bind activation to host-only outcomes and the configured release candidate.

    Path separation is deterministic, but a process running as the same OS user
    can still read any files its sandbox permits. Deployments must therefore keep
    ``agent_eval_outcome_root`` outside every external draft-task mount.
    """

    if manifest.suite_source != "private":
        raise AgentEvalV2Error("release activation requires a private Agent Eval suite")
    handoff_root = settings.agent_eval_private_root.expanduser().resolve() / "handoffs"
    outcome_root = settings.agent_eval_outcome_root.expanduser()
    if outcome_root.is_symlink() or not outcome_root.is_dir():
        raise AgentEvalV2Error("host outcome root must be an existing non-symlink directory")
    resolved_outcomes = outcome_root.resolve(strict=True)
    if (
        resolved_outcomes == handoff_root
        or resolved_outcomes.is_relative_to(handoff_root)
        or handoff_root.is_relative_to(resolved_outcomes)
    ):
        raise AgentEvalV2Error("host outcome root must be disjoint from the handoff root")
    expected_candidate_hash = settings.agent_eval_release_candidate_hash
    if expected_candidate_hash is None:
        raise AgentEvalV2Error("current release candidate hash is not configured")
    candidate_hash = eval_input.arm_manifest_hashes.get(manifest.candidate_arm_id)
    if candidate_hash != expected_candidate_hash:
        raise AgentEvalV2Error("Agent Eval candidate does not match the current release")
    candidate = eval_input.arm_manifests[manifest.candidate_arm_id]
    d1_target = next(
        (target for target in candidate.targets if target.target_id == CSI1000_D1_TARGET),
        None,
    )
    if d1_target is None:
        raise AgentEvalV2Error("current release candidate does not freeze the formal D1 target")
    if d1_target.research_program.content_hash != DEFAULT_RESEARCH_PROGRAM_V2.content_hash:
        raise AgentEvalV2Error("candidate research program is not the current v2 program")


def _evaluate_v2(
    suite: AgentEvalSuiteV2,
    *,
    eval_input: AgentEvalInputV2,
    baseline: AgentEvalDraftV2,
    candidate: AgentEvalDraftV2,
    review_draft: AgentEvalReviewDraftV2,
    ablation_draft: AgentEvalAblationDraftV2,
    completed_at: datetime,
) -> AgentEvalReportV2:
    baseline_outputs = {output.episode_id: output for output in baseline.outputs}
    candidate_outputs = {output.episode_id: output for output in candidate.outputs}
    reviews = {
        (item.arm_id, item.episode_id): item.review for item in review_draft.reviews
    }
    ablations: dict[str, list[AblationOutputV2]] = {}
    for item in ablation_draft.outputs:
        ablations.setdefault(item.episode_id, []).append(item)
    target_reports: dict[str, TargetEvalReportV2] = {}
    for target in suite.targets:
        target_episodes = [
            episode for episode in suite.episodes if episode.target_id == target.target_id
        ]
        paired = [
            episode
            for episode in target_episodes
            if baseline_outputs[episode.episode_id].status == "completed"
            and candidate_outputs[episode.episode_id].status == "completed"
        ]
        baseline_metrics = _metric_aggregate(paired, baseline_outputs)
        candidate_metrics = _metric_aggregate(paired, candidate_outputs)
        hard_gates = _target_hard_gates(
            target_episodes,
            candidate_outputs,
            threshold=suite.release_policy.must_pass_rate,
        )
        hard_gate_pass = all(gate.passed for gate in hard_gates.values())
        metric_values: dict[str, float | bool | None] = {
            "brier_delta": _delta(
                candidate_metrics.mean_brier,
                baseline_metrics.mean_brier,
            ),
            "direction_drop": _delta(
                baseline_metrics.direction_accuracy,
                candidate_metrics.direction_accuracy,
            ),
            "p95_latency_ratio": _ratio(
                candidate_metrics.p95_latency_ms,
                baseline_metrics.p95_latency_ms,
            ),
            "token_ratio": _ratio(
                candidate_metrics.mean_tokens,
                baseline_metrics.mean_tokens,
            ),
        }
        metric_pass = _metric_gate_pass(metric_values, suite.release_policy)
        enough = len(paired) >= suite.release_policy.min_metric_episodes
        if not hard_gate_pass:
            decision: Literal["pass", "fail", "insufficient_sample"] = "fail"
        elif not enough:
            decision = "insufficient_sample"
        elif metric_pass:
            decision = "pass"
        else:
            decision = "fail"
        metric_values["passed"] = metric_pass if enough else None
        target_reports[target.target_id] = TargetEvalReportV2(
            target_id=target.target_id,
            horizon=target.horizon,
            release_gate=target.release_gate,
            decision=decision,
            episode_count=len(paired),
            hard_gates=hard_gates,
            hard_gate_pass=hard_gate_pass,
            metric_gates=metric_values,
            metric_gate_pass=metric_pass if enough else None,
            baseline=baseline_metrics,
            candidate=candidate_metrics,
            reasoning={
                "baseline": _reasoning_summary(
                    target_episodes,
                    {
                        episode.episode_id: reviews[
                            (baseline.arm_id, episode.episode_id)
                        ]
                        for episode in target_episodes
                    },
                ),
                "candidate": _reasoning_summary(
                    target_episodes,
                    {
                        episode.episode_id: reviews[
                            (candidate.arm_id, episode.episode_id)
                        ]
                        for episode in target_episodes
                    },
                ),
            },
            ablation=_ablation_summary(target_episodes, candidate_outputs, ablations),
        )
    gated_decisions = [
        report.decision for report in target_reports.values() if report.release_gate
    ]
    if "fail" in gated_decisions:
        release_decision: Literal["pass", "fail", "insufficient_sample"] = "fail"
    elif "insufficient_sample" in gated_decisions:
        release_decision = "insufficient_sample"
    else:
        release_decision = "pass"
    return AgentEvalReportV2(
        schema_version=REPORT_SCHEMA_VERSION_V2,
        job_id=eval_input.job_id,
        suite_id=suite.suite_id,
        suite_version=suite.version,
        suite_hash=suite_hash_v2(suite),
        input_hash=eval_input.input_hash,
        baseline_arm_id=baseline.arm_id,
        candidate_arm_id=candidate.arm_id,
        status="completed",
        release_decision=release_decision,
        evaluator_version=EVALUATOR_VERSION_V2,
        policy=suite.release_policy,
        targets=target_reports,
        completed_at=completed_at,
    )


def _feedback_failed_report(
    database: Database,
    settings: Settings,
    *,
    report: AgentEvalReportV2,
    report_hash: str,
) -> None:
    """Durably bridge release failures to the shared bad-case lifecycle."""

    if report.release_decision != "fail":
        return
    subject_id = f"eval-v2:{report.job_id}"[:64]
    recorder = TraceRecorder(database, settings)
    trace_id = recorder.trace_id_for("agent_eval", subject_id) or recorder.start_trace(
        workflow_kind="agent_eval",
        subject_id=subject_id,
        mode="private",
        input_hash=report.input_hash,
        attributes={"suite_hash": report.suite_hash, "artifact_count": 1},
        started_at=report.completed_at,
    )
    if trace_id is None:
        trace_id = _ensure_bad_case_trace_anchor(
            database,
            report=report,
            subject_id=subject_id,
        )
    recorder.link_artifact(
        workflow_kind="agent_eval",
        subject_id=subject_id,
        trace_id=trace_id,
        artifact_kind="evaluation",
        artifact_id=report.job_id,
        relation="output",
        content_hash=report_hash,
    )
    for target_id, target in report.targets.items():
        if target.decision != "fail":
            continue
        failed_gates = sorted(
            name for name, gate in target.hard_gates.items() if not gate.passed
        )
        if target.metric_gate_pass is False:
            failed_gates.append("metric_gate")
        gate_identity = ",".join(failed_gates) or "target_release_gate"
        bad_case = create_bad_case(
            database,
            settings,
            BadCaseCreate(
                trace_id=trace_id,
                issue_type="agent_eval_v2_gate",
                severity="high",
                title=f"Agent Eval v2 failed for {target_id}",
                summary=(
                    f"Candidate {report.candidate_arm_id} failed: {gate_identity}. "
                    f"Report hash: {report_hash}."
                ),
                expected_behavior="Every release-gated target and must-pass gate succeeds.",
                input_hash=report.input_hash,
            ),
            actor="agent-eval-v2-finalize",
            idempotency_key=canonical_digest(
                {
                    "job_id": report.job_id,
                    "target_id": target_id,
                    "failed_gates": failed_gates,
                    "report_hash": report_hash,
                }
            ),
        )
        recorder.link_artifact(
            workflow_kind="agent_eval",
            subject_id=subject_id,
            trace_id=trace_id,
            artifact_kind="bad_case",
            artifact_id=bad_case.id,
            relation="diagnostic",
        )
    _seal_bad_case_trace_anchor(database, trace_id=trace_id, completed_at=report.completed_at)


def _ensure_bad_case_trace_anchor(
    database: Database,
    *,
    report: AgentEvalReportV2,
    subject_id: str,
) -> str:
    """Create the schema-required trace anchor independently of optional telemetry."""

    with database.session_factory() as session:
        existing = session.scalar(
            select(AgentTrace)
            .where(
                AgentTrace.workflow_kind == "agent_eval",
                AgentTrace.subject_id == subject_id,
            )
            .order_by(AgentTrace.attempt_number.desc())
        )
        if existing is not None:
            return existing.id
        trace_id = secrets.token_hex(16)
        session.add(
            AgentTrace(
                id=trace_id,
                workflow_kind="agent_eval",
                subject_id=subject_id,
                attempt_number=1,
                target_id=None,
                horizon=None,
                mode="private",
                status="running",
                started_at=report.completed_at,
                completed_at=None,
                input_hash=report.input_hash,
                trace_policy_version=TRACE_POLICY_VERSION,
                telemetry_complete=False,
                error_code=None,
                error_summary=None,
                attributes={"suite_hash": report.suite_hash, "artifact_count": 1},
            )
        )
        session.commit()
        return trace_id


def _seal_bad_case_trace_anchor(
    database: Database,
    *,
    trace_id: str,
    completed_at: datetime,
) -> None:
    with database.session_factory() as session:
        trace = session.get(AgentTrace, trace_id)
        if trace is None or trace.status != "running":
            return
        trace.status = "completed"
        trace.completed_at = completed_at
        session.commit()


def _metric_aggregate(
    episodes: list[EvalEpisodeV2],
    outputs: dict[str, DraftEpisodeOutputV2],
) -> MetricAggregateV2:
    briers: list[float] = []
    directions: list[float] = []
    latencies: list[float] = []
    tokens: list[float] = []
    for episode in episodes:
        output = outputs[episode.episode_id]
        assert output.probabilities is not None
        briers.append(_brier(output.probabilities, episode.outcome.label))
        directions.append(float(_direction(output.probabilities) == episode.outcome.label))
        latencies.append(output.latency_ms)
        tokens.append(float(output.total_tokens))
    return MetricAggregateV2(
        episode_count=len(episodes),
        direction_accuracy=_mean(directions),
        mean_brier=_mean(briers),
        p95_latency_ms=_percentile(latencies, 0.95),
        mean_tokens=_mean(tokens),
    )


def _target_hard_gates(
    episodes: list[EvalEpisodeV2],
    outputs: dict[str, DraftEpisodeOutputV2],
    *,
    threshold: float,
) -> dict[str, HardGateResultV2]:
    values: dict[str, list[bool]] = {
        "schema_valid": [],
        "cutoff_valid": [],
        "citation_valid": [],
        "trace_valid": [],
        "must_pass_bad_case": [],
    }
    for episode in episodes:
        output = outputs[episode.episode_id]
        evidence = {item.evidence_id: item for item in episode.evidence}
        cited = [item.evidence_id for item in output.citations]
        citation_valid = (
            all(evidence_id in evidence for evidence_id in cited)
            and (not evidence or bool(cited))
        )
        cutoff_valid = citation_valid and all(
            evidence[evidence_id].observed_at <= episode.evidence_cutoff for evidence_id in cited
        )
        trace_valid = output.trajectory == episode.expected_trajectory
        values["schema_valid"].append(output.status == "completed")
        values["cutoff_valid"].append(cutoff_valid)
        values["citation_valid"].append(citation_valid)
        values["trace_valid"].append(trace_valid)
        values["must_pass_bad_case"].append(
            not episode.must_pass
            or _must_pass_invariant_satisfied(
                episode,
                output,
                citation_valid=citation_valid,
                cutoff_valid=cutoff_valid,
                trace_valid=trace_valid,
            )
        )
    return {
        name: _hard_gate(values_for_gate, threshold=threshold)
        for name, values_for_gate in values.items()
    }


def _must_pass_invariant_satisfied(
    episode: EvalEpisodeV2,
    output: DraftEpisodeOutputV2,
    *,
    citation_valid: bool,
    cutoff_valid: bool,
    trace_valid: bool,
) -> bool:
    invariant = episode.must_pass_invariant
    if (
        invariant is None
        or output.status != "completed"
        or output.probabilities is None
        or not citation_valid
        or not cutoff_valid
        or not trace_valid
    ):
        return False
    if (
        invariant.expected_direction is not None
        and _direction(output.probabilities) != invariant.expected_direction
    ):
        return False
    probabilities = output.probabilities.model_dump()
    if any(
        not (bounds.minimum <= probabilities[label] <= bounds.maximum)
        for label, bounds in invariant.probability_bounds.items()
    ):
        return False
    cited_ids = {citation.evidence_id for citation in output.citations}
    return set(invariant.required_evidence_ids).issubset(cited_ids)


def _hard_gate(values: list[bool], *, threshold: float) -> HardGateResultV2:
    passed_count = sum(values)
    rate = passed_count / len(values) if values else 1.0
    return HardGateResultV2(
        eligible_count=len(values),
        passed_count=passed_count,
        rate=rate,
        passed=rate >= threshold,
    )


def _metric_gate_pass(values: dict[str, float | bool | None], policy: ReleasePolicyV2) -> bool:
    return bool(
        values["brier_delta"] is not None
        and float(values["brier_delta"]) <= policy.max_brier_delta
        and values["direction_drop"] is not None
        and float(values["direction_drop"]) <= policy.max_direction_drop
        and values["p95_latency_ratio"] is not None
        and float(values["p95_latency_ratio"]) <= policy.max_p95_latency_ratio
        and values["token_ratio"] is not None
        and float(values["token_ratio"]) <= policy.max_token_ratio
    )


def _reasoning_summary(
    episodes: list[EvalEpisodeV2],
    reviews_by_episode: dict[str, ReasoningReviewV2],
) -> ReasoningSummaryV2:
    reviews: list[ReasoningReviewV2] = []
    for episode in episodes:
        review = reviews_by_episode[episode.episode_id]
        if review.review_input_hash != episode.input_hash:
            raise AgentEvalV2Error(
                f"reasoning review input hash mismatch for {episode.episode_id}"
            )
        reviews.append(review)
    rubric_means = {
        dimension: _mean([float(getattr(review, dimension)) for review in reviews])
        for dimension in REASONING_DIMENSIONS
    }
    totals = [
        sum(getattr(review, dimension) for dimension in REASONING_DIMENSIONS)
        for review in reviews
    ]
    return ReasoningSummaryV2(
        review_count=len(reviews),
        mean_total_score=_mean([float(total) for total in totals]),
        rubric_means=rubric_means,
        rule_pass_rate=_mean([float(review.rule_passed) for review in reviews]),
        # Kept in the report schema for backwards compatibility. Human findings
        # are governed outside the outcome-blind LLM draft boundary and therefore
        # can never be asserted by an Agent Eval reviewer draft.
        human_confirmed_severe_count=0,
    )


def _ablation_summary(
    episodes: list[EvalEpisodeV2],
    outputs: dict[str, DraftEpisodeOutputV2],
    ablations: dict[str, list[AblationOutputV2]],
) -> list[AblationSummaryV2]:
    rows: dict[str, list[tuple[float, float]]] = {}
    for episode in episodes:
        output = outputs[episode.episode_id]
        if output.status != "completed" or output.probabilities is None:
            continue
        full = _brier(output.probabilities, episode.outcome.label)
        for ablation in ablations.get(episode.episode_id, []):
            if ablation.status != "completed" or ablation.probabilities is None:
                continue
            rows.setdefault(ablation.agent_id, []).append(
                (full, _brier(ablation.probabilities, episode.outcome.label))
            )
    return [
        AblationSummaryV2(
            agent_id=agent_id,
            episode_count=len(values),
            mean_full_brier=_mean([full for full, _ in values]),
            mean_ablated_brier=_mean([ablated for _, ablated in values]),
            mean_incremental_brier=_mean([ablated - full for full, ablated in values]),
        )
        for agent_id, values in sorted(rows.items())
    ]


def _load_frozen_job(
    directory: Path,
) -> tuple[
    AgentEvalManifestV2,
    AgentEvalInputV2,
    AgentEvalReviewInputV2,
    AgentEvalAblationInputV2,
]:
    manifest_raw, manifest_payload = _secure_read_json(_job_file(directory, "manifest.json"))
    del manifest_raw
    manifest = AgentEvalManifestV2.model_validate(manifest_payload)
    if manifest.job_id != directory.name:
        raise AgentEvalV2Error("manifest job_id does not match its directory")
    input_raw, input_payload = _secure_read_json(_job_file(directory, "input.json"))
    eval_input = AgentEvalInputV2.model_validate(input_payload)
    if _sha256(input_raw) != manifest.input_file_hash:
        raise AgentEvalV2Error("input.json changed after prepare")
    if (
        eval_input.job_id != manifest.job_id
        or eval_input.suite_id != manifest.suite_id
        or eval_input.suite_version != manifest.suite_version
        or eval_input.suite_hash != manifest.suite_hash
        or eval_input.input_hash != manifest.input_hash
        or eval_input.baseline_arm_id != manifest.baseline_arm_id
        or eval_input.candidate_arm_id != manifest.candidate_arm_id
    ):
        raise AgentEvalV2Error("input.json does not match its handoff manifest")
    review_raw, review_payload = _secure_read_json(
        _job_file(directory, "reviewer/input.json")
    )
    review_input = AgentEvalReviewInputV2.model_validate(review_payload)
    if (
        _sha256(review_raw) != manifest.review_input_file_hash
        or review_input.input_hash != manifest.review_input_hash
        or review_input.job_id != manifest.job_id
        or review_input.suite_hash != manifest.suite_hash
        or review_input.eval_input_hash != manifest.input_hash
        or review_input.arm_manifest_hashes != eval_input.arm_manifest_hashes
    ):
        raise AgentEvalV2Error("reviewer/input.json does not match its handoff manifest")
    ablation_raw, ablation_payload = _secure_read_json(
        _job_file(directory, "ablation/input.json")
    )
    ablation_input = AgentEvalAblationInputV2.model_validate(ablation_payload)
    if (
        _sha256(ablation_raw) != manifest.ablation_input_file_hash
        or ablation_input.input_hash != manifest.ablation_input_hash
        or ablation_input.job_id != manifest.job_id
        or ablation_input.suite_hash != manifest.suite_hash
        or ablation_input.eval_input_hash != manifest.input_hash
        or ablation_input.candidate_arm_id != manifest.candidate_arm_id
        or ablation_input.candidate_manifest_hash
        != eval_input.arm_manifest_hashes[manifest.candidate_arm_id]
    ):
        raise AgentEvalV2Error("ablation/input.json does not match its handoff manifest")
    return manifest, eval_input, review_input, ablation_input


def _load_bound_draft(
    directory: Path,
    arm_id: str,
    *,
    manifest: AgentEvalManifestV2,
    eval_input: AgentEvalInputV2,
) -> tuple[AgentEvalDraftV2, bytes]:
    raw, payload = _secure_read_json(_job_file(directory, f"{arm_id}/drafts.json"))
    draft = AgentEvalDraftV2.model_validate(payload)
    expected_arm_hash = eval_input.arm_manifest_hashes.get(arm_id)
    if (
        draft.job_id != manifest.job_id
        or draft.arm_id != arm_id
        or draft.suite_hash != manifest.suite_hash
        or draft.input_hash != manifest.input_hash
        or draft.arm_manifest_hash != expected_arm_hash
    ):
        raise AgentEvalV2Error(f"{arm_id} drafts.json does not match frozen bindings")
    expected_outputs = {
        (episode.episode_id, episode.target_id) for episode in eval_input.episodes
    }
    actual_outputs = {(output.episode_id, output.target_id) for output in draft.outputs}
    if actual_outputs != expected_outputs:
        raise AgentEvalV2Error(f"{arm_id} drafts.json must cover every episode exactly once")
    return draft, raw


def _require_distinct_arm_producers(arm_drafts: dict[str, AgentEvalDraftV2]) -> None:
    """Reject duplicate producer claims; this is not a task-identity proof."""

    producers = [item.generated_by.producer for item in arm_drafts.values()]
    if len(producers) != len(set(producers)):
        raise AgentEvalV2Error("baseline and candidate drafts require independent tasks")


def _load_bound_review_draft(
    directory: Path,
    *,
    manifest: AgentEvalManifestV2,
    eval_input: AgentEvalInputV2,
    review_input: AgentEvalReviewInputV2,
    arm_drafts: dict[str, AgentEvalDraftV2],
) -> tuple[AgentEvalReviewDraftV2, bytes]:
    raw, payload = _secure_read_json(_job_file(directory, "reviewer/drafts.json"))
    if _contains_reserved_outcome_key(payload):
        raise AgentEvalV2Error("reasoning review draft must not contain realized outcomes")
    draft = AgentEvalReviewDraftV2.model_validate(payload)
    if (
        draft.job_id != manifest.job_id
        or draft.suite_hash != manifest.suite_hash
        or draft.eval_input_hash != manifest.input_hash
        or draft.review_input_hash != review_input.input_hash
    ):
        raise AgentEvalV2Error("reviewer drafts.json does not match frozen bindings")
    arm_producers = {item.generated_by.producer for item in arm_drafts.values()}
    if draft.generated_by.producer in arm_producers:
        raise AgentEvalV2Error("reasoning review must be produced by an independent task")
    expected = {
        (arm_id, episode.episode_id, episode.target_id)
        for arm_id in review_input.arm_ids
        for episode in eval_input.episodes
    }
    actual = {(item.arm_id, item.episode_id, item.target_id) for item in draft.reviews}
    if actual != expected:
        raise AgentEvalV2Error("reviewer drafts.json must review every arm episode exactly once")
    episode_inputs = {item.episode_id: item for item in eval_input.episodes}
    arm_outputs = {
        arm_id: {item.episode_id: item for item in arm_draft.outputs}
        for arm_id, arm_draft in arm_drafts.items()
    }
    for item in draft.reviews:
        if item.review.review_input_hash != episode_inputs[item.episode_id].input_hash:
            raise AgentEvalV2Error(
                f"reasoning review input hash mismatch for {item.episode_id}"
            )
        expected_hash = _draft_output_hash(arm_outputs[item.arm_id][item.episode_id])
        if item.reviewed_output_hash != expected_hash:
            raise AgentEvalV2Error(
                f"reasoning review output hash mismatch for {item.arm_id}/{item.episode_id}"
            )
    return draft, raw


def _load_bound_ablation_draft(
    directory: Path,
    *,
    manifest: AgentEvalManifestV2,
    eval_input: AgentEvalInputV2,
    ablation_input: AgentEvalAblationInputV2,
    candidate: AgentEvalDraftV2,
    forbidden_producers: set[str],
) -> tuple[AgentEvalAblationDraftV2, bytes]:
    raw, payload = _secure_read_json(_job_file(directory, "ablation/drafts.json"))
    if _contains_reserved_outcome_key(payload):
        raise AgentEvalV2Error("ablation draft must not contain realized outcomes")
    draft = AgentEvalAblationDraftV2.model_validate(payload)
    if (
        draft.job_id != manifest.job_id
        or draft.suite_hash != manifest.suite_hash
        or draft.eval_input_hash != manifest.input_hash
        or draft.ablation_input_hash != ablation_input.input_hash
        or draft.candidate_arm_id != manifest.candidate_arm_id
    ):
        raise AgentEvalV2Error("ablation drafts.json does not match frozen bindings")
    if draft.generated_by.producer in forbidden_producers:
        raise AgentEvalV2Error("ablation must be produced by an independent task")
    assignments = {item.ablation_id: item for item in ablation_input.assignments}
    candidate_outputs = {item.episode_id: item for item in candidate.outputs}
    expected = {
        (assignment.ablation_id, episode.episode_id)
        for assignment in ablation_input.assignments
        for episode in eval_input.episodes
        if episode.target_id == assignment.target_id
    }
    actual = {(item.ablation_id, item.episode_id) for item in draft.outputs}
    if actual != expected:
        raise AgentEvalV2Error(
            "ablation drafts.json must cover every frozen no-impact assignment"
        )
    for item in draft.outputs:
        assignment = assignments.get(item.ablation_id)
        episode = candidate_outputs.get(item.episode_id)
        if (
            assignment is None
            or episode is None
            or item.target_id != assignment.target_id
            or item.agent_id != assignment.agent_id
            or item.replacement != assignment.override.replacement
            or item.ablation_input_hash != ablation_input.input_hash
        ):
            raise AgentEvalV2Error("ablation output does not match frozen no-impact override")
        if item.full_output_hash != _draft_output_hash(episode):
            raise AgentEvalV2Error(
                f"ablation full output hash mismatch for {item.episode_id}"
            )
    return draft, raw


def _load_receipt(
    directory: Path,
    manifest: AgentEvalManifestV2,
    *,
    report_path: Path,
) -> AgentEvalReceiptV2:
    _, payload = _secure_read_json(_job_file(directory, "receipt.json"))
    receipt = AgentEvalReceiptV2.model_validate(payload)
    if receipt.job_id != manifest.job_id or receipt.input_hash != manifest.input_hash:
        raise AgentEvalV2Error("receipt does not match frozen Agent Eval input")
    if not report_path.exists() or report_path.is_symlink():
        raise AgentEvalV2Error("finalized Agent Eval report is missing or symlinked")
    report_raw, report_payload = _secure_read_json(_job_file(directory, "report.json"))
    report = AgentEvalReportV2.model_validate(report_payload)
    if _sha256(report_raw) != receipt.report_hash:
        raise AgentEvalV2Error("Agent Eval report hash does not match receipt")
    if report.release_decision != receipt.release_decision:
        raise AgentEvalV2Error("Agent Eval report decision does not match receipt")
    baseline_raw, _ = _secure_read_json(
        _job_file(directory, f"{manifest.baseline_arm_id}/drafts.json")
    )
    candidate_raw, _ = _secure_read_json(
        _job_file(directory, f"{manifest.candidate_arm_id}/drafts.json")
    )
    if _sha256(baseline_raw) != receipt.baseline_draft_hash:
        raise AgentEvalV2Error("baseline draft hash does not match receipt")
    if _sha256(candidate_raw) != receipt.candidate_draft_hash:
        raise AgentEvalV2Error("candidate draft hash does not match receipt")
    review_raw, _ = _secure_read_json(_job_file(directory, "reviewer/drafts.json"))
    ablation_raw, _ = _secure_read_json(_job_file(directory, "ablation/drafts.json"))
    if _sha256(review_raw) != receipt.review_draft_hash:
        raise AgentEvalV2Error("reasoning review draft hash does not match receipt")
    if _sha256(ablation_raw) != receipt.ablation_draft_hash:
        raise AgentEvalV2Error("ablation draft hash does not match receipt")
    return receipt


def _outcome_free_episode(episode: EvalEpisodeV2) -> dict[str, Any]:
    return EvalEpisodeInputV2(
        episode_id=episode.episode_id,
        target_id=episode.target_id,
        independence_key=episode.independence_key,
        anchor_date=episode.anchor_date,
        target_date=episode.target_date,
        evidence_cutoff=episode.evidence_cutoff,
        input_payload=episode.input_payload,
        input_hash=episode.input_hash,
        evidence=episode.evidence,
        expected_trajectory=episode.expected_trajectory,
        must_pass=episode.must_pass,
        must_pass_invariant=episode.must_pass_invariant,
    ).model_dump(mode="json")


def _instruction_text(baseline_arm_id: str, candidate_arm_id: str) -> str:
    return f"""# Agent Eval v2 external draft boundary

Read only `input.json` and the selected arm's `drafts.template.json`.
Write exactly one file for each independent execution:

- `{baseline_arm_id}/drafts.json`
- `{candidate_arm_id}/drafts.json`

After both arms exist, run two additional independent, outcome-blind tasks:

- `reviewer/drafts.json` reads `reviewer/input.json` plus both arm drafts and
  reviews their structured reasoning using exactly `gpt-5.6-sol / high`.
- `ablation/drafts.json` reads `ablation/input.json` plus the candidate draft
  and independently reruns every frozen candidate episode with each listed
  Agent replaced by the explicit `no_impact` override.

Each task must use a different producer ID. Do not edit `input.json`, either
task input, `manifest.json`, templates, `report.json`, or `receipt.json`.
Outcomes are intentionally absent and are revealed only by the deterministic
finalize command after all four drafts validate.
"""


def _ablation_assignments(candidate: EvalArmV2) -> list[AblationAssignmentV2]:
    assignments: list[AblationAssignmentV2] = []
    for target in candidate.targets:
        for agent_id, agent in sorted(target.agents.items()):
            prompt = target.prompts[agent_id]
            body = {
                "ablation_id": f"{target.target_id}:{agent_id}:no-impact",
                "target_id": target.target_id,
                "agent_id": agent_id,
                "agent": agent.model_dump(mode="json"),
                "prompt": prompt.model_dump(mode="json"),
                "override": NoImpactOverrideV2().model_dump(mode="json"),
            }
            assignments.append(
                AblationAssignmentV2.model_validate(
                    {**body, "assignment_hash": canonical_digest(body)}
                )
            )
    return assignments


def _draft_output_hash(output: DraftEpisodeOutputV2) -> str:
    """Bind downstream work to the immutable full arm output, not self-reporting."""

    return canonical_digest(output.model_dump(mode="json"))


def _model_hash_without(model: BaseModel, excluded: str) -> str:
    payload = model.model_dump(mode="json")
    payload.pop(excluded, None)
    return canonical_digest(payload)


def _brier(probabilities: ProbabilitiesV2, label: str) -> float:
    values = probabilities.model_dump()
    return (
        sum((float(values[item]) - float(item == label)) ** 2 for item in LABELS)
        / len(LABELS)
    )


def _direction(probabilities: ProbabilitiesV2) -> str:
    values = probabilities.model_dump()
    return max(LABELS, key=lambda label: float(values[label]))


def _contains_reserved_outcome_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower() in _RESERVED_OUTCOME_KEYS
            or _contains_reserved_outcome_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_reserved_outcome_key(item) for item in value)
    return False


def _validate_component(value: str, label: str) -> None:
    if not value or value in {".", ".."} or any(character in value for character in "/\\\0"):
        raise AgentEvalV2Error(f"invalid {label}")


def _prepare_root(root: Path) -> Path:
    candidate = root.expanduser()
    candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
    if candidate.is_symlink():
        raise AgentEvalV2Error("Agent Eval handoff root may not be a symlink")
    resolved = candidate.resolve()
    os.chmod(resolved, 0o700)
    return resolved


def _resolve_job_dir(root: Path, job_dir: str | Path) -> Path:
    resolved_root = _prepare_root(root)
    candidate = Path(job_dir).expanduser()
    if not candidate.is_absolute():
        candidate = resolved_root / candidate
    try:
        UUID(candidate.name)
    except ValueError as exc:
        raise AgentEvalV2Error("Agent Eval job directory name must be a UUID") from exc
    if candidate.is_symlink() or candidate.parent.resolve() != resolved_root:
        raise AgentEvalV2Error("Agent Eval job must be a direct, non-symlink child of its root")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_dir() or resolved.parent != resolved_root:
        raise AgentEvalV2Error("invalid Agent Eval job directory")
    return resolved


def _job_file(directory: Path, relative: str) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise AgentEvalV2Error("Agent Eval file path must remain inside its job")
    candidate = directory / relative_path
    current = directory
    for part in relative_path.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise AgentEvalV2Error("Agent Eval path component may not be a symlink")
    if candidate.is_symlink():
        raise AgentEvalV2Error("Agent Eval file may not be a symlink")
    try:
        candidate.resolve(strict=True).relative_to(directory)
    except (FileNotFoundError, ValueError) as exc:
        raise AgentEvalV2Error("Agent Eval file is missing or escaped its job") from exc
    return candidate


def _secure_read_json(path: Path) -> tuple[bytes, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AgentEvalV2Error(f"cannot open Agent Eval file: {path.name}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AgentEvalV2Error("Agent Eval JSON artifact must be a regular file")
        if metadata.st_size <= 0 or metadata.st_size > MAX_JSON_BYTES:
            raise AgentEvalV2Error("Agent Eval JSON artifact has an invalid size")
        raw = b""
        while len(raw) < metadata.st_size:
            chunk = os.read(descriptor, metadata.st_size - len(raw))
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AgentEvalV2Error(f"invalid Agent Eval JSON: {path.name}") from exc
    return raw, payload


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode(
            "utf-8"
        )
        + b"\n"
    )


def _atomic_write_new(path: Path, payload: bytes, *, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    os.chmod(path, mode)


def _atomic_publish_immutable(path: Path, payload: bytes, *, mode: int) -> None:
    if path.exists() or path.is_symlink():
        existing, _ = _secure_read_json(path)
        if existing != payload:
            raise AgentEvalV2Error(
                f"Agent Eval output already exists with different bytes: {path.name}"
            )
        return
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        _atomic_write_new(temporary, payload, mode=0o600)
        os.link(temporary, path)
        os.chmod(path, mode)
    except FileExistsError:
        existing, _ = _secure_read_json(path)
        if existing != payload:
            raise AgentEvalV2Error(
                f"concurrent Agent Eval output conflict: {path.name}"
            ) from None
    finally:
        if temporary.exists():
            temporary.unlink()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * quantile + 0.999999)))
    return ordered[index]


def _delta(left: float | None, right: float | None) -> float | None:
    return None if left is None or right is None else left - right


def _ratio(left: float | None, right: float | None) -> float | None:
    if left is None or right in {None, 0}:
        return None
    return left / right
