from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from app.db import Database
from app.market_universe import DEFAULT_MARKET_UNIVERSE
from app.models import WorkflowRun
from app.services.schema_readiness import migration_config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError


def test_0011_backfills_universe_identity_and_scopes_active_uniqueness(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'market-universe-migration.sqlite3'}"
    configuration = migration_config(database_url)
    command.upgrade(configuration, "0010_judgment_agent_spec_binding")

    database = Database(database_url)
    try:
        # Revision 0001 creates current metadata on a fresh database. Remove
        # the new column to reproduce an actual schema that stopped at 0010.
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "DROP INDEX IF EXISTS "
                    "ix_workflow_runs_market_universe_hash"
                )
            )
            columns = {
                column["name"]
                for column in inspect(connection).get_columns("workflow_runs")
            }
            if "market_universe_hash" in columns:
                connection.execute(
                    text(
                        "ALTER TABLE workflow_runs "
                        "DROP COLUMN market_universe_hash"
                    )
                )
            connection.execute(
                text(
                    """
                    INSERT INTO workflow_runs (
                        id, as_of, data_cutoff, status, mode, started_at,
                        completed_at, duration_seconds, error, data_quality,
                        workflow_steps, input_hash
                    ) VALUES (
                        :id, :as_of, :data_cutoff, 'completed', 'live',
                        :started_at, :completed_at, 60.0, NULL, :data_quality,
                        '[]', :input_hash
                    )
                    """
                ),
                {
                    "id": "legacy-custom-universe-run",
                    "as_of": "2026-07-27 16:00:00.000000",
                    "data_cutoff": "2026-07-27 15:55:00.000000",
                    "started_at": "2026-07-27 16:00:00.000000",
                    "completed_at": "2026-07-27 16:01:00.000000",
                    "data_quality": (
                        '{"market_universe":{"content_hash":"'
                        + "f" * 64
                        + '"}}'
                    ),
                    "input_hash": "a" * 64,
                },
            )
    finally:
        database.dispose()

    command.upgrade(configuration, "head")
    database = Database(database_url)
    zone = ZoneInfo("Asia/Shanghai")
    try:
        with database.session_factory() as session:
            legacy = session.get(
                WorkflowRun,
                "legacy-custom-universe-run",
            )
            assert legacy is not None
            assert legacy.market_universe_hash == "f" * 64

            session.add(
                WorkflowRun(
                    id="same-time-default-universe-run",
                    as_of=datetime(2026, 7, 27, 16, tzinfo=zone),
                    data_cutoff=datetime(2026, 7, 27, 15, 55, tzinfo=zone),
                    status="completed",
                    mode="live",
                    started_at=datetime(2026, 7, 27, 16, tzinfo=zone),
                    completed_at=datetime(2026, 7, 27, 16, 1, tzinfo=zone),
                    duration_seconds=60.0,
                    error=None,
                    data_quality={},
                    workflow_steps=[],
                    input_hash="b" * 64,
                    market_universe_hash=DEFAULT_MARKET_UNIVERSE.content_hash,
                )
            )
            session.commit()

            session.add(
                WorkflowRun(
                    id="same-time-same-custom-universe-run",
                    as_of=datetime(2026, 7, 27, 16, tzinfo=zone),
                    data_cutoff=datetime(2026, 7, 27, 15, 55, tzinfo=zone),
                    status="completed",
                    mode="live",
                    started_at=datetime(2026, 7, 27, 16, tzinfo=zone),
                    completed_at=datetime(2026, 7, 27, 16, 1, tzinfo=zone),
                    duration_seconds=60.0,
                    error=None,
                    data_quality={},
                    workflow_steps=[],
                    input_hash="c" * 64,
                    market_universe_hash="f" * 64,
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

        indexes = {
            index["name"]: index
            for index in inspect(database.engine).get_indexes("workflow_runs")
        }
        assert indexes["uq_active_live_run_as_of"]["column_names"] == [
            "market_universe_hash",
            "as_of",
        ]
    finally:
        database.dispose()
