"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from alembic import command
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from . import models  # noqa: F401 - register SQLAlchemy metadata
from .api import router
from .config import Settings, get_settings
from .db import Database
from .domain import RunStatus
from .models import WorkflowRun
from .services.schema_readiness import migration_config, require_schema_current
from .services.seed import seed_demo_data
from .services.task_queue import PersistentTaskQueue
from .services.wiki import WikiCatalog
from .workflow import CommitteeWorkflow


def create_app(
    settings: Settings | None = None,
    *,
    allow_schema_bootstrap: bool = False,
) -> FastAPI:
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        database = Database(resolved.database_url)
        workflow: CommitteeWorkflow | None = None
        try:
            if allow_schema_bootstrap:
                # Explicitly test-only compatibility. Runtime processes must
                # reach the schema through Alembic before they start. Tests
                # create the current metadata directly, then stamp that exact
                # shape so downstream state-changing helpers exercise the same
                # readiness boundary as production.
                database.create_all()
                command.stamp(migration_config(resolved.database_url), "heads")
            else:
                require_schema_current(database.engine)
            _fail_interrupted_runs(database, timezone=resolved.timezone)
            wiki = WikiCatalog.from_settings(resolved)
            workflow = CommitteeWorkflow(
                settings=resolved,
                database=database,
                wiki=wiki,
            )
            task_queue = PersistentTaskQueue(
                database,
                timezone=resolved.timezone,
                max_attempts=resolved.task_max_attempts,
                lease_seconds=resolved.task_lease_seconds,
                timeout_seconds=resolved.task_timeout_seconds,
                retry_delay_seconds=resolved.task_retry_delay_seconds,
            )
            application.state.settings = resolved
            application.state.database = database
            application.state.wiki = wiki
            application.state.workflow = workflow
            application.state.task_queue = task_queue
            if resolved.auto_seed and resolved.use_demo_provider:
                seed_demo_data(workflow)
            yield
        finally:
            if workflow is not None:
                workflow.close()
            database.dispose()

    application = FastAPI(
        title=resolved.app_name,
        version=resolved.app_version,
        description="forecast-loop：可验证的多来源预测 Agent API",
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
        ],
    )
    application.include_router(router, prefix=resolved.api_prefix)
    return application


def _fail_interrupted_runs(database: Database, *, timezone: str) -> int:
    """Fail only legacy queued/running rows that have no durable task.

    Queue-backed rows are owned and recovered by the worker. A pre-queue row has
    no frozen task payload to resume, so it remains safer to fail closed.
    """

    now = datetime.now(ZoneInfo(timezone))
    with database.session_factory() as session:
        rows = session.scalars(
            select(WorkflowRun).where(
                WorkflowRun.status.in_(
                    [RunStatus.QUEUED.value, RunStatus.RUNNING.value]
                ),
                ~WorkflowRun.task.has(),
            )
        ).all()
        for row in rows:
            started_at = row.started_at
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=ZoneInfo(timezone))
            row.status = RunStatus.FAILED.value
            row.completed_at = now
            row.duration_seconds = max(0.0, (now - started_at).total_seconds())
            row.error = (
                "Interrupted legacy run has no persistent task payload and "
                "cannot be resumed. Create a new run."
            )
        if rows:
            session.commit()
        return len(rows)


app = create_app()
