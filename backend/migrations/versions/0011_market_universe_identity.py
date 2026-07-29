"""Bind workflow-run identity and active uniqueness to a market universe.

Revision ID: 0011_market_universe_identity
Revises: 0010_judgment_agent_spec_binding
Create Date: 2026-07-29
"""

from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op

revision = "0011_market_universe_identity"
down_revision = "0010_judgment_agent_spec_binding"
branch_labels = None
depends_on = None

_TABLE = "workflow_runs"
_COLUMN = "market_universe_hash"
_INDEX = "uq_active_live_run_as_of"
_LOOKUP_INDEX = "ix_workflow_runs_market_universe_hash"
_DEFAULT_MARKET_UNIVERSE_HASH = (
    "39bc170de19cdb657a2aa80f2de7908b7b422897bd1f39c9a08b649a2c636773"
)
_ACTIVE_LIVE_RUNS = sa.text(
    "mode = 'live' AND status IN "
    "('awaiting_draft', 'queued', 'running', 'completed')"
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if _COLUMN not in columns:
        op.add_column(
            _TABLE,
            sa.Column(
                _COLUMN,
                sa.String(length=64),
                nullable=False,
                server_default=_DEFAULT_MARKET_UNIVERSE_HASH,
            ),
        )
    else:
        bind.execute(
            sa.text(
                f"UPDATE {_TABLE} SET {_COLUMN} = :default_hash "
                f"WHERE {_COLUMN} IS NULL OR {_COLUMN} = ''"
            ),
            {"default_hash": _DEFAULT_MARKET_UNIVERSE_HASH},
        )
    _backfill_existing_universe_hashes(bind)

    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes(_TABLE)}
    if _INDEX in indexes:
        op.drop_index(_INDEX, table_name=_TABLE)
    if _LOOKUP_INDEX in indexes:
        op.drop_index(_LOOKUP_INDEX, table_name=_TABLE)
    op.create_index(
        _INDEX,
        _TABLE,
        [_COLUMN, "as_of"],
        unique=True,
        sqlite_where=_ACTIVE_LIVE_RUNS,
        postgresql_where=_ACTIVE_LIVE_RUNS,
    )
    op.create_index(
        _LOOKUP_INDEX,
        _TABLE,
        [_COLUMN],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes(_TABLE)}
    if _INDEX in indexes:
        op.drop_index(_INDEX, table_name=_TABLE)
    if _LOOKUP_INDEX in indexes:
        op.drop_index(_LOOKUP_INDEX, table_name=_TABLE)
    op.create_index(
        _INDEX,
        _TABLE,
        ["as_of"],
        unique=True,
        sqlite_where=_ACTIVE_LIVE_RUNS,
        postgresql_where=_ACTIVE_LIVE_RUNS,
    )
    op.drop_column(_TABLE, _COLUMN)


def _backfill_existing_universe_hashes(bind: sa.engine.Connection) -> None:
    runs = sa.table(
        _TABLE,
        sa.column("id", sa.String),
        sa.column("data_quality", sa.JSON),
        sa.column(_COLUMN, sa.String),
    )
    for row in bind.execute(
        sa.select(runs.c.id, runs.c.data_quality)
    ).mappings():
        quality = row["data_quality"]
        frozen = (
            quality.get("market_universe")
            if isinstance(quality, dict)
            else None
        )
        candidate = (
            frozen.get("content_hash")
            if isinstance(frozen, dict)
            else None
        )
        digest = (
            candidate
            if isinstance(candidate, str)
            and re.fullmatch(r"[0-9a-f]{64}", candidate)
            else _DEFAULT_MARKET_UNIVERSE_HASH
        )
        bind.execute(
            sa.update(runs)
            .where(runs.c.id == row["id"])
            .values({_COLUMN: digest})
        )
