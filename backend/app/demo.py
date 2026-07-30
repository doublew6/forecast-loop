"""Seed the local database with deterministic, clearly labelled demo meetings."""

from __future__ import annotations

from sqlalchemy import func, select

from .config import Settings
from .db import Database
from .models import Forecast, WorkflowRun
from .services.schema_readiness import require_schema_current
from .services.seed import seed_demo_data
from .services.wiki import WikiCatalog
from .workflow import CommitteeWorkflow


def main() -> None:
    settings = Settings(demo_mode=True, auto_seed=False)
    require_schema_current(settings.database_url, deep=True)
    database = Database(settings.database_url)
    workflow = CommitteeWorkflow(
        settings=settings,
        database=database,
        wiki=WikiCatalog.from_settings(settings),
    )
    try:
        seed_demo_data(workflow)
        with database.session_factory() as session:
            runs = session.scalar(select(func.count()).select_from(WorkflowRun)) or 0
            forecasts = session.scalar(select(func.count()).select_from(Forecast)) or 0
        print(f"demo database ready: {runs} runs, {forecasts} forecasts")
    finally:
        workflow.close()
        database.dispose()


if __name__ == "__main__":
    main()
