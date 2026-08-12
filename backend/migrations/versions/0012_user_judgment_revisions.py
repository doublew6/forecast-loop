"""Publish post-close blind targets and append-only judgment revisions.

Revision ID: 0012_user_judgment_revisions
Revises: 0011_market_universe_identity
Create Date: 2026-08-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_user_judgment_revisions"
down_revision = "0011_market_universe_identity"
branch_labels = None
depends_on = None

_TARGETS = "user_judgment_targets"
_JUDGMENTS = "user_judgments"
_EVALUATIONS = "user_judgment_evaluations"
_GUARDED_TABLES = {_TARGETS, _JUDGMENTS, _EVALUATIONS}
_SQLITE_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    # Historical SQLite databases may still expose ``UNIQUE (content_hash)``
    # without a constraint name.  Batch reflection must be able to assign a
    # deterministic name before recreating the table.
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "%(constraint_name)s",
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TARGETS not in inspector.get_table_names():
        _create_target_table()

    inspector = sa.inspect(bind)
    columns = {column["name"]: column for column in inspector.get_columns(_JUDGMENTS)}
    required = {
        "target_id",
        "revision_number",
        "supersedes_id",
        "target_content_hash",
    }
    forecast_nullable = columns["forecast_id"]["nullable"]
    forecast_hash_nullable = columns["forecast_input_hash"]["nullable"]
    if (
        not required.issubset(columns)
        or not forecast_nullable
        or not forecast_hash_nullable
    ):
        _remove_immutable_guards(bind)
        with op.batch_alter_table(
            _JUDGMENTS,
            recreate="always" if bind.dialect.name == "sqlite" else "auto",
            naming_convention=_SQLITE_NAMING_CONVENTION,
        ) as batch:
            if "target_id" not in columns:
                batch.add_column(sa.Column("target_id", sa.String(36), nullable=True))
            if "revision_number" not in columns:
                batch.add_column(
                    sa.Column(
                        "revision_number",
                        sa.Integer(),
                        nullable=False,
                        server_default="1",
                    )
                )
            if "supersedes_id" not in columns:
                batch.add_column(
                    sa.Column("supersedes_id", sa.String(36), nullable=True)
                )
            if "target_content_hash" not in columns:
                batch.add_column(
                    sa.Column("target_content_hash", sa.String(64), nullable=True)
                )
            if not forecast_nullable:
                batch.alter_column(
                    "forecast_id",
                    existing_type=sa.String(36),
                    nullable=True,
                )
            if not forecast_hash_nullable:
                batch.alter_column(
                    "forecast_input_hash",
                    existing_type=sa.String(64),
                    nullable=True,
                )
            if "target_id" not in columns:
                batch.create_foreign_key(
                    "fk_user_judgments_target_id_user_judgment_targets",
                    _TARGETS,
                    ["target_id"],
                    ["id"],
                )
            if "supersedes_id" not in columns:
                batch.create_foreign_key(
                    "fk_user_judgments_supersedes_id_user_judgments",
                    _JUDGMENTS,
                    ["supersedes_id"],
                    ["id"],
                )
            if "revision_number" not in columns:
                batch.create_unique_constraint(
                    "uq_user_judgment_actor_target_revision",
                    ["actor_id", "target_id", "revision_number"],
                )
                batch.create_check_constraint(
                    "ck_user_judgment_revision_binding",
                    "(target_id IS NULL AND revision_number = 1) OR "
                    "(target_id IS NOT NULL AND revision_number >= 1)",
                )

    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes(_JUDGMENTS)}
    for column in ("target_id", "supersedes_id"):
        name = f"ix_user_judgments_{column}"
        if name not in indexes:
            op.create_index(name, _JUDGMENTS, [column], unique=False)
    _install_immutable_guards(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _remove_immutable_guards(bind)
    if _TARGETS in sa.inspect(bind).get_table_names():
        bind.execute(
            sa.text(f"DELETE FROM {_JUDGMENTS} WHERE target_id IS NOT NULL")
        )
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns(_JUDGMENTS)
    }
    if "target_id" in columns:
        with op.batch_alter_table(
            _JUDGMENTS,
            recreate="always" if bind.dialect.name == "sqlite" else "auto",
            naming_convention=_SQLITE_NAMING_CONVENTION,
        ) as batch:
            batch.drop_index("ix_user_judgments_supersedes_id")
            batch.drop_index("ix_user_judgments_target_id")
            batch.drop_constraint(
                "uq_user_judgment_actor_target_revision",
                type_="unique",
            )
            batch.drop_constraint(
                "ck_user_judgment_revision_binding",
                type_="check",
            )
            batch.drop_column("target_content_hash")
            batch.drop_column("supersedes_id")
            batch.drop_column("revision_number")
            batch.drop_column("target_id")
            batch.alter_column(
                "forecast_id",
                existing_type=sa.String(36),
                nullable=False,
            )
            batch.alter_column(
                "forecast_input_hash",
                existing_type=sa.String(64),
                nullable=False,
            )
    if _TARGETS in sa.inspect(bind).get_table_names():
        op.drop_table(_TARGETS)
    _install_immutable_guards(bind, tables={_JUDGMENTS, _EVALUATIONS})


def _create_target_table() -> None:
    op.create_table(
        _TARGETS,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("mode", sa.String(24), nullable=False),
        sa.Column("index_code", sa.String(24), nullable=False),
        sa.Column("index_name", sa.String(64), nullable=False),
        sa.Column("horizon", sa.String(8), nullable=False),
        sa.Column("base_trade_date", sa.Date(), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("opens_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locks_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_input_hash", sa.String(64), nullable=False),
        sa.Column("market_universe_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "mode IN ('demo', 'live')",
            name="ck_user_judgment_target_mode",
        ),
        sa.CheckConstraint(
            "opens_at < locks_at",
            name="ck_user_judgment_target_window",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "run_id",
            "index_code",
            "horizon",
            name="uq_user_judgment_target_identity",
        ),
        sa.UniqueConstraint("content_hash"),
    )
    for column in (
        "run_id",
        "mode",
        "index_code",
        "horizon",
        "base_trade_date",
        "target_date",
        "as_of",
        "opens_at",
        "locks_at",
        "market_universe_hash",
        "content_hash",
    ):
        op.create_index(
            f"ix_user_judgment_targets_{column}",
            _TARGETS,
            [column],
            unique=False,
        )


def _install_immutable_guards(
    bind: sa.engine.Connection,
    *,
    tables: set[str] | None = None,
) -> None:
    guarded = set(tables or _GUARDED_TABLES)
    existing = set(sa.inspect(bind).get_table_names())
    guarded &= existing
    if bind.dialect.name == "sqlite":
        for table in sorted(guarded):
            for operation in ("UPDATE", "DELETE"):
                op.execute(
                    sa.text(
                        f"CREATE TRIGGER IF NOT EXISTS "
                        f"trg_{table}_reject_{operation.lower()} "
                        f"BEFORE {operation} ON {table} BEGIN "
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
                "RETURNS trigger AS $$ BEGIN "
                "RAISE EXCEPTION 'immutable User Judgment record'; "
                "END; $$ LANGUAGE plpgsql"
            )
        )
        for table in sorted(guarded):
            op.execute(
                sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
            )
            op.execute(
                sa.text(
                    f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE "
                    f"ON {table} FOR EACH ROW EXECUTE FUNCTION "
                    "vericouncil_reject_user_judgment_mutation()"
                )
            )


def _remove_immutable_guards(bind: sa.engine.Connection) -> None:
    existing = set(sa.inspect(bind).get_table_names())
    if bind.dialect.name == "sqlite":
        for table in sorted(_GUARDED_TABLES & existing):
            for operation in ("update", "delete"):
                op.execute(
                    sa.text(
                        f"DROP TRIGGER IF EXISTS trg_{table}_reject_{operation}"
                    )
                )
        return
    if bind.dialect.name == "postgresql":
        for table in sorted(_GUARDED_TABLES & existing):
            op.execute(
                sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
            )
