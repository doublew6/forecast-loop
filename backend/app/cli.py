"""Stable command-line interface for forecast-loop operator workflows."""

from __future__ import annotations

import argparse
import json
import os
import stat
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select

from .adapters import LocalJsonEvidenceSnapshotSource
from .agent_contracts import (
    AgentSpec,
    SignalEnvelope,
    agent_spec,
    registered_agent_specs,
    validate_signal_against_spec,
)
from .config import REPOSITORY_ROOT, Settings
from .db import Database
from .jobs import (
    JobExecutionStore,
    load_job_manifest,
    render_launchd_plist,
    render_systemd_units,
)
from .market_universe import MarketUniverseSpec, load_market_universe
from .models import UserJudgment
from .quant_contracts import QuantInputSnapshot, QuantSignalBundle
from .research_v2 import (
    AgentSignalEnvelopeV2,
    CodexHandoffRequestV3,
    EvidenceSnapshotV2,
    ReflectionDraftV2,
    ResearchProgramV2,
)
from .schemas import UserJudgmentCreate
from .services.agent_evaluation import (
    AgentEvalError,
    AgentEvalStore,
    EvalRunRequest,
    enqueue_experiment,
    run_next_eval_task,
)
from .services.agent_evaluation_v2 import (
    AgentEvalAblationDraftV2,
    AgentEvalAblationInputV2,
    AgentEvalDraftV2,
    AgentEvalInputV2,
    AgentEvalReportV2,
    AgentEvalReviewDraftV2,
    AgentEvalReviewInputV2,
    AgentEvalSuiteV2,
    AgentEvalV2Error,
    AgentEvalV2Store,
    agent_eval_v2_status,
    finalize_agent_eval_v2,
    prepare_agent_eval_v2,
)
from .services.agent_tracing import TraceRecorder
from .services.audit_bundle import export_audit_bundle, verify_audit_bundle
from .services.benchmark import (
    DEFAULT_BENCHMARK_ROOT,
    build_benchmark_report,
    verify_benchmark_golden,
)
from .services.daily_brief_v2 import (
    DEFAULT_BRIEF_TITLE,
    DEFAULT_DELIVERY_ROOT,
    DailyBriefV2Error,
    build_latest_daily_brief,
    load_feishu_owner_config,
    publish_daily_brief,
)
from .services.handoff import (
    finalize_handoff,
    prepare_handoff,
    retry_failed_handoff,
)
from .services.judgment_bundle import (
    export_judgment_bundle,
    verify_judgment_bundle,
)
from .services.premarket import (
    DEFAULT_PREMARKET_DELIVERY_ROOT,
    DEFAULT_PREMARKET_TITLE,
    PremarketServiceError,
    build_premarket_brief,
    evaluate_premarket_run,
    finalize_premarket_run,
    prepare_premarket_run,
    publish_premarket_brief,
)
from .services.recovery import (
    RecoveryError,
    create_backup,
    restore_backup,
    verify_backup,
)
from .services.research_v2 import (
    ResearchV2Error,
    activate_d1_v2,
    create_reflection_v2,
    evaluate_research_target,
    finalize_reasoning_review,
    finalize_research_run,
    prepare_research_run,
    review_reasoning,
    review_reflection_v2,
)
from .services.research_v2_shadow import (
    ManualShadowInputV2,
    admit_manual_shadow_signal_v2,
    admit_quant_shadow_signal_v2,
    finalize_shadow_reasoning_review_v2,
    seal_manual_shadow_input_v2,
)
from .services.run_bundle import export_run_bundle, verify_run_bundle
from .services.schema_readiness import (
    SchemaNotReadyError,
    inspect_schema,
    require_schema_current,
    upgrade_database,
)
from .services.task_queue import PersistentTaskQueue
from .services.user_judgment import create_user_judgment, verify_user_judgment
from .services.wiki import WikiCatalog
from .workflow import CommitteeWorkflow


