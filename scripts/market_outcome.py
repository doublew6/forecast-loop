"""Deterministic CLI for importing or blocking daily market outcomes."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path

from app.config import get_settings
from app.services.market_outcome import (
    import_market_snapshot,
    record_blocked_upstream,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import trusted live market outcomes.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser(
        "import",
        help="Validate a sealed market-outcome snapshot and evaluate due forecasts.",
    )
    import_parser.add_argument("snapshot", type=Path)

    block_parser = subparsers.add_parser(
        "block",
        help="Record a failed upstream gate without creating a reflection.",
    )
    block_parser.add_argument("--target-date", type=date.fromisoformat, required=True)
    block_parser.add_argument("--horizon", choices=("D1", "D2"), required=True)
    block_parser.add_argument("--reason-code", required=True)
    block_parser.add_argument("--error", required=True)

    args = parser.parse_args()
    settings = get_settings()
    if args.command == "import":
        result = import_market_snapshot(settings, args.snapshot)
        print(json.dumps(asdict(result), ensure_ascii=False, default=str))
        return
    batch = record_blocked_upstream(
        settings,
        target_date=args.target_date,
        horizon=args.horizon,
        reason_code=args.reason_code,
        error=args.error,
    )
    print(
        json.dumps(
            {
                "status": "no_due_live_forecast" if batch is None else batch.status,
                "batch_id": None if batch is None else batch.id,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
