"""Prevent concurrent active successors in one Reflection lineage.

Revision ID: 0006_reflection_lineage_guard
Revises: 0005_lesson_lifecycle
Create Date: 2026-07-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_reflection_lineage_guard"
down_revision = "0005_lesson_lifecycle"
branch_labels = None
depends_on = None

_INDEX_NAME = "uq_reflection_active_successor"
_TABLE_NAME = "reflection_runs"
_ACTIVE_SUCCESSOR = sa.text(
    "supersedes_id IS NOT NULL AND status IN "
    "('awaiting_sources', 'awaiting_analysis', 'completed')"
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {
        item["name"] for item in inspector.get_indexes(_TABLE_NAME)
    }
    if _INDEX_NAME in indexes:
        return
    conflicts = op.get_bind().execute(
        sa.text(
            "SELECT supersedes_id, COUNT(*) AS successor_count "
            "FROM reflection_runs "
            "WHERE supersedes_id IS NOT NULL "
            "AND status IN ('awaiting_sources', 'awaiting_analysis', 'completed') "
            "GROUP BY supersedes_id HAVING COUNT(*) > 1 "
            "ORDER BY supersedes_id"
        )
    ).mappings().all()
    if conflicts:
        parents = ", ".join(str(item["supersedes_id"]) for item in conflicts)
        raise RuntimeError(
            "cannot install Reflection lineage guard while active forks exist: "
            f"{parents}"
        )
    op.create_index(
        _INDEX_NAME,
        _TABLE_NAME,
        ["supersedes_id"],
        unique=True,
        sqlite_where=_ACTIVE_SUCCESSOR,
        postgresql_where=_ACTIVE_SUCCESSOR,
    )


def downgrade() -> None:
    indexes = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes(_TABLE_NAME)
    }
    if _INDEX_NAME in indexes:
        op.drop_index(_INDEX_NAME, table_name=_TABLE_NAME)
