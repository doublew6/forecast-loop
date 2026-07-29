from __future__ import annotations

import json
import shutil
from copy import deepcopy
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from app.cli import main
from app.domain import Direction, Horizon, multiclass_brier_score
from app.services.benchmark import (
    BenchmarkAgentSpecArchive,
    BenchmarkError,
    BenchmarkFixtureBody,
    _committee_records,
    _entity_metrics,
    _ScoredRecord,
    _validate_participant_spec_projection,
    build_benchmark_report,
    load_benchmark,
    verify_benchmark_golden,
)
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = ROOT / "benchmarks" / "cross-source-v1"


def test_public_cross_source_fixture_matches_golden() -> None:
    report = verify_benchmark_golden(FIXTURE_ROOT)

    assert report["counts"] == {
        "independent_period_count": 5,
        "target_opportunity_count": 10,
        "agent_opportunity_count": 40,
        "agent_observation_count": 37,
        "committee_opportunity_count": 30,
        "committee_observation_count": 27,
    }
    agents = {item["agent_id"]: item for item in report["agents"]}
    manual = agents["manual_fixture_agent"]
    assert manual["source_type"] == "manual"
    assert manual["probability_mode"] == "confidence"
    assert manual["metrics"]["multiclass_brier"] is None
    assert manual["metrics"]["classwise_calibration"] is None
    assert manual["metrics"]["coverage_rate"] + manual["metrics"]["failure_rate"] == 1

    committees = {item["committee_id"]: item for item in report["committees"]}
    assert set(committees) == {
        "fixed_committee",
        "equal_weight_baseline",
        "candidate_believability_committee",
    }
    assert {
        item["metrics"]["opportunity_count"] for item in committees.values()
    } == {10}
    assert {
        item["metrics"]["observation_count"] for item in committees.values()
    } == {9}
    candidate = committees["candidate_believability_committee"]
    assert candidate["fitted_on_fixture_outcomes"] is False
    assert candidate["weights_trained_through"] < "2026-01-05"


def test_metrics_macro_average_target_dates_not_observations() -> None:
    records = [
        _record(date(2026, 1, 5), f"A{index}", direction=Direction.UP)
        for index in range(3)
    ]
    records.append(
        _record(
            date(2026, 1, 6),
            "B",
            direction=Direction.DOWN,
            horizon=Horizon.D2,
        )
    )

    metrics = _entity_metrics(
        records,
        probability_eligible=False,
        calibration_edges=(0.0, 0.5, 1.0),
    )

    assert metrics["independent_period_count"] == 2
    assert metrics["opportunity_count"] == 4
    assert metrics["direction_hit"] == {
        "eligible_observation_count": 4,
        "independent_period_count": 2,
        "macro_average": 0.5,
    }
    assert metrics["direction_hit"]["macro_average"] != 0.75


def test_sign_and_material_boundaries_are_distinct() -> None:
    positive_at_threshold = _record(
        date(2026, 1, 5),
        "BOUNDARY",
        direction=Direction.UP,
        actual_return=0.005,
        actual_label=Direction.NEUTRAL,
        material_move=False,
    )
    zero = _record(
        date(2026, 1, 6),
        "ZERO",
        direction=Direction.DOWN,
        actual_return=0,
        actual_label=Direction.NEUTRAL,
        material_move=False,
    )

    metrics = _entity_metrics(
        [positive_at_threshold, zero],
        probability_eligible=False,
        calibration_edges=(0.0, 0.5, 1.0),
    )

    assert metrics["direction_hit"]["eligible_observation_count"] == 1
    assert metrics["direction_hit"]["macro_average"] == 1
    assert metrics["material_direction_hit"]["eligible_observation_count"] == 0
    assert metrics["material_direction_hit"]["macro_average"] is None


def test_brier_uses_mean_over_three_and_classwise_calibration() -> None:
    worst = multiclass_brier_score(
        {"up": 0.0, "neutral": 0.0, "down": 1.0},
        Direction.UP,
    )
    assert worst == pytest.approx(2 / 3)

    report = build_benchmark_report(FIXTURE_ROOT)
    ai_metrics = next(
        item["metrics"]
        for item in report["agents"]
        if item["agent_id"] == "ai_fixture_agent"
    )
    assert ai_metrics["multiclass_brier"]["formula"] == "mean_squared_error_over_3"
    assert set(ai_metrics["classwise_calibration"]) == {"up", "neutral", "down"}
    for class_report in ai_metrics["classwise_calibration"].values():
        assert sum(item["weighted_mass"] for item in class_report["bins"]) == pytest.approx(1)
        recomputed = sum(
            item["weighted_mass"]
            * abs(
                item["mean_predicted_probability"]
                - item["observed_frequency"]
            )
            for item in class_report["bins"]
        )
        assert recomputed == pytest.approx(
            class_report["expected_calibration_error"],
            abs=2e-8,
        )


