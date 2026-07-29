from __future__ import annotations

import json
import os
import sqlite3
import stat
from datetime import date
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from app.cli import main as cli_main
from app.config import Settings
from app.demo import main as demo_main
from app.main import create_app
from app.services.market_outcome import (
    import_market_snapshot,
    record_blocked_upstream,
)
from app.services.recovery import (
    MANIFEST_NAME,
    RecoveryError,
    create_backup,
    restore_backup,
    verify_backup,
)
from app.services.reflection_handoff import (
    finalize_reflection,
    freeze_reflection_sources,
    prepare_reflection,
)
from app.services.schema_readiness import (
    SchemaNotReadyError,
    downgrade_database,
    inspect_schema,
    migration_config,
    require_schema_current,
    upgrade_database,
)
from fastapi.testclient import TestClient

from scripts.migration_smoke import run_migration_smoke

_RUN_ID = "synthetic-recovery-run"


def test_runtime_requires_an_explicit_migration(tmp_path) -> None:
    database_path = tmp_path / "unmigrated.sqlite3"
    settings = Settings(
        database_url=f"sqlite:///{database_path}",
        checkpoint_path=tmp_path / "checkpoint.sqlite3",
        wiki_path=tmp_path / "wiki",
        demo_mode=True,
        auto_seed=False,
    )

    with pytest.raises(SchemaNotReadyError, match="database schema is not ready"):
        with TestClient(create_app(settings)):
            pass

    connection = sqlite3.connect(database_path)
    try:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    finally:
        connection.close()
    assert tables == []

    migrated = upgrade_database(settings.database_url)
    assert migrated.ready is True
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").status_code == 200


def test_schema_status_rejects_an_unversioned_create_all_database(tmp_path) -> None:
    from app.db import Database

    database = Database(f"sqlite:///{tmp_path / 'create-all.sqlite3'}")
    try:
        database.create_all()
        status = inspect_schema(database.engine)
        assert status.ready is False
        assert status.current_heads == ()
        with pytest.raises(SchemaNotReadyError, match="alembic_version"):
            require_schema_current(database.engine)
    finally:
        database.dispose()


def test_demo_entrypoint_rejects_an_empty_database_without_side_effects(
    monkeypatch,
    tmp_path,
) -> None:
    database_path = tmp_path / "empty-demo.sqlite3"
    database_path.touch()
    checkpoint_path, wiki_path = _configure_demo_entrypoint(
        monkeypatch,
        tmp_path,
        database_path,
    )

    with pytest.raises(SchemaNotReadyError, match="alembic_version"):
        demo_main()

    assert database_path.read_bytes() == b""
    assert not checkpoint_path.exists()
    assert not wiki_path.exists()


def test_demo_entrypoint_rejects_a_stale_database_without_side_effects(
    monkeypatch,
    tmp_path,
) -> None:
    _, database_path = _stale_settings(tmp_path)
    checkpoint_path, wiki_path = _configure_demo_entrypoint(
        monkeypatch,
        tmp_path,
        database_path,
    )
    database_before = database_path.read_bytes()

    with pytest.raises(SchemaNotReadyError, match="migration heads"):
        demo_main()

    assert database_path.read_bytes() == database_before
    assert not checkpoint_path.exists()
    assert not wiki_path.exists()
    with sqlite3.connect(
        f"file:{database_path}?mode=ro",
        uri=True,
    ) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM workflow_runs"
        ).fetchone()
    assert count == (0,)


def test_demo_entrypoint_seeds_only_an_explicitly_migrated_database(
    monkeypatch,
    tmp_path,
) -> None:
    database_path = tmp_path / "migrated-demo.sqlite3"
    database_url = f"sqlite:///{database_path}"
    upgrade_database(database_url)
    _configure_demo_entrypoint(monkeypatch, tmp_path, database_path)

    demo_main()

    status = require_schema_current(database_url, deep=True)
    assert status.ready is True
    with sqlite3.connect(
        f"file:{database_path}?mode=ro",
        uri=True,
    ) as connection:
        runs = connection.execute(
            "SELECT COUNT(*) FROM workflow_runs WHERE mode = 'demo'"
        ).fetchone()
        forecasts = connection.execute(
            "SELECT COUNT(*) FROM forecasts"
        ).fetchone()
    assert runs is not None and runs[0] > 0
    assert forecasts is not None and forecasts[0] > 0


def test_state_changing_runtimes_reject_stale_schema_before_side_effects(
    tmp_path,
) -> None:
    settings, database_path = _stale_settings(tmp_path)
    database_before = database_path.read_bytes()
    reflection_root = settings.reflection_root

    operations = (
        lambda: import_market_snapshot(
            settings,
            tmp_path / "missing-market-snapshot.json",
        ),
        lambda: record_blocked_upstream(
            settings,
            target_date=date(2026, 7, 28),
            horizon="D1",
            reason_code="synthetic",
            error="must not be persisted",
        ),
        lambda: prepare_reflection(
            settings,
            "missing-live-run",
            horizon="D1",
        ),
        lambda: freeze_reflection_sources(
            settings,
            tmp_path / "missing-reflection-job",
        ),
        lambda: finalize_reflection(
            settings,
            tmp_path / "missing-reflection-job",
        ),
    )

    for operation in operations:
        with pytest.raises(SchemaNotReadyError, match="migration heads"):
            operation()

    assert not reflection_root.exists()
    assert database_path.read_bytes() == database_before


