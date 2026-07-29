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
from .schemas import UserJudgmentCreate
from .services.audit_bundle import export_audit_bundle, verify_audit_bundle
from .services.benchmark import (
    DEFAULT_BENCHMARK_ROOT,
    build_benchmark_report,
    verify_benchmark_golden,
)
from .services.handoff import finalize_handoff, prepare_handoff
from .services.judgment_bundle import (
    export_judgment_bundle,
    verify_judgment_bundle,
)
from .services.recovery import (
    RecoveryError,
    create_backup,
    restore_backup,
    verify_backup,
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
        help=(
            "Verify a portable bundle path, or recompute a private record by "
            "judgment id."
        ),
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
            store.draft_instruction(args.execution_id)
            if state.phase == "awaiting_draft"
            else None
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
    if (
        args.judgment_command == "verify"
        and _looks_like_bundle_path(args.judgment_target)
    ):
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
                    window_minutes=settings.user_judgment_window_minutes,
                    expected_mode=(
                        "demo" if settings.use_demo_provider else "live"
                    ),
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
        _print_json(
            {
                "items": [
                    spec.model_dump(mode="json")
                    for spec in registered_agent_specs()
                ]
            }
        )
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
    }
    contract = contracts[args.contract_name]
    _print_json(contract.model_json_schema())
    return 0


def _worker_command(args: argparse.Namespace) -> int:
    settings = Settings()
    if args.database_url is not None:
        settings = settings.model_copy(
            update={"database_url": args.database_url}
        )
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
        wiki = WikiCatalog(settings.wiki_path)
        workflow = CommitteeWorkflow(
            settings=settings,
            database=database,
            wiki=wiki,
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


def _read_private_text(
    path: Path,
    *,
    label: str,
    maximum_bytes: int,
) -> str:
    source = path.expanduser()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise SystemExit(
                    f"{label} file must be a regular, non-symlink file"
                )
            if before.st_size > maximum_bytes:
                raise SystemExit(f"{label} file exceeds {maximum_bytes} bytes")
            content = stream.read(maximum_bytes + 1)
            after = os.fstat(stream.fileno())
    except OSError as exc:
        raise SystemExit(
            f"{label} file must be a readable, regular, non-symlink file"
        ) from exc
    if len(content) > maximum_bytes:
        raise SystemExit(f"{label} file exceeds {maximum_bytes} bytes")
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or after.st_size != len(content)
    ):
        raise SystemExit(f"{label} file changed while it was read")
    try:
        return content.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise SystemExit(f"{label} file must be UTF-8") from exc


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
        raise SystemExit(
            f"cannot infer handoff mode; pass --mode explicitly: {exc}"
        ) from exc
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
