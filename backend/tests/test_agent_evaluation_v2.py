from __future__ import annotations

import hashlib
import json
import stat
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from app.cli import main
from app.config import Settings
from app.db import Database
from app.models import AgentBadCase, AgentBadCaseEvent, AgentTrace
from app.research_v2 import DEFAULT_RESEARCH_PROGRAM_V2
from app.services.agent_evaluation_v2 import (
    DRAFT_SCHEMA_VERSION_V2,
    AgentEvalDraftV2,
    AgentEvalSuiteV2,
    AgentEvalV2Error,
    agent_eval_v2_status,
    arm_manifest_hash_v2,
    episode_input_hash,
    finalize_agent_eval_v2,
    latest_agent_eval_v2_ablation_values,
    list_agent_eval_v2_jobs,
    prepare_agent_eval_v2,
    verify_finalized_agent_eval_v2_job,
)
from app.services.agent_tracing import canonical_digest
from app.services.schema_readiness import upgrade_database
from sqlalchemy import select

ZONE = ZoneInfo("Asia/Shanghai")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _frozen(version: str) -> dict[str, str]:
    return {"version": version, "content_hash": _digest(version)}


def _manifest(target_id: str, arm_id: str) -> dict[str, object]:
    return {
        "target_id": target_id,
        "model": {
            "name": f"model-{arm_id}",
            "version": "2026-08-12",
            "content_hash": _digest(f"model-{arm_id}"),
        },
        "agents": {"strategy_agent": _frozen(f"agent-{arm_id}")},
        "prompts": {"strategy_agent": _frozen(f"prompt-{arm_id}")},
        "workflow": _frozen(f"workflow-{arm_id}"),
        "research_program": {
            "version": "program-v2",
            "content_hash": DEFAULT_RESEARCH_PROGRAM_V2.content_hash,
        },
        "aggregation": _frozen(f"aggregation-{arm_id}"),
        "wiki": _frozen("wiki-v3"),
    }


def _episode(number: int, *, target_id: str = "csi1000-absolute-d1") -> dict[str, object]:
    anchor = date(2026, 7, number + 1)
    evidence = [
        {
            "evidence_id": f"evidence-{target_id}-{number}",
            "observed_at": datetime(2026, 7, number + 1, 14, 0, tzinfo=ZONE).isoformat(),
            "content_hash": _digest(f"evidence-{target_id}-{number}"),
        }
    ]
    body: dict[str, object] = {
        "episode_id": f"{target_id}-{number}",
        "target_id": target_id,
        "independence_key": f"{target_id}-{number}",
        "anchor_date": anchor.isoformat(),
        "target_date": (anchor + timedelta(days=1)).isoformat(),
        "evidence_cutoff": datetime(
            2026, 7, number + 1, 14, 55, tzinfo=ZONE
        ).isoformat(),
        "input_payload": {"snapshot_id": f"snapshot-{number}"},
        "evidence": evidence,
        "expected_trajectory": ["freeze", "strategy_agent", "validate", "persist"],
        "must_pass": number == 1,
        "must_pass_invariant": (
            {
                "expected_direction": "up",
                "probability_bounds": {"up": {"minimum": 0.6, "maximum": 0.8}},
                "required_evidence_ids": [f"evidence-{target_id}-{number}"],
            }
            if number == 1
            else None
        ),
    }
    body["input_hash"] = episode_input_hash(body)
    body["outcome"] = {
        "label": "up" if number % 2 else "down",
        "observation_hash": _digest(f"outcome-{target_id}-{number}"),
    }
    return body


def _suite_payload(*, episode_count: int = 20) -> dict[str, object]:
    target_id = "csi1000-absolute-d1"
    return {
        "schema_version": "forecast-loop.agent-eval-suite/v2",
        "suite_id": "private-v2-replay",
        "version": "2.0.0",
        "title": "Private outcome-blind replay",
        "synthetic": False,
        "runner_kind": "codex_file_replay",
        "targets": [
            {
                "target_id": target_id,
                "horizon": "D1",
                "release_gate": True,
            }
        ],
        "arms": [
            {
                "arm_id": "baseline-v1",
                "targets": [_manifest(target_id, "baseline-v1")],
            },
            {
                "arm_id": "candidate-v2",
                "targets": [_manifest(target_id, "candidate-v2")],
            },
        ],
        "episodes": [_episode(number) for number in range(1, episode_count + 1)],
        "release_policy": {
            "version": "2.0.0",
            "min_metric_episodes": 20,
            "must_pass_rate": 1.0,
            "max_brier_delta": 0.01,
            "max_direction_drop": 0.02,
            "max_p95_latency_ratio": 1.2,
            "max_token_ratio": 1.15,
        },
    }