def test_judgment_record_rejects_stale_schema_before_private_file_reads(
    tmp_path,
) -> None:
    settings, database_path = _stale_settings(tmp_path)
    database_before = database_path.read_bytes()
    wiki_root = tmp_path / "private-user-wiki"

    with pytest.raises(SchemaNotReadyError, match="migration heads"):
        cli_main(
            [
                "judgment",
                "record",
                "--forecast-id",
                "missing-forecast",
                "--direction",
                "up",
                "--confidence",
                "0.5",
                "--rationale-file",
                str(tmp_path / "missing-rationale.txt"),
                "--counter-evidence-file",
                str(tmp_path / "missing-counter-evidence.txt"),
                "--invalidation-file",
                str(tmp_path / "missing-invalidation.txt"),
                "--database-url",
                settings.database_url,
                "--wiki-root",
                str(wiki_root),
            ]
        )

    assert not wiki_root.exists()
    assert database_path.read_bytes() == database_before


def test_migration_smoke_preserves_a_previous_revision_row(tmp_path) -> None:
    result = run_migration_smoke(tmp_path)

    assert result["status"] == "passed"
    assert result["preserved_run_id"] == "migration-smoke-preserved-run"
    assert result["current_heads"] == [result["to_revision"]]


def test_backup_restore_drill_uses_consistent_private_artifacts(tmp_path) -> None:
    database_path, checkpoint_path, mutable_root = _synthetic_state(tmp_path)
    backup_root = tmp_path / "backups"

    bundle = create_backup(
        database_path=database_path,
        checkpoint_path=checkpoint_path,
        roots={"handoffs": mutable_root},
        output_root=backup_root,
    )
    manifest = verify_backup(bundle)

    assert manifest["schema_version"] == "forecast-loop.recovery-backup/v1"
    assert stat.S_IMODE(os.lstat(bundle / MANIFEST_NAME).st_mode) == 0o600
    for artifact in manifest["artifacts"]:
        assert stat.S_IMODE(
            os.lstat(bundle / artifact["path"]).st_mode
        ) == 0o600

    # Mutating the live synthetic sources after the backup must not alter it.
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "UPDATE workflow_runs SET input_hash = ? WHERE id = ?",
            ("b" * 64, _RUN_ID),
        )
        connection.commit()
    finally:
        connection.close()
    (mutable_root / "draft.json").write_text("changed", encoding="utf-8")

    target = tmp_path / "isolated-restore"
    receipt = restore_backup(bundle, target_root=target)
    assert receipt.name == "restore-receipt.json"
    assert stat.S_IMODE(os.lstat(receipt).st_mode) == 0o600

    connection = sqlite3.connect(
        f"file:{target / 'files' / 'database.sqlite3'}?mode=ro",
        uri=True,
    )
    try:
        restored_hash = connection.execute(
            "SELECT input_hash FROM workflow_runs WHERE id = ?",
            (_RUN_ID,),
        ).fetchone()
    finally:
        connection.close()
    assert restored_hash == ("a" * 64,)

    checkpoint = sqlite3.connect(
        f"file:{target / 'files' / 'checkpoint.sqlite3'}?mode=ro",
        uri=True,
    )
    try:
        checkpoint_value = checkpoint.execute(
            "SELECT value FROM checkpoint_fixture WHERE key = 'cursor'"
        ).fetchone()
    finally:
        checkpoint.close()
    assert checkpoint_value == ("sealed",)
    assert (
        target / "roots" / "handoffs" / "draft.json"
    ).read_text(encoding="utf-8") == '{"synthetic":true}\n'

    receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert receipt_payload["source_manifest_hash"] == manifest["manifest_hash"]
    assert receipt_payload["database_summary"]["core_row_counts"][
        "workflow_runs"
    ] == 1


def test_backup_detects_tampering_and_restore_refuses_nonempty_target(
    tmp_path,
) -> None:
    database_path, checkpoint_path, mutable_root = _synthetic_state(tmp_path)
    bundle = create_backup(
        database_path=database_path,
        checkpoint_path=checkpoint_path,
        roots={"handoffs": mutable_root},
        output_root=tmp_path / "backups",
    )
    target = tmp_path / "not-empty"
    target.mkdir()
    (target / "existing").write_text("keep", encoding="utf-8")
    with pytest.raises(RecoveryError, match="must be empty"):
        restore_backup(bundle, target_root=target)

    manifest = verify_backup(bundle)
    database_artifact = next(
        item for item in manifest["artifacts"] if item["role"] == "database"
    )
    with (bundle / database_artifact["path"]).open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(RecoveryError, match="artifact hash mismatch"):
        verify_backup(bundle)


