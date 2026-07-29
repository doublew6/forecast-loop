"""Deterministic, cross-source benchmark fixture and golden report support."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from ..agent_contracts import (
    AgentSpec,
    ParticipationMode,
    ProbabilityMode,
    SignalProbabilityVector,
)
from ..domain import (
    AgentSourceType,
    Direction,
    Horizon,
    classify_return,
    multiclass_brier_score,
    predicted_direction,
)

BENCHMARK_FIXTURE_SCHEMA = "forecast-loop.benchmark-fixture/v1"
BENCHMARK_MANIFEST_SCHEMA = "forecast-loop.benchmark-manifest/v1"
BENCHMARK_REPORT_SCHEMA = "forecast-loop.benchmark-report/v1"
BENCHMARK_GOLDEN_SCHEMA = "forecast-loop.benchmark-golden/v1"
BENCHMARK_POLICY_SCHEMA = "forecast-loop.benchmark-policy/v1"
BELIEVABILITY_WEIGHT_SCHEMA = "forecast-loop.benchmark-believability-weights/v1"
HASH_PATTERN = r"^[0-9a-f]{64}$"
DEFAULT_BENCHMARK_ROOT = Path("benchmarks/cross-source-v1")
MAX_JSON_BYTES = 4 * 1024 * 1024


class BenchmarkError(ValueError):
    """The benchmark bundle or its deterministic report is invalid."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BenchmarkEvaluationPolicy(_StrictModel):
    schema_version: Literal["forecast-loop.benchmark-policy/v1"] = (
        BENCHMARK_POLICY_SCHEMA
    )
    direction_label_basis: Literal["thresholded_three_class"] = (
        "thresholded_three_class"
    )
    sign_direction_eligibility: Literal["nonzero_return"] = "nonzero_return"
    material_move_rule: Literal["abs_return_strictly_greater_than_threshold"] = (
        "abs_return_strictly_greater_than_threshold"
    )
    aggregate_method: Literal["target_date_macro_average"] = (
        "target_date_macro_average"
    )
    brier_formula: Literal["mean_squared_error_over_3"] = (
        "mean_squared_error_over_3"
    )
    brier_range: tuple[Literal[0], Literal["2/3"]] = (0, "2/3")
    calibration_method: Literal["three_class_classwise_ece"] = (
        "three_class_classwise_ece"
    )
    calibration_bin_edges: tuple[float, ...]

    @field_validator("calibration_bin_edges")
    @classmethod
    def calibration_edges_are_complete(
        cls,
        values: tuple[float, ...],
    ) -> tuple[float, ...]:
        if len(values) < 3:
            raise ValueError("calibration requires at least two bins")
        if values[0] != 0 or values[-1] != 1:
            raise ValueError("calibration bin edges must start at 0 and end at 1")
        if any(
            not math.isfinite(value) or value < 0 or value > 1
            for value in values
        ):
            raise ValueError("calibration bin edges must be finite values in [0, 1]")
        if any(
            left >= right
            for left, right in zip(values, values[1:], strict=False)
        ):
            raise ValueError("calibration bin edges must be strictly increasing")
        return values


class BenchmarkParticipant(_StrictModel):
    agent_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    agent_version: str = Field(min_length=1, max_length=32)
    source_type: AgentSourceType
    probability_mode: ProbabilityMode
    evaluation_metrics: tuple[
        Literal["direction", "multiclass_brier", "calibration"],
        ...,
    ]

    @model_validator(mode="after")
    def supported_probability_mode(self) -> BenchmarkParticipant:
        if self.probability_mode not in {
            ProbabilityMode.CONFIDENCE,
            ProbabilityMode.MULTICLASS,
        }:
            raise ValueError("benchmark participants must submit a direction")
        if (
            self.source_type is AgentSourceType.MANUAL
            and self.probability_mode is not ProbabilityMode.CONFIDENCE
        ):
            raise ValueError("manual benchmark fixture must remain confidence-only")
        metrics = set(self.evaluation_metrics)
        if len(metrics) != len(self.evaluation_metrics):
            raise ValueError("participant evaluation_metrics must be unique")
        expected = (
            {"direction"}
            if self.probability_mode is ProbabilityMode.CONFIDENCE
            else {"direction", "multiclass_brier", "calibration"}
        )
        if metrics != expected:
            raise ValueError(
                "evaluation metrics must exactly match the frozen probability capability"
            )
        return self


