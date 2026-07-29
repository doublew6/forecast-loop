"""Small, deterministic local dataset for an immediately useful first launch."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from ..models import WorkflowRun
from ..workflow import CommitteeWorkflow


def seed_demo_data(workflow: CommitteeWorkflow, *, historical_days: int = 6) -> None:
    with workflow.database.session_factory() as session:
        count = session.scalar(select(func.count()).select_from(WorkflowRun)) or 0
    if count:
        return

    timezone = ZoneInfo(workflow.settings.timezone)
    close_hour, close_minute = (
        int(part) for part in workflow.universe.session_close.split(":", maxsplit=1)
    )
    current = datetime.now(timezone).replace(
        hour=close_hour,
        minute=close_minute,
        second=0,
        microsecond=0,
    )
    dates: list[datetime] = []
    cursor = current - timedelta(days=1)
    while len(dates) < historical_days:
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor -= timedelta(days=1)
    for as_of in reversed(dates):
        workflow.run(as_of=as_of)
    workflow.run(as_of=current)
