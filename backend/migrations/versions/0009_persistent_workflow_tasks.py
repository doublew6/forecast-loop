"""Add a persistent queue for committee workflow execution.

Revision ID: 0009_persistent_workflow_tasks
Revises: 0008_agent_contracts
Create Date: 2026-07-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_persistent_workflow_tasks"
down_revision = "0008_agent_contracts"
branch_labels = None
depends_on = None

_TABLE = "workflow_tasks"
_EXPECTED_COLUMNS = {
    "id",
    "run_id",
    "kind",
    "status",
    "stage",
    "idempotency_key",
    "payload",
    "payload_hash",
    "attempt_count",
    "max_attempts",
    "available_at",
    "attempt_started_at",
    "lease_owner",
    "lease_token",
    "lease_expires_at",
    "timeout_seconds",
    "last_error",
    "created_at",
    "updated_at",
    "completed_at",
    "version",
}
_EXPECTED_INDEXES = {
    "ix_workflow_tasks_run_id",
    "ix_workflow_tasks_status",
    "ix_workflow_tasks_available_at",
    "ix_workflow_tasks_lease_expires_at",
    "ix_workflow_tasks_claim",
    "ix_workflow_tasks_expired_lease",
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        _create_table()
        inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    missing = sorted(_EXPECTED_COLUMNS - columns)
    if missing:
        raise RuntimeError(
            "partial persistent task schema detected; missing columns: "
            + ", ".join(missing)
        )
    indexes = {index["name"] for index in inspector.get_indexes(_TABLE)}
    missing_indexes = sorted(_EXPECTED_INDEXES - indexes)
    if missing_indexes:
        raise RuntimeError(
            "partial persistent task schema detected; missing indexes: "
            + ", ".join(missing_indexes)
        )


def downgrade() -> None:
    if _TABLE in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table(_TABLE)


def _create_table() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=120), nullable=True),
        sa.Column("lease_token", sa.String(length=36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'completed', 'failed')",
            name="ck_workflow_task_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1 "
            "AND attempt_count <= max_attempts",
            name="ck_workflow_task_attempts",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND lease_owner IS NOT NULL "
            "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND attempt_started_at IS NOT NULL) OR "
            "(status != 'running' AND lease_owner IS NULL "
            "AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_workflow_task_lease",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_workflow_task_idempotency"),
        sa.UniqueConstraint("run_id", name="uq_workflow_task_run"),
    )
    op.create_index("ix_workflow_tasks_run_id", _TABLE, ["run_id"])
    op.create_index("ix_workflow_tasks_status", _TABLE, ["status"])
    op.create_index("ix_workflow_tasks_available_at", _TABLE, ["available_at"])
    op.create_index(
        "ix_workflow_tasks_lease_expires_at",
        _TABLE,
        ["lease_expires_at"],
    )
    op.create_index(
        "ix_workflow_tasks_claim",
        _TABLE,
        ["status", "available_at", "created_at"],
    )
    op.create_index(
        "ix_workflow_tasks_expired_lease",
        _TABLE,
        ["status", "lease_expires_at"],
    )
