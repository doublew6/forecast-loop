"""Exercise a data-bearing previous schema through the current Alembic head."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from alembic.script import ScriptDirectory
from app.services.schema_readiness import (
    downgrade_database,
    migration_config,
    require_schema_current,
    upgrade_database,
)

_SENTINEL_RUN_ID = "migration-smoke-preserved-run"


def run_migration_smoke(work_root: Path | None = None) -> dict[str, Any]:
    """Run the smoke test using only a synthetic temporary SQLite database."""

    if work_root is None:
        with tempfile.TemporaryDirectory(prefix="forecast-loop-migration-smoke-") as raw:
            return _run(Path(raw))
    work_root.mkdir(parents=True, exist_ok=True)
    return _run(work_root)


def _run(work_root: Path) -> dict[str, Any]:
    database_path = work_root / "migration-smoke.sqlite3"
    database_url = f"sqlite:///{database_path}"
    upgrade_database(database_url)

    script = ScriptDirectory.from_config(migration_config(database_url))
    heads = script.get_heads()
    if len(heads) != 1:
        raise RuntimeError(
            "migration smoke requires one linear head; "
            f"found {sorted(heads)}"
        )
    head = heads[0]
    down_revision = script.get_revision(head).down_revision
    if not isinstance(down_revision, str):
        raise RuntimeError("migration smoke requires a single previous revision")

    downgrade_database(database_url, down_revision)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            """
            INSERT INTO workflow_runs (
                id, as_of, data_cutoff, status, mode, started_at,
                completed_at, duration_seconds, error, data_quality,
                workflow_steps, input_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _SENTINEL_RUN_ID,
                "2026-07-27 15:00:00",
                "2026-07-27 14:55:00",
                "completed",
                "demo",
                "2026-07-27 15:00:00",
                "2026-07-27 15:01:00",
                60.0,
                None,
                '{"fixture":"synthetic"}',
                "[]",
                "a" * 64,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    upgrade_database(database_url)
    status = require_schema_current(database_url, deep=True)
    connection = sqlite3.connect(
        f"file:{database_path}?mode=ro",
        uri=True,
    )
    try:
        preserved = connection.execute(
            "SELECT input_hash, data_quality FROM workflow_runs WHERE id = ?",
            (_SENTINEL_RUN_ID,),
        ).fetchone()
    finally:
        connection.close()
    if preserved != ("a" * 64, '{"fixture":"synthetic"}'):
        raise RuntimeError("migration smoke did not preserve the sentinel row")

    return {
        "status": "passed",
        "from_revision": down_revision,
        "to_revision": head,
        "preserved_run_id": _SENTINEL_RUN_ID,
        "current_heads": list(status.current_heads),
    }


def main() -> None:
    print(
        json.dumps(
            run_migration_smoke(),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