class BenchmarkSignal(_StrictModel):
    status: Literal["submitted", "failed", "missing"]
    direction: Literal[Direction.UP, Direction.DOWN] | None = None
    confidence: float | None = Field(default=None, ge=0.5, le=1, allow_inf_nan=False)
    probabilities: SignalProbabilityVector | None = None
    failure_code: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def status_fields_are_consistent(self) -> BenchmarkSignal:
        if self.status == "submitted":
            if self.direction is None:
                raise ValueError("submitted benchmark signal requires direction")
            if self.failure_code is not None:
                raise ValueError("submitted benchmark signal cannot contain failure_code")
            return self
        if self.direction is not None or self.confidence is not None:
            raise ValueError("unsubmitted benchmark signal cannot contain a prediction")
        if self.probabilities is not None:
            raise ValueError("unsubmitted benchmark signal cannot contain probabilities")
        if self.status == "failed" and self.failure_code is None:
            raise ValueError("failed benchmark signal requires failure_code")
        if self.status == "missing" and self.failure_code is not None:
            raise ValueError("missing benchmark signal cannot contain failure_code")
        return self


class BenchmarkOpportunity(_StrictModel):
    target_date: date
    index_code: str = Field(min_length=1, max_length=32)
    horizon: Horizon
    actual_return: float = Field(allow_inf_nan=False)
    neutral_threshold: float = Field(gt=0, allow_inf_nan=False)
    actual_label: Direction
    material_move: bool
    signals: dict[str, BenchmarkSignal]

    @model_validator(mode="after")
    def actual_label_matches_threshold(self) -> BenchmarkOpportunity:
        expected = classify_return(self.actual_return, self.neutral_threshold)
        if self.actual_label is not expected:
            raise ValueError(
                "actual_label must be derived from actual_return and neutral_threshold"
            )
        return self


class BenchmarkCommittee(_StrictModel):
    committee_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    kind: Literal["fixed", "equal_weight_baseline", "candidate_believability"]
    member_weights: dict[str, float]
    minimum_members: int = Field(ge=1)
    weight_origin: Literal[
        "frozen_policy",
        "equal_by_definition",
        "predeclared_out_of_sample",
    ]
    fitted_on_fixture_outcomes: Literal[False] = False
    weights_effective_at: date | None = None
    weights_trained_through: date | None = None
    weights_source_hash: str | None = Field(default=None, pattern=HASH_PATTERN)

    @field_validator("member_weights")
    @classmethod
    def weights_are_positive(cls, values: dict[str, float]) -> dict[str, float]:
        if not values:
            raise ValueError("committee requires member_weights")
        if any(
            not math.isfinite(value) or value <= 0
            for value in values.values()
        ):
            raise ValueError("committee weights must be finite and positive")
        if not math.isclose(sum(values.values()), 1.0, abs_tol=1e-9):
            raise ValueError("committee weights must sum to one")
        return values

    @model_validator(mode="after")
    def kind_matches_weight_origin(self) -> BenchmarkCommittee:
        expected_origin = {
            "fixed": "frozen_policy",
            "equal_weight_baseline": "equal_by_definition",
            "candidate_believability": "predeclared_out_of_sample",
        }[self.kind]
        if self.weight_origin != expected_origin:
            raise ValueError("committee kind and weight_origin do not match")
        if self.kind == "equal_weight_baseline":
            weights = tuple(self.member_weights.values())
            if not all(math.isclose(value, weights[0]) for value in weights):
                raise ValueError("equal-weight baseline must use equal fixture weights")
        candidate_fields = (
            self.weights_effective_at,
            self.weights_trained_through,
            self.weights_source_hash,
        )
        if self.kind == "candidate_believability":
            if any(value is None for value in candidate_fields):
                raise ValueError(
                    "candidate weights require effective_at, trained_through and source_hash"
                )
            if self.weights_source_hash == "0" * 64:
                raise ValueError("candidate weights_source_hash cannot be a placeholder")
        elif any(value is not None for value in candidate_fields):
            raise ValueError("only candidate committee declares weight training metadata")
        return self


