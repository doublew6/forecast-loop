"""Operate forecast-loop's audited daily-reflection file handoff."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import Settings
from app.db import Database
from app.services.reflection_governance import record_reflection_human_review
from app.services.reflection_handoff import (
    finalize_reflection,
    freeze_reflection_sources,
    prepare_reflection,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare, freeze sources for, and finalize a Codex-first daily reflection."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser(
        "prepare",
        help="Freeze a completed live run, its evaluation batch and reflection tasks",
    )
    prepare.add_argument("source_run_id")
    prepare.add_argument("--horizon", choices=("D1", "D2"), required=True)
    prepare.add_argument(
        "--market-snapshot",
        type=Path,
        required=True,
        help="Trusted, source-bound market snapshot produced by the read-only adapter",
    )
    prepare.add_argument(
        "--schema-version",
        default="1.0.0",
        help="Append-only reflection schema version (MAJOR.MINOR.PATCH)",
    )
    prepare.add_argument(
        "--supersedes",
        default=None,
        help="Completed reflection UUID replaced by this newer schema version",
    )

    freeze = commands.add_parser(
        "freeze-sources",
        help="Validate source-discovery drafts and freeze trusted captured sources",
    )
    freeze.add_argument("job_dir", type=Path)
    freeze.add_argument(
        "--sources",
        type=Path,
        default=None,
        help=(
            "Trusted capture bundle. Omit to freeze an empty bundle and force "
            "causal findings to remain unresolved."
        ),
    )

    finalize = commands.add_parser(
        "finalize",
        help="Validate analysis/drafts.json and append findings and lesson proposals",
    )
    finalize.add_argument("job_dir", type=Path)

    review = commands.add_parser(
        "review",
        help="Append an immutable human review for a completed Live reflection",
    )
    review.add_argument("reflection_id")
    review.add_argument("--decision", choices=("approved", "rejected"), required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--notes-file", type=Path, default=None)
    review.add_argument("--reviewed-at", default=None)

    args = parser.parse_args()
    settings = Settings()
    if args.command == "prepare":
        job_dir = prepare_reflection(
            settings,
            args.source_run_id,
            horizon=args.horizon,
            market_snapshot_path=args.market_snapshot,
            schema_version=args.schema_version,
            supersedes_id=args.supersedes,
        )
        print(job_dir)
        print(f"Codex should now fill: {job_dir / 'source-discovery' / 'drafts.json'}")
        return
    if args.command == "freeze-sources":
        snapshot = freeze_reflection_sources(
            settings,
            args.job_dir,
            sources_path=args.sources,
        )
        print(
            json.dumps(
                snapshot.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            )
        )
        print(f"Codex should now fill: {Path(args.job_dir) / 'analysis' / 'drafts.json'}")
        return
    if args.command == "review":
        notes = ""
        if args.notes_file is not None:
            path = args.notes_file.expanduser()
            if path.is_symlink() or not path.is_file():
                raise ValueError("review notes must be a regular non-symlink file")
            notes = path.read_text(encoding="utf-8")
        reviewed_at = (
            datetime.fromisoformat(args.reviewed_at)
            if args.reviewed_at
            else datetime.now(ZoneInfo(settings.timezone))
        )
        database = Database(settings.database_url)
        try:
            with database.session_factory() as session:
                row = record_reflection_human_review(
                    session,
                    reflection_id=args.reflection_id,
                    decision=args.decision,
                    reviewer=args.reviewer,
                    notes=notes,
                    reviewed_at=reviewed_at,
                )
                session.commit()
                print(
                    json.dumps(
                        {
                            "review_id": row.id,
                            "reflection_id": row.reflection_run_id,
                            "decision": row.decision,
                            "reviewer": row.reviewer,
                            "notes_hash": row.notes_hash,
                            "reviewed_at": row.reviewed_at.isoformat(),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
        finally:
            database.dispose()
        return

    receipt = finalize_reflection(
        settings,
        args.job_dir,
    )
    print(
        json.dumps(
            receipt.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
