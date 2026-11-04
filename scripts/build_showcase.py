"""Build an isolated public site without an API, operator proxy or private assets."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

from app.public_intraday import parse_publication  # noqa: E402
from app.public_showcase import export_snapshot  # noqa: E402

from scripts.audit_public_boundary import (  # noqa: E402
    Candidate,
    _load_private_rules,
    audit_candidates,
)


def build(
    *, database: Path | None, snapshot_path: Path | None, empty: bool, demo: bool, output: Path
) -> None:
    if output.is_symlink() or output.resolve() != output.absolute():
        raise ValueError("output must not traverse symlinks")
    if output == ROOT / "dist" or not output.is_relative_to(ROOT / "dist"):
        raise ValueError("build output must be inside the ignored dist directory")
    if database:
        snapshot = export_snapshot(database, mode="demo" if demo else "live")
        raw = snapshot.model_dump_json(indent=2)
    elif snapshot_path:
        if (
            snapshot_path.resolve(strict=True) != snapshot_path.absolute()
            or snapshot_path.stat().st_size > 1_000_000
        ):
            raise ValueError("invalid snapshot file")
        snapshot = parse_publication(snapshot_path.read_bytes())
        if snapshot.mode == "demo" and not demo:
            raise ValueError("synthetic data requires --demo")
        raw = snapshot.model_dump_json(indent=2)
    elif empty:
        raw = json.dumps(
            {
                "schema_version": "forecast-loop.public-showcase/v1",
                "status": "awaiting_publication",
            }
        )
    else:
        raise ValueError("an explicit source or --empty is required")
    output.parent.mkdir(parents=True, exist_ok=True)
    npm = shutil.which("npm")
    if npm is None:
        raise ValueError("npm is required")
    with tempfile.TemporaryDirectory(prefix="showcase-", dir=output.parent) as directory:
        stage = Path(directory)
        # The isolated entrypoint imports no operational app or API client.
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(
                ("VITE_", "LLM_", "OPENAI_", "LANGSMITH_", "FORECAST_LOOP_", "VERICOUNCIL_")
            )
        }
        subprocess.run(
            [
                npm,
                "exec",
                "--",
                "vite",
                "build",
                "--config",
                "vite.showcase.config.ts",
                "--outDir",
                str(stage),
            ],
            cwd=ROOT / "frontend",
            env=environment,
            check=True,
        )
        (stage / "showcase.html").rename(stage / "index.html")
        (stage / "forecast.json").write_text(raw + "\n", encoding="utf-8")
        (stage / "_headers").write_text(
            "/*\n"
            "  X-Content-Type-Options: nosniff\n"
            "  Referrer-Policy: no-referrer\n"
            "  X-Frame-Options: DENY\n"
            "  Permissions-Policy: camera=(), microphone=(), geolocation=()\n"
            "  Content-Security-Policy: default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'none'\n"
            "/forecast.json\n  Cache-Control: no-store\n"
            "/index.html\n  Cache-Control: no-cache\n",
            encoding="utf-8",
        )
        candidates = [
            Candidate(path.relative_to(stage).as_posix(), path.read_bytes())
            for path in stage.rglob("*")
            if path.is_file()
        ]
        counts, _, skipped = audit_candidates(
            candidates, private_rules=_load_private_rules(ROOT, None)
        )
        if counts or skipped:
            raise ValueError("public artifact privacy scan failed")
        # Keep the previous successful artifact if export, build or scanning fails.
        previous = output.with_name(output.name + ".previous")
        if previous.exists():
            shutil.rmtree(previous)
        if output.exists():
            output.rename(previous)
        try:
            shutil.copytree(stage, output)
        except Exception:
            shutil.rmtree(output, ignore_errors=True)
            if previous.exists():
                previous.rename(output)
            raise
        shutil.rmtree(previous, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--database", type=Path)
    source.add_argument("--snapshot", type=Path)
    source.add_argument("--empty", action="store_true")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "showcase")
    args = parser.parse_args()
    try:
        build(
            database=args.database,
            snapshot_path=args.snapshot,
            empty=args.empty,
            demo=args.demo,
            output=args.output.absolute(),
        )
    except Exception:
        print("Public showcase build blocked; previous artifact retained.")
        return 1
    print("Public showcase built successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
