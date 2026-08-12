"""Versioned offline Agent evaluation and bad-case governance.

The runner is intentionally deterministic.  It evaluates frozen fixture or
replay projections and never invokes a model or mutates a production upstream.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from ..config import Settings
from ..db import Database
from ..models import (
    AgentBadCase,
    AgentBadCaseEvent,
    AgentEvalExperiment,
    AgentEvalResult,
    AgentEvalTask,
    AgentTrace,
)
from .agent_tracing import TraceRecorder, canonical_digest

SUITE_SCHEMA_VERSION = "forecast-loop.agent-eval-suite/v1"
REPORT_SCHEMA_VERSION = "forecast-loop.agent-eval-report/v1"
BAD_CASE_SCHEMA_VERSION = "forecast-loop.agent-bad-case/v1"
RELEASE_POLICY_VERSION = "1.0.0"
EVALUATOR_VERSION = "1.0.0"


class EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReleasePolicy(EvalModel):
    version: str = RELEASE_POLICY_VERSION
    min_metric_cases: int = Field(default=20, ge=1)
    must_pass_rate: float = Field(default=1.0, ge=0, le=1)
    max_brier_delta: float = Field(default=0.01, ge=0)
    max_direction_drop: float = Field(default=0.02, ge=0, le=1)
    max_p95_latency_ratio: float = Field(default=1.2, ge=1)
    max_token_ratio: float = Field(default=1.15, ge=1)


class FixtureOutput(EvalModel):
    status: Literal["completed", "failed"]
    trajectory: list[str] = Field(min_length=1)
    hard_gates: dict[str, bool] = Field(default_factory=dict)
    direction_correct: bool | None = None
    brier: float | None = Field(default=None, ge=0, le=2)
    latency_ms: float = Field(ge=0)
    total_tokens: int = Field(ge=0)
    qualitative_score: float | None = Field(default=None, ge=0, le=1)
    output_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class EvalCase(EvalModel):
    case_id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=200)
    workflow_kind: Literal["prediction", "reflection"]
    tags: list[str] = Field(default_factory=list)
    must_pass: bool = False
    expected_trajectory: list[str] = Field(min_length=1)
    arm_outputs: dict[str, FixtureOutput]


class EvalTarget(EvalModel):
    target_id: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=32)
    description: str = ""
    model_name: str | None = None
    workflow_version: str | None = None
    prompt_version: str | None = None


class AgentEvalSuite(EvalModel):
    schema_version: Literal["forecast-loop.agent-eval-suite/v1"]
    suite_id: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    synthetic: bool
    runner_kind: Literal["fixture", "reflection_replay"] = "fixture"
    targets: list[EvalTarget] = Field(min_length=2)
    cases: list[EvalCase] = Field(min_length=1)
    release_policy: ReleasePolicy = Field(default_factory=ReleasePolicy)

    @model_validator(mode="after")
    def validate_identities(self) -> AgentEvalSuite:
        target_ids = [target.target_id for target in self.targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("suite target IDs must be unique")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("suite case IDs must be unique")
        required_targets = set(target_ids)
        for case in self.cases:
            if set(case.arm_outputs) != required_targets:
                raise ValueError(f"case {case.case_id} must provide exactly all target outputs")
        return self


class SuiteDescriptor(EvalModel):
    suite_id: str
    version: str
    title: str
    description: str
    synthetic: bool
    runner_kind: str
    case_count: int
    target_ids: list[str]
    content_hash: str
    source: Literal["public", "private"]


class EvalRunRequest(EvalModel):
    suite_id: str
    suite_version: str | None = None
    baseline_target_id: str
    candidate_target_id: str
    source: Literal["public", "private"] = "public"


class BadCaseCreate(EvalModel):
    trace_id: str
    span_id: str | None = None
    issue_type: str = Field(min_length=1, max_length=64)
    severity: Literal["low", "medium", "high", "critical"]
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1)
    expected_behavior: str = ""
    input_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class BadCaseTransition(EvalModel):
    to_status: Literal["triaged", "confirmed", "materialized", "resolved", "rejected"]
    actor: str = Field(min_length=1, max_length=120)
    notes: str = ""
    dataset_id: str | None = Field(default=None, max_length=120)
    dataset_version: str | None = Field(default=None, max_length=32)
    test_case: dict[str, Any] | None = None


class AgentEvalError(RuntimeError):
    pass


class AgentEvalStore:
    """Load strict suites without allowing path traversal or symlink escape."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def list_suites(self) -> list[SuiteDescriptor]:
        descriptors: list[SuiteDescriptor] = []
        for source, root in (
            ("public", self.settings.agent_eval_public_root),
            ("private", self.settings.agent_eval_private_root / "suites"),
        ):
            if not root.exists():
                continue
            for path in sorted(root.glob("*/suite.json")):
                try:
                    suite = self._read_suite(path, root=root)
                except (OSError, ValueError):
                    continue
                descriptors.append(self.describe(suite, source=source))
        return descriptors

    def load(
        self,
        suite_id: str,
        *,
        version: str | None,
        source: Literal["public", "private"],
    ) -> AgentEvalSuite:
        _validate_component(suite_id, "suite_id")
        root = (
            self.settings.agent_eval_public_root
            if source == "public"
            else self.settings.agent_eval_private_root / "suites"
        )
        candidates: list[tuple[Path, AgentEvalSuite]] = []
        if not root.exists():
            raise AgentEvalError(f"Agent eval {source} suite root does not exist")
        for path in sorted(root.glob("*/suite.json")):
            suite = self._read_suite(path, root=root)
            if suite.suite_id == suite_id and (version is None or suite.version == version):
                candidates.append((path, suite))
        if not candidates:
            suffix = f" version {version}" if version else ""
            raise AgentEvalError(f"suite {suite_id}{suffix} was not found")
        if len(candidates) > 1:
            raise AgentEvalError("suite identity is ambiguous; specify suite_version")
        return candidates[0][1]

    def describe(
        self,
        suite: AgentEvalSuite,
        *,
        source: Literal["public", "private"],
    ) -> SuiteDescriptor:
        return SuiteDescriptor(
            suite_id=suite.suite_id,
            version=suite.version,
            title=suite.title,
            description=suite.description,
            synthetic=suite.synthetic,
            runner_kind=suite.runner_kind,
            case_count=len(suite.cases),
            target_ids=[target.target_id for target in suite.targets],
            content_hash=suite_hash(suite),
            source=source,
        )

    def _read_suite(self, path: Path, *, root: Path) -> AgentEvalSuite:
        resolved_root = root.resolve()
        resolved_path = path.resolve(strict=True)
        if path.is_symlink() or not resolved_path.is_relative_to(resolved_root):
            raise ValueError("suite path escapes configured root")
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("suite exceeds the 4 MiB limit")
        return AgentEvalSuite.model_validate_json(path.read_text(encoding="utf-8"))