def main(argv: Sequence[str] | None = None) -> int:
    """Run the public forecast-loop CLI."""

    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "forecast":
        return _forecast_command(args)
    if args.command == "run":
        return _run_command(args)
    if args.command == "snapshot":
        return _snapshot_command(args)
    if args.command == "jobs":
        return _jobs_command(args)
    if args.command == "audit":
        return _audit_command(args)
    if args.command == "judgment":
        return _judgment_command(args)
    if args.command == "agent":
        return _agent_command(args)
    if args.command == "contract":
        return _contract_command(args)
    if args.command == "worker":
        return _worker_command(args)
    if args.command == "benchmark":
        return _benchmark_command(args)
    if args.command == "agent-eval":
        return _agent_eval_command(args)
    if args.command == "research-v2":
        return _research_v2_command(args)
    if args.command == "premarket":
        return _premarket_command(args)
    if args.command == "database":
        return _database_command(args)
    if args.command == "recovery":
        return _recovery_command(args)
    parser.error(f"unsupported command: {args.command}")
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forecast-loop",
        description="Prepare, validate, persist and export verifiable forecasts.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"forecast-loop {Settings().app_version}",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    forecast = commands.add_parser(
        "forecast",
        help="Operate the deterministic Codex file-handoff boundary.",
    )
    forecast_commands = forecast.add_subparsers(
        dest="forecast_command",
        required=True,
    )
    prepare = forecast_commands.add_parser(
        "prepare",
        help="Freeze input and create a draft template without calling a model API.",
    )
    prepare.add_argument("--mode", choices=("demo", "live"), default=None)
    prepare.add_argument("--as-of", type=_datetime, default=None)
    prepare.add_argument("--snapshot", type=Path, default=None)
    prepare.add_argument("--output-root", type=Path, default=None)

    finalize = forecast_commands.add_parser(
        "finalize",
        help="Validate drafts and persist one immutable run.",
    )
    finalize.add_argument("--mode", choices=("demo", "live"), default=None)
    finalize.add_argument("--snapshot", type=Path, default=None)
    finalize.add_argument("--output-root", type=Path, default=None)
    finalize.add_argument("job_dir", type=Path)

    retry = forecast_commands.add_parser(
        "retry",
        help="Re-arm one sealed failed v3 handoff without admitting inputs again.",
    )
    retry.add_argument("--mode", choices=("demo", "live"), default=None)
    retry.add_argument("--snapshot", type=Path, default=None)
    retry.add_argument("--output-root", type=Path, default=None)
    retry.add_argument("job_dir", type=Path)

    run = commands.add_parser(
        "run",
        help="Export or verify a portable forecast run bundle.",
    )
    run_commands = run.add_subparsers(dest="run_command", required=True)
    export = run_commands.add_parser(
        "export",
        help="Export one completed database run to an immutable local bundle.",
    )
    export.add_argument("run_id")
    export.add_argument(
        "--output-root",
        type=Path,
        default=REPOSITORY_ROOT / "data" / "exports",
    )
    export.add_argument("--database-url", default=None)

    verify = run_commands.add_parser(
        "verify",
        help="Verify hashes and schemas in an exported run bundle.",
    )
    verify.add_argument("bundle_path", type=Path)

    snapshot = commands.add_parser(
        "snapshot",
        help="Validate a frozen evidence snapshot through a data-source adapter.",
    )
    snapshot_commands = snapshot.add_subparsers(
        dest="snapshot_command",
        required=True,
    )
    validate = snapshot_commands.add_parser(
        "validate",
        help="Validate a local JSON snapshot's schema, provenance, time and hashes.",
    )
    validate.add_argument("snapshot_path", type=Path)
    validate.add_argument("--root", type=Path, default=None)
    validate.add_argument("--as-of", type=_datetime, required=True)

    jobs = commands.add_parser(
        "jobs",
        help="Validate, render or track scheduler-neutral LLM job manifests.",
    )
    job_commands = jobs.add_subparsers(dest="jobs_command", required=True)
    job_validate = job_commands.add_parser(
        "validate",
        help="Validate one strict JSON job manifest and its prompt path.",
    )
    job_validate.add_argument("manifest_path", type=Path)
    job_validate.add_argument("--project-root", type=Path, default=Path.cwd())

    job_render = job_commands.add_parser(
        "render",
        help="Render a launchd plist or systemd user units around a dispatcher.",
    )
    job_render.add_argument("manifest_path", type=Path)
    job_render.add_argument("--target", choices=("launchd", "systemd"), required=True)
    job_render.add_argument("--dispatcher", required=True)
    job_render.add_argument("--output-dir", type=Path, required=True)
    job_render.add_argument("--host-timezone", default=None)
    job_render.add_argument("--project-root", type=Path, default=Path.cwd())

    job_begin = job_commands.add_parser(
        "begin",
        help="Open or resume one append-only external draft execution.",
    )
    job_begin.add_argument("manifest_path", type=Path)
    job_begin.add_argument("--idempotency-key", required=True)
    _add_job_store_arguments(job_begin)

    job_prepared = job_commands.add_parser(
        "prepared",
        help="Bind a completed deterministic prepare step to an execution.",
    )
    job_prepared.add_argument("execution_id")
    job_prepared.add_argument("job_dir", type=Path)
    _add_job_store_arguments(job_prepared)

    job_instruction = job_commands.add_parser(
        "instruction",
        help="Print the exact read/write scope for the external Codex draft.",
    )
    job_instruction.add_argument("execution_id")
    _add_job_store_arguments(job_instruction)

    job_draft_ready = job_commands.add_parser(
        "draft-ready",
        help="Validate and seal drafts.json before deterministic finalize.",
    )
    job_draft_ready.add_argument("execution_id")
    _add_job_store_arguments(job_draft_ready)

    job_finalized = job_commands.add_parser(
        "finalized",
        help="Verify and record the deterministic handoff receipt.",
    )
    job_finalized.add_argument("execution_id")
    _add_job_store_arguments(job_finalized)

    job_status = job_commands.add_parser(
        "status",
        help="Verify and print the latest append-only execution state.",
    )
    job_status.add_argument("execution_id")
    _add_job_store_arguments(job_status)

    audit = commands.add_parser(
        "audit",
        help="Export or verify a completed file-handoff audit bundle.",
    )
    audit_commands = audit.add_subparsers(dest="audit_command", required=True)
    audit_export = audit_commands.add_parser(
        "export",
        help="Bind frozen handoff artifacts to a verified result bundle.",
    )
    audit_export.add_argument("job_dir", type=Path)
    audit_export.add_argument("--run-bundle", type=Path, required=True)
    audit_export.add_argument("--handoff-root", type=Path, default=None)
    audit_export.add_argument(
        "--output-root",
        type=Path,
        default=REPOSITORY_ROOT / "data" / "audit-bundles",
    )
    audit_verify = audit_commands.add_parser(
        "verify",
        help="Verify audit artifact hashes, schemas and cross-file seals.",
    )
    audit_verify.add_argument("bundle_path", type=Path)

    judgment = commands.add_parser(
        "judgment",
        help="Record, export, or verify a private User Judgment.",
    )
    judgment_commands = judgment.add_subparsers(
        dest="judgment_command",
        required=True,
    )
    judgment_record = judgment_commands.add_parser(
        "record",
        help="Seal one immutable human judgment without exposing reasons in shell history.",
    )
    judgment_record.add_argument("--forecast-id", required=True)
    judgment_record.add_argument("--direction", choices=("up", "down"), required=True)
    judgment_record.add_argument("--confidence", type=float, required=True)
    judgment_record.add_argument("--rationale-file", type=Path, required=True)
    judgment_record.add_argument("--counter-evidence-file", type=Path, required=True)
    judgment_record.add_argument("--invalidation-file", type=Path, required=True)
    judgment_record.add_argument(
        "--blind",
        action="store_true",
        help="Attest that the committee conclusion has not been viewed.",
    )
    judgment_record.add_argument("--database-url", default=None)
    judgment_record.add_argument("--wiki-root", type=Path, default=None)

    judgment_export = judgment_commands.add_parser(
        "export",
        help="Export one immutable, privacy-minimized judgment bundle.",
    )
    judgment_export.add_argument("judgment_id")
    judgment_export.add_argument(
        "--output-root",
        type=Path,
        default=REPOSITORY_ROOT / "data" / "judgment-bundles",
    )
    judgment_export.add_argument(
        "--include-actor-id",
        action="store_true",
        help="Explicitly include the local actor identifier; omitted by default.",
    )
    judgment_export.add_argument("--database-url", default=None)
    judgment_export.add_argument("--wiki-root", type=Path, default=None)

    judgment_verify = judgment_commands.add_parser(
        "verify",
        help=("Verify a portable bundle path, or recompute a private record by judgment id."),
    )
    judgment_verify.add_argument("judgment_target")
    judgment_verify.add_argument("--database-url", default=None)
    judgment_verify.add_argument("--wiki-root", type=Path, default=None)

    agent = commands.add_parser(
        "agent",
        help="Inspect Agent contracts or validate a sealed SignalEnvelope.",
    )
    agent_commands = agent.add_subparsers(dest="agent_command", required=True)
    agent_commands.add_parser(
        "list",
        help="List all content-addressed AgentSpec records.",
    )
    agent_show = agent_commands.add_parser(
        "show",
        help="Show one exact AgentSpec.",
    )
    agent_show.add_argument("agent_id")
    agent_validate = agent_commands.add_parser(
        "validate",
        help="Validate a SignalEnvelope without writing to the database.",
    )
    agent_validate.add_argument("signal_path", type=Path)
    agent_validate.add_argument(
        "--spec",
        dest="spec_path",
        type=Path,
        default=None,
        help="Use an archived AgentSpec instead of the current registry entry.",
    )

    contract = commands.add_parser(
        "contract",
        help="Print public JSON Schemas for adapter authors.",
    )
    contract_commands = contract.add_subparsers(
        dest="contract_command",
        required=True,
    )
    contract_schema = contract_commands.add_parser(
        "schema",
        help="Print one versioned contract's JSON Schema.",
    )
    contract_schema.add_argument(
        "contract_name",
        choices=(
            "agent-spec",
            "signal-envelope",
            "quant-signal-bundle",
            "quant-input-snapshot",
            "market-universe",
            "agent-eval-suite-v2",
            "agent-eval-input-v2",
            "agent-eval-drafts-v2",
            "agent-eval-review-input-v2",
            "agent-eval-review-draft-v2",
            "agent-eval-ablation-input-v2",
            "agent-eval-ablation-draft-v2",
            "agent-eval-report-v2",
            "research-program-v2",
            "evidence-snapshot-v2",
            "agent-signal-v2",
            "codex-handoff-v3",
            "reflection-v2",
        ),
    )

    worker = commands.add_parser(
        "worker",
        help="Execute persistent workflow tasks outside the API process.",
    )
    worker_commands = worker.add_subparsers(
        dest="worker_command",
        required=True,
    )
    worker_run = worker_commands.add_parser(
        "run",
        help="Claim and execute queued committee runs with leases and retries.",
    )
    worker_run.add_argument(
        "--once",
        action="store_true",
        help="Process at most one task, print its result, and exit.",
    )
    worker_run.add_argument(
        "--worker-id",
        default=None,
        help="Stable worker label for lease diagnostics.",
    )
    worker_run.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Idle polling interval in seconds (default: 1.0).",
    )
    worker_run.add_argument("--database-url", default=None)

    benchmark = commands.add_parser(
        "benchmark",
        help="Run or verify the sealed cross-source benchmark fixture.",
    )
    benchmark_commands = benchmark.add_subparsers(
        dest="benchmark_command",
        required=True,
    )
    benchmark_run = benchmark_commands.add_parser(
        "run",
        help="Recompute the deterministic benchmark report.",
    )
    benchmark_run.add_argument(
        "fixture_root",
        type=Path,
        nargs="?",
        default=REPOSITORY_ROOT / DEFAULT_BENCHMARK_ROOT,
    )
    benchmark_verify = benchmark_commands.add_parser(
        "verify",
        help="Verify fixture seals and the exact golden benchmark report.",
    )
    benchmark_verify.add_argument(
        "fixture_root",
        type=Path,
        nargs="?",
        default=REPOSITORY_ROOT / DEFAULT_BENCHMARK_ROOT,
    )
    benchmark_verify.add_argument("--golden", type=Path, default=None)

    agent_eval = commands.add_parser(
        "agent-eval",
        help="Run versioned offline Agent workflow benchmarks and release gates.",
    )
    agent_eval_commands = agent_eval.add_subparsers(
        dest="agent_eval_command",
        required=True,
    )
    agent_eval_list = agent_eval_commands.add_parser(
        "list",
        help="List validated public and private Agent evaluation suites.",
    )
    agent_eval_list.add_argument("--database-url", default=None)
    agent_eval_run = agent_eval_commands.add_parser(
        "run",
        help="Queue and execute one deterministic offline comparison.",
    )
    agent_eval_run.add_argument("--suite", required=True)
    agent_eval_run.add_argument("--suite-version", default=None)
    agent_eval_run.add_argument("--baseline", required=True)
    agent_eval_run.add_argument("--candidate", required=True)
    agent_eval_run.add_argument(
        "--source",
        choices=("public", "private"),
        default="public",
    )
    agent_eval_run.add_argument("--idempotency-key", default=None)
    agent_eval_run.add_argument("--database-url", default=None)
    agent_eval_prepare = agent_eval_commands.add_parser(
        "prepare",
        help="Freeze an outcome-blind v2 replay handoff for two draft arms.",
    )
    agent_eval_prepare.add_argument("--suite", required=True)
    agent_eval_prepare.add_argument("--suite-version", default=None)
    agent_eval_prepare.add_argument("--baseline", required=True)
    agent_eval_prepare.add_argument("--candidate", required=True)
    agent_eval_prepare.add_argument(
        "--source",
        choices=("public", "private"),
        default="private",
    )
    agent_eval_prepare.add_argument("--output-root", type=Path, default=None)
    agent_eval_finalize = agent_eval_commands.add_parser(
        "finalize",
        help="Validate v2 drafts, reveal trusted outcomes, and seal the report.",
    )
    agent_eval_finalize.add_argument("job_dir", type=Path)
    agent_eval_finalize.add_argument("--output-root", type=Path, default=None)
    agent_eval_status = agent_eval_commands.add_parser(
        "status",
        help="Report whether a v2 handoff awaits drafts, is ready, or completed.",
    )
    agent_eval_status.add_argument("job_dir", type=Path)
    agent_eval_status.add_argument("--output-root", type=Path, default=None)
    agent_eval_commands.add_parser(
        "list-v2",
        help="List validated public and private Agent evaluation v2 suites.",
    )

    research_v2 = commands.add_parser(
        "research-v2",
        help="Operate the focused v2 file handoff, scoring, reviews, and activation.",
    )
    research_v2_commands = research_v2.add_subparsers(
        dest="research_v2_command",
        required=True,
    )
    research_prepare = research_v2_commands.add_parser(
        "prepare",
        help="Freeze a v2 snapshot and create an outcome-blind Codex task.",
    )
    research_prepare.add_argument("--snapshot", type=Path, required=True)
    research_prepare.add_argument("--mode", choices=("demo", "live"), required=True)
    research_prepare.add_argument("--database-url", default=None)
    research_finalize = research_v2_commands.add_parser(
        "finalize",
        help="Validate v2 drafts, derive CIO, and persist append-only records.",
    )
    research_finalize.add_argument("job_dir", type=Path)
    research_finalize.add_argument("--database-url", default=None)
    research_notify = research_v2_commands.add_parser(
        "notify",
        help="Render or idempotently send the latest Live CSI1000 D1 owner brief.",
    )
    research_notify.add_argument("--run-id", default=None)
    research_notify.add_argument("--env-file", type=Path, default=None)
    research_notify.add_argument("--env-prefix", default="FORECAST_LOOP_FEISHU")
    research_notify.add_argument("--title", default=DEFAULT_BRIEF_TITLE)
    research_notify.add_argument("--state-root", type=Path, default=DEFAULT_DELIVERY_ROOT)
    research_notify.add_argument("--dry-run", action="store_true")
    research_notify.add_argument("--database-url", default=None)
    reasoning_finalize = research_v2_commands.add_parser(
        "reasoning-finalize",
        help="Finalize the blind reasoning review file task.",
    )
    reasoning_finalize.add_argument("job_dir", type=Path)
    reasoning_finalize.add_argument("--database-url", default=None)
    shadow_reasoning_finalize = research_v2_commands.add_parser(
        "shadow-reasoning-finalize",
        help="Finalize one outcome-blind late Manual/Quant shadow review task.",
    )
    shadow_reasoning_finalize.add_argument("job_dir", type=Path)
    shadow_reasoning_finalize.add_argument("--database-url", default=None)
    reasoning_review = research_v2_commands.add_parser(
        "reasoning-review",
        help="Append one required human blind-review decision.",
    )
    reasoning_review.add_argument("review_id")
    reasoning_review.add_argument(
        "--decision",
        choices=("approved", "rejected"),
        required=True,
    )
    reasoning_review.add_argument("--reviewer", required=True)
    reasoning_review.add_argument("--notes-file", type=Path, default=None)
    reasoning_review.add_argument("--database-url", default=None)
    research_evaluate = research_v2_commands.add_parser(
        "evaluate",
        help="Load one trusted, sealed target outcome and score v2 signals.",
    )
    research_evaluate.add_argument("observation", type=Path)
    research_evaluate.add_argument("--database-url", default=None)
    shadow_manual = research_v2_commands.add_parser(
        "shadow-manual",
        help="Append one explicit CSI1000 D1 Manual shadow probability submission.",
    )
    shadow_manual.add_argument("submission", type=Path)
    shadow_manual.add_argument("--database-url", default=None)
    shadow_quant = research_v2_commands.add_parser(
        "shadow-quant",
        help="Append the exact CSI1000 D1 signal from a verified Quant bundle.",
    )
    shadow_quant.add_argument("run_id")
    shadow_quant.add_argument("--root", type=Path, required=True)
    shadow_quant.add_argument("--manifest", type=Path, required=True)
    shadow_quant.add_argument("--database-url", default=None)
    reflection_create = research_v2_commands.add_parser(
        "reflection-create",
        help="Create one target-scoped v2 reflection.",
    )
    reflection_create.add_argument("draft", type=Path)
    reflection_create.add_argument("--database-url", default=None)
    reflection_review = research_v2_commands.add_parser(
        "reflection-review",
        help="Append one immutable reflection approval.",
    )
    reflection_review.add_argument("reflection_id")
    reflection_review.add_argument(
        "--decision",
        choices=("approved", "rejected"),
        required=True,
    )
    reflection_review.add_argument("--reviewer", required=True)
    reflection_review.add_argument("--notes-file", type=Path, default=None)
    reflection_review.add_argument("--database-url", default=None)
    research_activate = research_v2_commands.add_parser(
        "activate",
        help="Append the D1 activation event after every release gate passes.",
    )
    research_activate.add_argument("--agent-eval-report", type=Path, required=True)
    research_activate.add_argument("--actor", required=True)
    research_activate.add_argument("--database-url", default=None)

    premarket = commands.add_parser(
        "premarket",
        help="Operate the 09:15 open-to-open file handoff and owner brief.",
    )
    premarket_commands = premarket.add_subparsers(
        dest="premarket_command",
        required=True,
    )
    premarket_prepare = premarket_commands.add_parser(
        "prepare",
        help="Validate a premarket snapshot and create the Codex draft task.",
    )
    premarket_prepare.add_argument("--snapshot", type=Path, required=True)
    premarket_finalize = premarket_commands.add_parser(
        "finalize",
        help="Validate premarket drafts and seal the open-to-open forecast.",
    )
    premarket_finalize.add_argument("job_dir", type=Path)
    premarket_evaluate = premarket_commands.add_parser(
        "evaluate",
        help="Seal a trusted open-to-open outcome and score the forecast.",
    )
    premarket_evaluate.add_argument("job_dir", type=Path)
    premarket_evaluate.add_argument("--outcome", type=Path, required=True)
    premarket_brief = premarket_commands.add_parser(
        "brief",
        help="Render the sealed premarket forecast as a short owner report.",
    )
    premarket_brief.add_argument("job_dir", type=Path)
    premarket_brief.add_argument("--title", default=DEFAULT_PREMARKET_TITLE)
    premarket_notify = premarket_commands.add_parser(
        "notify",
        help="Idempotently send a sealed premarket forecast to the owner.",
    )
    premarket_notify.add_argument("job_dir", type=Path)
    premarket_notify.add_argument("--env-file", type=Path, required=True)
    premarket_notify.add_argument("--env-prefix", default="FORECAST_LOOP_FEISHU")
    premarket_notify.add_argument("--title", default=DEFAULT_PREMARKET_TITLE)
    premarket_notify.add_argument(
        "--state-root",
        type=Path,
        default=DEFAULT_PREMARKET_DELIVERY_ROOT,
    )

    database = commands.add_parser(
        "database",
        help="Run explicit Alembic migrations or inspect schema readiness.",
    )
    database_commands = database.add_subparsers(
        dest="database_command",
        required=True,
    )
    database_migrate = database_commands.add_parser(
        "migrate",
        help="Upgrade the configured database to every Alembic head.",
    )
    database_migrate.add_argument("--database-url", default=None)
    database_status = database_commands.add_parser(
        "status",
        help="Report whether the database is safe for API/worker startup.",
    )
    database_status.add_argument("--database-url", default=None)
    database_status.add_argument(
        "--deep",
        action="store_true",
        help="Also run SQLite integrity and foreign-key checks.",
    )

    recovery = commands.add_parser(
        "recovery",
        help="Create, verify, or restore a private hash-sealed backup.",
    )
    recovery_commands = recovery.add_subparsers(
        dest="recovery_command",
        required=True,
    )
    recovery_backup = recovery_commands.add_parser(
        "backup",
        help="Back up explicit SQLite files and local mutable roots.",
    )
    recovery_backup.add_argument("--database", type=Path, required=True)
    recovery_backup.add_argument("--checkpoint", type=Path, required=True)
    recovery_backup.add_argument(
        "--root",
        action="append",
        type=_backup_root,
        default=[],
        metavar="NAME=PATH",
        help="Add one required mutable root; repeat for additional roots.",
    )
    recovery_backup.add_argument(
        "--output-root",
        type=Path,
        required=True,
    )
    recovery_verify = recovery_commands.add_parser(
        "verify",
        help="Verify a backup without changing it.",
    )
    recovery_verify.add_argument("bundle", type=Path)
    recovery_restore = recovery_commands.add_parser(
        "restore",
        help="Restore into a new or empty isolated target and verify it.",
    )
    recovery_restore.add_argument("bundle", type=Path)
    recovery_restore.add_argument("--target-root", type=Path, required=True)
    return parser


