from __future__ import annotations

import json
from pathlib import Path

from app.config import REPOSITORY_ROOT, Settings
from app.db import Database
from app.models import AgentBadCase, AgentEvalResult, AgentTrace
from app.services.agent_evaluation import (
    AgentEvalSuite,
    BadCaseTransition,
    EvalRunRequest,
    enqueue_experiment,
    run_next_eval_task,
    transition_bad_case,
)
from sqlalchemy import func, select


def _settings(tmp_path: Path) -> Settings:
    return Settings().model_copy(
        update={
            "database_url": f"sqlite:///{tmp_path / 'eval.sqlite3'}",
            "agent_eval_public_root": REPOSITORY_ROOT / "benchmarks",
            "agent_eval_private_root": tmp_path / "evals",
        }
    )


def test_public_agent_workflow_suite_passes_release_gate(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    database.create_all()
    try:
        experiment = enqueue_experiment(
            database,
            settings,
            EvalRunRequest(
                suite_id="agent-workflow-v1",
                baseline_target_id="baseline-v1",
                candidate_target_id="candidate-v2",
            ),
            idempotency_key="public-pass",
        )
        completed = run_next_eval_task(database, settings, worker_id="test-worker")
        assert completed is not None
        assert completed.id == experiment.id
        assert completed.status == "completed"
        assert completed.release_decision == "pass"
        assert completed.summary["hard_gate_pass"] is True
        assert completed.summary["metric_gate_pass"] is True
        with database.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(AgentEvalResult)) == 280
            trace = session.scalar(select(AgentTrace).where(AgentTrace.subject_id == completed.id))
            assert trace is not None
            assert trace.status == "completed"
            assert session.scalar(select(func.count()).select_from(AgentBadCase)) == 0
    finally:
        database.dispose()


def test_failed_gate_flows_into_private_bad_case_dataset(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    suite_data = json.loads(
        (REPOSITORY_ROOT / "benchmarks" / "agent-workflow-v1" / "suite.json").read_text(
            encoding="utf-8"
        )
    )
    suite_data["suite_id"] = "private-regression"
    suite_data["version"] = "1.0.0"
    suite_data["synthetic"] = False
    suite_data["cases"][0]["arm_outputs"]["candidate-v2"]["hard_gates"]["schema_valid"] = False
    suite = AgentEvalSuite.model_validate(suite_data)
    suite_path = settings.agent_eval_private_root / "suites" / "private-regression"
    suite_path.mkdir(parents=True)
    (suite_path / "suite.json").write_text(
        suite.model_dump_json(indent=2),
        encoding="utf-8",
    )

    database = Database(settings.database_url)
    database.create_all()
    try:
        enqueue_experiment(
            database,
            settings,
            EvalRunRequest(
                suite_id="private-regression",
                baseline_target_id="baseline-v1",
                candidate_target_id="candidate-v2",
                source="private",
            ),
            idempotency_key="private-fail",
        )
        completed = run_next_eval_task(database, settings, worker_id="test-worker")
        assert completed is not None
        assert completed.release_decision == "fail"
        with database.session_factory() as session:
            bad_case = session.scalar(select(AgentBadCase))
            assert bad_case is not None
            bad_case_id = bad_case.id

        row = transition_bad_case(
            database,
            settings,
            bad_case_id,
            BadCaseTransition(to_status="triaged", actor="tester"),
            idempotency_key="triage-1",
        )
        assert row.status == "triaged"
        row = transition_bad_case(
            database,
            settings,
            bad_case_id,
            BadCaseTransition(
                to_status="confirmed",
                actor="tester",
                test_case={"input": {"snapshot": "sealed"}, "expected": "schema_valid"},
            ),
            idempotency_key="confirm-1",
        )
        assert row.status == "confirmed"
        row = transition_bad_case(
            database,
            settings,
            bad_case_id,
            BadCaseTransition(
                to_status="materialized",
                actor="tester",
                dataset_id="regressions",
                dataset_version="2026.08.07",
            ),
            idempotency_key="materialize-1",
        )
        artifact = (
            settings.agent_eval_private_root
            / "datasets"
            / "regressions"
            / "2026.08.07"
            / f"{bad_case_id}.json"
        )
        assert row.status == "materialized"
        assert artifact.is_file()
        assert json.loads(artifact.read_text(encoding="utf-8"))["bad_case_id"] == bad_case_id
    finally:
        database.dispose()
