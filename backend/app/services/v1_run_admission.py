"""Fail-closed admission guard for legacy five-index v1 run creation.

Historical v1 records remain readable and auditable.  This module only guards
boundaries that can persist a *new* v1 run after the focused v2 D1 target has
been activated.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..models import ResearchActivationEventV2
from ..research_v2 import (
    CSI1000_D1_TARGET,
    DEFAULT_RESEARCH_PROGRAM_V2,
)


class V1RunAdmissionError(RuntimeError):
    """Raised when a new legacy v1 run cannot safely be admitted."""


def assert_v1_run_creation_allowed(session: Session) -> None:
    """Reject new v1 runs when the latest v2 D1 event is ``activated``.

    ``retired`` is an explicit append-only rollback event and therefore reopens
    v1 admission.  Query failures reject admission instead of guessing that v2
    is inactive.
    """

    try:
        latest = session.scalar(
            select(ResearchActivationEventV2)
            .where(
                ResearchActivationEventV2.target_id == CSI1000_D1_TARGET,
                ResearchActivationEventV2.program_hash
                == DEFAULT_RESEARCH_PROGRAM_V2.content_hash,
            )
            .order_by(
                ResearchActivationEventV2.occurred_at.desc(),
                ResearchActivationEventV2.id.desc(),
            )
            .limit(1)
        )
    except SQLAlchemyError as exc:
        raise V1RunAdmissionError(
            "Cannot verify v2 D1 activation state; new legacy v1 runs are disabled "
            "fail-closed."
        ) from exc
    if latest is not None and latest.event_type == "activated":
        raise V1RunAdmissionError(
            "The focused v2 D1 target is activated; creation of new legacy "
            "five-index v1 runs has stopped. Historical v1 records remain readable."
        )