def _add_job_store_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-root",
        type=Path,
        default=REPOSITORY_ROOT / "data" / "job-executions",
    )
    parser.add_argument("--handoff-root", type=Path, default=None)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())


def _forecast_command(args: argparse.Namespace) -> int:
    if args.forecast_command == "prepare":
        mode = args.mode or _configured_mode(Settings())
        settings = _handoff_settings(
            mode,
            snapshot=args.snapshot,
            output_root=args.output_root,
        )
        job_dir = prepare_handoff(
            settings,
            as_of=args.as_of,
            handoff_root=args.output_root,
        )
        _print_json(
            {
                "status": "awaiting_draft",
                "job_dir": str(job_dir.resolve()),
                "drafts_file": str((job_dir / "drafts.json").resolve()),
            }
        )
        return 0

    mode = args.mode or _infer_handoff_mode(args.job_dir)
    settings = _handoff_settings(
        mode,
        snapshot=args.snapshot,
        output_root=args.output_root,
    )
    if args.forecast_command == "retry":
        job_dir = retry_failed_handoff(
            settings,
            args.job_dir,
            handoff_root=args.output_root,
        )
        _print_json(
            {
                "status": "awaiting_draft",
                "job_dir": str(job_dir.resolve()),
                "drafts_file": str((job_dir / "drafts.json").resolve()),
            }
        )
        return 0

    receipt = finalize_handoff(
        settings,
        args.job_dir,
        handoff_root=args.output_root,
    )
    _print_json(receipt.model_dump(mode="json"))
    return 0


