"""Alembic migration helpers and deterministic database readiness checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url

from ..config import REPOSITORY_ROOT

ALEMBIC_CONFIG_PATH = REPOSITORY_ROOT / "backend" / "alembic.ini"
CORE_TABLES = frozenset(
    {
        "agent_specs",
        "forecasts",
        "signal_envelopes",
        "user_judgments",
        "workflow_runs",
        "workflow_tasks",
    }
)


class SchemaNotReadyError(RuntimeError):
    """Raised when a runtime is pointed at a missing or stale schema."""


@dataclass(frozen=True)
class SchemaStatus:
    """Serializable result of a migration/readiness inspection."""

    database_exists: bool
    current_heads: tuple[str, ...]
    expected_heads: tuple[str, ...]
    missing_core_tables: tuple[str, ...]
    integrity_errors: tuple[str, ...] = ()
    foreign_key_violations: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return (
            self.database_exists
            and bool(self.current_heads)
            and self.current_heads == self.expected_heads
            and not self.missing_core_tables
            and not self.integrity_errors
            and not self.foreign_key_violations
        )

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "ready": self.ready}


def migration_config(
    database_url: str,
    *,
    config_path: Path = ALEMBIC_CONFIG_PATH,
) -> Config:
    """Build an Alembic config without mutating process-wide environment state."""

    configuration = Config(str(config_path))
    configuration.attributes["forecast_loop_database_url"] = database_url
    return configuration


def upgrade_database(
    database_url: str,
    revision: str = "head",
    *,
    config_path: Path = ALEMBIC_CONFIG_PATH,
) -> SchemaStatus:
    """Upgrade a database explicitly and return its resulting status."""

    _prepare_sqlite_parent(database_url)
    command.upgrade(
        migration_config(database_url, config_path=config_path),
        revision,
    )
    return inspect_schema(
        database_url,
        deep=revision == "head",
        config_path=config_path,
    )


def downgrade_database(
    database_url: str,
    revision: str,
    *,
    config_path: Path = ALEMBIC_CONFIG_PATH,
) -> SchemaStatus:
    """Downgrade an isolated database, primarily for migration smoke tests."""

    command.downgrade(
        migration_config(database_url, config_path=config_path),
        revision,
    )
    return inspect_schema(database_url, config_path=config_path)


def inspect_schema(
    database: str | Engine,
    *,
    deep: bool = False,
    config_path: Path = ALEMBIC_CONFIG_PATH,
) -> SchemaStatus:
    """Inspect Alembic heads, required tables, and optional SQLite invariants."""

    expected_heads = _expected_heads(config_path)
    owns_engine = isinstance(database, str)
    if isinstance(database, str):
        if not _database_exists(database):
            return SchemaStatus(
                database_exists=False,
                current_heads=(),
                expected_heads=expected_heads,
                missing_core_tables=tuple(sorted(CORE_TABLES)),
            )
        engine = create_engine(database, future=True)
    else:
        engine = database

    try:
        with engine.connect() as connection:
            table_names = set(inspect(connection).get_table_names())
            current_heads = (
                tuple(
                    sorted(
                        MigrationContext.configure(connection).get_current_heads()
                    )
                )
                if "alembic_version" in table_names
                else ()
            )
            integrity_errors: tuple[str, ...] = ()
            foreign_key_violations: tuple[str, ...] = ()
            if deep and connection.dialect.name == "sqlite":
                integrity_rows = tuple(
                    str(row[0])
                    for row in connection.execute(
                        text("PRAGMA integrity_check")
                    ).all()
                )
                if integrity_rows != ("ok",):
                    integrity_errors = integrity_rows
                foreign_key_violations = tuple(
                    "|".join(str(value) for value in row)
                    for row in connection.execute(
                        text("PRAGMA foreign_key_check")
                    ).all()
                )
            return SchemaStatus(
                database_exists=True,
                current_heads=current_heads,
                expected_heads=expected_heads,
                missing_core_tables=tuple(sorted(CORE_TABLES - table_names)),
                integrity_errors=integrity_errors,
                foreign_key_violations=foreign_key_violations,
            )
    finally:
        if owns_engine:
            engine.dispose()


def require_schema_current(
    database: str | Engine,
    *,
    deep: bool = False,
    config_path: Path = ALEMBIC_CONFIG_PATH,
) -> SchemaStatus:
    """Fail closed unless the database is at every current Alembic head."""

    status = inspect_schema(
        database,
        deep=deep,
        config_path=config_path,
    )
    if status.ready:
        return status

    problems: list[str] = []
    if not status.database_exists:
        problems.append("database file is missing")
    if not status.current_heads:
        problems.append("alembic_version is missing")
    elif status.current_heads != status.expected_heads:
        problems.append(
            "migration heads are "
            f"{list(status.current_heads)}, expected {list(status.expected_heads)}"
        )
    if status.missing_core_tables:
        problems.append(
            "core tables are missing: "
            + ", ".join(status.missing_core_tables)
        )
    if status.integrity_errors:
        problems.append(
            "SQLite integrity_check failed: "
            + "; ".join(status.integrity_errors)
        )
    if status.foreign_key_violations:
        problems.append(
            "SQLite foreign_key_check failed: "
            + "; ".join(status.foreign_key_violations)
        )
    detail = "; ".join(problems) or "unknown schema error"
    raise SchemaNotReadyError(
        f"database schema is not ready: {detail}. "
        "Run `forecast-loop database migrate` (or `make migrate`) explicitly."
    )


def _expected_heads(config_path: Path) -> tuple[str, ...]:
    script = ScriptDirectory.from_config(Config(str(config_path)))
    return tuple(sorted(script.get_heads()))


def _database_exists(database_url: str) -> bool:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return True
    if not url.database or url.database == ":memory:":
        return True
    return Path(url.database).expanduser().is_file()


def _prepare_sqlite_parent(database_url: str) -> None:
    url = make_url(database_url)
    if (
        url.get_backend_name() != "sqlite"
        or not url.database
        or url.database == ":memory:"
    ):
        return
    Path(url.database).expanduser().absolute().parent.mkdir(
        parents=True,
        exist_ok=True,
    )