def enqueue_experiment(
    database: Database,
    settings: Settings,
    request: EvalRunRequest,
    *,
    idempotency_key: str,
) -> AgentEvalExperiment:
    suite = AgentEvalStore(settings).load(
        request.suite_id,
        version=request.suite_version,
        source=request.source,
    )
    target_by_id = {target.target_id: target for target in suite.targets}
    try:
        baseline = target_by_id[request.baseline_target_id]
        candidate = target_by_id[request.candidate_target_id]
    except KeyError as exc:
        raise AgentEvalError(f"unknown eval target: {exc.args[0]}") from exc
    if baseline.target_id == candidate.target_id:
        raise AgentEvalError("baseline and candidate targets must be different")
    now = _now(settings)
    payload = {
        **request.model_dump(mode="json"),
        "suite_version": suite.version,
        "suite_hash": suite_hash(suite),
    }
    payload_hash = canonical_digest(payload)
    with database.session_factory() as session:
        existing = session.scalar(
            select(AgentEvalTask)
            .options(selectinload(AgentEvalTask.experiment))
            .where(AgentEvalTask.idempotency_key == idempotency_key)
        )
        if existing is not None:
            if existing.payload_hash != payload_hash:
                raise AgentEvalError("idempotency key already binds a different request")
            return existing.experiment
        experiment = AgentEvalExperiment(
            id=str(uuid4()),
            suite_id=suite.suite_id,
            suite_version=suite.version,
            suite_hash=suite_hash(suite),
            baseline_target_id=baseline.target_id,
            baseline_target_hash=target_hash(suite, baseline.target_id),
            candidate_target_id=candidate.target_id,
            candidate_target_hash=target_hash(suite, candidate.target_id),
            status="queued",
            release_decision="pending",
            policy_version=suite.release_policy.version,
            created_at=now,
            started_at=None,
            completed_at=None,
            report_hash=None,
            error=None,
            summary={},
        )
        session.add(experiment)
        session.add(
            AgentEvalTask(
                id=str(uuid4()),
                experiment_id=experiment.id,
                status="queued",
                idempotency_key=idempotency_key[:255],
                payload=payload,
                payload_hash=payload_hash,
                attempt_count=0,
                max_attempts=2,
                available_at=now,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                last_error=None,
                created_at=now,
                updated_at=now,
                completed_at=None,
                version=0,
            )
        )
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise AgentEvalError("experiment enqueue conflicted with another request") from exc
        session.refresh(experiment)
        return experiment