def _run_command(args: argparse.Namespace) -> int:
    if args.run_command == "verify":
        manifest = verify_run_bundle(args.bundle_path)
        _print_json(
            {
                **manifest.model_dump(mode="json"),
                "verification_status": "verified",
            }
        )
        return 0

    settings = Settings()
    database_url = args.database_url or settings.database_url
    _require_existing_database(database_url)
    database = Database(database_url)
    try:
        bundle = export_run_bundle(
            database,
            run_id=args.run_id,
            output_root=args.output_root,
        )
        manifest = verify_run_bundle(bundle)
    finally:
        database.dispose()
    _print_json(
        {
            "status": "exported",
            "bundle_path": str(bundle),
            "bundle_hash": manifest.bundle_hash,
            "run_id": manifest.run_id,
        }
    )
    return 0


def _snapshot_command(args: argparse.Namespace) -> int:
    source_path = Path(os.path.abspath(args.snapshot_path.expanduser()))
    source = LocalJsonEvidenceSnapshotSource(
        root=args.root or source_path.parent,
        snapshot_path=source_path,
    )
    snapshot = source.load_snapshot(as_of=args.as_of)
    _print_json(
        {
            "status": "valid",
            "schema": "vericouncil.evidence-snapshot/v1",
            "content_hash": snapshot.content_hash,
            "as_of": snapshot.as_of.isoformat(),
            "data_cutoff": snapshot.data_cutoff.isoformat(),
            "items": len(snapshot.items),
        }
    )
    return 0