class BenchmarkFixtureBody(_StrictModel):
    schema_version: Literal["forecast-loop.benchmark-fixture/v1"] = (
        BENCHMARK_FIXTURE_SCHEMA
    )
    benchmark_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    fixture_version: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=200)
    redistribution_license: Literal["CC0-1.0"] = "CC0-1.0"
    synthetic_data: Literal[True] = True
    evaluation_policy: BenchmarkEvaluationPolicy
    participants: tuple[BenchmarkParticipant, ...]
    committees: tuple[BenchmarkCommittee, ...]
    opportunities: tuple[BenchmarkOpportunity, ...]

    @model_validator(mode="after")
    def fixture_is_cross_source_and_out_of_sample(self) -> BenchmarkFixtureBody:
        participant_by_id = {item.agent_id: item for item in self.participants}
        if len(participant_by_id) != len(self.participants):
            raise ValueError("benchmark participant ids must be unique")
        if {item.source_type for item in self.participants} != set(AgentSourceType):
            raise ValueError(
                "benchmark must contain manual, AI, quant and deterministic participants"
            )
        committee_by_id = {item.committee_id: item for item in self.committees}
        if len(committee_by_id) != len(self.committees):
            raise ValueError("benchmark committee ids must be unique")
        if {item.kind for item in self.committees} != {
            "fixed",
            "equal_weight_baseline",
            "candidate_believability",
        }:
            raise ValueError("benchmark must define all three committee variants")
        rosters = {frozenset(item.member_weights) for item in self.committees}
        if len(rosters) != 1:
            raise ValueError("all benchmark committees must use the same frozen roster")
        keys = [
            (item.target_date, item.index_code, item.horizon)
            for item in self.opportunities
        ]
        if len(keys) != len(set(keys)):
            raise ValueError(
                "target_date, index_code and horizon opportunities must be unique"
            )
        if len({item.target_date for item in self.opportunities}) < 2:
            raise ValueError("benchmark requires multiple independent target dates")
        evaluation_window_start = min(item.target_date for item in self.opportunities)

        participant_ids = set(participant_by_id)
        for committee in self.committees:
            if not set(committee.member_weights).issubset(participant_ids):
                raise ValueError("committee references an unknown participant")
            if committee.minimum_members > len(committee.member_weights):
                raise ValueError("committee minimum_members exceeds its roster")
            if committee.minimum_members != len(committee.member_weights):
                raise ValueError(
                    "committee requires its complete roster; availability renormalization "
                    "is prohibited"
                )
            for member_id in committee.member_weights:
                if (
                    participant_by_id[member_id].probability_mode
                    is not ProbabilityMode.MULTICLASS
                ):
                    raise ValueError(
                        "committee aggregation requires multiclass member signals"
                    )
            if committee.kind == "candidate_believability":
                if (
                    committee.weights_trained_through is None
                    or committee.weights_trained_through >= evaluation_window_start
                ):
                    raise ValueError(
                        "candidate weights must be trained before the evaluation window"
                    )
                if (
                    committee.weights_effective_at is None
                    or committee.weights_effective_at >= evaluation_window_start
                ):
                    raise ValueError(
                        "candidate weights must be effective before the evaluation window"
                    )
                if (
                    committee.weights_effective_at is None
                    or committee.weights_trained_through is None
                    or committee.weights_trained_through
                    >= committee.weights_effective_at
                ):
                    raise ValueError(
                        "candidate weights must be trained before they become effective"
                    )
                if (
                    committee.weights_source_hash
                    != _canonical_hash(_candidate_weight_source_body(committee))
                ):
                    raise ValueError(
                        "candidate weights_source_hash does not match its embedded "
                        "pre-window policy body"
                    )

        for opportunity in self.opportunities:
            if set(opportunity.signals) != participant_ids:
                raise ValueError(
                    "every opportunity must declare each participant as submitted, "
                    "failed or missing"
                )
            expected_material = (
                abs(opportunity.actual_return) > opportunity.neutral_threshold
            )
            if opportunity.material_move != expected_material:
                raise ValueError(
                    "material_move must match the frozen threshold multiple"
                )
            if opportunity.material_move and opportunity.actual_label is Direction.NEUTRAL:
                raise ValueError("material benchmark move cannot be neutral")
            for agent_id, signal in opportunity.signals.items():
                participant = participant_by_id[agent_id]
                if signal.status != "submitted":
                    continue
                if participant.probability_mode is ProbabilityMode.MULTICLASS:
                    if signal.probabilities is None or signal.confidence is not None:
                        raise ValueError(
                            "multiclass participant requires probabilities only"
                        )
                    if predicted_direction(signal.probabilities.as_dict()) is not signal.direction:
                        raise ValueError(
                            "submitted direction conflicts with submitted probabilities"
                        )
                elif (
                    signal.confidence is None
                    or signal.probabilities is not None
                ):
                    raise ValueError(
                        "confidence-only participant cannot submit probabilities"
                    )
        return self


