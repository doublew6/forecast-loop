"""Add append-only Agent trace attempts and artifact links.

Revision ID: 0014_trace_attempts
Revises: 0013_agent_eval_observability
Create Date: 2026-08-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_trace_attempts"
down_revision = "0013_agent_eval_observability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    trace_columns = {
        column["name"] for column in inspector.get_columns("agent_traces")
    }
    if not {"attempt_number", "target_id", "horizon"}.issubset(trace_columns):
        with op.batch_alter_table("agent_traces") as batch:
            batch.add_column(
                sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1")
            )
            batch.add_column(sa.Column("target_id", sa.String(120), nullable=True))
            batch.add_column(sa.Column("horizon", sa.String(32), nullable=True))
            batch.drop_constraint("uq_agent_trace_subject", type_="unique")
            batch.create_unique_constraint(
                "uq_agent_trace_attempt",
                ["workflow_kind", "subject_id", "attempt_number"],
            )
            batch.create_check_constraint(
                "ck_agent_trace_attempt_number", "attempt_number >= 1"
            )
        op.create_index("ix_agent_traces_target_id", "agent_traces", ["target_id"])
        op.create_index("ix_agent_traces_horizon", "agent_traces", ["horizon"])

    span_foreign_keys = inspector.get_foreign_keys("agent_trace_spans")
    if not any(
        key.get("name") == "fk_agent_trace_span_parent" for key in span_foreign_keys
    ):
        with op.batch_alter_table("agent_trace_spans") as batch:
            batch.create_foreign_key(
                "fk_agent_trace_span_parent",
                "agent_trace_spans",
                ["trace_id", "parent_span_id"],
                ["trace_id", "span_id"],
            )

    if "agent_trace_artifact_links" not in inspector.get_table_names():
        op.create_table(
            "agent_trace_artifact_links",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "trace_id",
                sa.String(32),
                sa.ForeignKey("agent_traces.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("span_id", sa.String(16), nullable=True),
            sa.Column("artifact_kind", sa.String(32), nullable=False),
            sa.Column("artifact_id", sa.String(160), nullable=False),
            sa.Column("relation", sa.String(24), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "trace_id",
                "span_id",
                "artifact_kind",
                "artifact_id",
                "relation",
                name="uq_agent_trace_artifact_link",
            ),
            sa.ForeignKeyConstraint(
                ["trace_id", "span_id"],
                ["agent_trace_spans.trace_id", "agent_trace_spans.span_id"],
                name="fk_agent_trace_artifact_link_span",
            ),
            sa.CheckConstraint(
                "artifact_kind IN ('signal', 'forecast', 'evaluation', 'reasoning_review', "
                "'reflection', 'bad_case')",
                name="ck_agent_trace_artifact_kind",
            ),
            sa.CheckConstraint(
                "relation IN ('input', 'output', 'reused', 'diagnostic')",
                name="ck_agent_trace_artifact_relation",
            ),
        )
        for column in (
            "trace_id",
            "span_id",
            "artifact_kind",
            "artifact_id",
            "relation",
            "created_at",
        ):
            op.create_index(
                f"ix_agent_trace_artifact_links_{column}",
                "agent_trace_artifact_links",
                [column],
            )
    _install_trace_seal_guards()


def downgrade() -> None:
    _drop_trace_seal_guards()
    op.drop_table("agent_trace_artifact_links")
    op.drop_index("ix_agent_traces_horizon", table_name="agent_traces")
    op.drop_index("ix_agent_traces_target_id", table_name="agent_traces")
    with op.batch_alter_table("agent_trace_spans") as batch:
        batch.drop_constraint("fk_agent_trace_span_parent", type_="foreignkey")
    with op.batch_alter_table("agent_traces") as batch:
        batch.drop_constraint("uq_agent_trace_attempt", type_="unique")
        batch.drop_constraint("ck_agent_trace_attempt_number", type_="check")
        batch.create_unique_constraint(
            "uq_agent_trace_subject", ["workflow_kind", "subject_id"]
        )
        batch.drop_column("horizon")
        batch.drop_column("target_id")
        batch.drop_column("attempt_number")


def _install_trace_seal_guards() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        statements = (
            "CREATE TRIGGER IF NOT EXISTS trg_agent_traces_reject_sealed_update "
            "BEFORE UPDATE ON agent_traces WHEN OLD.status != 'running' BEGIN "
            "SELECT RAISE(ABORT, 'sealed Agent trace is immutable'); END",
            "CREATE TRIGGER IF NOT EXISTS trg_agent_traces_reject_delete "
            "BEFORE DELETE ON agent_traces BEGIN "
            "SELECT RAISE(ABORT, 'Agent traces are retained'); END",
            "CREATE TRIGGER IF NOT EXISTS trg_agent_trace_spans_reject_sealed_insert "
            "BEFORE INSERT ON agent_trace_spans WHEN EXISTS "
            "(SELECT 1 FROM agent_traces WHERE id = NEW.trace_id AND status != 'running') "
            "BEGIN SELECT RAISE(ABORT, 'sealed Agent trace is immutable'); END",
            "CREATE TRIGGER IF NOT EXISTS trg_agent_trace_spans_reject_sealed_update "
            "BEFORE UPDATE ON agent_trace_spans WHEN EXISTS "
            "(SELECT 1 FROM agent_traces WHERE id = OLD.trace_id AND status != 'running') "
            "BEGIN SELECT RAISE(ABORT, 'sealed Agent trace is immutable'); END",
            "CREATE TRIGGER IF NOT EXISTS trg_agent_trace_spans_reject_delete "
            "BEFORE DELETE ON agent_trace_spans BEGIN "
            "SELECT RAISE(ABORT, 'Agent trace spans are retained'); END",
            "CREATE TRIGGER IF NOT EXISTS trg_agent_trace_links_reject_sealed_insert "
            "BEFORE INSERT ON agent_trace_artifact_links WHEN EXISTS "
            "(SELECT 1 FROM agent_traces WHERE id = NEW.trace_id AND status != 'running') "
            "BEGIN SELECT RAISE(ABORT, 'sealed Agent trace is immutable'); END",
            "CREATE TRIGGER IF NOT EXISTS trg_agent_trace_links_reject_update "
            "BEFORE UPDATE ON agent_trace_artifact_links BEGIN "
            "SELECT RAISE(ABORT, 'Agent trace artifact links are immutable'); END",
            "CREATE TRIGGER IF NOT EXISTS trg_agent_trace_links_reject_delete "
            "BEFORE DELETE ON agent_trace_artifact_links BEGIN "
            "SELECT RAISE(ABORT, 'Agent trace artifact links are retained'); END",
        )
        for statement in statements:
            connection.execute(sa.text(statement))
        return
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                "CREATE OR REPLACE FUNCTION forecast_loop_reject_sealed_trace_mutation() "
                "RETURNS trigger AS $$ BEGIN "
                "IF TG_OP = 'DELETE' OR OLD.status != 'running' THEN "
                "RAISE EXCEPTION 'sealed Agent trace is immutable'; END IF; RETURN NEW; "
                "END; $$ LANGUAGE plpgsql"
            )
        )
        connection.execute(
            sa.text(
                "CREATE TRIGGER trg_agent_traces_sealed BEFORE UPDATE OR DELETE "
                "ON agent_traces FOR EACH ROW EXECUTE FUNCTION "
                "forecast_loop_reject_sealed_trace_mutation()"
            )
        )
        connection.execute(
            sa.text(
                "CREATE OR REPLACE FUNCTION forecast_loop_reject_sealed_trace_child() "
                "RETURNS trigger AS $$ DECLARE parent_status text; BEGIN "
                "IF TG_OP = 'DELETE' OR "
                "(TG_OP = 'UPDATE' AND TG_TABLE_NAME = 'agent_trace_artifact_links') THEN "
                "RAISE EXCEPTION 'Agent trace child records are immutable'; END IF; "
                "SELECT status INTO parent_status FROM agent_traces WHERE id = NEW.trace_id; "
                "IF parent_status != 'running' THEN "
                "RAISE EXCEPTION 'sealed Agent trace is immutable'; END IF; RETURN NEW; "
                "END; $$ LANGUAGE plpgsql"
            )
        )
        connection.execute(
            sa.text(
                "CREATE TRIGGER trg_agent_trace_spans_immutable "
                "BEFORE INSERT OR UPDATE OR DELETE ON agent_trace_spans "
                "FOR EACH ROW EXECUTE FUNCTION forecast_loop_reject_sealed_trace_child()"
            )
        )
        connection.execute(
            sa.text(
                "CREATE TRIGGER trg_agent_trace_artifact_links_immutable "
                "BEFORE INSERT OR UPDATE OR DELETE ON agent_trace_artifact_links "
                "FOR EACH ROW EXECUTE FUNCTION forecast_loop_reject_sealed_trace_child()"
            )
        )


def _drop_trace_seal_guards() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        for trigger in (
            "trg_agent_traces_reject_sealed_update",
            "trg_agent_traces_reject_delete",
            "trg_agent_trace_spans_reject_sealed_insert",
            "trg_agent_trace_spans_reject_sealed_update",
            "trg_agent_trace_spans_reject_delete",
            "trg_agent_trace_links_reject_sealed_insert",
            "trg_agent_trace_links_reject_update",
            "trg_agent_trace_links_reject_delete",
        ):
            connection.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger}"))
        return
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text("DROP TRIGGER IF EXISTS trg_agent_traces_sealed ON agent_traces")
        )
        for table in ("agent_trace_spans", "agent_trace_artifact_links"):
            connection.execute(
                sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
            )
        connection.execute(
            sa.text("DROP FUNCTION IF EXISTS forecast_loop_reject_sealed_trace_mutation()")
        )
        connection.execute(
            sa.text("DROP FUNCTION IF EXISTS forecast_loop_reject_sealed_trace_child()")
        )