def _jobs_command(args: argparse.Namespace) -> int:
    if args.jobs_command in {
        "begin",
        "prepared",
        "instruction",
        "draft-ready",
        "finalized",
        "status",
    }:
        return _job_execution_command(args)

    configured_manifest_path = args.manifest_path.expanduser()
    manifest = load_job_manifest(configured_manifest_path)
    manifest_path = configured_manifest_path.resolve()
    project_root = _validate_job_project(
        manifest.draft.prompt,
        args.project_root,
    )
    if args.jobs_command == "validate":
        _print_json(
            {
                "status": "valid",
                "schema": manifest.schema_id,
                "name": manifest.name,
                "schedule": manifest.schedule,
                "timezone": manifest.timezone,
                "prompt": str(project_root / manifest.draft.prompt),
            }
        )
        return 0

    output_dir = _prepare_new_output_directory(args.output_dir)
    invocation = [args.dispatcher, str(manifest_path)]
    written: list[str] = []
    if args.target == "launchd":
        if not args.host_timezone:
            raise SystemExit("--host-timezone is required for launchd rendering")
        path = output_dir / f"org.vericouncil.job.{manifest.name}.plist"
        _require_new_cli_artifacts(path)
        _write_cli_artifact(
            path,
            render_launchd_plist(
                manifest,
                invocation,
                host_timezone=args.host_timezone,
            ),
        )
        written.append(str(path))
    else:
        units = render_systemd_units(manifest, invocation)
        service_path = output_dir / units.service_name
        timer_path = output_dir / units.timer_name
        _require_new_cli_artifacts(service_path, timer_path)
        _write_cli_artifact(service_path, units.service.encode("utf-8"))
        _write_cli_artifact(timer_path, units.timer.encode("utf-8"))
        written.extend((str(service_path), str(timer_path)))
    _print_json(
        {
            "status": "rendered",
            "target": args.target,
            "dispatcher": args.dispatcher,
            "files": written,
        }
    )
    return 0


def _job_execution_command(args: argparse.Namespace) -> int:
    settings = Settings()
    store = JobExecutionStore(
        state_root=args.state_root,
        project_root=args.project_root,
        handoff_root=args.handoff_root or settings.handoff_root,
    )
    if args.jobs_command == "begin":
        manifest = load_job_manifest(args.manifest_path.expanduser())
        state = store.begin(
            manifest,
            idempotency_key=args.idempotency_key,
        )
        _print_json(state.model_dump(mode="json", by_alias=True))
        return 0
    if args.jobs_command == "prepared":
        state = store.record_prepared(args.execution_id, args.job_dir)
        instruction = (
            store.draft_instruction(args.execution_id) if state.phase == "awaiting_draft" else None
        )
        _print_json(
            {
                "state": state.model_dump(mode="json", by_alias=True),
                "external_draft": (
                    None
                    if instruction is None
                    else instruction.model_dump(
                        mode="json",
                        by_alias=True,
                    )
                ),
            }
        )
        return 0
    if args.jobs_command == "instruction":
        instruction = store.draft_instruction(args.execution_id)
        _print_json(instruction.model_dump(mode="json", by_alias=True))
        return 0
    if args.jobs_command == "draft-ready":
        state = store.record_draft_ready(args.execution_id)
        _print_json(state.model_dump(mode="json", by_alias=True))
        return 0
    if args.jobs_command == "finalized":
        state = store.record_finalized(args.execution_id)
        _print_json(state.model_dump(mode="json", by_alias=True))
        return 0
    state = store.resume(args.execution_id)
    _print_json(state.model_dump(mode="json", by_alias=True))
    return 0


def _audit_command(args: argparse.Namespace) -> int:
    if args.audit_command == "verify":
        manifest = verify_audit_bundle(args.bundle_path)
        _print_json(
            {
                **manifest.model_dump(mode="json"),
                "verification_status": "verified",
            }
        )
        return 0

    settings = Settings()
    bundle = export_audit_bundle(
        handoff_root=args.handoff_root or settings.handoff_root,
        job_dir=args.job_dir,
        run_bundle_path=args.run_bundle,
        output_root=args.output_root,
    )
    manifest = verify_audit_bundle(bundle)
    _print_json(
        {
            "status": "exported",
            "bundle_path": str(bundle),
            "bundle_hash": manifest.bundle_hash,
            "run_id": manifest.run_id,
            "publisher_authentication": manifest.publisher_authentication,
        }
    )
    return 0


def _judgment_command(args: argparse.Namespace) -> int:
    if args.judgment_command == "verify" and _looks_like_bundle_path(args.judgment_target):
        manifest = verify_judgment_bundle(Path(args.judgment_target))
        _print_json(
            {
                **manifest.model_dump(mode="json"),
                "verification_status": "verified",
            }
        )
        return 0

    settings = Settings()
    database_url = args.database_url or settings.database_url
    wiki_root = args.wiki_root or settings.user_judgment_wiki_root
    _require_existing_database(database_url)
    if args.judgment_command == "record":
        require_schema_current(database_url)
    database = Database(database_url)
    try:
        if args.judgment_command == "export":
            bundle = export_judgment_bundle(
                database,
                judgment_id=args.judgment_id,
                output_root=args.output_root,
                wiki_root=wiki_root,
                timezone=settings.timezone,
                include_actor_id=args.include_actor_id,
            )
            manifest = verify_judgment_bundle(bundle)
            _print_json(
                {
                    "status": "exported",
                    "bundle_path": str(bundle),
                    "bundle_hash": manifest.bundle_hash,
                    "manifest_hash": manifest.manifest_hash,
                    "judgment_id": manifest.judgment_id,
                    "record_class": manifest.record_class,
                    "actor_privacy": manifest.actor_privacy,
                    "evaluation_status": manifest.evaluation_status,
                }
            )
            return 0

        with database.session_factory() as session:
            if args.judgment_command == "record":
                request = UserJudgmentCreate(
                    forecast_id=args.forecast_id,
                    direction=args.direction,
                    confidence=args.confidence,
                    rationale=_read_private_text(
                        args.rationale_file,
                        label="rationale",
                        maximum_bytes=16 * 1024,
                    ),
                    counter_evidence=_read_private_text(
                        args.counter_evidence_file,
                        label="counter evidence",
                        maximum_bytes=8 * 1024,
                    ),
                    invalidation_condition=_read_private_text(
                        args.invalidation_file,
                        label="invalidation condition",
                        maximum_bytes=8 * 1024,
                    ),
                    blind_attestation=args.blind,
                )
                row, created = create_user_judgment(
                    session,
                    request=request,
                    actor_id=settings.user_judgment_actor_id,
                    wiki_root=wiki_root,
                    timezone=settings.timezone,
                    market_open=settings.user_judgment_market_open,
                    expected_mode=("demo" if settings.use_demo_provider else "live"),
                    market_universe_hash=load_market_universe(
                        settings.market_universe_path
                    ).content_hash,
                )
                _print_json(
                    {
                        "status": "sealed" if created else "already_sealed",
                        "judgment_id": row.id,
                        "formal_score_eligible": row.formal_score_eligible,
                        "content_hash": row.content_hash,
                        "wiki_path": row.wiki_path,
                        "wiki_artifact_hash": row.wiki_artifact_hash,
                    }
                )
                return 0

            row = session.scalar(
                select(UserJudgment).where(
                    UserJudgment.id == args.judgment_target,
                    UserJudgment.actor_id == settings.user_judgment_actor_id,
                )
            )
            if row is None:
                raise SystemExit("User Judgment not found")
            verify_user_judgment(
                row,
                wiki_root=wiki_root,
                timezone=settings.timezone,
            )
            _print_json(
                {
                    "status": "verified",
                    "judgment_id": row.id,
                    "content_hash": row.content_hash,
                    "wiki_path": row.wiki_path,
                    "wiki_artifact_hash": row.wiki_artifact_hash,
                }
            )
            return 0
    finally:
        database.dispose()