def test_classwise_ece_aggregates_equal_date_weights_before_bin_gap() -> None:
    probability = {"up": 0.5, "neutral": 0.25, "down": 0.25}
    records = [
        replace(
            _record(
                date(2026, 1, 5),
                "UP",
                actual_return=0.01,
                actual_label=Direction.UP,
            ),
            probabilities=probability,
        ),
        replace(
            _record(
                date(2026, 1, 6),
                "DOWN",
                actual_return=-0.01,
                actual_label=Direction.DOWN,
            ),
            probabilities=probability,
        ),
    ]

    metrics = _entity_metrics(
        records,
        probability_eligible=True,
        calibration_edges=(0.0, 0.6, 1.0),
    )
    up = metrics["classwise_calibration"]["up"]

    assert up["expected_calibration_error"] == 0
    assert up["bins"] == [
        {
            "lower": 0.0,
            "upper": 0.6,
            "upper_inclusive": False,
            "observation_count": 2,
            "independent_period_count": 2,
            "weighted_mass": 1.0,
            "mean_predicted_probability": 0.5,
            "observed_frequency": 0.5,
        }
    ]


def test_manual_probabilities_and_post_window_candidate_fit_fail_closed() -> None:
    fixture = _fixture_body()
    fixture["opportunities"][0]["signals"]["manual_fixture_agent"][
        "probabilities"
    ] = {"up": 0.7, "neutral": 0.2, "down": 0.1}
    with pytest.raises(ValidationError, match="confidence-only"):
        BenchmarkFixtureBody.model_validate(fixture)

    fixture = _fixture_body()
    candidate = next(
        item
        for item in fixture["committees"]
        if item["kind"] == "candidate_believability"
    )
    candidate["weights_trained_through"] = "2026-01-05"
    with pytest.raises(ValidationError, match="trained before"):
        BenchmarkFixtureBody.model_validate(fixture)

    fixture = _fixture_body()
    candidate = next(
        item
        for item in fixture["committees"]
        if item["kind"] == "candidate_believability"
    )
    candidate["weights_trained_through"] = candidate["weights_effective_at"]
    with pytest.raises(ValidationError, match="before they become effective"):
        BenchmarkFixtureBody.model_validate(fixture)

    fixture = _fixture_body()
    candidate = next(
        item
        for item in fixture["committees"]
        if item["kind"] == "candidate_believability"
    )
    candidate["weights_effective_at"] = fixture["opportunities"][0]["target_date"]
    with pytest.raises(ValidationError, match="before the evaluation window"):
        BenchmarkFixtureBody.model_validate(fixture)


def test_agent_spec_archive_hashes_and_participant_projection_fail_closed() -> None:
    loaded = load_benchmark(FIXTURE_ROOT)
    report = build_benchmark_report(FIXTURE_ROOT)
    report_hashes = {
        item["agent_id"]: item["agent_spec_hash"]
        for item in report["agents"]
    }
    assert report_hashes == {
        item.agent_id: item.content_hash for item in loaded.agent_specs
    }

    archive = json.loads(
        (FIXTURE_ROOT / "agent-specs.json").read_text(encoding="utf-8")
    )
    tampered = deepcopy(archive)
    tampered["specs"][0]["role"] += " tampered"
    with pytest.raises(ValidationError, match="content_hash"):
        BenchmarkAgentSpecArchive.model_validate(tampered)

    random_hash = deepcopy(archive)
    random_hash["specs"][0]["content_hash"] = "1" * 64
    with pytest.raises(ValidationError, match="content_hash"):
        BenchmarkAgentSpecArchive.model_validate(random_hash)

    manual = next(
        item
        for item in loaded.fixture.participants
        if item.agent_id == "manual_fixture_agent"
    )
    mismatched = manual.model_copy(
        update={"agent_version": "9.9.9"}
    )
    fixture = loaded.fixture.model_copy(
        update={
            "participants": tuple(
                mismatched if item.agent_id == mismatched.agent_id else item
                for item in loaded.fixture.participants
            )
        }
    )
    with pytest.raises(BenchmarkError, match="projection"):
        _validate_participant_spec_projection(fixture, loaded.agent_specs)


