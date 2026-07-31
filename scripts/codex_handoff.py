"""Prepare and finalize forecast-loop's audited Codex file handoff."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from app.config import Settings
from app.services.handoff import (
    finalize_handoff,
    prepare_handoff,
    retry_failed_handoff,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Codex-first file handoff without calling a model API."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="Freeze input.json and the 50-draft template")
    prepare.add_argument("--mode", choices=("demo", "live"), default=None)
    prepare.add_argument("--as-of", type=_datetime, default=None)
    prepare.add_argument("--snapshot", type=Path, default=None)
    prepare.add_argument("--quant-manifest", type=Path, default=None)
    prepare.add_argument("--output-root", type=Path, default=None)

    finalize = commands.add_parser("finalize", help="Validate drafts.json and persist one run")
    finalize.add_argument("--mode", choices=("demo", "live"), default=None)
    finalize.add_argument("--snapshot", type=Path, default=None)
    finalize.add_argument("--output-root", type=Path, default=None)
    finalize.add_argument("job_dir", type=Path)

    retry = commands.add_parser(
        "retry",
        help="Re-arm one sealed failed v3 run without admitting inputs again",
    )
    retry.add_argument("--mode", choices=("demo", "live"), default=None)
    retry.add_argument("--snapshot", type=Path, default=None)
    retry.add_argument("--output-root", type=Path, default=None)
    retry.add_argument("job_dir", type=Path)

    args = parser.parse_args()
    if args.command == "prepare":
        mode = args.mode or _configured_mode(Settings())
        settings = _settings(mode, snapshot=args.snapshot, output_root=args.output_root)
        job_dir = prepare_handoff(
            settings,
            as_of=args.as_of,
            handoff_root=args.output_root,
            quant_manifest_path=args.quant_manifest,
        )
        print(job_dir)
        print(f"Codex should now fill: {job_dir / 'drafts.json'}")
        return

    mode = args.mode or _infer_mode(args.job_dir)
    settings = _settings(mode, snapshot=args.snapshot, output_root=args.output_root)
    if args.command == "retry":
        job_dir = retry_failed_handoff(
            settings,
            args.job_dir,
            handoff_root=args.output_root,
        )
        print(job_dir)
        print(f"Codex should now refill: {job_dir / 'drafts.json'}")
        return

    receipt = finalize_handoff(
        settings,
        args.job_dir,
        handoff_root=args.output_root,
    )
    print(json.dumps(receipt.model_dump(mode="json"), ensure_ascii=False, indent=2))


def _settings(
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


def _configured_mode(settings: Settings) -> Literal["demo", "live"]:
    return "demo" if settings.use_demo_provider else "live"


def _infer_mode(job_dir: Path) -> Literal["demo", "live"]:
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


if __name__ == "__main__":
    main()
