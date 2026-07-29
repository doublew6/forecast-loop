"""Add versioned Agent contracts and capability-safe evaluation storage.

Revision ID: 0008_agent_contracts
Revises: 0007_user_judgment_agent
Create Date: 2026-07-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_agent_contracts"
down_revision = "0007_user_judgment_agent"
branch_labels = None
depends_on = None

_TABLES = ("agent_specs", "signal_envelopes")
_EXPECTED_COLUMNS = {
    "agent_specs": {
        "content_hash",
        "schema_version",
        "agent_id",
        "agent_version",
        "source_type",
        "participation_policy_id",
        "participation_policy_version",
        "participation_mode",
        "spec",
    },
    "signal_envelopes": {
        "id",
        "schema_version",
        "agent_id",
        "agent_version",
        "agent_spec_hash",
        "run_id",
        "mode",
        "source_type",
        "index_code",
        "horizon",
        "base_trade_date",
        "target_date",
        "submitted_at",
        "accepted_at",
        "participation_policy_id",
        "participation_policy_version",
        "participation_mode",
        "routing_lane",
        "formal_aggregation",
        "shadow_benchmark",
        "source_record_type",
        "source_record_id",
        "content_hash",
        "envelope",
    },
}
_EXPECTED_INDEXES = {
    "agent_specs": {
        "ix_agent_specs_agent_id",
        "ix_agent_specs_agent_version",
        "ix_agent_specs_source_type",
        "ix_agent_specs_participation_mode",
    },
    "signal_envelopes": {
        "ix_signal_envelopes_agent_id",
        "ix_signal_envelopes_agent_spec_hash",
        "ix_signal_envelopes_run_id",
        "ix_signal_envelopes_mode",
        "ix_signal_envelopes_source_type",
        "ix_signal_envelopes_index_code",
        "ix_signal_envelopes_horizon",
        "ix_signal_envelopes_target_date",
        "ix_signal_envelopes_accepted_at",
        "ix_signal_envelopes_participation_mode",
        "ix_signal_envelopes_routing_lane",
        "ux_signal_envelopes_content_hash",
    },
}
_EXPECTED_PRIMARY_KEYS = {
    "agent_specs": {"content_hash"},
    "signal_envelopes": {"id"},
}
_EXPECTED_CHECKS = {
    "signal_envelopes": {
        "ck_signal_envelope_mode",
        "ck_signal_envelope_source_type",
        "ck_signal_envelope_participation_mode",
        "ck_signal_envelope_routing_lane",
        "ck_signal_envelope_route_flags",
        "ck_signal_envelope_source_record_pair",
    },
}


def upgrade() -> None:
    bind = op.get_bind()
    present = set(sa.inspect(bind).get_table_names()).intersection(_TABLES)
    if present and present != set(_TABLES):
        missing = ", ".join(sorted(set(_TABLES) - present))
        raise RuntimeError(f"partial Agent contract schema detected; missing: {missing}")
    if not present:
        _create_tables()
    # Revision 0001 creates current metadata on a fresh database, so both
    # tables may already exist. Never mistake a partially-created table for a
    # complete contract store.
    _validate_schema(bind)
    _make_opinion_brier_nullable(bind)
    _install_immutable_guards(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _restore_opinion_brier_not_null(bind)
    _remove_immutable_guards(bind)
    existing = set(sa.inspect(bind).get_table_names())
    if "signal_envelopes" in existing:
        op.drop_table("signal_envelopes")
    if "agent_specs" in existing:
        op.drop_table("agent_specs")


def _create_tables() -> None:
    op.create_table(
        "agent_specs",
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("agent_version", sa.String(length=32), nullable=False),
        sa.Column("source_type", sa.String(length=24), nullable=False),
        sa.Column("participation_policy_id", sa.String(length=120), nullable=False),
        sa.Column("participation_policy_version", sa.String(length=32), nullable=False),
        sa.Column("participation_mode", sa.String(length=24), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("content_hash"),
    )
    for column in (
        "agent_id",
        "agent_version",
        "source_type",
        "participation_mode",
    ):
        op.create_index(
            f"ix_agent_specs_{column}",
            "agent_specs",
            [column],
            unique=False,
        )

    op.create_table(
        "signal_envelopes",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("agent_version", sa.String(length=32), nullable=False),
        sa.Column("agent_spec_hash", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("mode", sa.String(length=24), nullable=False),
        sa.Column("source_type", sa.String(length=24), nullable=False),
        sa.Column("index_code", sa.String(length=32), nullable=False),
        sa.Column("horizon", sa.String(length=16), nullable=False),
        sa.Column("base_trade_date", sa.Date(), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("participation_policy_id", sa.String(length=120), nullable=False),
        sa.Column("participation_policy_version", sa.String(length=32), nullable=False),
        sa.Column("participation_mode", sa.String(length=24), nullable=False),
        sa.Column("routing_lane", sa.String(length=32), nullable=False),
        sa.Column("formal_aggregation", sa.Boolean(), nullable=False),
        sa.Column("shadow_benchmark", sa.Boolean(), nullable=False),
        sa.Column("source_record_type", sa.String(length=48), nullable=True),
        sa.Column("source_record_id", sa.String(length=64), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("envelope", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "mode IN ('demo', 'live')",
            name="ck_signal_envelope_mode",
        ),
        sa.CheckConstraint(
            "source_type IN ('ai', 'manual', 'quant', 'deterministic')",
            name="ck_signal_envelope_source_type",
        ),
        sa.CheckConstraint(
            "participation_mode IN ('formal', 'shadow', 'disabled')",
            name="ck_signal_envelope_participation_mode",
        ),
        sa.CheckConstraint(
            "routing_lane IN "
            "('formal_input', 'formal_advisory', 'formal_decision', "
            "'shadow_benchmark')",
            name="ck_signal_envelope_routing_lane",
        ),
        sa.CheckConstraint(
            "(formal_aggregation AND NOT shadow_benchmark) OR "
            "(NOT formal_aggregation AND shadow_benchmark)",
            name="ck_signal_envelope_route_flags",
        ),
        sa.CheckConstraint(
            "(source_record_type IS NULL AND source_record_id IS NULL) OR "
            "(source_record_type IS NOT NULL AND source_record_id IS NOT NULL)",
            name="ck_signal_envelope_source_record_pair",
        ),
        sa.ForeignKeyConstraint(["agent_spec_hash"], ["agent_specs.content_hash"]),
        sa.ForeignKeyConstraint(["run_id"], ["workflow_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_record_type",
            "source_record_id",
            name="uq_signal_envelope_source_record",
        ),
    )
    for column in (
        "agent_id",
        "agent_spec_hash",
        "run_id",
        "mode",
        "source_type",
        "index_code",
        "horizon",
        "target_date",
        "accepted_at",
        "participation_mode",
        "routing_lane",
    ):
        op.create_index(
            f"ix_signal_envelopes_{column}",
            "signal_envelopes",
            [column],
            unique=False,
        )
    op.create_index(
        "ux_signal_envelopes_content_hash",
        "signal_envelopes",
        ["content_hash"],
        unique=True,
    )


def _validate_schema(bind: sa.engine.Connection) -> None:
    inspector = sa.inspect(bind)
    for table in _TABLES:
        column_items = inspector.get_columns(table)
        columns = {item["name"] for item in column_items}
        if columns != _EXPECTED_COLUMNS[table]:
            missing = ", ".join(sorted(_EXPECTED_COLUMNS[table] - columns))
            unexpected = ", ".join(sorted(columns - _EXPECTED_COLUMNS[table]))
            raise RuntimeError(
                f"incomplete {table} schema; missing=[{missing}], "
                f"unexpected=[{unexpected}]"
            )
        primary_key = set(
            inspector.get_pk_constraint(table).get(
                "constrained_columns",
                (),
            )
        )
        if primary_key != _EXPECTED_PRIMARY_KEYS[table]:
            raise RuntimeError(f"incomplete {table} primary key")
        nullable = {
            item["name"]
            for item in column_items
            if item.get("nullable")
        }
        expected_nullable = (
            {"source_record_type", "source_record_id"}
            if table == "signal_envelopes"
            else set()
        )
        if nullable != expected_nullable:
            raise RuntimeError(
                f"incomplete {table} nullability; nullable="
                + ", ".join(sorted(nullable))
            )
        indexes = {item["name"]: item for item in inspector.get_indexes(table)}
        missing_indexes = _EXPECTED_INDEXES[table] - set(indexes)
        if missing_indexes:
            raise RuntimeError(
                f"incomplete {table} indexes; missing: "
                + ", ".join(sorted(missing_indexes))
            )
    content_index = {
        item["name"]: item
        for item in inspector.get_indexes("signal_envelopes")
    }["ux_signal_envelopes_content_hash"]
    if not content_index.get("unique"):
        raise RuntimeError("SignalEnvelope content hash index must be unique")
    foreign_keys = {
        (
            tuple(item["constrained_columns"]),
            item["referred_table"],
            tuple(item["referred_columns"]),
        )
        for item in inspector.get_foreign_keys("signal_envelopes")
    }
    expected_foreign_keys = {
        (("agent_spec_hash",), "agent_specs", ("content_hash",)),
        (("run_id",), "workflow_runs", ("id",)),
    }
    if not expected_foreign_keys.issubset(foreign_keys):
        raise RuntimeError("incomplete signal_envelopes foreign keys")
    unique_constraints = {
        (
            item.get("name"),
            tuple(item.get("column_names") or ()),
        )
        for item in inspector.get_unique_constraints("signal_envelopes")
    }
    if (
        "uq_signal_envelope_source_record",
        ("source_record_type", "source_record_id"),
    ) not in unique_constraints:
        raise RuntimeError("incomplete signal_envelopes source-record uniqueness")
    checks = {
        item.get("name")
        for item in inspector.get_check_constraints("signal_envelopes")
    }
    missing_checks = _EXPECTED_CHECKS["signal_envelopes"] - checks
    if missing_checks:
        raise RuntimeError(
            "incomplete signal_envelopes checks; missing: "
            + ", ".join(sorted(missing_checks))
        )


def _make_opinion_brier_nullable(bind: sa.engine.Connection) -> None:
    column = next(
        item
        for item in sa.inspect(bind).get_columns("opinion_evaluations")
        if item["name"] == "brier_score"
    )
    if column["nullable"]:
        return
    recreate = "always" if bind.dialect.name == "sqlite" else "auto"
    with op.batch_alter_table(
        "opinion_evaluations",
        recreate=recreate,
    ) as batch:
        batch.alter_column(
            "brier_score",
            existing_type=sa.Float(),
            nullable=True,
        )


def _restore_opinion_brier_not_null(bind: sa.engine.Connection) -> None:
    if "opinion_evaluations" not in sa.inspect(bind).get_table_names():
        return
    null_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM opinion_evaluations "
            "WHERE brier_score IS NULL"
        )
    ).scalar_one()
    if null_count:
        raise RuntimeError(
            "cannot downgrade while capability-only opinion evaluations "
            "contain NULL Brier scores"
        )
    column = next(
        item
        for item in sa.inspect(bind).get_columns("opinion_evaluations")
        if item["name"] == "brier_score"
    )
    if not column["nullable"]:
        return
    recreate = "always" if bind.dialect.name == "sqlite" else "auto"
    with op.batch_alter_table(
        "opinion_evaluations",
        recreate=recreate,
    ) as batch:
        batch.alter_column(
            "brier_score",
            existing_type=sa.Float(),
            nullable=False,
        )


def _install_immutable_guards(bind: sa.engine.Connection) -> None:
    if bind.dialect.name == "sqlite":
        for table in _TABLES:
            for operation in ("UPDATE", "DELETE"):
                trigger = f"trg_{table}_reject_{operation.lower()}"
                op.execute(
                    sa.text(
                        f"CREATE TRIGGER IF NOT EXISTS {trigger} "
                        f"BEFORE {operation} ON {table} "
                        "BEGIN "
                        "SELECT RAISE(ABORT, 'immutable Agent contract record'); "
                        "END"
                    )
                )
        return
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                "CREATE OR REPLACE FUNCTION "
                "forecast_loop_reject_agent_contract_mutation() "
                "RETURNS trigger AS $$ "
                "BEGIN "
                "RAISE EXCEPTION 'immutable Agent contract record'; "
                "END; "
                "$$ LANGUAGE plpgsql"
            )
        )
        for table in _TABLES:
            trigger = f"trg_{table}_immutable"
            op.execute(
                sa.text(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
            )
            op.execute(
                sa.text(
                    f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} "
                    "FOR EACH ROW EXECUTE FUNCTION "
                    "forecast_loop_reject_agent_contract_mutation()"
                )
            )


def _remove_immutable_guards(bind: sa.engine.Connection) -> None:
    if bind.dialect.name == "sqlite":
        for table in _TABLES:
            for operation in ("update", "delete"):
                op.execute(
                    sa.text(
                        f"DROP TRIGGER IF EXISTS "
                        f"trg_{table}_reject_{operation}"
                    )
                )
        return
    if bind.dialect.name == "postgresql":
        for table in _TABLES:
            op.execute(
                sa.text(
                    f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}"
                )
            )
        op.execute(
            sa.text(
                "DROP FUNCTION IF EXISTS "
                "forecast_loop_reject_agent_contract_mutation()"
            )
        )
