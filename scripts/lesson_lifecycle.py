"""Operate deterministic Lesson replay, activation, and revalidation."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import Settings
from app.db import Database
from app.services.lesson_lifecycle import (
    approve_lesson,
    due_lesson_reviews,
    parse_replay_bundle,
    record_lesson_replay,
    revalidate_lesson,
    verify_lesson_audit,
)

MAX_INPUT_BYTES = 25 * 1024 * 1024


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Append audited replay evidence and operate the Lesson state machine."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    replay = commands.add_parser(
        "replay",
        help="Append detailed target-date observations and compute deterministic metrics",
    )
    replay.add_argument("input", type=Path)
    replay.add_argument("--submitted-by", required=True)
    replay.add_argument("--recorded-at", default=None)

    approve = commands.add_parser(
        "approve",
        help="Human-approve an eligible candidate as active without promoting Wiki",
    )
    approve.add_argument("lesson_id")
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--notes-file", type=Path, required=True)
    approve.add_argument("--approved-at", default=None)
    approve.add_argument("--supersedes", default=None)

    revalidate = commands.add_parser(
        "revalidate",
        help="Run a due monthly/+20-date/60-session deterministic review",
    )
    revalidate.add_argument("lesson_id")
    revalidate.add_argument("--reviewer", required=True)
    revalidate.add_argument("--notes-file", type=Path, required=True)
    revalidate.add_argument("--reviewed-at", default=None)
    revalidate.add_argument(
        "--checklist-valid",
        choices=("true", "false"),
        default=None,
        help="Required only for an extreme-event checklist candidate",
    )

    due = commands.add_parser(
        "due",
        help="List active/challenged lessons due for monthly or replay review",
    )
    due.add_argument("--as-of", default=None)

    verify = commands.add_parser(
        "verify",
        help="Recompute replay hashes, metrics, event chain, projection, and lineage",
    )
    verify.add_argument("lesson_id")

    args = parser.parse_args()
    settings = Settings()
    now = datetime.now(ZoneInfo(settings.timezone))
    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            if args.command == "verify":
                report = verify_lesson_audit(
                    session,
                    lesson_id=args.lesson_id,
                )
                output = {
                    "lesson_id": report.lesson_id,
                    "status": report.status,
                    "replay_batch_count": report.replay_batch_count,
                    "lifecycle_event_count": report.lifecycle_event_count,
                    "latest_replay_hash": report.latest_replay_hash,
                    "audit_root_hash": report.audit_root_hash,
                    "verified": True,
                }
            elif args.command == "due":
                reviews = due_lesson_reviews(
                    session,
                    as_of=_parse_time(args.as_of, fallback=now),
                )
                output = {
                    "items": [
                        {
                            "lesson_id": item.lesson_id,
                            "status": item.status,
                            "reasons": list(item.reasons),
                            "latest_replay_hash": item.latest_replay_hash,
                        }
                        for item in reviews
                    ]
                }
            elif args.command == "replay":
                payload = json.loads(_read_regular_file(args.input))
                bundle = parse_replay_bundle(payload)
                result = record_lesson_replay(
                    session,
                    bundle=bundle,
                    submitted_by=args.submitted_by,
                    recorded_at=_parse_time(args.recorded_at, fallback=now),
                    required_shadow_target_dates=(
                        settings.reflection_shadow_target_dates
                    ),
                )
                session.commit()
                output = {
                    "lesson_id": result.batch.lesson_proposal_id,
                    "replay_batch_id": result.batch.id,
                    "content_hash": result.batch.content_hash,
                    "observation_count": result.batch.observation_count,
                    "aggregate_metrics": result.batch.aggregate_metrics,
                    "event_id": result.event.id,
                    "idempotent": result.idempotent,
                }
            elif args.command == "approve":
                result = approve_lesson(
                    session,
                    lesson_id=args.lesson_id,
                    reviewer=args.reviewer,
                    notes=_read_regular_file(args.notes_file),
                    approved_at=_parse_time(args.approved_at, fallback=now),
                    supersedes_id=args.supersedes,
                )
                session.commit()
                output = _transition_output(result)
            else:
                checklist_valid = (
                    None
                    if args.checklist_valid is None
                    else args.checklist_valid == "true"
                )
                result = revalidate_lesson(
                    session,
                    lesson_id=args.lesson_id,
                    reviewer=args.reviewer,
                    notes=_read_regular_file(args.notes_file),
                    reviewed_at=_parse_time(args.reviewed_at, fallback=now),
                    required_shadow_target_dates=(
                        settings.reflection_shadow_target_dates
                    ),
                    checklist_valid=checklist_valid,
                )
                session.commit()
                output = _transition_output(result)
            print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        database.dispose()


def _transition_output(result) -> dict[str, object]:
    return {
        "lesson_id": result.lesson.id,
        "status": result.lesson.status,
        "event_id": result.event.id,
        "event_type": result.event.event_type,
        "event_hash": result.event.payload_hash,
        "replay_metrics": result.lesson.replay_metrics,
        "supersedes_id": result.lesson.supersedes_id,
        "idempotent": result.idempotent,
        "wiki_promotion_performed": False,
    }


def _read_regular_file(path: Path) -> str:
    path = path.expanduser()
    if path.is_symlink() or not path.is_file():
        raise ValueError("input must be a regular non-symlink file")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("input file is too large")
    return path.read_text(encoding="utf-8")


def _parse_time(raw: str | None, *, fallback: datetime) -> datetime:
    value = datetime.fromisoformat(raw) if raw else fallback
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("lifecycle timestamp must be timezone-aware")
    return value


if __name__ == "__main__":
    main()