def run_next_eval_task(
    database: Database,
    settings: Settings,
    *,
    worker_id: str,
) -> AgentEvalExperiment | None:
    now = _now(settings)
    lease_token = str(uuid4())
    with database.session_factory() as session:
        expired = session.scalars(
            select(AgentEvalTask).where(
                AgentEvalTask.status == "running",
                AgentEvalTask.lease_expires_at.is_not(None),
                AgentEvalTask.lease_expires_at <= now,
            )
        ).all()
        for expired_task in expired:
            expired_experiment = session.get(
                AgentEvalExperiment,
                expired_task.experiment_id,
            )
            terminal = expired_task.attempt_count >= expired_task.max_attempts
            expired_task.status = "failed" if terminal else "retry_wait"
            expired_task.available_at = now
            expired_task.lease_owner = None
            expired_task.lease_token = None
            expired_task.lease_expires_at = None
            expired_task.last_error = "Agent eval worker lease expired"
            expired_task.updated_at = now
            expired_task.completed_at = now if terminal else None
            expired_task.version += 1
            if expired_experiment is not None:
                expired_experiment.status = "failed" if terminal else "queued"
                expired_experiment.error = expired_task.last_error
                expired_experiment.completed_at = now if terminal else None
        if expired:
            session.commit()
        task = session.scalar(
            select(AgentEvalTask)
            .where(
                AgentEvalTask.status.in_(["queued", "retry_wait"]),
                AgentEvalTask.available_at <= now,
            )
            .order_by(AgentEvalTask.created_at, AgentEvalTask.id)
        )
        if task is None:
            return None
        task.status = "running"
        task.attempt_count += 1
        task.lease_owner = worker_id[:120]
        task.lease_token = lease_token
        task.lease_expires_at = now + timedelta(minutes=10)
        task.updated_at = now
        task.version += 1
        experiment = session.get(AgentEvalExperiment, task.experiment_id)
        assert experiment is not None
        experiment.status = "running"
        experiment.started_at = now
        session.commit()
        task_id = task.id
        attempt_number = task.attempt_count
        payload = dict(task.payload)
        experiment_id = experiment.id

    recorder = TraceRecorder(database, settings)
    trace_id = recorder.start_trace(
        workflow_kind="agent_eval",
        subject_id=experiment_id,
        mode="offline",
        input_hash=payload["suite_hash"],
        attempt_number=attempt_number,
        target_id=payload["candidate_target_id"],
        attributes={"suite_hash": payload["suite_hash"], "experiment_id": experiment_id},
    )
    try:
        suite = AgentEvalStore(settings).load(
            payload["suite_id"],
            version=payload["suite_version"],
            source=payload["source"],
        )
        if suite_hash(suite) != payload["suite_hash"]:
            raise AgentEvalError("suite changed after the experiment was queued")
        with recorder.span(
            workflow_kind="agent_eval",
            subject_id=experiment_id,
            node_id="deterministic_evaluators",
            name="Run deterministic evaluators",
            span_kind="validator",
            attributes={"case_id": "all", "experiment_id": experiment_id},
            trace_id=trace_id,
        ):
            report, rows = _evaluate_suite(
                suite,
                baseline_target_id=payload["baseline_target_id"],
                candidate_target_id=payload["candidate_target_id"],
            )
        completed_at = _now(settings)
        with database.session_factory() as session:
            task = session.get(AgentEvalTask, task_id)
            experiment = session.get(AgentEvalExperiment, experiment_id)
            if task is None or experiment is None or task.lease_token != lease_token:
                raise AgentEvalError("eval task lease was lost before persistence")
            for row in rows:
                session.add(
                    AgentEvalResult(
                        id=str(uuid4()),
                        experiment_id=experiment_id,
                        created_at=completed_at,
                        trace_id=trace_id,
                        **row,
                    )
                )
            experiment.status = "completed"
            experiment.release_decision = report["release_decision"]
            experiment.completed_at = completed_at
            experiment.report_hash = canonical_digest(report)
            experiment.error = None
            experiment.summary = report
            task.status = "completed"
            task.completed_at = completed_at
            task.updated_at = completed_at
            task.lease_owner = None
            task.lease_token = None
            task.lease_expires_at = None
            task.version += 1
            session.commit()
            _detect_eval_bad_cases(session, experiment, settings=settings)
            session.refresh(experiment)
            for result in experiment.results:
                recorder.link_artifact(
                    workflow_kind="agent_eval",
                    subject_id=experiment_id,
                    trace_id=trace_id,
                    artifact_kind="evaluation",
                    artifact_id=result.id,
                    relation="output",
                    content_hash=result.output_hash,
                )
            for bad_case in session.scalars(
                select(AgentBadCase).where(AgentBadCase.trace_id == trace_id)
            ).all():
                recorder.link_artifact(
                    workflow_kind="agent_eval",
                    subject_id=experiment_id,
                    trace_id=trace_id,
                    artifact_kind="bad_case",
                    artifact_id=bad_case.id,
                    relation="diagnostic",
                )
        recorder.finish_trace(
            workflow_kind="agent_eval",
            subject_id=experiment_id,
            status="completed",
            attributes={"experiment_id": experiment_id},
            trace_id=trace_id,
        )
        return experiment
    except Exception as exc:
        _fail_eval_task(
            database,
            settings,
            task_id=task_id,
            lease_token=lease_token,
            experiment_id=experiment_id,
            error=exc,
        )
        recorder.finish_trace(
            workflow_kind="agent_eval",
            subject_id=experiment_id,
            status="failed",
            error=exc,
            trace_id=trace_id,
        )
        raise