def _settings(tmp_path: Path, *, episode_count: int = 20) -> Settings:
    private_root = tmp_path / "evals"
    outcome_root = tmp_path / "eval-outcomes"
    suite_root = outcome_root / "private-v2-replay"
    suite_root.mkdir(parents=True)
    suite = AgentEvalSuiteV2.model_validate(_suite_payload(episode_count=episode_count))
    (suite_root / "suite.json").write_text(
        suite.model_dump_json(indent=2),
        encoding="utf-8",
    )
    candidate = next(arm for arm in suite.arms if arm.arm_id == "candidate-v2")
    return Settings().model_copy(
        update={
            "agent_eval_private_root": private_root,
            "agent_eval_outcome_root": outcome_root,
            "agent_eval_release_candidate_hash": arm_manifest_hash_v2(candidate),
            "agent_eval_public_root": tmp_path / "benchmarks",
            "database_url": f"sqlite:///{tmp_path / 'eval-v2.sqlite3'}",
        }
    )


def _draft_payload(
    job_dir: Path,
    arm_id: str,
    *,
    invalid_citation: bool = False,
) -> dict[str, object]:
    eval_input = json.loads((job_dir / "input.json").read_text(encoding="utf-8"))
    outputs = []
    for episode in eval_input["episodes"]:
        label_up = int(episode["episode_id"].rsplit("-", 1)[-1]) % 2 == 1
        probabilities = (
            {"up": 0.7, "neutral": 0.2, "down": 0.1}
            if label_up
            else {"up": 0.1, "neutral": 0.2, "down": 0.7}
        )
        outputs.append(
            {
                "episode_id": episode["episode_id"],
                "target_id": episode["target_id"],
                "status": "completed",
                "trajectory": episode["expected_trajectory"],
                "citations": [
                    {
                        "evidence_id": (
                            "unknown-evidence"
                            if invalid_citation
                            else episode["evidence"][0]["evidence_id"]
                        )
                    }
                ],
                "probabilities": probabilities,
                "reasoning": {
                    "rationale": "Frozen evidence supports the submitted distribution.",
                    "causal_chain": ["evidence", "market transmission", "target"],
                    "counter_evidence": ["The transmission may fail before the horizon."],
                    "invalidation_conditions": ["The frozen catalyst is reversed."],
                },
                "latency_ms": 1000 if arm_id == "baseline-v1" else 1050,
                "total_tokens": 1000 if arm_id == "baseline-v1" else 1050,
                "reasoning_review": {
                    "evidence_relevance": 2,
                    "causal_chain": 2,
                    "target_horizon_mapping": 2,
                    "counterevidence_invalidation": 1,
                    "calibration_uncertainty": 1,
                    "rule_passed": True,
                    "review_input_hash": episode["input_hash"],
                    "reviewer_model": "gpt-5.6-sol",
                    "reasoning_effort": "high",
                    "reviewer_id": f"blind-review-{arm_id}",
                },
                "ablations": [
                    {
                        "agent_id": "macro_policy_agent",
                        "replacement": "no_impact",
                        "probabilities": {"up": 0.34, "neutral": 0.33, "down": 0.33},
                    }
                ],
            }
        )
    return {
        "schema_version": DRAFT_SCHEMA_VERSION_V2,
        "job_id": eval_input["job_id"],
        "arm_id": arm_id,
        "suite_hash": eval_input["suite_hash"],
        "input_hash": eval_input["input_hash"],
        "arm_manifest_hash": eval_input["arm_manifest_hashes"][arm_id],
        "generated_by": {
            "producer": f"codex-task-{arm_id}",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
        },
        "outputs": outputs,
    }