def test_backup_verification_and_restore_reject_public_bundle_mode(
    tmp_path,
) -> None:
    database_path, checkpoint_path, mutable_root = _synthetic_state(tmp_path)
    bundle = create_backup(
        database_path=database_path,
        checkpoint_path=checkpoint_path,
        roots={"handoffs": mutable_root},
        output_root=tmp_path / "backups",
    )
    os.chmod(bundle, 0o755)

    with pytest.raises(
        RecoveryError,
        match="backup bundle must not be accessible by group/other",
    ):
        verify_backup(bundle)
    with pytest.raises(
        RecoveryError,
        match="backup bundle must not be accessible by group/other",
    ):
        restore_backup(bundle, target_root=tmp_path / "restore")

    assert not (tmp_path / "restore").exists()


def test_backup_rejects_duplicate_and_nested_sources(tmp_path) -> None:
    database_path, checkpoint_path, mutable_root = _synthetic_state(tmp_path)
    nested_root = mutable_root / "nested"
    nested_root.mkdir()

    invalid_sources = (
        {
            "database_path": database_path,
            "checkpoint_path": database_path,
            "roots": {"handoffs": mutable_root},
        },
        {
            "database_path": database_path,
            "checkpoint_path": checkpoint_path,
            "roots": {"handoffs": mutable_root, "duplicate": mutable_root},
        },
        {
            "database_path": database_path,
            "checkpoint_path": checkpoint_path,
            "roots": {"handoffs": mutable_root, "nested": nested_root},
        },
        {
            "database_path": database_path,
            "checkpoint_path": checkpoint_path,
            "roots": {"all-state": tmp_path},
        },
    )

    for index, sources in enumerate(invalid_sources):
        with pytest.raises(
            RecoveryError,
            match="backup sources must be mutually isolated",
        ):
            create_backup(
                **sources,
                output_root=tmp_path / f"backups-{index}",
            )

    assert not any(tmp_path.glob("backups-*"))


def test_backup_rejects_symlinks_inside_mutable_roots(tmp_path) -> None:
    database_path, checkpoint_path, mutable_root = _synthetic_state(tmp_path)
    (mutable_root / "unsafe").symlink_to(mutable_root / "draft.json")

    with pytest.raises(RecoveryError, match="may not contain symlinks"):
        create_backup(
            database_path=database_path,
            checkpoint_path=checkpoint_path,
            roots={"handoffs": mutable_root},
            output_root=tmp_path / "backups",
        )


def _synthetic_state(tmp_path: Path) -> tuple[Path, Path, Path]:
    database_path = tmp_path / "source.sqlite3"
    upgrade_database(f"sqlite:///{database_path}")
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
                _RUN_ID,
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

    checkpoint_path = tmp_path / "checkpoint.sqlite3"
    checkpoint = sqlite3.connect(checkpoint_path)
    try:
        checkpoint.execute(
            "CREATE TABLE checkpoint_fixture (key TEXT PRIMARY KEY, value TEXT)"
        )
        checkpoint.execute(
            "INSERT INTO checkpoint_fixture (key, value) VALUES (?, ?)",
            ("cursor", "sealed"),
        )
        checkpoint.commit()
    finally:
        checkpoint.close()

    mutable_root = tmp_path / "handoffs"
    mutable_root.mkdir()
    (mutable_root / "draft.json").write_text(
        '{"synthetic":true}\n',
        encoding="utf-8",
    )
    return database_path, checkpoint_path, mutable_root


def _stale_settings(tmp_path: Path) -> tuple[Settings, Path]:
    database_path = tmp_path / "stale.sqlite3"
    database_url = f"sqlite:///{database_path}"
    upgrade_database(database_url)
    script = ScriptDirectory.from_config(migration_config(database_url))
    head = script.get_current_head()
    assert head is not None
    previous = script.get_revision(head).down_revision
    assert isinstance(previous, str)
    downgrade_database(database_url, previous)
    return (
        Settings(
            database_url=database_url,
            checkpoint_path=tmp_path / "checkpoint.sqlite3",
            wiki_path=tmp_path / "wiki",
            reflection_root=tmp_path / "reflections",
            demo_mode=False,
            execution_provider="codex_file",
            auto_seed=False,
        ),
        database_path,
    )


def _configure_demo_entrypoint(
    monkeypatch,
    tmp_path: Path,
    database_path: Path,
) -> tuple[Path, Path]:
    checkpoint_path = tmp_path / "demo-checkpoint.sqlite3"
    wiki_path = tmp_path / "demo-wiki"
    monkeypatch.setenv(
        "VERICOUNCIL_DATABASE_URL",
        f"sqlite:///{database_path}",
    )
    monkeypatch.setenv(
        "VERICOUNCIL_CHECKPOINT_PATH",
        str(checkpoint_path),
    )
    monkeypatch.setenv("VERICOUNCIL_WIKI_PATH", str(wiki_path))
    monkeypatch.setenv("VERICOUNCIL_EXECUTION_PROVIDER", "demo")
    return checkpoint_path, wiki_path