class BenchmarkFixture(BenchmarkFixtureBody):
    content_hash: str = Field(pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def content_hash_matches_body(self) -> BenchmarkFixture:
        expected = _canonical_hash(
            self.model_dump(mode="json", exclude={"content_hash"})
        )
        if self.content_hash != expected or self.content_hash == "0" * 64:
            raise ValueError("benchmark fixture content_hash is invalid")
        return self


class BenchmarkManifestFile(_StrictModel):
    path: str = Field(min_length=1, max_length=240)
    size: int = Field(gt=0)
    sha256: str = Field(pattern=HASH_PATTERN)

    @field_validator("path")
    @classmethod
    def path_is_local(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or value != path.as_posix():
            raise ValueError("manifest file path must be normalized and relative")
        return value


class BenchmarkManifestBody(_StrictModel):
    schema_version: Literal["forecast-loop.benchmark-manifest/v1"] = (
        BENCHMARK_MANIFEST_SCHEMA
    )
    benchmark_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    fixture_version: str = Field(min_length=1, max_length=32)
    redistribution_license: Literal["CC0-1.0"] = "CC0-1.0"
    files: tuple[BenchmarkManifestFile, ...]

    @field_validator("files")
    @classmethod
    def file_paths_are_unique(
        cls,
        values: tuple[BenchmarkManifestFile, ...],
    ) -> tuple[BenchmarkManifestFile, ...]:
        paths = [item.path for item in values]
        if len(paths) != len(set(paths)):
            raise ValueError("manifest file paths must be unique")
        if set(paths) != {
            "agent-specs.json",
            "benchmark.json",
            "LICENSE.txt",
        }:
            raise ValueError(
                "manifest must seal agent-specs.json, benchmark.json and LICENSE.txt"
            )
        return values


class BenchmarkManifest(BenchmarkManifestBody):
    manifest_hash: str = Field(pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def manifest_hash_matches_body(self) -> BenchmarkManifest:
        expected = _canonical_hash(
            self.model_dump(mode="json", exclude={"manifest_hash"})
        )
        if self.manifest_hash != expected or self.manifest_hash == "0" * 64:
            raise ValueError("benchmark manifest_hash is invalid")
        return self


class BenchmarkAgentSpecArchiveBody(_StrictModel):
    schema_version: Literal[
        "forecast-loop.benchmark-agent-spec-archive/v1"
    ] = "forecast-loop.benchmark-agent-spec-archive/v1"
    benchmark_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    fixture_version: str = Field(min_length=1, max_length=32)
    specs: tuple[AgentSpec, ...]

    @field_validator("specs")
    @classmethod
    def spec_ids_are_unique(
        cls,
        values: tuple[AgentSpec, ...],
    ) -> tuple[AgentSpec, ...]:
        ids = [item.agent_id for item in values]
        if len(ids) != len(set(ids)):
            raise ValueError("benchmark AgentSpec ids must be unique")
        return values


class BenchmarkAgentSpecArchive(BenchmarkAgentSpecArchiveBody):
    content_hash: str = Field(pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def archive_hash_matches_body(self) -> BenchmarkAgentSpecArchive:
        expected = _canonical_hash(
            self.model_dump(mode="json", exclude={"content_hash"})
        )
        if self.content_hash != expected or self.content_hash == "0" * 64:
            raise ValueError("benchmark AgentSpec archive content_hash is invalid")
        return self


class BenchmarkGoldenBody(_StrictModel):
    schema_version: Literal["forecast-loop.benchmark-golden/v1"] = (
        BENCHMARK_GOLDEN_SCHEMA
    )
    benchmark_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    fixture_version: str = Field(min_length=1, max_length=32)
    fixture_content_hash: str = Field(pattern=HASH_PATTERN)
    fixture_manifest_hash: str = Field(pattern=HASH_PATTERN)
    expected_report_hash: str = Field(pattern=HASH_PATTERN)


class BenchmarkGolden(BenchmarkGoldenBody):
    golden_hash: str = Field(pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def golden_hash_matches_body(self) -> BenchmarkGolden:
        expected = _canonical_hash(
            self.model_dump(mode="json", exclude={"golden_hash"})
        )
        if self.golden_hash != expected or self.golden_hash == "0" * 64:
            raise ValueError("benchmark golden_hash is invalid")
        return self


@dataclass(frozen=True, slots=True)
class LoadedBenchmark:
    root: Path
    manifest: BenchmarkManifest
    fixture: BenchmarkFixture
    agent_specs: tuple[AgentSpec, ...]


@dataclass(frozen=True, slots=True)
class _ScoredRecord:
    target_date: date
    index_code: str
    horizon: Horizon
    actual_return: float
    actual_label: Direction
    material_move: bool
    status: Literal["submitted", "failed", "missing"]
    direction: Direction | None
    probabilities: Mapping[str, float] | None


def load_benchmark(root: Path) -> LoadedBenchmark:
    """Load a sealed, redistributable benchmark directory without following links."""

    bundle_root = _resolve_bundle_root(root)
    manifest = BenchmarkManifest.model_validate(
        _read_json(bundle_root / "manifest.json", maximum_bytes=MAX_JSON_BYTES)
    )
    for item in manifest.files:
        raw = _secure_read(
            bundle_root,
            item.path,
            maximum_bytes=MAX_JSON_BYTES,
        )
        if len(raw) != item.size or hashlib.sha256(raw).hexdigest() != item.sha256:
            raise BenchmarkError(f"manifest seal does not match {item.path}")
    fixture = BenchmarkFixture.model_validate(
        _read_json(bundle_root / "benchmark.json", maximum_bytes=MAX_JSON_BYTES)
    )
    archive = BenchmarkAgentSpecArchive.model_validate(
        _read_json(bundle_root / "agent-specs.json", maximum_bytes=MAX_JSON_BYTES)
    )
    if (
        fixture.benchmark_id != manifest.benchmark_id
        or fixture.fixture_version != manifest.fixture_version
        or fixture.redistribution_license != manifest.redistribution_license
    ):
        raise BenchmarkError("fixture identity does not match its manifest")
    if (
        archive.benchmark_id != fixture.benchmark_id
        or archive.fixture_version != fixture.fixture_version
    ):
        raise BenchmarkError("AgentSpec archive identity does not match the fixture")
    _validate_participant_spec_projection(fixture, archive.specs)
    return LoadedBenchmark(
        root=bundle_root,
        manifest=manifest,
        fixture=fixture,
        agent_specs=archive.specs,
    )


def build_benchmark_report(root: Path) -> dict[str, Any]:
    """Recompute the deterministic target-date-macro benchmark report."""

    loaded = load_benchmark(root)
    fixture = loaded.fixture
    participant_records = {
        participant.agent_id: _participant_records(fixture, participant.agent_id)
        for participant in fixture.participants
    }
    agent_spec_by_id = {
        specification.agent_id: specification
        for specification in loaded.agent_specs
    }
    committee_records = {
        committee.committee_id: _committee_records(fixture, committee)
        for committee in fixture.committees
    }
    agents = [
        {
            "agent_id": participant.agent_id,
            "agent_version": participant.agent_version,
            "agent_spec_hash": agent_spec_by_id[
                participant.agent_id
            ].content_hash,
            "source_type": participant.source_type.value,
            "probability_mode": participant.probability_mode.value,
            "evaluation_metrics": participant.evaluation_metrics,
            "metrics": _entity_metrics(
                participant_records[participant.agent_id],
                probability_eligible=(
                    participant.probability_mode is ProbabilityMode.MULTICLASS
                ),
                calibration_edges=fixture.evaluation_policy.calibration_bin_edges,
            ),
        }
        for participant in sorted(fixture.participants, key=lambda item: item.agent_id)
    ]
    committees = [
        {
            "committee_id": committee.committee_id,
            "kind": committee.kind,
            "member_weights": committee.member_weights,
            "minimum_members": committee.minimum_members,
            "weight_origin": committee.weight_origin,
            "fitted_on_fixture_outcomes": committee.fitted_on_fixture_outcomes,
            "weights_effective_at": (
                committee.weights_effective_at.isoformat()
                if committee.weights_effective_at is not None
                else None
            ),
            "weights_trained_through": (
                committee.weights_trained_through.isoformat()
                if committee.weights_trained_through is not None
                else None
            ),
            "weights_source_hash": committee.weights_source_hash,
            "metrics": _entity_metrics(
                committee_records[committee.committee_id],
                probability_eligible=True,
                calibration_edges=fixture.evaluation_policy.calibration_bin_edges,
            ),
        }
        for committee in sorted(
            fixture.committees,
            key=lambda item: item.committee_id,
        )
    ]
    unique_dates = {item.target_date for item in fixture.opportunities}
    report_body: dict[str, Any] = {
        "schema_version": BENCHMARK_REPORT_SCHEMA,
        "benchmark_id": fixture.benchmark_id,
        "fixture_version": fixture.fixture_version,
        "fixture_content_hash": fixture.content_hash,
        "fixture_manifest_hash": loaded.manifest.manifest_hash,
        "redistribution_license": fixture.redistribution_license,
        "synthetic_data": fixture.synthetic_data,
        "evaluation_policy": fixture.evaluation_policy.model_dump(mode="json"),
        "counts": {
            "independent_period_count": len(unique_dates),
            "target_opportunity_count": len(fixture.opportunities),
            "agent_opportunity_count": sum(
                len(records) for records in participant_records.values()
            ),
            "agent_observation_count": sum(
                record.status == "submitted"
                for records in participant_records.values()
                for record in records
            ),
            "committee_opportunity_count": sum(
                len(records) for records in committee_records.values()
            ),
            "committee_observation_count": sum(
                record.status == "submitted"
                for records in committee_records.values()
                for record in records
            ),
        },
        "agents": agents,
        "committees": committees,
    }
    return {
        **report_body,
        "report_hash": _canonical_hash(report_body),
    }


def verify_benchmark_golden(
    root: Path,
    *,
    golden_path: Path | None = None,
) -> dict[str, Any]:
    """Verify all fixture seals and exact deterministic golden report bytes."""

    loaded = load_benchmark(root)
    expected_path = golden_path or loaded.root / "golden-report.json"
    expected = BenchmarkGolden.model_validate(
        _read_json(expected_path, maximum_bytes=MAX_JSON_BYTES)
    )
    actual = build_benchmark_report(loaded.root)
    if (
        expected.benchmark_id != loaded.fixture.benchmark_id
        or expected.fixture_version != loaded.fixture.fixture_version
        or expected.fixture_content_hash != loaded.fixture.content_hash
        or expected.fixture_manifest_hash != loaded.manifest.manifest_hash
    ):
        raise BenchmarkError("golden report binds a different fixture")
    if expected.expected_report_hash != actual["report_hash"]:
        raise BenchmarkError("benchmark report does not match golden-report.json")
    return actual


def _participant_records(
    fixture: BenchmarkFixture,
    agent_id: str,
) -> list[_ScoredRecord]:
    records: list[_ScoredRecord] = []
    for opportunity in fixture.opportunities:
        signal = opportunity.signals[agent_id]
        records.append(
            _ScoredRecord(
                target_date=opportunity.target_date,
                index_code=opportunity.index_code,
                horizon=opportunity.horizon,
                actual_return=opportunity.actual_return,
                actual_label=opportunity.actual_label,
                material_move=opportunity.material_move,
                status=signal.status,
                direction=signal.direction,
                probabilities=(
                    signal.probabilities.as_dict()
                    if signal.probabilities is not None
                    else None
                ),
            )
        )
    return records


def _candidate_weight_source_body(
    committee: BenchmarkCommittee,
) -> dict[str, Any]:
    if (
        committee.weights_effective_at is None
        or committee.weights_trained_through is None
    ):
        raise BenchmarkError("candidate committee is missing weight provenance")
    return {
        "schema_version": BELIEVABILITY_WEIGHT_SCHEMA,
        "committee_id": committee.committee_id,
        "member_weights": committee.member_weights,
        "weights_effective_at": committee.weights_effective_at.isoformat(),
        "weights_trained_through": committee.weights_trained_through.isoformat(),
        "fitted_on_fixture_outcomes": committee.fitted_on_fixture_outcomes,
    }


def _validate_participant_spec_projection(
    fixture: BenchmarkFixture,
    specs: Sequence[AgentSpec],
) -> None:
    spec_by_id = {item.agent_id: item for item in specs}
    participant_by_id = {
        item.agent_id: item for item in fixture.participants
    }
    if set(spec_by_id) != set(participant_by_id):
        raise BenchmarkError(
            "AgentSpec archive does not exactly cover benchmark participants"
        )
    for agent_id, participant in participant_by_id.items():
        spec = spec_by_id[agent_id]
        metrics = tuple(item.value for item in spec.participation.evaluation_metrics)
        if (
            spec.agent_version != participant.agent_version
            or spec.source_type is not participant.source_type
            or spec.capabilities.probability_mode
            is not participant.probability_mode
            or metrics != participant.evaluation_metrics
            or spec.participation.mode is not ParticipationMode.SHADOW
        ):
            raise BenchmarkError(
                f"participant projection does not match AgentSpec: {agent_id}"
            )


def _committee_records(
    fixture: BenchmarkFixture,
    committee: BenchmarkCommittee,
) -> list[_ScoredRecord]:
    records: list[_ScoredRecord] = []
    for opportunity in fixture.opportunities:
        available: list[tuple[str, float, dict[str, float]]] = []
        for agent_id in sorted(committee.member_weights):
            weight = committee.member_weights[agent_id]
            signal = opportunity.signals[agent_id]
            if signal.status == "submitted" and signal.probabilities is not None:
                available.append(
                    (agent_id, weight, signal.probabilities.as_dict())
                )
        if len(available) != len(committee.member_weights):
            records.append(
                _ScoredRecord(
                    target_date=opportunity.target_date,
                    index_code=opportunity.index_code,
                    horizon=opportunity.horizon,
                    actual_return=opportunity.actual_return,
                    actual_label=opportunity.actual_label,
                    material_move=opportunity.material_move,
                    status="failed",
                    direction=None,
                    probabilities=None,
                )
            )
            continue
        total_weight = math.fsum(weight for _, weight, _ in available)
        probabilities = {
            direction.value: math.fsum(
                weight * values[direction.value]
                for _, weight, values in available
            )
            / total_weight
            for direction in Direction
        }
        try:
            direction = predicted_direction(probabilities)
        except ValueError:
            records.append(
                _ScoredRecord(
                    target_date=opportunity.target_date,
                    index_code=opportunity.index_code,
                    horizon=opportunity.horizon,
                    actual_return=opportunity.actual_return,
                    actual_label=opportunity.actual_label,
                    material_move=opportunity.material_move,
                    status="failed",
                    direction=None,
                    probabilities=None,
                )
            )
            continue
        records.append(
            _ScoredRecord(
                target_date=opportunity.target_date,
                index_code=opportunity.index_code,
                horizon=opportunity.horizon,
                actual_return=opportunity.actual_return,
                actual_label=opportunity.actual_label,
                material_move=opportunity.material_move,
                status="submitted",
                direction=direction,
                probabilities=probabilities,
            )
        )
    return records


def _entity_metrics(
    records: Sequence[_ScoredRecord],
    *,
    probability_eligible: bool,
    calibration_edges: Sequence[float],
) -> dict[str, Any]:
    dates = {record.target_date for record in records}
    submitted = [record for record in records if record.status == "submitted"]
    failed = [record for record in records if record.status == "failed"]
    missing = [record for record in records if record.status == "missing"]
    direction = _macro_metric(
        records,
        eligible=lambda item: (
            item.status == "submitted"
            and item.actual_return != 0
        ),
        value=lambda item: float(
            item.direction
            is (Direction.UP if item.actual_return > 0 else Direction.DOWN)
        ),
    )
    material = _macro_metric(
        records,
        eligible=lambda item: item.status == "submitted" and item.material_move,
        value=lambda item: float(item.direction is item.actual_label),
    )
    brier = None
    calibration = None
    if probability_eligible:
        brier_metric = _macro_metric(
            records,
            eligible=lambda item: (
                item.status == "submitted" and item.probabilities is not None
            ),
            value=lambda item: multiclass_brier_score(
                _required_probabilities(item),
                item.actual_label,
            ),
        )
        brier = {
            **brier_metric,
            "formula": "mean_squared_error_over_3",
            "range": [0, "2/3"],
        }
        calibration = _classwise_calibration(records, calibration_edges)
    return {
        "independent_period_count": len(dates),
        "opportunity_count": len(records),
        "observation_count": len(submitted),
        "observation_period_count": len(
            {record.target_date for record in submitted}
        ),
        "failed_count": len(failed),
        "missing_count": len(missing),
        "coverage_rate": _macro_status_rate(records, status="submitted"),
        "failure_rate": _macro_non_observation_rate(records),
        "direction_hit": direction,
        "material_direction_hit": material,
        "multiclass_brier": brier,
        "classwise_calibration": calibration,
    }


def _macro_status_rate(
    records: Sequence[_ScoredRecord],
    *,
    status: str,
) -> float | None:
    grouped = _by_date(records)
    values = [
        sum(item.status == status for item in date_records) / len(date_records)
        for date_records in grouped.values()
    ]
    return _rounded(_mean(values)) if values else None


def _macro_non_observation_rate(
    records: Sequence[_ScoredRecord],
) -> float | None:
    grouped = _by_date(records)
    values = [
        sum(item.status != "submitted" for item in date_records)
        / len(date_records)
        for date_records in grouped.values()
    ]
    return _rounded(_mean(values)) if values else None


def _macro_metric(
    records: Sequence[_ScoredRecord],
    *,
    eligible: Callable[[_ScoredRecord], bool],
    value: Callable[[_ScoredRecord], float],
) -> dict[str, int | float | None]:
    grouped = _by_date(records)
    date_values: list[float] = []
    eligible_count = 0
    for date_records in grouped.values():
        selected = [item for item in date_records if eligible(item)]
        if not selected:
            continue
        eligible_count += len(selected)
        date_values.append(_mean([value(item) for item in selected]))
    return {
        "eligible_observation_count": eligible_count,
        "independent_period_count": len(date_values),
        "macro_average": _rounded(_mean(date_values)) if date_values else None,
    }


def _classwise_calibration(
    records: Sequence[_ScoredRecord],
    edges: Sequence[float],
) -> dict[str, Any]:
    eligible = [
        record
        for record in records
        if record.status == "submitted" and record.probabilities is not None
    ]
    grouped = _by_date(eligible)
    result: dict[str, Any] = {}
    for direction in Direction:
        bin_date_values: dict[
            int,
            list[tuple[date, float, float, float]],
        ] = defaultdict(list)
        date_count = len(grouped)
        for date_records in grouped.values():
            for record in date_records:
                probability = _required_probabilities(record)[direction.value]
                bucket = _calibration_bucket(probability, edges)
                bin_date_values[bucket].append(
                    (
                        record.target_date,
                        probability,
                        float(record.actual_label is direction),
                        1 / (date_count * len(date_records)),
                    )
                )
        bins = []
        ece_terms: list[float] = []
        for bucket, values in sorted(bin_date_values.items()):
            total_date_weight = math.fsum(item[3] for item in values)
            predicted = (
                math.fsum(item[1] * item[3] for item in values)
                / total_date_weight
            )
            observed = (
                math.fsum(item[2] * item[3] for item in values)
                / total_date_weight
            )
            ece_terms.append(total_date_weight * abs(predicted - observed))
            bins.append(
                {
                    "lower": edges[bucket],
                    "upper": edges[bucket + 1],
                    "upper_inclusive": bucket == len(edges) - 2,
                    "observation_count": len(values),
                    "independent_period_count": len(
                        {item[0] for item in values}
                    ),
                    "weighted_mass": _rounded(total_date_weight),
                    "mean_predicted_probability": _rounded(predicted),
                    "observed_frequency": _rounded(observed),
                }
            )
        result[direction.value] = {
            "eligible_observation_count": len(eligible),
            "independent_period_count": len(grouped),
            "expected_calibration_error": (
                _rounded(math.fsum(ece_terms)) if ece_terms else None
            ),
            "bins": bins,
        }
    return result


def _calibration_bucket(value: float, edges: Sequence[float]) -> int:
    if value == edges[-1]:
        return len(edges) - 2
    for index, (lower, upper) in enumerate(
        zip(edges, edges[1:], strict=False)
    ):
        if lower <= value < upper:
            return index
    raise BenchmarkError("probability falls outside calibration bins")


def _required_probabilities(record: _ScoredRecord) -> Mapping[str, float]:
    if record.probabilities is None:
        raise BenchmarkError("multiclass metric received no probability vector")
    return record.probabilities


def _by_date(
    records: Sequence[_ScoredRecord],
) -> dict[date, list[_ScoredRecord]]:
    grouped: dict[date, list[_ScoredRecord]] = defaultdict(list)
    for record in records:
        grouped[record.target_date].append(record)
    return dict(sorted(grouped.items()))


def _resolve_bundle_root(root: Path) -> Path:
    configured = root.expanduser()
    if configured.is_symlink():
        raise BenchmarkError("benchmark root must not be a symlink")
    resolved = Path(os.path.abspath(configured))
    if not resolved.is_dir():
        raise BenchmarkError("benchmark root must be an existing directory")
    expected_names = {
        "LICENSE.txt",
        "agent-specs.json",
        "benchmark.json",
        "golden-report.json",
        "manifest.json",
    }
    members = {item.name for item in resolved.iterdir()}
    required_names = {
        "LICENSE.txt",
        "agent-specs.json",
        "benchmark.json",
        "manifest.json",
    }
    if not required_names.issubset(members):
        raise BenchmarkError("benchmark directory is missing required artifacts")
    unexpected = members - expected_names
    if unexpected:
        raise BenchmarkError(
            "benchmark directory contains unexpected artifacts: "
            + ", ".join(sorted(unexpected))
        )
    return resolved


def _read_json(path: Path, *, maximum_bytes: int) -> Any:
    parent = path.parent
    raw = _secure_read(parent, path.name, maximum_bytes=maximum_bytes)
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"{path.name} is not valid strict JSON") from exc


def _secure_read(root: Path, relative: str, *, maximum_bytes: int) -> bytes:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise BenchmarkError("benchmark file may not escape its bundle root")
    candidate = root.joinpath(relative_path)
    current = root
    for part in relative_path.parts:
        current = current / part
        if current.is_symlink():
            raise BenchmarkError("benchmark files must not be symlinks")
    if candidate.is_symlink():
        raise BenchmarkError("benchmark files must not be symlinks")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise BenchmarkError(f"cannot read benchmark file {relative}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise BenchmarkError("benchmark artifacts must be regular files")
        if metadata.st_size > maximum_bytes:
            raise BenchmarkError("benchmark artifact exceeds the size limit")
        raw = b""
        while len(raw) <= maximum_bytes:
            chunk = os.read(descriptor, min(64 * 1024, maximum_bytes + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        if len(raw) > maximum_bytes:
            raise BenchmarkError("benchmark artifact exceeds the size limit")
        return raw
    finally:
        os.close(descriptor)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BenchmarkError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise BenchmarkError(f"non-finite JSON number is not allowed: {value}")


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _mean(values: Sequence[float]) -> float:
    return math.fsum(values) / len(values)


def _rounded(value: float) -> float:
    return round(value, 8)