def _write_draft(job_dir: Path, arm_id: str, **kwargs: object) -> None:
    (job_dir / arm_id / "drafts.json").write_text(
        json.dumps(_draft_payload(job_dir, arm_id, **kwargs), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _review_draft_payload(job_dir: Path) -> dict[str, object]:
    review_input = json.loads(
        (job_dir / "reviewer/input.json").read_text(encoding="utf-8")
    )
    reviews: list[dict[str, object]] = []
    for arm_id in review_input["arm_ids"]:
        arm_payload = json.loads(
            (job_dir / arm_id / "drafts.json").read_text(encoding="utf-8")
        )
        arm = AgentEvalDraftV2.model_validate(arm_payload)
        outputs = {item.episode_id: item for item in arm.outputs}
        for episode in review_input["episodes"]:
            output = outputs[episode["episode_id"]]
            reviews.append(
                {
                    "arm_id": arm_id,
                    "episode_id": episode["episode_id"],
                    "target_id": episode["target_id"],
                    "reviewed_output_hash": canonical_digest(
                        output.model_dump(mode="json")
                    ),
                    "review": {
                        "evidence_relevance": 2,
                        "causal_chain": 2,
                        "target_horizon_mapping": 2,
                        "counterevidence_invalidation": 1,
                        "calibration_uncertainty": 1,
                        "rule_passed": True,
                        "review_input_hash": episode["input_hash"],
                        "reviewer_model": "gpt-5.6-sol",
                        "reasoning_effort": "high",
                        "reviewer_id": "codex-task-independent-reviewer",
                    },
                }
            )
    return {
        "schema_version": "forecast-loop.agent-eval-review-draft/v2",
        "job_id": review_input["job_id"],
        "suite_hash": review_input["suite_hash"],
        "eval_input_hash": review_input["eval_input_hash"],
        "review_input_hash": review_input["input_hash"],
        "generated_by": {
            "producer": "codex-task-independent-reviewer",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
        },
        "reviews": reviews,
    }


def _ablation_draft_payload(job_dir: Path) -> dict[str, object]:
    ablation_input = json.loads(
        (job_dir / "ablation/input.json").read_text(encoding="utf-8")
    )
    candidate_payload = json.loads(
        (job_dir / ablation_input["candidate_arm_id"] / "drafts.json").read_text(
            encoding="utf-8"
        )
    )
    candidate = AgentEvalDraftV2.model_validate(candidate_payload)
    outputs = {item.episode_id: item for item in candidate.outputs}
    rows: list[dict[str, object]] = []
    for assignment in ablation_input["assignments"]:
        for episode in ablation_input["episodes"]:
            if episode["target_id"] != assignment["target_id"]:
                continue
            rows.append(
                {
                    "ablation_id": assignment["ablation_id"],
                    "episode_id": episode["episode_id"],
                    "target_id": episode["target_id"],
                    "agent_id": assignment["agent_id"],
                    "replacement": "no_impact",
                    "status": "completed",
                    "full_output_hash": canonical_digest(
                        outputs[episode["episode_id"]].model_dump(mode="json")
                    ),
                    "ablation_input_hash": ablation_input["input_hash"],
                    "probabilities": {"up": 0.34, "neutral": 0.33, "down": 0.33},
                }
            )
    return {
        "schema_version": "forecast-loop.agent-eval-ablation-draft/v2",
        "job_id": ablation_input["job_id"],
        "suite_hash": ablation_input["suite_hash"],
        "eval_input_hash": ablation_input["eval_input_hash"],
        "ablation_input_hash": ablation_input["input_hash"],
        "candidate_arm_id": ablation_input["candidate_arm_id"],
        "generated_by": {
            "producer": "codex-task-independent-ablation",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
        },
        "outputs": rows,
    }


def _write_support_drafts(job_dir: Path) -> None:
    (job_dir / "reviewer/drafts.json").write_text(
        json.dumps(_review_draft_payload(job_dir), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (job_dir / "ablation/drafts.json").write_text(
        json.dumps(_ablation_draft_payload(job_dir), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def test_prepare_is_outcome_blind_and_reports_awaiting_draft(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    prepared = prepare_agent_eval_v2(
        settings,
        suite_id="private-v2-replay",
        suite_version="2.0.0",
        source="private",
        baseline_arm_id="baseline-v1",
        candidate_arm_id="candidate-v2",
    )
    job_dir = Path(prepared["job_dir"])
    input_payload = json.loads((job_dir / "input.json").read_text(encoding="utf-8"))

    assert prepared["status"] == "awaiting_draft"
    assert prepared["pending_arms"] == ["baseline-v1", "candidate-v2"]
    assert prepared["pending_tasks"] == ["reviewer", "ablation"]
    assert "outcome" not in json.dumps(input_payload)
    assert "outcome" not in (job_dir / "reviewer/input.json").read_text(encoding="utf-8")
    assert "outcome" not in (job_dir / "ablation/input.json").read_text(encoding="utf-8")
    assert input_payload["arm_manifests"]["candidate-v2"]["targets"][0]["wiki"]
    assert agent_eval_v2_status(settings, job_dir)["status"] == "awaiting_draft"
    job = list_agent_eval_v2_jobs(settings)[0]
    assert job.status == "awaiting_draft"
    assert job.release_decision == "pending"
    assert job.pending_tasks == ["reviewer", "ablation"]


def test_finalize_scores_each_target_and_is_idempotent(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    prepared = prepare_agent_eval_v2(
        settings,
        suite_id="private-v2-replay",
        suite_version=None,
        source="private",
        baseline_arm_id="baseline-v1",
        candidate_arm_id="candidate-v2",
    )
    job_dir = Path(prepared["job_dir"])
    _write_draft(job_dir, "baseline-v1")
    _write_draft(job_dir, "candidate-v2")
    _write_support_drafts(job_dir)

    assert agent_eval_v2_status(settings, job_dir)["status"] == "ready_to_finalize"
    report = finalize_agent_eval_v2(settings, job_dir)
    repeated = finalize_agent_eval_v2(settings, job_dir)
    target = report.targets["csi1000-absolute-d1"]

    assert report.release_decision == "pass"
    assert repeated == report
    assert target.decision == "pass"
    assert target.episode_count == 20
    assert target.hard_gate_pass is True
    assert target.metric_gate_pass is True
    assert target.reasoning["candidate"].mean_total_score == 8
    assert target.ablation[0].agent_id == "strategy_agent"
    assert target.ablation[0].mean_incremental_brier is not None
    assert agent_eval_v2_status(settings, job_dir)["release_decision"] == "pass"
    job = list_agent_eval_v2_jobs(settings)[0]
    assert job.status == "completed"
    assert job.release_decision == "pass"
    assert set(job.targets) == {"csi1000-absolute-d1"}
    assert job.report_hash is not None
    ablation_values = latest_agent_eval_v2_ablation_values(settings)
    assert ablation_values[
        (
            "csi1000-absolute-d1",
            "strategy_agent",
            "agent-candidate-v2",
            "model-candidate-v2",
            "prompt-candidate-v2",
        )
    ] == target.ablation[0].mean_incremental_brier
    assert stat.S_IMODE((job_dir / "candidate-v2" / "drafts.json").stat().st_mode) == 0o400


def test_verify_finalized_job_rejects_draft_tampering_after_finalize(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    prepared = prepare_agent_eval_v2(
        settings,
        suite_id="private-v2-replay",
        suite_version=None,
        source="private",
        baseline_arm_id="baseline-v1",
        candidate_arm_id="candidate-v2",
    )
    job_dir = Path(prepared["job_dir"])
    _write_draft(job_dir, "baseline-v1")
    _write_draft(job_dir, "candidate-v2")
    _write_support_drafts(job_dir)
    report = finalize_agent_eval_v2(settings, job_dir)

    verified, report_hash = verify_finalized_agent_eval_v2_job(
        settings, job_dir / "report.json"
    )
    assert verified == report
    assert report_hash == hashlib.sha256(
        (job_dir / "report.json").read_bytes()
    ).hexdigest()

    draft_path = job_dir / "candidate-v2/drafts.json"
    draft_path.chmod(0o600)
    payload = json.loads(draft_path.read_text(encoding="utf-8"))
    payload["outputs"][0]["latency_ms"] += 1
    draft_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(
        AgentEvalV2Error,
        match="review output hash mismatch|draft hash does not match receipt",
    ):
        verify_finalized_agent_eval_v2_job(settings, job_dir / "report.json")


def test_release_verifier_requires_current_candidate_binding(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    prepared = prepare_agent_eval_v2(
        settings,
        suite_id="private-v2-replay",
        suite_version=None,
        source="private",
        baseline_arm_id="baseline-v1",
        candidate_arm_id="candidate-v2",
    )
    job_dir = Path(prepared["job_dir"])
    _write_draft(job_dir, "baseline-v1")
    _write_draft(job_dir, "candidate-v2")
    _write_support_drafts(job_dir)
    finalize_agent_eval_v2(settings, job_dir)

    stale_settings = settings.model_copy(
        update={"agent_eval_release_candidate_hash": "f" * 64}
    )
    with pytest.raises(AgentEvalV2Error, match="current release"):
        verify_finalized_agent_eval_v2_job(stale_settings, job_dir / "report.json")


def test_release_verifier_rejects_public_or_synthetic_suite(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    suite_path = settings.agent_eval_outcome_root / "private-v2-replay/suite.json"
    public_suite_path = settings.agent_eval_public_root / "private-v2-replay/suite.json"
    public_suite_path.parent.mkdir(parents=True)
    public_suite_path.write_bytes(suite_path.read_bytes())
    prepared = prepare_agent_eval_v2(
        settings,
        suite_id="private-v2-replay",
        suite_version=None,
        source="public",
        baseline_arm_id="baseline-v1",
        candidate_arm_id="candidate-v2",
    )
    public_job = Path(prepared["job_dir"])
    _write_draft(public_job, "baseline-v1")
    _write_draft(public_job, "candidate-v2")
    _write_support_drafts(public_job)
    finalize_agent_eval_v2(settings, public_job)
    with pytest.raises(AgentEvalV2Error, match="private Agent Eval suite"):
        verify_finalized_agent_eval_v2_job(settings, public_job / "report.json")

    suite_payload = json.loads(suite_path.read_text(encoding="utf-8"))
    suite_payload["synthetic"] = True
    suite_path.write_text(json.dumps(suite_payload), encoding="utf-8")
    prepared = prepare_agent_eval_v2(
        settings,
        suite_id="private-v2-replay",
        suite_version=None,
        source="private",
        baseline_arm_id="baseline-v1",
        candidate_arm_id="candidate-v2",
    )
    synthetic_job = Path(prepared["job_dir"])
    _write_draft(synthetic_job, "baseline-v1")
    _write_draft(synthetic_job, "candidate-v2")
    _write_support_drafts(synthetic_job)
    finalize_agent_eval_v2(settings, synthetic_job)
    with pytest.raises(AgentEvalV2Error, match="non-synthetic"):
        verify_finalized_agent_eval_v2_job(settings, synthetic_job / "report.json")


def test_finalize_recomputes_even_when_report_and_receipt_are_resealed(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    prepared = prepare_agent_eval_v2(
        settings,
        suite_id="private-v2-replay",
        suite_version=None,
        source="private",
        baseline_arm_id="baseline-v1",
        candidate_arm_id="candidate-v2",
    )
    job_dir = Path(prepared["job_dir"])
    _write_draft(job_dir, "baseline-v1")
    _write_draft(job_dir, "candidate-v2")
    _write_support_drafts(job_dir)
    finalize_agent_eval_v2(settings, job_dir)

    report_path = job_dir / "report.json"
    receipt_path = job_dir / "receipt.json"
    report_path.chmod(0o600)
    receipt_path.chmod(0o600)
    report_payload = json.loads(report_path.read_text(encoding="utf-8"))
    target = report_payload["targets"]["csi1000-absolute-d1"]
    target["candidate"]["mean_brier"] = 0.4
    forged_report = (
        json.dumps(report_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()
    report_path.write_bytes(forged_report)
    receipt_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt_payload["report_hash"] = hashlib.sha256(forged_report).hexdigest()
    receipt_path.write_text(
        json.dumps(receipt_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(AgentEvalV2Error, match="deterministic outcome-bound"):
        finalize_agent_eval_v2(settings, job_dir)


def test_finalize_fails_closed_on_bad_citation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    prepared = prepare_agent_eval_v2(
        settings,
        suite_id="private-v2-replay",
        suite_version=None,
        source="private",
        baseline_arm_id="baseline-v1",
        candidate_arm_id="candidate-v2",
    )
    job_dir = Path(prepared["job_dir"])
    _write_draft(job_dir, "baseline-v1")
    _write_draft(job_dir, "candidate-v2", invalid_citation=True)
    _write_support_drafts(job_dir)

    report = finalize_agent_eval_v2(settings, job_dir)

    assert report.release_decision == "fail"
    target = report.targets["csi1000-absolute-d1"]
    assert target.hard_gates["citation_valid"].passed is False


def test_failed_finalize_enters_shared_bad_case_state_machine_idempotently(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path).model_copy(update={"agent_trace_enabled": False})
    upgrade_database(settings.database_url)
    prepared = prepare_agent_eval_v2(
        settings,
        suite_id="private-v2-replay",
        suite_version=None,
        source="private",
        baseline_arm_id="baseline-v1",
        candidate_arm_id="candidate-v2",
    )
    job_dir = Path(prepared["job_dir"])
    _write_draft(job_dir, "baseline-v1")
    _write_draft(job_dir, "candidate-v2", invalid_citation=True)
    _write_support_drafts(job_dir)
    database = Database(settings.database_url)
    try:
        report = finalize_agent_eval_v2(settings, job_dir, database=database)
        repeated = finalize_agent_eval_v2(settings, job_dir, database=database)

        assert report.release_decision == "fail"
        assert repeated == report
        with database.session_factory() as session:
            bad_cases = session.scalars(select(AgentBadCase)).all()
            events = session.scalars(select(AgentBadCaseEvent)).all()
            traces = session.scalars(select(AgentTrace)).all()
            assert len(bad_cases) == 1
            assert bad_cases[0].issue_type == "agent_eval_v2_gate"
            assert bad_cases[0].status == "detected"
            assert len(events) == 1
            assert len(traces) == 1
            assert traces[0].status == "completed"
            assert traces[0].telemetry_complete is False
    finally:
        database.dispose()


def test_finalize_rejects_binding_change_and_missing_arm(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    prepared = prepare_agent_eval_v2(
        settings,
        suite_id="private-v2-replay",
        suite_version=None,
        source="private",
        baseline_arm_id="baseline-v1",
        candidate_arm_id="candidate-v2",
    )
    job_dir = Path(prepared["job_dir"])
    _write_draft(job_dir, "baseline-v1")
    with pytest.raises(AgentEvalV2Error, match="missing"):
        finalize_agent_eval_v2(settings, job_dir)

    candidate = _draft_payload(job_dir, "candidate-v2")
    candidate["input_hash"] = "f" * 64
    (job_dir / "candidate-v2" / "drafts.json").write_text(
        json.dumps(candidate), encoding="utf-8"
    )
    with pytest.raises(AgentEvalV2Error, match="frozen bindings"):
        finalize_agent_eval_v2(settings, job_dir)


def test_insufficient_sample_and_cli_contract(tmp_path: Path, capsys) -> None:
    settings = _settings(tmp_path, episode_count=2)
    prepared = prepare_agent_eval_v2(
        settings,
        suite_id="private-v2-replay",
        suite_version=None,
        source="private",
        baseline_arm_id="baseline-v1",
        candidate_arm_id="candidate-v2",
    )
    job_dir = Path(prepared["job_dir"])
    _write_draft(job_dir, "baseline-v1")
    _write_draft(job_dir, "candidate-v2")
    _write_support_drafts(job_dir)

    report = finalize_agent_eval_v2(settings, job_dir)

    assert report.release_decision == "insufficient_sample"
    assert main(["contract", "schema", "agent-eval-report-v2"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["title"] == "AgentEvalReportV2"


def test_finalize_recovers_when_report_exists_without_receipt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    prepared = prepare_agent_eval_v2(
        settings,
        suite_id="private-v2-replay",
        suite_version=None,
        source="private",
        baseline_arm_id="baseline-v1",
        candidate_arm_id="candidate-v2",
    )
    job_dir = Path(prepared["job_dir"])
    _write_draft(job_dir, "baseline-v1")
    _write_draft(job_dir, "candidate-v2")
    _write_support_drafts(job_dir)
    report = finalize_agent_eval_v2(settings, job_dir)
    (job_dir / "receipt.json").unlink()

    recovered = finalize_agent_eval_v2(settings, job_dir)

    assert recovered == report
    assert (job_dir / "receipt.json").is_file()


def test_suite_rejects_outcome_leakage_in_input_payload() -> None:
    payload = _suite_payload(episode_count=1)
    payload["episodes"][0]["input_payload"]["realized_return"] = 0.02

    with pytest.raises(ValueError, match="realized outcomes"):
        AgentEvalSuiteV2.model_validate(payload)


def test_suite_rejects_weaker_release_policy() -> None:
    payload = _suite_payload()
    payload["release_policy"]["max_brier_delta"] = 0.011

    with pytest.raises(ValueError, match="Brier regression limit"):
        AgentEvalSuiteV2.model_validate(payload)


def test_must_pass_episode_requires_semantic_invariant() -> None:
    payload = _suite_payload(episode_count=1)
    payload["episodes"][0]["must_pass_invariant"] = None
    payload["episodes"][0]["input_hash"] = episode_input_hash(payload["episodes"][0])

    with pytest.raises(ValueError, match="semantic invariant"):
        AgentEvalSuiteV2.model_validate(payload)


def test_must_pass_semantic_invariant_is_a_hard_gate(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    prepared = prepare_agent_eval_v2(
        settings,
        suite_id="private-v2-replay",
        suite_version=None,
        source="private",
        baseline_arm_id="baseline-v1",
        candidate_arm_id="candidate-v2",
    )
    job_dir = Path(prepared["job_dir"])
    _write_draft(job_dir, "baseline-v1")
    candidate = _draft_payload(job_dir, "candidate-v2")
    candidate["outputs"][0]["probabilities"] = {
        "up": 0.1,
        "neutral": 0.2,
        "down": 0.7,
    }
    (job_dir / "candidate-v2/drafts.json").write_text(
        json.dumps(candidate), encoding="utf-8"
    )
    _write_support_drafts(job_dir)

    report = finalize_agent_eval_v2(settings, job_dir)

    assert report.release_decision == "fail"
    assert (
        report.targets["csi1000-absolute-d1"]
        .hard_gates["must_pass_bad_case"]
        .passed
        is False
    )


def test_suite_counts_d1_independence_by_target_date() -> None:
    payload = _suite_payload(episode_count=2)
    first_target_date = payload["episodes"][0]["target_date"]
    payload["episodes"][1]["anchor_date"] = "2026-07-01"
    payload["episodes"][1]["target_date"] = first_target_date
    payload["episodes"][1]["input_hash"] = episode_input_hash(payload["episodes"][1])

    with pytest.raises(ValueError, match="target_date must be unique"):
        AgentEvalSuiteV2.model_validate(payload)


def test_suite_rejects_overlapping_non_d1_episodes() -> None:
    payload = _suite_payload(episode_count=2)
    payload["targets"][0]["horizon"] = "W1"
    payload["episodes"][0]["anchor_date"] = "2026-07-02"
    payload["episodes"][0]["target_date"] = "2026-07-09"
    payload["episodes"][0]["input_hash"] = episode_input_hash(payload["episodes"][0])
    payload["episodes"][1]["anchor_date"] = "2026-07-06"
    payload["episodes"][1]["target_date"] = "2026-07-13"
    payload["episodes"][1]["input_hash"] = episode_input_hash(payload["episodes"][1])

    with pytest.raises(ValueError, match="must not overlap"):
        AgentEvalSuiteV2.model_validate(payload)


def test_finalize_rejects_non_independent_reasoning_reviewer(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    prepared = prepare_agent_eval_v2(
        settings,
        suite_id="private-v2-replay",
        suite_version=None,
        source="private",
        baseline_arm_id="baseline-v1",
        candidate_arm_id="candidate-v2",
    )
    job_dir = Path(prepared["job_dir"])
    _write_draft(job_dir, "baseline-v1")
    _write_draft(job_dir, "candidate-v2")
    review = _review_draft_payload(job_dir)
    review["generated_by"]["producer"] = "codex-task-candidate-v2"
    for item in review["reviews"]:
        item["review"]["reviewer_id"] = "codex-task-candidate-v2"
    (job_dir / "reviewer/drafts.json").write_text(
        json.dumps(review), encoding="utf-8"
    )
    (job_dir / "ablation/drafts.json").write_text(
        json.dumps(_ablation_draft_payload(job_dir)), encoding="utf-8"
    )

    with pytest.raises(AgentEvalV2Error, match="independent task"):
        finalize_agent_eval_v2(settings, job_dir)


def test_arm_self_reports_are_ignored_and_support_drafts_are_required(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, episode_count=2)
    prepared = prepare_agent_eval_v2(
        settings,
        suite_id="private-v2-replay",
        suite_version=None,
        source="private",
        baseline_arm_id="baseline-v1",
        candidate_arm_id="candidate-v2",
    )
    job_dir = Path(prepared["job_dir"])
    _write_draft(job_dir, "baseline-v1")
    candidate = _draft_payload(job_dir, "candidate-v2")
    for output in candidate["outputs"]:
        output["reasoning_review"].update(
            {
                "evidence_relevance": 0,
                "causal_chain": 0,
                "target_horizon_mapping": 0,
                "counterevidence_invalidation": 0,
                "calibration_uncertainty": 0,
            }
        )
        output["ablations"][0]["probabilities"] = {
            "up": 1.0,
            "neutral": 0.0,
            "down": 0.0,
        }
    (job_dir / "candidate-v2/drafts.json").write_text(
        json.dumps(candidate), encoding="utf-8"
    )

    status = agent_eval_v2_status(settings, job_dir)
    assert status["status"] == "awaiting_draft"
    assert status["pending_tasks"] == ["reviewer", "ablation"]
    with pytest.raises(AgentEvalV2Error, match="missing"):
        finalize_agent_eval_v2(settings, job_dir)

    _write_support_drafts(job_dir)
    report = finalize_agent_eval_v2(settings, job_dir)
    target = report.targets["csi1000-absolute-d1"]
    assert target.reasoning["candidate"].mean_total_score == 8
    assert target.reasoning["candidate"].human_confirmed_severe_count == 0
    assert target.hard_gate_pass is True
    assert target.ablation[0].mean_ablated_brier != 0.0


def test_reasoning_reviewer_cannot_self_report_human_confirmation(tmp_path: Path) -> None:
    settings = _settings(tmp_path, episode_count=2)
    prepared = prepare_agent_eval_v2(
        settings,
        suite_id="private-v2-replay",
        suite_version=None,
        source="private",
        baseline_arm_id="baseline-v1",
        candidate_arm_id="candidate-v2",
    )
    job_dir = Path(prepared["job_dir"])
    _write_draft(job_dir, "baseline-v1")
    _write_draft(job_dir, "candidate-v2")
    review = _review_draft_payload(job_dir)
    review["reviews"][0]["review"]["human_confirmed_severe"] = True
    (job_dir / "reviewer/drafts.json").write_text(
        json.dumps(review), encoding="utf-8"
    )
    (job_dir / "ablation/drafts.json").write_text(
        json.dumps(_ablation_draft_payload(job_dir)), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="human_confirmed_severe"):
        finalize_agent_eval_v2(settings, job_dir)


def test_finalize_rejects_tampered_review_or_ablation_bindings(tmp_path: Path) -> None:
    settings = _settings(tmp_path, episode_count=2)
    prepared = prepare_agent_eval_v2(
        settings,
        suite_id="private-v2-replay",
        suite_version=None,
        source="private",
        baseline_arm_id="baseline-v1",
        candidate_arm_id="candidate-v2",
    )
    job_dir = Path(prepared["job_dir"])
    _write_draft(job_dir, "baseline-v1")
    _write_draft(job_dir, "candidate-v2")
    review = _review_draft_payload(job_dir)
    review["reviews"][0]["reviewed_output_hash"] = "f" * 64
    (job_dir / "reviewer/drafts.json").write_text(json.dumps(review), encoding="utf-8")
    (job_dir / "ablation/drafts.json").write_text(
        json.dumps(_ablation_draft_payload(job_dir)), encoding="utf-8"
    )
    with pytest.raises(AgentEvalV2Error, match="review output hash mismatch"):
        finalize_agent_eval_v2(settings, job_dir)


def test_finalize_rejects_tampered_ablation_binding(tmp_path: Path) -> None:
    settings = _settings(tmp_path, episode_count=2)
    prepared = prepare_agent_eval_v2(
        settings,
        suite_id="private-v2-replay",
        suite_version=None,
        source="private",
        baseline_arm_id="baseline-v1",
        candidate_arm_id="candidate-v2",
    )
    job_dir = Path(prepared["job_dir"])
    _write_draft(job_dir, "baseline-v1")
    _write_draft(job_dir, "candidate-v2")
    (job_dir / "reviewer/drafts.json").write_text(
        json.dumps(_review_draft_payload(job_dir)), encoding="utf-8"
    )
    ablation = _ablation_draft_payload(job_dir)
    ablation["outputs"][0]["full_output_hash"] = "f" * 64
    (job_dir / "ablation/drafts.json").write_text(
        json.dumps(ablation), encoding="utf-8"
    )
    with pytest.raises(AgentEvalV2Error, match="ablation full output hash mismatch"):
        finalize_agent_eval_v2(settings, job_dir)


def test_review_and_ablation_drafts_reject_outcome_fields(tmp_path: Path) -> None:
    settings = _settings(tmp_path, episode_count=2)
    prepared = prepare_agent_eval_v2(
        settings,
        suite_id="private-v2-replay",
        suite_version=None,
        source="private",
        baseline_arm_id="baseline-v1",
        candidate_arm_id="candidate-v2",
    )
    job_dir = Path(prepared["job_dir"])
    _write_draft(job_dir, "baseline-v1")
    _write_draft(job_dir, "candidate-v2")
    review = _review_draft_payload(job_dir)
    review["outcome"] = "up"
    (job_dir / "reviewer/drafts.json").write_text(json.dumps(review), encoding="utf-8")
    (job_dir / "ablation/drafts.json").write_text(
        json.dumps(_ablation_draft_payload(job_dir)), encoding="utf-8"
    )
    with pytest.raises(AgentEvalV2Error, match="must not contain realized outcomes"):
        finalize_agent_eval_v2(settings, job_dir)
