"""Add the immutable reflection review gate and purge synthetic Demo scores.

Revision ID: 0004_reflection_review_gate
Revises: 0003_daily_reflection
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_reflection_review_gate"
down_revision = "0003_daily_reflection"
branch_labels = None
depends_on = None

_TABLE = "reflection_human_reviews"


def upgrade() -> None:
    # Demo forecasts remain available as UI examples, but their old synthetic
    # outcomes must not survive as formal scores.
    op.execute(
        sa.text(
            "DELETE FROM evaluation_results "
            "WHERE forecast_id IN ("
            "SELECT forecasts.id FROM forecasts "
            "JOIN workflow_runs ON workflow_runs.id = forecasts.run_id "
            "WHERE workflow_runs.mode = 'demo'"
            ")"
        )
    )
    op.execute(sa.text("DELETE FROM price_observations WHERE mode = 'demo'"))

    if _TABLE in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("reflection_run_id", sa.String(length=36), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("reviewer", sa.String(length=120), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("notes_hash", sa.String(length=64), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="ck_reflection_human_review_decision",
        ),
        sa.ForeignKeyConstraint(["reflection_run_id"], ["reflection_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reflection_run_id"),
    )
    for column in ("reflection_run_id", "decision", "reviewed_at"):
        op.create_index(
            op.f(f"ix_{_TABLE}_{column}"),
            _TABLE,
            [column],
            unique=column == "reflection_run_id",
        )


def downgrade() -> None:
    if _TABLE in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table(_TABLE)
    # Synthetic Demo evaluations deliberately cannot be reconstructed.