def _agent_command(args: argparse.Namespace) -> int:
    if args.agent_command == "list":
        _print_json({"items": [spec.model_dump(mode="json") for spec in registered_agent_specs()]})
        return 0
    if args.agent_command == "show":
        try:
            spec = agent_spec(args.agent_id)
        except KeyError as exc:
            raise SystemExit(f"unknown Agent: {args.agent_id}") from exc
        _print_json(spec.model_dump(mode="json"))
        return 0

    raw = _read_private_text(
        args.signal_path,
        label="SignalEnvelope",
        maximum_bytes=1024 * 1024,
    )
    try:
        signal = SignalEnvelope.model_validate_json(raw)
        spec = (
            AgentSpec.model_validate_json(
                _read_private_text(
                    args.spec_path,
                    label="AgentSpec",
                    maximum_bytes=256 * 1024,
                )
            )
            if args.spec_path is not None
            else agent_spec(signal.agent_id)
        )
        validate_signal_against_spec(signal, spec)
    except (KeyError, ValidationError, ValueError) as exc:
        raise SystemExit(f"invalid SignalEnvelope: {exc}") from exc
    _print_json(
        {
            "status": "valid",
            "signal_id": signal.signal_id,
            "agent_id": signal.agent_id,
            "agent_spec_hash": spec.content_hash,
            "content_hash": signal.content_hash,
            "participation_mode": signal.participation.mode,
        }
    )
    return 0


def _looks_like_bundle_path(value: str) -> bool:
    candidate = Path(value).expanduser()
    return (
        candidate.exists()
        or candidate.is_absolute()
        or value.startswith(".")
        or os.sep in value
        or (os.altsep is not None and os.altsep in value)
    )


def _contract_command(args: argparse.Namespace) -> int:
    contracts = {
        "agent-spec": AgentSpec,
        "signal-envelope": SignalEnvelope,
        "quant-signal-bundle": QuantSignalBundle,
        "quant-input-snapshot": QuantInputSnapshot,
        "market-universe": MarketUniverseSpec,
        "agent-eval-suite-v2": AgentEvalSuiteV2,
        "agent-eval-input-v2": AgentEvalInputV2,
        "agent-eval-drafts-v2": AgentEvalDraftV2,
        "agent-eval-review-input-v2": AgentEvalReviewInputV2,
        "agent-eval-review-draft-v2": AgentEvalReviewDraftV2,
        "agent-eval-ablation-input-v2": AgentEvalAblationInputV2,
        "agent-eval-ablation-draft-v2": AgentEvalAblationDraftV2,
        "agent-eval-report-v2": AgentEvalReportV2,
        "research-program-v2": ResearchProgramV2,
        "evidence-snapshot-v2": EvidenceSnapshotV2,
        "agent-signal-v2": AgentSignalEnvelopeV2,
        "codex-handoff-v3": CodexHandoffRequestV3,
        "reflection-v2": ReflectionDraftV2,
    }
    contract = contracts[args.contract_name]
    _print_json(contract.model_json_schema())
    return 0


def _worker_command(args: argparse.Namespace) -> int:
    settings = Settings()
    if args.database_url is not None:
        settings = settings.model_copy(update={"database_url": args.database_url})
    if args.poll_interval <= 0:
        raise SystemExit("--poll-interval must be positive")
    worker_id = args.worker_id or f"worker-{uuid4().hex[:12]}"
    database = Database(settings.database_url)
    workflow: CommitteeWorkflow | None = None
    try:
        try:
            require_schema_current(database.engine)
        except SchemaNotReadyError as exc:
            raise SystemExit(str(exc)) from exc
        wiki = WikiCatalog.from_settings(settings)
        workflow = CommitteeWorkflow(
            settings=settings,
            database=database,
            wiki=wiki,
            trace_recorder=TraceRecorder(database, settings),
        )
        queue = PersistentTaskQueue(
            database,
            timezone=settings.timezone,
            max_attempts=settings.task_max_attempts,
            lease_seconds=settings.task_lease_seconds,
            timeout_seconds=settings.task_timeout_seconds,
            retry_delay_seconds=settings.task_retry_delay_seconds,
        )
        if args.once:
            result = queue.run_once(workflow, worker_id=worker_id)
            if result is None:
                _print_json(
                    {
                        "status": "idle",
                        "worker_id": worker_id,
                    }
                )
            else:
                _print_json(
                    {
                        "status": result.status,
                        "worker_id": worker_id,
                        "task_id": result.task_id,
                        "run_id": result.run_id,
                        "attempt_count": result.attempt_count,
                        "error": result.error,
                    }
                )
            return 0
        _print_json(
            {
                "status": "started",
                "worker_id": worker_id,
                "poll_interval_seconds": args.poll_interval,
            }
        )
        queue.run_forever(
            workflow,
            worker_id=worker_id,
            poll_interval_seconds=args.poll_interval,
        )
    except KeyboardInterrupt:
        _print_json({"status": "stopped", "worker_id": worker_id})
    finally:
        if workflow is not None:
            workflow.close()
        database.dispose()
    return 0


def _benchmark_command(args: argparse.Namespace) -> int:
    if args.benchmark_command == "run":
        _print_json(build_benchmark_report(args.fixture_root))
        return 0
    report = verify_benchmark_golden(
        args.fixture_root,
        golden_path=args.golden,
    )
    _print_json(
        {
            "status": "verified",
            "benchmark_id": report["benchmark_id"],
            "fixture_version": report["fixture_version"],
            "fixture_manifest_hash": report["fixture_manifest_hash"],
            "report_hash": report["report_hash"],
            "counts": report["counts"],
        }
    )
    return 0


def _agent_eval_command(args: argparse.Namespace) -> int:
    settings = Settings()
    if getattr(args, "database_url", None) is not None:
        settings = settings.model_copy(update={"database_url": args.database_url})
    if args.agent_eval_command == "list":
        _print_json(
            {
                "items": [
                    item.model_dump(mode="json") for item in AgentEvalStore(settings).list_suites()
                ]
            }
        )
        return 0
    if args.agent_eval_command == "list-v2":
        _print_json(
            {
                "items": [
                    item.model_dump(mode="json")
                    for item in AgentEvalV2Store(settings).list_suites()
                ]
            }
        )
        return 0
    try:
        if args.agent_eval_command == "prepare":
            _print_json(
                prepare_agent_eval_v2(
                    settings,
                    suite_id=args.suite,
                    suite_version=args.suite_version,
                    source=args.source,
                    baseline_arm_id=args.baseline,
                    candidate_arm_id=args.candidate,
                    output_root=args.output_root,
                )
            )
            return 0
        if args.agent_eval_command == "status":
            _print_json(
                agent_eval_v2_status(
                    settings,
                    args.job_dir,
                    output_root=args.output_root,
                )
            )
            return 0
        if args.agent_eval_command == "finalize":
            database = Database(settings.database_url)
            try:
                require_schema_current(database.engine)
                report = finalize_agent_eval_v2(
                    settings,
                    args.job_dir,
                    output_root=args.output_root,
                    database=database,
                )
                _print_json(report.model_dump(mode="json"))
                return 0 if report.release_decision == "pass" else 1
            finally:
                database.dispose()
    except AgentEvalV2Error as exc:
        raise SystemExit(str(exc)) from exc

    database = Database(settings.database_url)
    try:
        try:
            require_schema_current(database.engine)
        except SchemaNotReadyError as exc:
            raise SystemExit(str(exc)) from exc
        request = EvalRunRequest(
            suite_id=args.suite,
            suite_version=args.suite_version,
            baseline_target_id=args.baseline,
            candidate_target_id=args.candidate,
            source=args.source,
        )
        idempotency_key = args.idempotency_key or (
            f"cli:{args.suite}:{args.suite_version or 'latest'}:{args.baseline}:{args.candidate}"
        )
        enqueue_experiment(
            database,
            settings,
            request,
            idempotency_key=idempotency_key,
        )
        experiment = run_next_eval_task(
            database,
            settings,
            worker_id=f"agent-eval-cli-{uuid4().hex[:12]}",
        )
        if experiment is None:
            raise AgentEvalError("no queued Agent evaluation task is available")
        _print_json(
            {
                "status": experiment.status,
                "experiment_id": experiment.id,
                "release_decision": experiment.release_decision,
                "report_hash": experiment.report_hash,
                "summary": experiment.summary,
            }
        )
        return 0 if experiment.release_decision == "pass" else 1
    except AgentEvalError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        database.dispose()