def test_committee_order_is_stable_and_required_member_failure_is_not_renormalized() -> None:
    fixture = load_benchmark(FIXTURE_ROOT).fixture
    committee = next(
        item for item in fixture.committees if item.kind == "fixed"
    )
    reversed_committee = committee.model_copy(
        update={
            "member_weights": dict(
                reversed(tuple(committee.member_weights.items()))
            )
        }
    )

    assert _committee_records(fixture, committee) == _committee_records(
        fixture,
        reversed_committee,
    )
    records = _committee_records(fixture, committee)
    assert records[-1].status == "failed"
    assert records[-1].probabilities is None


def test_loader_rejects_tampering_symlinks_and_unexpected_files(
    tmp_path: Path,
) -> None:
    copied = tmp_path / "benchmark"
    shutil.copytree(FIXTURE_ROOT, copied)
    benchmark_path = copied / "benchmark.json"
    benchmark_path.write_text(
        benchmark_path.read_text(encoding="utf-8").replace(
            '"actual_return": 0.02',
            '"actual_return": 0.03',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(BenchmarkError, match="manifest seal"):
        load_benchmark(copied)

    shutil.rmtree(copied)
    shutil.copytree(FIXTURE_ROOT, copied)
    specs_path = copied / "agent-specs.json"
    specs_path.write_text(
        specs_path.read_text(encoding="utf-8").replace(
            "Synthetic manual fixture Agent",
            "Tampered manual fixture Agent",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(BenchmarkError, match="manifest seal"):
        load_benchmark(copied)

    shutil.rmtree(copied)
    shutil.copytree(FIXTURE_ROOT, copied)
    license_path = copied / "LICENSE.txt"
    license_path.unlink()
    license_path.symlink_to(FIXTURE_ROOT / "LICENSE.txt")
    with pytest.raises(BenchmarkError, match="symlink"):
        load_benchmark(copied)

    shutil.rmtree(copied)
    shutil.copytree(FIXTURE_ROOT, copied)
    (copied / "unexpected.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(BenchmarkError, match="unexpected artifacts"):
        load_benchmark(copied)


def test_record_order_and_horizon_do_not_inflate_independent_periods() -> None:
    records = [
        _record(date(2026, 1, 5), "A", horizon=Horizon.D1),
        _record(date(2026, 1, 5), "A", horizon=Horizon.D2),
        _record(date(2026, 1, 6), "B", horizon=Horizon.D1),
    ]
    forward = _entity_metrics(
        records,
        probability_eligible=False,
        calibration_edges=(0.0, 0.5, 1.0),
    )
    backward = _entity_metrics(
        list(reversed(records)),
        probability_eligible=False,
        calibration_edges=(0.0, 0.5, 1.0),
    )

    assert forward == backward
    assert forward["independent_period_count"] == 2
    assert forward["opportunity_count"] == 3


def test_benchmark_cli_runs_and_verifies(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["benchmark", "run", str(FIXTURE_ROOT)]) == 0
    run_report = json.loads(capsys.readouterr().out)
    assert run_report["schema_version"] == "forecast-loop.benchmark-report/v1"

    assert main(["benchmark", "verify", str(FIXTURE_ROOT)]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["status"] == "verified"
    assert verified["report_hash"] == run_report["report_hash"]


def _record(
    target_date: date,
    index_code: str,
    *,
    direction: Direction = Direction.UP,
    horizon: Horizon = Horizon.D1,
    actual_return: float = 0.01,
    actual_label: Direction = Direction.UP,
    material_move: bool = True,
) -> _ScoredRecord:
    return _ScoredRecord(
        target_date=target_date,
        index_code=index_code,
        horizon=horizon,
        actual_return=actual_return,
        actual_label=actual_label,
        material_move=material_move,
        status="submitted",
        direction=direction,
        probabilities=None,
    )


def _fixture_body() -> dict:
    payload = json.loads(
        (FIXTURE_ROOT / "benchmark.json").read_text(encoding="utf-8")
    )
    payload.pop("content_hash")
    return payload
