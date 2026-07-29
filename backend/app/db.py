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
    if not tables.issubset(inspect(connection).get_table_names()):
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