def _research_v2_command(args: argparse.Namespace) -> int:
    settings = Settings()
    if args.database_url is not None:
        settings = settings.model_copy(update={"database_url": args.database_url})
    database = Database(settings.database_url)
    try:
        try:
            require_schema_current(database.engine)
        except SchemaNotReadyError as exc:
            raise SystemExit(str(exc)) from exc
        command = args.research_v2_command
        if command == "prepare":
            job_dir = prepare_research_run(
                database,
                settings,
                snapshot_path=args.snapshot,
                mode=args.mode,
            )
            _print_json(
                {
                    "status": "awaiting_draft",
                    "job_dir": str(job_dir.resolve()),
                    "drafts_file": str((job_dir / "drafts.json").resolve()),
                }
            )
            return 0
        if command == "finalize":
            row = finalize_research_run(database, settings, job_dir=args.job_dir)
            _print_json(
                {
                    "status": row.status,
                    "run_id": row.id,
                    "receipt": row.receipt,
                    "reasoning_job_dir": str((args.job_dir / "reasoning").resolve()),
                }
            )
            return 0
        if command == "notify":
            brief = build_latest_daily_brief(
                database,
                settings,
                run_id=args.run_id,
                title=args.title,
            )
            if args.dry_run:
                _print_json({"status": "dry_run", "brief": brief.as_dict()})
                return 0
            if args.env_file is None:
                raise DailyBriefV2Error("--env-file is required unless --dry-run is used")
            config = load_feishu_owner_config(
                args.env_file,
                env_prefix=args.env_prefix,
            )
            result = publish_daily_brief(
                brief,
                config,
                state_root=args.state_root,
            )
            _print_json(
                {
                    "status": result.status,
                    "forecast_id": result.forecast_id,
                    "target_date": result.target_date.isoformat(),
                    "delivery_marker": str(result.marker),
                }
            )
            return 0
        if command == "reasoning-finalize":
            rows = finalize_reasoning_review(database, settings, job_dir=args.job_dir)
            _print_json(
                {
                    "status": "completed",
                    "items": [
                        {
                            "review_id": row.id,
                            "signal_id": row.signal_id,
                            "total_score": row.total_score,
                            "human_review_required": row.human_review_required,
                            "human_review_status": row.human_review_status,
                        }
                        for row in rows
                    ],
                }
            )
            return 0
        if command == "shadow-reasoning-finalize":
            row = finalize_shadow_reasoning_review_v2(
                database,
                settings,
                job_dir=args.job_dir,
            )
            _print_json(
                {
                    "status": "completed",
                    "review_id": row.id,
                    "signal_id": row.signal_id,
                    "total_score": row.total_score,
                    "human_review_required": row.human_review_required,
                    "human_review_status": row.human_review_status,
                }
            )
            return 0
        if command == "reasoning-review":
            notes = _optional_private_text(args.notes_file, label="review notes")
            row = review_reasoning(
                database,
                settings,
                review_id=args.review_id,
                decision=args.decision,
                reviewer=args.reviewer,
                notes=notes,
            )
            _print_json({"status": args.decision, "review_id": row.id})
            return 0
        if command == "evaluate":
            rows = evaluate_research_target(
                database,
                settings,
                observation_path=args.observation,
            )
            _print_json(
                {
                    "status": "completed",
                    "evaluated_signals": len(rows),
                    "evaluation_ids": [row.id for row in rows],
                }
            )
            return 0
        if command == "shadow-manual":
            payload = json.loads(
                _read_private_bytes(
                    args.submission,
                    label="v2 Manual shadow input",
                    maximum_bytes=2 * 1024 * 1024,
                )
            )
            if not isinstance(payload, dict):
                raise ValueError("v2 Manual shadow input must be a JSON object")
            submission = (
                ManualShadowInputV2.model_validate(payload)
                if "content_hash" in payload
                else seal_manual_shadow_input_v2(payload)
            )
            row = admit_manual_shadow_signal_v2(
                database,
                settings,
                submission=submission,
            )
            _print_json(
                {
                    "status": "completed",
                    "signal_id": row.id,
                    "agent_id": row.agent_id,
                    "target_id": row.target_id,
                    "horizon": row.natural_horizon,
                    "participation": "shadow",
                    "formal_forecast_influence": "none",
                }
            )
            return 0
        if command == "shadow-quant":
            row = admit_quant_shadow_signal_v2(
                database,
                settings,
                run_id=args.run_id,
                quant_root=args.root,
                manifest_path=args.manifest,
            )
            _print_json(
                {
                    "status": "completed",
                    "signal_id": row.id,
                    "agent_id": row.agent_id,
                    "target_id": row.target_id,
                    "horizon": row.natural_horizon,
                    "participation": "shadow",
                    "formal_forecast_influence": "none",
                }
            )
            return 0
        if command == "reflection-create":
            draft = ReflectionDraftV2.model_validate_json(
                _read_private_bytes(
                    args.draft,
                    label="v2 reflection draft",
                    maximum_bytes=2 * 1024 * 1024,
                )
            )
            row = create_reflection_v2(database, settings, draft=draft)
            _print_json(
                {
                    "status": row.status,
                    "reflection_id": row.id,
                    "content_hash": row.content_hash,
                }
            )
            return 0
        if command == "reflection-review":
            notes = _optional_private_text(
                args.notes_file,
                label="reflection review notes",
            )
            row = review_reflection_v2(
                database,
                settings,
                reflection_id=args.reflection_id,
                decision=args.decision,
                reviewer=args.reviewer,
                notes=notes,
            )
            _print_json({"status": args.decision, "reflection_id": row.id})
            return 0
        row = activate_d1_v2(
            database,
            settings,
            actor=args.actor,
            agent_eval_report_path=args.agent_eval_report,
        )
        _print_json(
            {
                "status": row.event_type,
                "activation_event_id": row.id,
                "target_id": row.target_id,
                "content_hash": row.content_hash,
            }
        )
        return 0
    except (ResearchV2Error, DailyBriefV2Error, ValidationError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        database.dispose()


def _premarket_command(args: argparse.Namespace) -> int:
    settings = Settings()
    try:
        if args.premarket_command == "prepare":
            job_dir = prepare_premarket_run(settings, snapshot_path=args.snapshot)
            _print_json(
                {
                    "status": "awaiting_draft",
                    "job_dir": str(job_dir),
                    "drafts_file": str(job_dir / "drafts.json"),
                }
            )
            return 0
        if args.premarket_command == "finalize":
            forecast = finalize_premarket_run(settings, job_dir=args.job_dir)
            _print_json(
                {
                    "status": "completed",
                    "run_id": forecast.run_id,
                    "forecast_hash": forecast.content_hash,
                    "forecast_session": forecast.forecast_session.isoformat(),
                    "target_session": forecast.target_session.isoformat(),
                    "direction": forecast.direction,
                }
            )
            return 0
        if args.premarket_command == "evaluate":
            evaluation = evaluate_premarket_run(
                settings,
                job_dir=args.job_dir,
                outcome_path=args.outcome,
            )
            _print_json(
                {
                    "status": "evaluated",
                    "forecast_hash": evaluation.forecast_hash,
                    "outcome_hash": evaluation.outcome_hash,
                    "actual_label": evaluation.actual_label,
                    "direction_correct": evaluation.direction_correct,
                    "brier_score": evaluation.brier_score,
                    "evaluation_hash": evaluation.content_hash,
                }
            )
            return 0
        brief = build_premarket_brief(
            settings,
            job_dir=args.job_dir,
            title=args.title,
        )
        if args.premarket_command == "brief":
            _print_json(
                {
                    "status": "rendered",
                    "forecast_hash": brief.forecast_hash,
                    "content_hash": brief.content_hash,
                    "text": brief.text,
                }
            )
            return 0
        if args.premarket_command == "notify":
            config = load_feishu_owner_config(
                args.env_file,
                env_prefix=args.env_prefix,
            )
            result = publish_premarket_brief(
                brief,
                config,
                state_root=args.state_root,
            )
            _print_json(
                {
                    "status": result.status,
                    "forecast_hash": result.forecast_hash,
                    "delivery_marker": str(result.marker),
                }
            )
            return 0
        raise PremarketServiceError("unsupported premarket command")
    except (DailyBriefV2Error, PremarketServiceError, ValidationError) as exc:
        raise SystemExit(str(exc)) from exc


def _database_command(args: argparse.Namespace) -> int:
    settings = Settings()
    database_url = args.database_url or settings.database_url
    if args.database_command == "migrate":
        status = upgrade_database(database_url)
        try:
            require_schema_current(database_url, deep=True)
        except SchemaNotReadyError as exc:
            raise SystemExit(str(exc)) from exc
        _print_json({"status": "migrated", **status.to_dict()})
        return 0

    status = inspect_schema(database_url, deep=args.deep)
    _print_json({"status": "ready" if status.ready else "blocked", **status.to_dict()})
    return 0 if status.ready else 1


def _recovery_command(args: argparse.Namespace) -> int:
    try:
        if args.recovery_command == "backup":
            roots: dict[str, Path] = {}
            for name, path in args.root:
                if name in roots:
                    raise RecoveryError(f"duplicate backup root: {name}")
                roots[name] = path
            bundle = create_backup(
                database_path=args.database,
                checkpoint_path=args.checkpoint,
                roots=roots,
                output_root=args.output_root,
            )
            manifest = verify_backup(bundle)
            _print_json(
                {
                    "status": "created",
                    "bundle_path": str(bundle),
                    "manifest_hash": manifest["manifest_hash"],
                }
            )
            return 0
        if args.recovery_command == "verify":
            manifest = verify_backup(args.bundle)
            _print_json(
                {
                    "status": "verified",
                    "bundle_path": str(args.bundle.expanduser().absolute()),
                    "manifest_hash": manifest["manifest_hash"],
                }
            )
            return 0

        receipt = restore_backup(
            args.bundle,
            target_root=args.target_root,
        )
        restored = _read_private_text(
            receipt,
            label="restore receipt",
            maximum_bytes=1024 * 1024,
        )
        _print_json(
            {
                "status": "restored",
                "target_root": str(receipt.parent),
                "receipt": json.loads(restored),
            }
        )
        return 0
    except RecoveryError as exc:
        raise SystemExit(str(exc)) from exc


def _read_private_bytes(
    path: Path,
    *,
    label: str,
    maximum_bytes: int,
) -> bytes:
    source = path.expanduser()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise SystemExit(f"{label} file must be a regular, non-symlink file")
            if before.st_size > maximum_bytes:
                raise SystemExit(f"{label} file exceeds {maximum_bytes} bytes")
            content = stream.read(maximum_bytes + 1)
            after = os.fstat(stream.fileno())
    except OSError as exc:
        raise SystemExit(f"{label} file must be a readable, regular, non-symlink file") from exc
    if len(content) > maximum_bytes:
        raise SystemExit(f"{label} file exceeds {maximum_bytes} bytes")
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or after.st_size != len(content)
    ):
        raise SystemExit(f"{label} file changed while it was read")
    return content


