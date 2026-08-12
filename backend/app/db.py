"""SQLAlchemy database helpers."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from fastapi import Request
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class Database:
    """Small database container owned by a FastAPI application instance."""

    def __init__(self, url: str) -> None:
        if url.startswith("sqlite:///") and ":memory:" not in url:
            Path(url.removeprefix("sqlite:///")).expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True
            )
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine: Engine = create_engine(url, connect_args=connect_args, future=True)
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", _enable_sqlite_foreign_keys)
        self.session_factory = sessionmaker(
            bind=self.engine, class_=Session, expire_on_commit=False, autoflush=False
        )

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            _install_agent_contract_guards(connection)

    def dispose(self) -> None:
        self.engine.dispose()


def get_db(request: Request) -> Generator[Session, None, None]:
    """Yield a transaction-scoped SQLAlchemy session."""

    with request.app.state.database.session_factory() as session:
        try:
            yield session
        except Exception:
            session.rollback()
            raise


def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _install_agent_contract_guards(connection) -> None:
    tables = {"agent_specs", "signal_envelopes"}
    table_names = set(inspect(connection).get_table_names())
    if not tables.issubset(table_names):
        _install_trace_guards(connection, table_names)
        _install_v2_research_guards(connection, table_names)
        return
    if connection.dialect.name == "sqlite":
        for table in sorted(tables):
            for operation in ("UPDATE", "DELETE"):
                trigger = f"trg_{table}_reject_{operation.lower()}"
                connection.execute(
                    text(
                        f"CREATE TRIGGER IF NOT EXISTS {trigger} "
                        f"BEFORE {operation} ON {table} "
                        "BEGIN "
                        "SELECT RAISE(ABORT, 'immutable Agent contract record'); "
                        "END"
                    )
                )
        _install_trace_guards(connection, table_names)
        _install_v2_research_guards(connection, table_names)
        return
    if connection.dialect.name == "postgresql":
        connection.execute(
            text(
                "CREATE OR REPLACE FUNCTION "
                "forecast_loop_reject_agent_contract_mutation() "
                "RETURNS trigger AS $$ "
                "BEGIN "
                "RAISE EXCEPTION 'immutable Agent contract record'; "
                "END; "
                "$$ LANGUAGE plpgsql"
            )
        )
        for table in sorted(tables):
            trigger = f"trg_{table}_immutable"
            connection.execute(
                text(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
            )
            connection.execute(
                text(
                    f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} "
                    "FOR EACH ROW EXECUTE FUNCTION "
                    "forecast_loop_reject_agent_contract_mutation()"
                )
            )
        _install_trace_guards(connection, table_names)
        _install_v2_research_guards(connection, table_names)


def _install_trace_guards(connection, table_names: set[str]) -> None:
    """Install retention/seal guards for create-all test databases.

    Production databases receive equivalent guards from Alembic migration
    ``0014_trace_attempts``.  Keeping this here makes the explicit test-only
    metadata bootstrap exercise the same append-only boundary.
    """

    required = {
        "agent_traces",
        "agent_trace_spans",
        "agent_trace_artifact_links",
    }
    if connection.dialect.name != "sqlite" or not required.issubset(table_names):
        return
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
        connection.execute(text(statement))


def _install_v2_research_guards(connection, table_names: set[str]) -> None:
    """Mirror migration 0015 retention rules in test-only create-all databases."""

    append_only = {
        "agent_signals_v2",
        "forecasts_v2",
        "outcome_observations_v2",
        "signal_evaluations_v2",
        "forecast_evaluations_v2",
        "reasoning_reviews_v2",
        "reasoning_review_human_events_v2",
        "reflections_v2",
        "reflection_review_events_v2",
        "research_activation_events_v2",
    }
    if connection.dialect.name != "sqlite" or not append_only.issubset(table_names):
        return
    for table in sorted(append_only):
        for operation in ("UPDATE", "DELETE"):
            connection.execute(
                text(
                    f"CREATE TRIGGER IF NOT EXISTS trg_{table}_reject_{operation.lower()} "
                    f"BEFORE {operation} ON {table} BEGIN "
                    "SELECT RAISE(ABORT, 'immutable v2 research record'); END"
                )
            )
    connection.execute(
        text(
            "CREATE TRIGGER IF NOT EXISTS trg_research_runs_v2_reject_delete "
            "BEFORE DELETE ON research_runs_v2 BEGIN "
            "SELECT RAISE(ABORT, 'v2 research runs are retained'); END"
        )
    )
    connection.execute(
        text(
            "CREATE TRIGGER IF NOT EXISTS trg_research_runs_v2_reject_terminal_update "
            "BEFORE UPDATE ON research_runs_v2 WHEN OLD.status != 'awaiting_draft' BEGIN "
            "SELECT RAISE(ABORT, 'terminal v2 research run is immutable'); END"
        )
    )