def create_bad_case(
    database: Database,
    settings: Settings,
    request: BadCaseCreate,
    *,
    actor: str,
    idempotency_key: str,
) -> AgentBadCase:
    now = _now(settings)
    with database.session_factory() as session:
        trace = session.get(AgentTrace, request.trace_id)
        if trace is None:
            raise AgentEvalError("trace was not found")
        dedupe_hash = canonical_digest(
            {
                "trace_id": trace.id,
                "span_id": request.span_id,
                "issue_type": request.issue_type,
                "title": request.title,
            }
        )
        existing = session.scalar(
            select(AgentBadCase).where(AgentBadCase.dedupe_hash == dedupe_hash)
        )
        if existing is not None:
            return existing
        row = AgentBadCase(
            id=str(uuid4()),
            trace_id=trace.id,
            span_id=request.span_id,
            eval_result_id=None,
            workflow_kind=trace.workflow_kind,
            issue_type=request.issue_type,
            severity=request.severity,
            status="detected",
            title=request.title,
            summary=request.summary,
            expected_behavior=request.expected_behavior,
            input_hash=request.input_hash or trace.input_hash,
            dedupe_hash=dedupe_hash,
            dataset_id=None,
            dataset_version=None,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        _append_bad_case_event(
            session,
            row,
            to_status="detected",
            actor=actor,
            notes="Bad case detected from trace",
            payload={},
            idempotency_key=idempotency_key,
            occurred_at=now,
        )
        session.commit()
        session.refresh(row)
        return row


def transition_bad_case(
    database: Database,
    settings: Settings,
    bad_case_id: str,
    transition: BadCaseTransition,
    *,
    idempotency_key: str,
) -> AgentBadCase:
    now = _now(settings)
    with database.session_factory() as session:
        row = session.scalar(
            select(AgentBadCase)
            .options(selectinload(AgentBadCase.events))
            .where(AgentBadCase.id == bad_case_id)
        )
        if row is None:
            raise AgentEvalError("bad case was not found")
        existing = next(
            (event for event in row.events if event.idempotency_key == idempotency_key),
            None,
        )
        if existing is not None:
            return row
        _validate_transition(row.status, transition.to_status)
        payload: dict[str, Any] = {}
        materialized_path: Path | None = None
        if transition.test_case is not None:
            payload["test_case"] = transition.test_case
        if transition.to_status == "materialized":
            if not transition.dataset_id or not transition.dataset_version:
                raise AgentEvalError("materialization requires dataset_id and dataset_version")
            test_case = transition.test_case or _confirmed_test_case(row.events)
            if test_case is None:
                raise AgentEvalError("materialization requires a confirmed test_case payload")
            artifact = _materialize_bad_case(
                settings,
                row,
                dataset_id=transition.dataset_id,
                dataset_version=transition.dataset_version,
                test_case=test_case,
            )
            row.dataset_id = transition.dataset_id
            row.dataset_version = transition.dataset_version
            payload.update(artifact)
            materialized_path = (
                settings.agent_eval_private_root / str(artifact["artifact_path"])
            )
        try:
            _append_bad_case_event(
                session,
                row,
                to_status=transition.to_status,
                actor=transition.actor,
                notes=transition.notes,
                payload=payload,
                idempotency_key=idempotency_key,
                occurred_at=now,
            )
            row.status = transition.to_status
            row.updated_at = now
            session.commit()
        except Exception:
            if materialized_path is not None and materialized_path.is_file():
                materialized_path.unlink()
            raise
        session.refresh(row)
        return row


def suite_hash(suite: AgentEvalSuite) -> str:
    return canonical_digest(suite.model_dump(mode="json"))


def target_hash(suite: AgentEvalSuite, target_id: str) -> str:
    target = next(target for target in suite.targets if target.target_id == target_id)
    return canonical_digest(
        {
            "target": target.model_dump(mode="json"),
            "outputs": [
                case.arm_outputs[target_id].model_dump(mode="json") for case in suite.cases
            ],
        }
    )


def _evaluate_suite(
    suite: AgentEvalSuite,
    *,
    baseline_target_id: str,
    candidate_target_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    for arm, target_id in (
        ("baseline", baseline_target_id),
        ("candidate", candidate_target_id),
    ):
        for case in suite.cases:
            output = case.arm_outputs[target_id]
            digest = output.output_digest or canonical_digest(output.model_dump(mode="json"))
            trajectory_passed = output.trajectory == case.expected_trajectory
            hard_gate_passed = output.status == "completed" and all(output.hard_gates.values())
            rows.extend(
                [
                    _result_row(
                        arm,
                        case,
                        "trajectory_exact",
                        "trajectory",
                        1.0 if trajectory_passed else 0.0,
                        trajectory_passed,
                        digest,
                        "Workflow node order matches the frozen expectation.",
                    ),
                    _result_row(
                        arm,
                        case,
                        "hard_gate",
                        "hard_gate",
                        _bool_mean(output.hard_gates),
                        hard_gate_passed,
                        digest,
                        "All deterministic validators and completion gates pass.",
                    ),
                    _result_row(
                        arm,
                        case,
                        "direction_accuracy",
                        "direction_accuracy",
                        None
                        if output.direction_correct is None
                        else float(output.direction_correct),
                        output.direction_correct,
                        digest,
                        "Outcome-bound direction accuracy.",
                    ),
                    _result_row(
                        arm,
                        case,
                        "brier",
                        "brier",
                        output.brier,
                        None,
                        digest,
                        "Outcome-bound multiclass Brier score; lower is better.",
                    ),
                    _result_row(
                        arm,
                        case,
                        "latency_ms",
                        "latency_ms",
                        output.latency_ms,
                        None,
                        digest,
                        "End-to-end replay latency in milliseconds.",
                    ),
                    _result_row(
                        arm,
                        case,
                        "total_tokens",
                        "tokens",
                        float(output.total_tokens),
                        None,
                        digest,
                        "Total input and output tokens reported by the frozen fixture.",
                    ),
                    _result_row(
                        arm,
                        case,
                        "qualitative_score",
                        "judge_advisory",
                        output.qualitative_score,
                        None,
                        digest,
                        "Advisory qualitative score; never a hard release gate.",
                    ),
                ]
            )
    summary = _release_summary(suite, rows)
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "suite_id": suite.suite_id,
        "suite_version": suite.version,
        "suite_hash": suite_hash(suite),
        "baseline_target_id": baseline_target_id,
        "candidate_target_id": candidate_target_id,
        **summary,
    }
    return report, rows


def _release_summary(suite: AgentEvalSuite, rows: list[dict[str, Any]]) -> dict[str, Any]:
    policy = suite.release_policy
    candidate = [row for row in rows if row["arm"] == "candidate"]
    must_pass_ids = {case.case_id for case in suite.cases if case.must_pass}
    required = [
        row
        for row in candidate
        if row["case_id"] in must_pass_ids
        and row["evaluator_id"] in {"trajectory_exact", "hard_gate"}
    ]
    must_pass_rate = (
        sum(row["passed"] is True for row in required) / len(required) if required else 1.0
    )
    hard_gate_pass = must_pass_rate >= policy.must_pass_rate

    baseline_metrics = _aggregate_metrics(rows, "baseline")
    candidate_metrics = _aggregate_metrics(rows, "candidate")
    sample_count = min(
        baseline_metrics["outcome_case_count"],
        candidate_metrics["outcome_case_count"],
    )
    metric_gates = {
        "brier_delta": _delta(candidate_metrics["mean_brier"], baseline_metrics["mean_brier"]),
        "direction_drop": _delta(
            baseline_metrics["direction_accuracy"], candidate_metrics["direction_accuracy"]
        ),
        "p95_latency_ratio": _ratio(
            candidate_metrics["p95_latency_ms"], baseline_metrics["p95_latency_ms"]
        ),
        "token_ratio": _ratio(candidate_metrics["mean_tokens"], baseline_metrics["mean_tokens"]),
    }
    metric_gate_pass = (
        metric_gates["brier_delta"] is not None
        and metric_gates["brier_delta"] <= policy.max_brier_delta
        and metric_gates["direction_drop"] is not None
        and metric_gates["direction_drop"] <= policy.max_direction_drop
        and metric_gates["p95_latency_ratio"] is not None
        and metric_gates["p95_latency_ratio"] <= policy.max_p95_latency_ratio
        and metric_gates["token_ratio"] is not None
        and metric_gates["token_ratio"] <= policy.max_token_ratio
    )
    if not hard_gate_pass:
        decision = "fail"
    elif sample_count < policy.min_metric_cases:
        decision = "insufficient_sample"
    elif metric_gate_pass:
        decision = "pass"
    else:
        decision = "fail"
    return {
        "release_decision": decision,
        "policy": policy.model_dump(mode="json"),
        "case_count": len(suite.cases),
        "outcome_case_count": sample_count,
        "must_pass_rate": must_pass_rate,
        "hard_gate_pass": hard_gate_pass,
        "metric_gate_pass": metric_gate_pass if sample_count >= policy.min_metric_cases else None,
        "metric_gates": metric_gates,
        "baseline": baseline_metrics,
        "candidate": candidate_metrics,
    }


def _aggregate_metrics(rows: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    selected = [row for row in rows if row["arm"] == arm]
    directions = _scores(selected, "direction_accuracy")
    briers = _scores(selected, "brier")
    latencies = _scores(selected, "latency_ms")
    tokens = _scores(selected, "total_tokens")
    qualitative = _scores(selected, "qualitative_score")
    return {
        "outcome_case_count": min(len(directions), len(briers)),
        "direction_accuracy": _mean(directions),
        "mean_brier": _mean(briers),
        "p95_latency_ms": _percentile(latencies, 0.95),
        "mean_tokens": _mean(tokens),
        "qualitative_advisory": _mean(qualitative),
    }


def _result_row(
    arm: str,
    case: EvalCase,
    evaluator_id: str,
    metric_kind: str,
    score: float | None,
    passed: bool | None,
    output_hash: str,
    explanation: str,
) -> dict[str, Any]:
    return {
        "arm": arm,
        "case_id": case.case_id,
        "evaluator_id": evaluator_id,
        "evaluator_version": EVALUATOR_VERSION,
        "metric_kind": metric_kind,
        "score": score,
        "passed": passed,
        "status": (
            "not_applicable"
            if score is None or passed is None
            else "passed"
            if passed
            else "failed"
        ),
        "label": "must_pass" if case.must_pass else None,
        "explanation": explanation,
        "output_hash": output_hash,
    }


def _detect_eval_bad_cases(session, experiment: AgentEvalExperiment, *, settings: Settings) -> None:
    trace_id = session.scalar(
        select(AgentTrace.id).where(
            AgentTrace.workflow_kind == "agent_eval",
            AgentTrace.subject_id == experiment.id,
        )
    )
    if trace_id is None:
        return
    failures = session.scalars(
        select(AgentEvalResult).where(
            AgentEvalResult.experiment_id == experiment.id,
            AgentEvalResult.arm == "candidate",
            AgentEvalResult.passed.is_(False),
            AgentEvalResult.evaluator_id.in_(["trajectory_exact", "hard_gate"]),
        )
    ).all()
    now = _now(settings)
    for result in failures:
        dedupe_hash = canonical_digest({"experiment_id": experiment.id, "result_id": result.id})
        if session.scalar(select(AgentBadCase.id).where(AgentBadCase.dedupe_hash == dedupe_hash)):
            continue
        row = AgentBadCase(
            id=str(uuid4()),
            trace_id=trace_id,
            span_id=None,
            eval_result_id=result.id,
            workflow_kind="agent_eval",
            issue_type=f"eval_{result.evaluator_id}",
            severity="high" if result.label == "must_pass" else "medium",
            status="detected",
            title=f"{result.case_id}: {result.evaluator_id} failed",
            summary=result.explanation,
            expected_behavior="Candidate must satisfy the frozen evaluator contract.",
            input_hash=experiment.suite_hash,
            dedupe_hash=dedupe_hash,
            dataset_id=None,
            dataset_version=None,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        _append_bad_case_event(
            session,
            row,
            to_status="detected",
            actor="agent-eval-worker",
            notes="Automatically detected by a deterministic evaluator",
            payload={"eval_result_id": result.id, "experiment_id": experiment.id},
            idempotency_key=f"detected:{result.id}",
            occurred_at=now,
        )
    session.commit()


def _append_bad_case_event(
    session,
    row: AgentBadCase,
    *,
    to_status: str,
    actor: str,
    notes: str,
    payload: dict[str, Any],
    idempotency_key: str,
    occurred_at: datetime,
) -> AgentBadCaseEvent:
    events = sorted(row.events, key=lambda event: event.sequence_number) if row.events else []
    previous = events[-1] if events else None
    sequence = previous.sequence_number + 1 if previous else 1
    envelope = {
        "schema_version": BAD_CASE_SCHEMA_VERSION,
        "bad_case_id": row.id,
        "sequence_number": sequence,
        "event_type": to_status,
        "from_status": row.status if previous else None,
        "to_status": to_status,
        "actor": actor,
        "notes": notes,
        "payload": payload,
        "previous_event_hash": previous.content_hash if previous else None,
        "occurred_at": occurred_at.isoformat(),
    }
    event = AgentBadCaseEvent(
        id=str(uuid4()),
        bad_case_id=row.id,
        sequence_number=sequence,
        event_type=to_status,
        from_status=envelope["from_status"],
        to_status=to_status,
        idempotency_key=idempotency_key[:160],
        actor=actor,
        notes=notes,
        payload=payload,
        previous_event_hash=envelope["previous_event_hash"],
        content_hash=canonical_digest(envelope),
        occurred_at=occurred_at,
    )
    session.add(event)
    row.events.append(event)
    return event


def _validate_transition(current: str, target: str) -> None:
    allowed = {
        "detected": {"triaged", "rejected"},
        "triaged": {"confirmed", "rejected"},
        "confirmed": {"materialized", "rejected"},
        "materialized": {"resolved"},
        "resolved": set(),
        "rejected": set(),
    }
    if target not in allowed[current]:
        raise AgentEvalError(f"invalid bad-case transition: {current} -> {target}")


def _confirmed_test_case(events: list[AgentBadCaseEvent]) -> dict[str, Any] | None:
    for event in sorted(events, key=lambda item: item.sequence_number, reverse=True):
        test_case = (event.payload or {}).get("test_case")
        if event.to_status == "confirmed" and isinstance(test_case, dict):
            return test_case
    return None


def _materialize_bad_case(
    settings: Settings,
    row: AgentBadCase,
    *,
    dataset_id: str,
    dataset_version: str,
    test_case: dict[str, Any],
) -> dict[str, Any]:
    _validate_component(dataset_id, "dataset_id")
    _validate_component(dataset_version, "dataset_version")
    root = (settings.agent_eval_private_root / "datasets").resolve()
    destination = (root / dataset_id / dataset_version / f"{row.id}.json").resolve()
    if not destination.is_relative_to(root):
        raise AgentEvalError("bad-case materialization path escapes the private root")
    if destination.exists() or destination.is_symlink():
        raise AgentEvalError("bad-case dataset artifact already exists")
    payload = {
        "schema_version": BAD_CASE_SCHEMA_VERSION,
        "bad_case_id": row.id,
        "trace_id": row.trace_id,
        "workflow_kind": row.workflow_kind,
        "issue_type": row.issue_type,
        "severity": row.severity,
        "title": row.title,
        "summary": row.summary,
        "expected_behavior": row.expected_behavior,
        "input_hash": row.input_hash,
        "test_case": test_case,
    }
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode(
            "utf-8"
        )
        + b"\n"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(4)}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        "artifact_path": str(destination.relative_to(settings.agent_eval_private_root.resolve())),
        "artifact_hash": hashlib.sha256(encoded).hexdigest(),
    }


def _fail_eval_task(
    database: Database,
    settings: Settings,
    *,
    task_id: str,
    lease_token: str,
    experiment_id: str,
    error: Exception,
) -> None:
    now = _now(settings)
    with database.session_factory() as session:
        task = session.get(AgentEvalTask, task_id)
        experiment = session.get(AgentEvalExperiment, experiment_id)
        if task is None or experiment is None or task.lease_token != lease_token:
            return
        message = " ".join(str(error).split())[:1200]
        terminal = task.attempt_count >= task.max_attempts
        task.status = "failed" if terminal else "retry_wait"
        task.available_at = now if terminal else now + timedelta(seconds=30)
        task.last_error = message
        task.updated_at = now
        task.completed_at = now if terminal else None
        task.lease_owner = None
        task.lease_token = None
        task.lease_expires_at = None
        task.version += 1
        experiment.status = "failed" if terminal else "queued"
        experiment.error = message
        experiment.completed_at = now if terminal else None
        session.commit()


def _validate_component(value: str, label: str) -> None:
    if not value or value in {".", ".."} or any(character in value for character in "/\\\0"):
        raise AgentEvalError(f"invalid {label}")


def _bool_mean(values: dict[str, bool]) -> float:
    return sum(values.values()) / len(values) if values else 1.0


def _scores(rows: list[dict[str, Any]], evaluator_id: str) -> list[float]:
    return [
        float(row["score"])
        for row in rows
        if row["evaluator_id"] == evaluator_id and row["score"] is not None
    ]


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


def _now(settings: Settings) -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))
