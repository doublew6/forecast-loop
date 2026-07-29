"""Bind new User Judgments to their creation-time AgentSpec.

Revision ID: 0010_judgment_agent_spec_binding
Revises: 0009_persistent_workflow_tasks
Create Date: 2026-07-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_judgment_agent_spec_binding"
down_revision = "0009_persistent_workflow_tasks"
branch_labels = None
depends_on = None

_TABLE = "user_judgments"
_COLUMN = "agent_spec_hash"
_INDEX = "ix_user_judgments_agent_spec_hash"
_FOREIGN_KEY = "fk_user_judgments_agent_spec_hash_agent_specs"
_LEGACY_SPEC = {
    "agent_id": "user_judgment_agent",
    "agent_version": "0.1.0",
    "capabilities": {
        "direction": True,
        "evidence_mode": "none",
        "probability_mode": "confidence",
        "reasoning_mode": "structured",
        "supports_blind_submission": True,
        "supports_input_binding": True,
    },
    "content_hash": (
        "b8268786bb1db8a9eebd55da4710ac9961114f0e865b4dc11dcf360c1084b40c"
    ),
    "name": "用户判断 Agent",
    "participation": {
        "evaluation_metrics": ["direction", "reasoning"],
        "influence": "none",
        "mode": "shadow",
        "policy_id": "manual-shadow",
        "policy_version": "1.0.0",
        "schema_version": "forecast-loop.participation-policy/v1",
    },
    "role": "在查看委员会结论前独立选择涨跌，并封存理由、反证与失效条件。",
    "schema_version": "forecast-loop.agent-spec/v1",
    "source_type": "manual",
    "workflow_role": "shadow",
}
_SQLITE_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}


def upgrade() -> None:
    bind = op.get_bind()
    _seed_legacy_agent_spec(bind)
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    has_column = _COLUMN in columns
    has_binding = _has_agent_spec_binding(inspector)

    if bind.dialect.name == "sqlite" and (
        not has_column or not has_binding
    ):
        with op.batch_alter_table(
            _TABLE,
            recreate="always",
            naming_convention=_SQLITE_NAMING_CONVENTION,
        ) as batch:
            if not has_column:
                batch.add_column(
                    sa.Column(
                        _COLUMN,
                        sa.String(length=64),
                        nullable=True,
                    )
                )
            if not has_binding:
                batch.create_foreign_key(
                    _FOREIGN_KEY,
                    "agent_specs",
                    [_COLUMN],
                    ["content_hash"],
                )
        _install_sqlite_immutable_guards()
        inspector = sa.inspect(bind)
    elif not has_column:
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.String(length=64), nullable=True),
        )
        inspector = sa.inspect(bind)

    indexes = {index["name"] for index in inspector.get_indexes(_TABLE)}
    if _INDEX not in indexes:
        op.create_index(_INDEX, _TABLE, [_COLUMN], unique=False)

    if bind.dialect.name != "sqlite":
        if not _has_agent_spec_binding(sa.inspect(bind)):
            op.create_foreign_key(
                _FOREIGN_KEY,
                _TABLE,
                "agent_specs",
                [_COLUMN],
                ["content_hash"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes(_TABLE)}
    if _INDEX in indexes:
        op.drop_index(_INDEX, table_name=_TABLE)
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if _COLUMN in columns:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table(
                _TABLE,
                recreate="always",
                naming_convention=_SQLITE_NAMING_CONVENTION,
            ) as batch:
                batch.drop_column(_COLUMN)
            _install_sqlite_immutable_guards()
        else:
            binding = next(
                (
                    foreign_key
                    for foreign_key in inspector.get_foreign_keys(_TABLE)
                    if foreign_key.get("constrained_columns") == [_COLUMN]
                    and foreign_key.get("referred_table") == "agent_specs"
                    and foreign_key.get("referred_columns") == ["content_hash"]
                ),
                None,
            )
            if binding is not None and binding.get("name"):
                op.drop_constraint(
                    binding["name"],
                    _TABLE,
                    type_="foreignkey",
                )
            op.drop_column(_TABLE, _COLUMN)


def _has_agent_spec_binding(inspector: sa.Inspector) -> bool:
    return any(
        foreign_key.get("constrained_columns") == [_COLUMN]
        and foreign_key.get("referred_table") == "agent_specs"
        and foreign_key.get("referred_columns") == ["content_hash"]
        for foreign_key in inspector.get_foreign_keys(_TABLE)
    )


def _seed_legacy_agent_spec(bind: sa.engine.Connection) -> None:
    table = sa.table(
        "agent_specs",
        sa.column("content_hash", sa.String),
        sa.column("schema_version", sa.String),
        sa.column("agent_id", sa.String),
        sa.column("agent_version", sa.String),
        sa.column("source_type", sa.String),
        sa.column("participation_policy_id", sa.String),
        sa.column("participation_policy_version", sa.String),
        sa.column("participation_mode", sa.String),
        sa.column("spec", sa.JSON),
    )
    content_hash = _LEGACY_SPEC["content_hash"]
    existing = bind.execute(
        sa.select(table).where(table.c.content_hash == content_hash)
    ).mappings().one_or_none()
    expected = {
        "content_hash": content_hash,
        "schema_version": _LEGACY_SPEC["schema_version"],
        "agent_id": _LEGACY_SPEC["agent_id"],
        "agent_version": _LEGACY_SPEC["agent_version"],
        "source_type": _LEGACY_SPEC["source_type"],
        "participation_policy_id": _LEGACY_SPEC["participation"]["policy_id"],
        "participation_policy_version": (
            _LEGACY_SPEC["participation"]["policy_version"]
        ),
        "participation_mode": _LEGACY_SPEC["participation"]["mode"],
        "spec": _LEGACY_SPEC,
    }
    if existing is None:
        bind.execute(sa.insert(table).values(**expected))
        return
    if dict(existing) != expected:
        raise RuntimeError(
            "stored legacy User Judgment AgentSpec conflicts with "
            "the migration snapshot"
        )


def _install_sqlite_immutable_guards() -> None:
    for operation in ("UPDATE", "DELETE"):
        trigger = f"trg_{_TABLE}_reject_{operation.lower()}"
        op.execute(
            sa.text(
                f"CREATE TRIGGER IF NOT EXISTS {trigger} "
                f"BEFORE {operation} ON {_TABLE} "
                "BEGIN "
                "SELECT RAISE(ABORT, "
                "'immutable User Judgment record'); "
                "END"
            )
        )
