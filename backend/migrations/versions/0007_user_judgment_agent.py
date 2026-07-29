"""Add immutable User Judgment Agent records and derived evaluations.

Revision ID: 0007_user_judgment_agent
Revises: 0006_reflection_lineage_guard
Create Date: 2026-07-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_user_judgment_agent"
down_revision = "0006_reflection_lineage_guard"
branch_labels = None
depends_on = None

_TABLES = {"user_judgments", "user_judgment_evaluations"}


def upgrade() -> None:
    bind = op.get_bind()
    present = set(sa.inspect(bind).get_table_names()) & _TABLES
    if present and present != _TABLES:
        missing = ", ".join(sorted(_TABLES - present))
        raise RuntimeError(f"partial User Judgment schema detected; missing: {missing}")
    if not present:
        _create_tables()
    # Revision 0001 intentionally creates current metadata on a fresh database.
    # The tables may therefore pre-exist, but the database-level immutability
    # guard must still be installed here.
    _install_immutable_guards(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _remove_immutable_guards(bind)
    existing = set(sa.inspect(bind).get_table_names())
    if "user_judgment_evaluations" in existing:
        op.drop_table("user_judgment_evaluations")
    if "user_judgments" in existing:
        op.drop_table("user_judgments")


def _create_tables() -> None:
    op.create_table(
        "user_judgments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_id", sa.String(length=120), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("agent_version", sa.String(length=32), nullable=False),
        sa.Column("forecast_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("mode", sa.String(length=24), nullable=False),
        sa.Column("index_code", sa.String(length=24), nullable=False),
        sa.Column("horizon", sa.String(length=8), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("counter_evidence", sa.Text(), nullable=False),
        sa.Column("invalidation_condition", sa.Text(), nullable=False),
        sa.Column("blind_attestation", sa.Boolean(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submission_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("formal_score_eligible", sa.Boolean(), nullable=False),
        sa.Column("run_input_hash", sa.String(length=64), nullable=False),
        sa.Column("forecast_input_hash", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=32), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("wiki_path", sa.Text(), nullable=False),
        sa.Column("wiki_artifact_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "direction IN ('up', 'down')",
            name="ck_user_judgment_direction",
        ),
        sa.CheckConstraint(
            "confidence >= 0.5 AND confidence <= 1.0",
            name="ck_user_judgment_confidence",
        ),
        sa.CheckConstraint(
            "mode IN ('demo', 'live')",
            name="ck_user_judgment_mode",
        ),
        sa.CheckConstraint(
            "NOT formal_score_eligible OR "
            "(mode = 'live' AND blind_attestation "
            "AND submission_deadline IS NOT NULL "
            "AND submitted_at < submission_deadline)",
            name="ck_user_judgment_formal_eligibility",
        ),
        sa.CheckConstraint(
            "length(trim(rationale)) >= 20",
            name="ck_user_judgment_rationale",
        ),
        sa.CheckConstraint(
            "length(trim(counter_evidence)) >= 10",
            name="ck_user_judgment_counter_evidence",
        ),
        sa.CheckConstraint(
            "length(trim(invalidation_condition)) >= 10",
            name="ck_user_judgment_invalidation",
        ),
        sa.ForeignKeyConstraint(["forecast_id"], ["forecasts.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["workflow_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_id",
            "forecast_id",
            name="uq_user_judgment_actor_forecast",
        ),
        sa.UniqueConstraint("content_hash"),
    )
    for column in (
        "actor_id",
        "agent_id",
        "forecast_id",
        "run_id",
        "mode",
        "index_code",
        "horizon",
        "target_date",
        "direction",
        "submitted_at",
        "formal_score_eligible",
        "content_hash",
    ):
        op.create_index(
            op.f(f"ix_user_judgments_{column}"),
            "user_judgments",
            [column],
            unique=False,
        )

    op.create_table(
        "user_judgment_evaluations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_judgment_id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("evaluation_result_id", sa.String(length=36), nullable=False),
        sa.Column("actual_return", sa.Float(), nullable=False),
        sa.Column("actual_label", sa.String(length=16), nullable=False),
        sa.Column("sign_correct", sa.Boolean(), nullable=True),
        sa.Column("material_direction_correct", sa.Boolean(), nullable=True),
        sa.Column("observation_hash", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=32), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "actual_label IN ('up', 'neutral', 'down')",
            name="ck_user_judgment_evaluation_label",
        ),
        sa.ForeignKeyConstraint(["batch_id"], ["evaluation_batches.id"]),
        sa.ForeignKeyConstraint(["evaluation_result_id"], ["evaluation_results.id"]),
        sa.ForeignKeyConstraint(["user_judgment_id"], ["user_judgments.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_judgment_id"),
        sa.UniqueConstraint("content_hash"),
    )
    for column in (
        "user_judgment_id",
        "batch_id",
        "evaluation_result_id",
        "evaluated_at",
        "content_hash",
    ):
        op.create_index(
            op.f(f"ix_user_judgment_evaluations_{column}"),
            "user_judgment_evaluations",
            [column],
            unique=False,
        )


def _install_immutable_guards(bind: sa.engine.Connection) -> None:
    if bind.dialect.name == "sqlite":
        for table in sorted(_TABLES):
            for operation in ("UPDATE", "DELETE"):
                trigger = f"trg_{table}_reject_{operation.lower()}"
                op.execute(
                    sa.text(
                        f"CREATE TRIGGER IF NOT EXISTS {trigger} "
                        f"BEFORE {operation} ON {table} "
                        "BEGIN "
                        "SELECT RAISE(ABORT, 'immutable User Judgment record'); "
                        "END"
                    )
                )
        return
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                "CREATE OR REPLACE FUNCTION "
                "vericouncil_reject_user_judgment_mutation() "
                "RETURNS trigger AS $$ "
                "BEGIN "
                "RAISE EXCEPTION 'immutable User Judgment record'; "
                "END; "
                "$$ LANGUAGE plpgsql"
            )
        )
        for table in sorted(_TABLES):
            trigger = f"trg_{table}_immutable"
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger} ON {table}"))
            op.execute(
                sa.text(
                    f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} "
                    "FOR EACH ROW EXECUTE FUNCTION "
                    "vericouncil_reject_user_judgment_mutation()"
                )
            )


def _remove_immutable_guards(bind: sa.engine.Connection) -> None:
    if bind.dialect.name == "sqlite":
        for table in sorted(_TABLES):
            for operation in ("update", "delete"):
                op.execute(
                    sa.text(
                        f"DROP TRIGGER IF EXISTS trg_{table}_reject_{operation}"
                    )
                )
        return
    if bind.dialect.name == "postgresql":
        for table in sorted(_TABLES):
            op.execute(
                sa.text(
                    f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}"
                )
            )
        op.execute(
            sa.text(
                "DROP FUNCTION IF EXISTS "
                "vericouncil_reject_user_judgment_mutation()"
            )
        )