def _read_private_text(
    path: Path,
    *,
    label: str,
    maximum_bytes: int,
) -> str:
    content = _read_private_bytes(path, label=label, maximum_bytes=maximum_bytes)
    try:
        return content.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise SystemExit(f"{label} file must be UTF-8") from exc


def _optional_private_text(path: Path | None, *, label: str) -> str:
    if path is None:
        return ""
    return _read_private_text(path, label=label, maximum_bytes=32 * 1024)


def _validate_job_project(prompt: str, project_root: Path) -> Path:
    root = project_root.expanduser()
    if root.is_symlink() or not root.is_dir():
        raise SystemExit(f"job project root must be a real directory: {root}")
    root = root.resolve()
    prompt_path = root / prompt
    current = root
    for part in Path(prompt).parts:
        current /= part
        if current.is_symlink():
            raise SystemExit(f"job prompt path may not contain symlinks: {current}")
    if not prompt_path.is_file():
        raise SystemExit(f"job prompt is missing or unsafe: {prompt_path}")
    try:
        prompt_path.resolve(strict=True).relative_to(root)
    except (OSError, ValueError):
        raise SystemExit(f"job prompt escaped project root: {prompt_path}") from None
    return root


def _prepare_new_output_directory(path: Path) -> Path:
    output = path.expanduser()
    if output.is_symlink():
        raise SystemExit(f"job render output may not be a symlink: {output}")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not output.is_dir():
        raise SystemExit(f"job render output is not a directory: {output}")
    return output.resolve()


def _write_cli_artifact(path: Path, body: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(body)


def _require_new_cli_artifacts(*paths: Path) -> None:
    existing = [path for path in paths if path.exists() or path.is_symlink()]
    if existing:
        raise SystemExit(f"job render will not overwrite: {existing[0]}")


def _handoff_settings(
    mode: Literal["demo", "live"],
    *,
    snapshot: Path | None,
    output_root: Path | None,
) -> Settings:
    settings = Settings()
    updates: dict[str, object] = {
        "execution_provider": "demo" if mode == "demo" else "codex_file",
        "demo_mode": mode == "demo",
        "auto_seed": False,
    }
    if snapshot is not None:
        updates["evidence_snapshot_path"] = snapshot
    if output_root is not None:
        updates["handoff_root"] = output_root
    return settings.model_copy(update=updates)


def _require_existing_database(database_url: str) -> None:
    if not database_url.startswith("sqlite:///") or ":memory:" in database_url:
        return
    path = Path(database_url.removeprefix("sqlite:///")).expanduser()
    if not path.is_file():
        raise SystemExit(f"run export database does not exist: {path}")


def _configured_mode(settings: Settings) -> Literal["demo", "live"]:
    return "demo" if settings.use_demo_provider else "live"


def _infer_handoff_mode(job_dir: Path) -> Literal["demo", "live"]:
    try:
        payload = json.loads((job_dir / "input.json").read_text(encoding="utf-8"))
        mode = payload["mode"]
    except (OSError, KeyError, json.JSONDecodeError, TypeError) as exc:
        raise SystemExit(f"cannot infer handoff mode; pass --mode explicitly: {exc}") from exc
    if mode not in {"demo", "live"}:
        raise SystemExit("input.json mode must be demo or live")
    return mode


def _datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an ISO 8601 timestamp") from exc


def _backup_root(value: str) -> tuple[str, Path]:
    name, separator, raw_path = value.partition("=")
    if not separator or not name or not raw_path:
        raise argparse.ArgumentTypeError("expected NAME=PATH")
    return name, Path(raw_path)


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
