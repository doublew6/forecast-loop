"""Include awaiting Codex drafts in the active live-run guard.

Revision ID: 0002_handoff_waiting_index
Revises: 0001_initial
Create Date: 2026-07-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_handoff_waiting_index"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

_INDEX_NAME = "uq_active_live_run_as_of"
_TABLE_NAME = "workflow_runs"
_OLD_ACTIVE_LIVE_RUNS = sa.text(
    "mode = 'live' AND status IN ('queued', 'running', 'completed')"
)
_HANDOFF_ACTIVE_LIVE_RUNS = sa.text(
    "mode = 'live' AND status IN "
    "('awaiting_draft', 'queued', 'running', 'completed')"
)


def upgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name=_TABLE_NAME)
    op.create_index(
        _INDEX_NAME,
        _TABLE_NAME,
        ["as_of"],
        unique=True,
        sqlite_where=_HANDOFF_ACTIVE_LIVE_RUNS,
        postgresql_where=_HANDOFF_ACTIVE_LIVE_RUNS,
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name=_TABLE_NAME)
    op.create_index(
        _INDEX_NAME,
        _TABLE_NAME,
        ["as_of"],
        unique=True,
        sqlite_where=_OLD_ACTIVE_LIVE_RUNS,
        postgresql_where=_OLD_ACTIVE_LIVE_RUNS,
    )
