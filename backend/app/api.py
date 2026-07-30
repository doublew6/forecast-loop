"""FastAPI routes for forecasts, meetings, user judgments and audit views."""

from __future__ import annotations

import time as time_module
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .adapters import LocalJsonEvidenceSnapshotSource
from .agent_contracts import AgentSpec, SignalEnvelope, agent_spec
from .auth import require_operator
from .db import get_db
from .domain import (
    AGENT_BY_ID,
    AGENTS,
    DISPLAY_AGENTS,
    Horizon,
    RunStatus,
)
from .market_universe import MarketUniverseSpec, load_market_universe
from .models import (
    EvaluationBatch,
    Forecast,
    ForecastDiagnostic,
    LessonProposal,
    ReflectionRun,
    UserJudgment,
    WorkflowRun,
    WorkflowTask,
)
from .ports import EvidenceSnapshotSourceError
from .quant_contracts import QuantInputSnapshot, QuantSignalBundle
from .schemas import (
    AgentListResponse,
    AgentRead,
    EvaluationRunRequest,
    EvaluationRunResponse,
    ForecastRead,
    HealthRead,
    LatestForecastResponse,
    LessonListResponse,
    MeetingRead,
    PredictionStatusResponse,
    ReflectionDetailRead,
    ReflectionListResponse,
    ReflectionSourceRead,
    RunCreate,
    RunListResponse,
    ScorecardRead,
    UserJudgmentCreate,
    UserJudgmentListResponse,
    UserJudgmentRead,
    UserJudgmentTargetListResponse,
    UserJudgmentTargetRead,
    WikiEntryRead,
    WikiListResponse,
    WorkflowRunRead,
)
from .serializers import (
    forecast_read,
    lesson_proposal_read,
    opinion_read,
    reflection_detail_read,
    reflection_run_read,
    run_read,
    user_judgment_read,
)
from .services.evaluation import (
    DemoForecastNotScoredError,
    ForecastAlreadyEvaluatedError,
    ForecastNotMatureError,
    PriceObservationConflictError,
    evaluate_forecast,
    evaluation_read,
)
from .services.evaluation_facade import agent_scorecard as scorecard_facade
from .services.prediction_status import (
    build_prediction_status,
    run_uses_market_universe,
)
from .services.reflection_sources import load_frozen_source_timeline
from .services.snapshot import LiveEvidenceRequiredError
from .services.task_queue import (
    TaskIdempotencyConflictError,
    default_idempotency_key,
    validate_idempotency_key,
)
from .services.user_judgment import (
    UserJudgmentClosedError,
    UserJudgmentConflictError,
    UserJudgmentNotFoundError,
    create_user_judgment,
    user_judgment_submission_status,
    verify_user_judgment,
)
from .services.user_judgment_markdown import UserJudgmentWikiError

router = APIRouter()
DBSession = Annotated[Session, Depends(get_db)]


def require_live_evaluation_mode(request: Request) -> None:
    """Reject the Live-only evaluation API before Demo can inspect a Forecast ID."""

    if request.app.state.settings.use_demo_provider:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Live forecast evaluation is unavailable in Demo mode",
            headers={"Cache-Control": "no-store"},
        )


def _request_data_mode(request: Request) -> Literal["demo", "live"]:
    return "demo" if request.app.state.settings.use_demo_provider else "live"


@router.get("/health", response_model=HealthRead)
def health(request: Request) -> HealthRead:
    settings = request.app.state.settings
    if settings.use_demo_provider:
        mode = "demo"
    elif settings.use_codex_file_provider:
        # File mode never starts a prediction through HTTP.  Each handoff CLI
        # invocation supplies and validates its own frozen evidence snapshot,
        # so the read-only API must remain available even between daily runs.
        mode = "codex-file"
    elif settings.llm_api_key and settings.evidence_snapshot_path:
        mode = "live"
    else:
        mode = "blocked-live"
    return HealthRead(
        status="ok",
        mode=mode,
        version=settings.app_version,
    )


@router.get("/market-universe", response_model=MarketUniverseSpec)
def market_universe(request: Request) -> MarketUniverseSpec:
    """Return the exact versioned target universe used by this process."""

    return request.app.state.workflow.universe


@router.get("/prediction-status", response_model=PredictionStatusResponse)
def prediction_status(request: Request, db: DBSession) -> PredictionStatusResponse:
    try:
        return build_prediction_status(
            request.app.state.settings,
            db,
            universe=_current_market_universe(request),
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail="Prediction preparation receipts failed integrity validation",
        ) from exc


@router.get("/forecasts/latest", response_model=LatestForecastResponse)
def latest_forecasts(
    request: Request,
    db: DBSession,
    index_code: str | None = None,
    horizon: Annotated[Horizon | None, Query()] = None,
) -> LatestForecastResponse:
    universe = _current_market_universe(request)
    run = _first_run_for_universe(
        db,
        select(WorkflowRun)
        .where(
            WorkflowRun.status == RunStatus.COMPLETED.value,
            WorkflowRun.mode
            == ("demo" if request.app.state.settings.use_demo_provider else "live"),
        )
        .order_by(WorkflowRun.as_of.desc(), WorkflowRun.completed_at.desc()),
        universe=universe,
    )
    if run is None:
        raise HTTPException(status_code=404, detail="No completed forecast run is available")
    statement = (
        select(Forecast)
        .options(selectinload(Forecast.evaluation))
        .where(Forecast.run_id == run.id)
        .order_by(Forecast.index_code, Forecast.horizon)
    )
    if index_code:
        statement = statement.where(Forecast.index_code == index_code)
    if horizon:
        statement = statement.where(Forecast.horizon == horizon.value)
    forecasts = db.scalars(statement).all()
    run_view = run_read(run, forecasts_count=len(forecasts))
    return LatestForecastResponse(
        run_id=run.id,
        as_of=run_view.as_of,
        data_cutoff=run_view.data_cutoff,
        forecasts=[forecast_read(row) for row in forecasts],
    )


@router.get("/forecasts/{forecast_id}", response_model=ForecastRead)
def forecast_detail(forecast_id: str, db: DBSession) -> ForecastRead:
    row = db.scalar(
        select(Forecast)
        .options(selectinload(Forecast.evaluation))
        .where(Forecast.id == forecast_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Forecast not found")
    return forecast_read(row)


@router.get("/meetings/{run_id}", response_model=MeetingRead)
def meeting_detail(run_id: str, db: DBSession) -> MeetingRead:
    run = db.scalar(
        select(WorkflowRun)
        .options(
            selectinload(WorkflowRun.opinions),
            selectinload(WorkflowRun.forecasts).selectinload(Forecast.evaluation),
            selectinload(WorkflowRun.task),
        )
        .where(WorkflowRun.id == run_id)
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    agent_order = {agent.id: position for position, agent in enumerate(AGENTS)}
    opinions = sorted(
        run.opinions,
        key=lambda item: (
            item.index_code,
            item.horizon,
            agent_order.get(item.agent_id, len(agent_order)),
        ),
    )
    forecasts = sorted(run.forecasts, key=lambda item: (item.index_code, item.horizon))
    workflow_steps = run.workflow_steps or []
    return MeetingRead(
        run=run_read(run, forecasts_count=len(forecasts), task=run.task),
        opinions=[opinion_read(row) for row in opinions],
        forecasts=[forecast_read(row) for row in forecasts],
        workflow_steps=workflow_steps,
    )


@router.get("/agents", response_model=AgentListResponse)
def agents() -> AgentListResponse:
    return AgentListResponse(
        items=[
            AgentRead(**asdict(agent), spec=agent_spec(agent.id))
            for agent in DISPLAY_AGENTS
        ]
    )


@router.get("/agents/{agent_id}/spec", response_model=AgentSpec)
def read_agent_spec(agent_id: str) -> AgentSpec:
    try:
        return agent_spec(agent_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Agent not found") from exc


@router.get("/contracts/{contract_name}/schema")
def contract_schema(contract_name: str) -> dict[str, object]:
    contracts = {
        "agent-spec": AgentSpec,
        "signal-envelope": SignalEnvelope,
        "quant-signal-bundle": QuantSignalBundle,
        "quant-input-snapshot": QuantInputSnapshot,
        "market-universe": MarketUniverseSpec,
    }
    contract = contracts.get(contract_name)
    if contract is None:
        raise HTTPException(status_code=404, detail="Contract not found")
    return contract.model_json_schema()


@router.get("/agents/{agent_id}/scorecard", response_model=ScorecardRead)
def agent_scorecard(
    agent_id: str,
    request: Request,
    db: DBSession,
    index_code: str | None = None,
    horizon: Annotated[Horizon, Query()] = Horizon.D2,
) -> ScorecardRead:
    if agent_id not in AGENT_BY_ID:
        raise HTTPException(status_code=404, detail="Agent not found")
    universe = _current_market_universe(request)
    try:
        return scorecard_facade(
            db,
            agent_id=agent_id,
            index_code=index_code,
            horizon=horizon.value,
            mode="live",
            actor_id=request.app.state.settings.user_judgment_actor_id,
            timezone=request.app.state.settings.timezone,
            market_universe_hash=universe.content_hash,
            model_name=request.app.state.workflow.model_name_for_agent(agent_id),
            forecast_model_version=request.app.state.workflow.workflow_version,
        )
    except UserJudgmentWikiError as exc:
        raise HTTPException(
            status_code=409,
            detail="User Judgment scorecard failed integrity validation",
        ) from exc


@router.get(
    "/user-judgments/targets",
    response_model=UserJudgmentTargetListResponse,
    dependencies=[Depends(require_operator)],
)
def user_judgment_targets(
    request: Request,
    response: Response,
    db: DBSession,
) -> UserJudgmentTargetListResponse:
    settings = request.app.state.settings
    mode = _request_data_mode(request)
    universe = _current_market_universe(request)
    response.headers["Cache-Control"] = "no-store"
    run = _first_run_for_universe(
        db,
        select(WorkflowRun)
        .where(
            WorkflowRun.status == RunStatus.COMPLETED.value,
            WorkflowRun.mode == mode,
        )
        .order_by(WorkflowRun.as_of.desc(), WorkflowRun.completed_at.desc()),
        universe=universe,
    )
    if run is None:
        return UserJudgmentTargetListResponse(items=[])
    forecasts = db.scalars(
        select(Forecast)
        .options(
            selectinload(Forecast.run),
            selectinload(Forecast.evaluation),
            selectinload(Forecast.user_judgments),
        )
        .where(Forecast.run_id == run.id)
        .order_by(Forecast.index_code, Forecast.horizon)
    ).all()
    zone = ZoneInfo(settings.timezone)
    now = datetime.now(zone)
    items = []
    for forecast in forecasts:
        is_open, note, deadline, existing = user_judgment_submission_status(
            forecast,
            actor_id=settings.user_judgment_actor_id,
            timezone=settings.timezone,
            market_open=settings.user_judgment_market_open,
            now=now,
        )
        items.append(
            UserJudgmentTargetRead(
                forecast_id=forecast.id,
                run_id=forecast.run_id,
                mode=forecast.run.mode,
                index_code=forecast.index_code,
                index_name=forecast.index_name,
                horizon=forecast.horizon,
                base_trade_date=forecast.base_trade_date,
                target_date=forecast.target_date,
                as_of=_aware_api_datetime(forecast.as_of, settings.timezone),
                data_cutoff=_aware_api_datetime(
                    forecast.data_cutoff,
                    settings.timezone,
                ),
                submission_deadline=deadline,
                submission_open=is_open,
                submission_note=note,
                score_eligible_if_blind=forecast.run.mode == "live" and is_open,
                existing_judgment_id=existing.id if existing else None,
            )
        )
    return UserJudgmentTargetListResponse(items=items)


@router.post(
    "/user-judgments",
    response_model=UserJudgmentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_operator)],
)
def create_user_judgment_route(
    payload: UserJudgmentCreate,
    request: Request,
    response: Response,
    db: DBSession,
) -> UserJudgmentRead:
    settings = request.app.state.settings
    mode = _request_data_mode(request)
    universe = _current_market_universe(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        row, created = create_user_judgment(
            db,
            request=payload,
            actor_id=settings.user_judgment_actor_id,
            wiki_root=settings.user_judgment_wiki_root,
            timezone=settings.timezone,
            market_open=settings.user_judgment_market_open,
            expected_mode=mode,
            market_universe_hash=universe.content_hash,
        )
    except UserJudgmentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (UserJudgmentConflictError, UserJudgmentClosedError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, UserJudgmentWikiError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not created:
        response.status_code = status.HTTP_200_OK
    return user_judgment_read(row, timezone=settings.timezone)


@router.get(
    "/user-judgments",
    response_model=UserJudgmentListResponse,
    dependencies=[Depends(require_operator)],
)
def list_user_judgments(
    request: Request,
    response: Response,
    db: DBSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    index_code: str | None = None,
    horizon: Annotated[Horizon | None, Query()] = None,
) -> UserJudgmentListResponse:
    settings = request.app.state.settings
    mode = _request_data_mode(request)
    response.headers["Cache-Control"] = "no-store"
    statement = (
        select(UserJudgment)
        .join(Forecast, Forecast.id == UserJudgment.forecast_id)
        .join(WorkflowRun, WorkflowRun.id == Forecast.run_id)
        .options(
            selectinload(UserJudgment.forecast).selectinload(Forecast.run),
            selectinload(UserJudgment.forecast).selectinload(
                Forecast.evaluation
            ),
            selectinload(UserJudgment.evaluation),
        )
        .where(
            UserJudgment.actor_id == settings.user_judgment_actor_id,
            UserJudgment.mode == mode,
            UserJudgment.run_id == WorkflowRun.id,
            WorkflowRun.mode == mode,
        )
        .order_by(UserJudgment.submitted_at.desc(), UserJudgment.id.desc())
    )
    if index_code is not None:
        statement = statement.where(UserJudgment.index_code == index_code)
    if horizon is not None:
        statement = statement.where(UserJudgment.horizon == horizon.value)
    rows = db.scalars(statement.limit(limit)).all()
    return UserJudgmentListResponse(
        items=[
            user_judgment_read(row, timezone=settings.timezone)
            for row in rows
        ]
    )


@router.get(
    "/user-judgments/{judgment_id}",
    response_model=UserJudgmentRead,
    dependencies=[Depends(require_operator)],
)
def user_judgment_detail(
    judgment_id: str,
    request: Request,
    response: Response,
    db: DBSession,
) -> UserJudgmentRead:
    settings = request.app.state.settings
    mode = _request_data_mode(request)
    response.headers["Cache-Control"] = "no-store"
    row = db.scalar(
        select(UserJudgment)
        .join(Forecast, Forecast.id == UserJudgment.forecast_id)
        .join(WorkflowRun, WorkflowRun.id == Forecast.run_id)
        .options(
            selectinload(UserJudgment.forecast).selectinload(Forecast.run),
            selectinload(UserJudgment.forecast).selectinload(
                Forecast.evaluation
            ),
            selectinload(UserJudgment.evaluation),
        )
        .where(
            UserJudgment.id == judgment_id,
            UserJudgment.actor_id == settings.user_judgment_actor_id,
            UserJudgment.mode == mode,
            UserJudgment.run_id == WorkflowRun.id,
            WorkflowRun.mode == mode,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="User Judgment not found")
    return user_judgment_read(row, timezone=settings.timezone)


@router.get(
    "/user-judgments/{judgment_id}/wiki",
    response_class=PlainTextResponse,
    dependencies=[Depends(require_operator)],
)
def user_judgment_wiki(
    judgment_id: str,
    request: Request,
    db: DBSession,
) -> PlainTextResponse:
    settings = request.app.state.settings
    mode = _request_data_mode(request)
    row = db.scalar(
        select(UserJudgment)
        .join(Forecast, Forecast.id == UserJudgment.forecast_id)
        .join(WorkflowRun, WorkflowRun.id == Forecast.run_id)
        .where(
            UserJudgment.id == judgment_id,
            UserJudgment.actor_id == settings.user_judgment_actor_id,
            UserJudgment.mode == mode,
            UserJudgment.run_id == WorkflowRun.id,
            WorkflowRun.mode == mode,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="User Judgment not found")
    try:
        markdown = verify_user_judgment(
            row,
            wiki_root=settings.user_judgment_wiki_root,
            timezone=settings.timezone,
        )
    except UserJudgmentWikiError as exc:
        raise HTTPException(
            status_code=409,
            detail="User Judgment Wiki failed integrity validation",
        ) from exc
    return PlainTextResponse(
        markdown,
        media_type="text/markdown",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'inline; filename="{row.id}.md"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/wiki", response_model=WikiListResponse)
def wiki_entries(request: Request, db: DBSession) -> WikiListResponse:
    entries = request.app.state.wiki.list_entries()
    _attach_reference_counts(entries, db)
    return WikiListResponse(items=entries)


@router.get("/wiki/{entry_id}", response_model=WikiEntryRead)
def wiki_entry(entry_id: str, request: Request, db: DBSession) -> WikiEntryRead:
    entry = request.app.state.wiki.get(entry_id, include_body=True)
    if entry is None:
        raise HTTPException(status_code=404, detail="Wiki entry not found")
    _attach_reference_counts([entry], db)
    return entry


@router.get("/runs", response_model=RunListResponse)
def list_runs(
    db: DBSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    run_status: Annotated[str | None, Query(alias="status")] = None,
) -> RunListResponse:
    statement = (
        select(WorkflowRun)
        .options(
            selectinload(WorkflowRun.forecasts),
            selectinload(WorkflowRun.task),
        )
        .order_by(WorkflowRun.as_of.desc(), WorkflowRun.started_at.desc())
    )
    if run_status:
        statement = statement.where(WorkflowRun.status == run_status)
    rows = db.scalars(statement.limit(limit)).all()
    return RunListResponse(
        items=[
            run_read(
                row,
                forecasts_count=len(row.forecasts),
                task=row.task,
            )
            for row in rows
        ]
    )


@router.get("/reflections", response_model=ReflectionListResponse)
def list_reflections(
    db: DBSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    target_date: date | None = None,
    horizon: Annotated[Horizon | None, Query()] = None,
    reflection_status: Annotated[str | None, Query(alias="status")] = None,
) -> ReflectionListResponse:
    statement = (
        select(ReflectionRun)
        .join(WorkflowRun, WorkflowRun.id == ReflectionRun.source_run_id)
        .options(
            selectinload(ReflectionRun.source_run),
            selectinload(ReflectionRun.source_batch).selectinload(
                EvaluationBatch.diagnostics
            ),
            selectinload(ReflectionRun.findings),
            selectinload(ReflectionRun.lesson_proposals),
            selectinload(ReflectionRun.lesson_proposals).selectinload(
                LessonProposal.replay_batches
            ),
            selectinload(ReflectionRun.lesson_proposals).selectinload(
                LessonProposal.lifecycle_events
            ),
        )
        .where(
            WorkflowRun.mode == "live",
            WorkflowRun.status == "completed",
        )
        .order_by(ReflectionRun.target_date.desc(), ReflectionRun.created_at.desc())
    )
    if target_date is not None:
        statement = statement.where(ReflectionRun.target_date == target_date)
    if horizon is not None:
        statement = statement.where(ReflectionRun.horizon == horizon.value)
    if reflection_status:
        statement = statement.where(ReflectionRun.status == reflection_status)
    rows = db.scalars(statement.limit(limit)).all()
    return ReflectionListResponse(items=[reflection_run_read(row) for row in rows])


@router.get("/reflections/{reflection_id}", response_model=ReflectionDetailRead)
def reflection_detail(
    reflection_id: str,
    request: Request,
    db: DBSession,
) -> ReflectionDetailRead:
    row = db.scalar(
        select(ReflectionRun)
        .join(WorkflowRun, WorkflowRun.id == ReflectionRun.source_run_id)
        .options(
            selectinload(ReflectionRun.source_run),
            selectinload(ReflectionRun.source_batch).selectinload(
                EvaluationBatch.market_snapshots
            ),
            selectinload(ReflectionRun.source_batch)
            .selectinload(EvaluationBatch.diagnostics)
            .selectinload(ForecastDiagnostic.forecast),
            selectinload(ReflectionRun.source_batch)
            .selectinload(EvaluationBatch.diagnostics)
            .selectinload(ForecastDiagnostic.evaluation),
            selectinload(ReflectionRun.findings),
            selectinload(ReflectionRun.lesson_proposals),
            selectinload(ReflectionRun.lesson_proposals).selectinload(
                LessonProposal.replay_batches
            ),
            selectinload(ReflectionRun.lesson_proposals).selectinload(
                LessonProposal.lifecycle_events
            ),
        )
        .where(
            ReflectionRun.id == reflection_id,
            WorkflowRun.mode == "live",
            WorkflowRun.status == "completed",
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Reflection not found")
    try:
        frozen_sources = load_frozen_source_timeline(
            request.app.state.settings,
            reflection_id=row.id,
            source_run_id=row.source_run_id,
            expected_hash=row.source_snapshot_hash,
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail="Frozen reflection source timeline failed integrity validation",
        ) from exc
    source_timeline = [
        ReflectionSourceRead(
            id=item.id,
            title=item.title,
            summary=item.summary,
            source_url=item.source_url,
            event_time=item.event_time,
            published_at=item.published_at,
            ingested_at=item.ingested_at,
            source_kind=item.source_kind,
            related_index_codes=item.related_index_codes,
            time_class=item.time_class,
            content_hash=item.content_hash,
        )
        for item in sorted(
            frozen_sources,
            key=lambda source: (source.published_at, source.id),
        )
    ]
    return reflection_detail_read(row, source_timeline=source_timeline)


@router.get("/lessons", response_model=LessonListResponse)
def list_lessons(
    db: DBSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    lesson_status: Annotated[str | None, Query(alias="status")] = None,
) -> LessonListResponse:
    statement = (
        select(LessonProposal)
        .join(ReflectionRun, ReflectionRun.id == LessonProposal.reflection_run_id)
        .join(WorkflowRun, WorkflowRun.id == ReflectionRun.source_run_id)
        .options(
            selectinload(LessonProposal.replay_batches),
            selectinload(LessonProposal.lifecycle_events),
        )
        .where(
            WorkflowRun.mode == "live",
            WorkflowRun.status == "completed",
        )
        .order_by(LessonProposal.created_at.desc())
    )
    if lesson_status:
        statement = statement.where(LessonProposal.status == lesson_status)
    rows = db.scalars(statement.limit(limit)).all()
    return LessonListResponse(items=[lesson_proposal_read(row) for row in rows])


@router.post(
    "/runs",
    response_model=WorkflowRunRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_operator)],
)
def create_run(
    payload: RunCreate,
    request: Request,
    response: Response,
    db: DBSession,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> WorkflowRunRead:
    as_of = _validate_live_run_request(payload, request)
    universe = _current_market_universe(request)
    task: WorkflowTask | None = None
    if not request.app.state.settings.use_demo_provider:
        assert as_of is not None
        try:
            key = (
                validate_idempotency_key(idempotency_key)
                if idempotency_key is not None
                else default_idempotency_key(
                    as_of,
                    market_universe_hash=universe.content_hash,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        replay = request.app.state.task_queue.find_by_idempotency_key(key)
        if replay is not None:
            return _idempotent_run_response(
                db,
                task=replay,
                as_of=as_of,
                timezone=request.app.state.settings.timezone,
                response=response,
                universe=universe,
            )
        existing = _first_run_for_universe(
            db,
            select(WorkflowRun)
            .where(
                WorkflowRun.mode == "live",
                WorkflowRun.as_of == as_of,
                WorkflowRun.status.in_(["awaiting_draft", "queued", "running", "completed"]),
            )
            .order_by(WorkflowRun.started_at.desc()),
            universe=universe,
        )
        if existing is not None:
            replay = _wait_for_idempotent_task(
                request.app.state.task_queue,
                key,
                attempts=5,
            )
            if replay is not None and replay.run_id == existing.id:
                return _idempotent_run_response(
                    db,
                    task=replay,
                    as_of=as_of,
                    timezone=request.app.state.settings.timezone,
                    response=response,
                    universe=universe,
                )
            raise HTTPException(
                status_code=409,
                detail=f"A live run already exists for this as_of: {existing.id}",
            )
    try:
        if request.app.state.settings.use_demo_provider:
            run = request.app.state.workflow.run(as_of=as_of)
        else:
            prepared = request.app.state.workflow.prepare_run(
                as_of=as_of,
                persist=False,
            )
            try:
                task, _created = request.app.state.task_queue.enqueue(
                    prepared,
                    idempotency_key=key,
                )
            except TaskIdempotencyConflictError as exc:
                replay = _wait_for_idempotent_task(
                    request.app.state.task_queue,
                    key,
                )
                if replay is not None:
                    return _idempotent_run_response(
                        db,
                        task=replay,
                        as_of=as_of,
                        timezone=request.app.state.settings.timezone,
                        response=response,
                        universe=universe,
                    )
                _fail_unqueued_prepared_run(
                    db,
                    run_id=prepared.row.id,
                    timezone=request.app.state.settings.timezone,
                    error=str(exc),
                )
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except Exception as exc:
                _fail_unqueued_prepared_run(
                    db,
                    run_id=prepared.row.id,
                    timezone=request.app.state.settings.timezone,
                    error=f"Persistent task enqueue failed: {exc}",
                )
                raise
            response.status_code = status.HTTP_202_ACCEPTED
            db.expire_all()
            run = db.get(WorkflowRun, prepared.row.id)
            if run is None:  # pragma: no cover - protected by the task FK
                raise HTTPException(
                    status_code=500,
                    detail="Prepared run disappeared before it was queued",
                )
    except LiveEvidenceRequiredError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        if not request.app.state.settings.use_demo_provider:
            assert as_of is not None
            replay = _wait_for_idempotent_task(
                request.app.state.task_queue,
                key,
            )
            if replay is not None:
                return _idempotent_run_response(
                    db,
                    task=replay,
                    as_of=as_of,
                    timezone=request.app.state.settings.timezone,
                    response=response,
                    universe=universe,
                )
        raise HTTPException(
            status_code=409,
            detail="A live run already exists for this as_of",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Committee run failed: {exc}") from exc
    forecasts_count = db.scalar(
        select(func.count()).select_from(Forecast).where(Forecast.run_id == run.id)
    )
    return run_read(
        run,
        forecasts_count=int(forecasts_count or 0),
        task=task,
    )


@router.post(
    "/evaluations/run",
    response_model=EvaluationRunResponse,
    dependencies=[
        Depends(require_operator),
        Depends(require_live_evaluation_mode),
    ],
)
def run_evaluations(
    payload: EvaluationRunRequest,
    request: Request,
    db: DBSession,
) -> EvaluationRunResponse:
    results = []
    for observation in payload.observations:
        forecast = db.scalar(
            select(Forecast)
            .options(selectinload(Forecast.evaluation), selectinload(Forecast.run))
            .where(Forecast.id == observation.forecast_id)
        )
        if forecast is None:
            raise HTTPException(status_code=404, detail="Forecast not found")
        try:
            result = evaluate_forecast(
                db,
                forecast=forecast,
                price_source=observation.price_source,
                observed_at=observation.observed_at,
                start_trade_date=observation.start.trade_date,
                start_close=observation.start.close,
                start_source_url=str(observation.start.source_url),
                start_source_hash=observation.start.source_hash,
                end_trade_date=observation.end.trade_date,
                end_close=observation.end.close,
                end_source_url=str(observation.end.source_url),
                end_source_hash=observation.end.source_hash,
                timezone=request.app.state.settings.timezone,
                trusted_sources_only=forecast.run.mode == "live",
            )
        except (DemoForecastNotScoredError, ForecastNotMatureError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ForecastAlreadyEvaluatedError, PriceObservationConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, LiveEvidenceRequiredError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Evaluation or price observation already exists with conflicting data",
            ) from exc
        results.append(result)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Evaluation or price observation already exists with conflicting data",
        ) from exc
    return EvaluationRunResponse(
        evaluated=len(results),
        results=[evaluation_read(result) for result in results],
    )


def _validate_live_run_request(payload: RunCreate, request: Request) -> datetime | None:
    settings = request.app.state.settings
    if settings.use_demo_provider:
        return payload.as_of
    if settings.use_codex_file_provider:
        raise HTTPException(
            status_code=409,
            detail=(
                "Codex file mode does not call a model from FastAPI; use "
                "scripts/codex_handoff.py prepare/finalize"
            ),
        )
    if not settings.llm_api_key:
        raise HTTPException(
            status_code=409,
            detail="Live mode is blocked: set LLM_API_KEY or explicitly enable Demo mode",
        )
    if settings.evidence_snapshot_path is None:
        raise HTTPException(
            status_code=409,
            detail="Live mode is blocked: set VERICOUNCIL_EVIDENCE_SNAPSHOT_PATH",
        )
    zone = ZoneInfo(settings.timezone)
    universe = load_market_universe(settings.market_universe_path)
    now = datetime.now(zone)
    close_hour, close_minute = (
        int(part) for part in universe.session_close.split(":", maxsplit=1)
    )
    close_ready = datetime.combine(
        now.date(),
        time(close_hour, close_minute),
        tzinfo=zone,
    ) + timedelta(minutes=5)
    if now.weekday() >= 5 or now < close_ready:
        raise HTTPException(
            status_code=409,
            detail="Live committee runs are issued only after the current trading-day close",
        )
    if payload.as_of is None:
        path = settings.evidence_snapshot_path
        assert path is not None
        try:
            as_of = LocalJsonEvidenceSnapshotSource(
                root=path.parent,
                snapshot_path=path.name,
                instrument_codes=universe.codes,
            ).peek_as_of()
        except EvidenceSnapshotSourceError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Cannot read live evidence snapshot as_of: {exc}",
            ) from exc
    else:
        as_of = payload.as_of
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=zone)
    else:
        as_of = as_of.astimezone(zone)
    if as_of.date() != now.date() or as_of > now:
        raise HTTPException(
            status_code=422,
            detail=(
                "Live as_of must be a non-future timestamp on today's configured "
                "market session; historical replays are excluded from live scorecards"
            ),
        )
    return as_of


def _fail_unqueued_prepared_run(
    db: Session,
    *,
    run_id: str,
    timezone: str,
    error: str,
) -> None:
    row = db.get(WorkflowRun, run_id)
    if row is None or row.status != RunStatus.QUEUED.value:
        return
    completed_at = datetime.now(ZoneInfo(timezone))
    started_at = _aware_api_datetime(row.started_at, timezone)
    row.status = RunStatus.FAILED.value
    row.completed_at = completed_at
    row.duration_seconds = max(
        0.0,
        (completed_at - started_at).total_seconds(),
    )
    row.error = error
    db.commit()


def _wait_for_idempotent_task(
    queue,
    idempotency_key: str,
    *,
    attempts: int = 25,
    delay_seconds: float = 0.01,
) -> WorkflowTask | None:
    """Briefly wait for the winner of a concurrent identical request."""

    for attempt in range(attempts):
        task = queue.find_by_idempotency_key(idempotency_key)
        if task is not None:
            return task
        if attempt + 1 < attempts:
            time_module.sleep(delay_seconds)
    return None


def _idempotent_run_response(
    db: Session,
    *,
    task: WorkflowTask,
    as_of: datetime,
    timezone: str,
    response: Response,
    universe: MarketUniverseSpec,
) -> WorkflowRunRead:
    db.expire_all()
    replay_run = db.get(WorkflowRun, task.run_id)
    if replay_run is None:
        raise HTTPException(
            status_code=409,
            detail="Idempotency record points to a missing run",
        )
    replay_as_of = _aware_api_datetime(replay_run.as_of, timezone)
    if replay_as_of != as_of:
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key is already bound to another as_of",
        )
    if not run_uses_market_universe(replay_run, universe):
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key is already bound to another Market Universe",
        )
    response.status_code = (
        status.HTTP_200_OK
        if task.status in {"completed", "failed"}
        else status.HTTP_202_ACCEPTED
    )
    forecasts_count = db.scalar(
        select(func.count())
        .select_from(Forecast)
        .where(Forecast.run_id == replay_run.id)
    )
    return run_read(
        replay_run,
        forecasts_count=int(forecasts_count or 0),
        task=task,
    )


def _current_market_universe(request: Request) -> MarketUniverseSpec:
    """Return the immutable process-local Universe, with a test-stub fallback."""

    universe = getattr(request.app.state.workflow, "universe", None)
    if isinstance(universe, MarketUniverseSpec):
        return universe
    return load_market_universe(request.app.state.settings.market_universe_path)


def _first_run_for_universe(
    db: Session,
    statement,
    *,
    universe: MarketUniverseSpec,
) -> WorkflowRun | None:
    return db.scalar(
        statement.where(
            WorkflowRun.market_universe_hash == universe.content_hash,
        ).limit(1)
    )


def _attach_reference_counts(entries: list[WikiEntryRead], db: Session) -> None:
    counts = {entry.id: 0 for entry in entries}
    rows = db.scalars(select(Forecast)).all()
    for forecast in rows:
        referenced = {
            citation.get("wiki_entry_id")
            for citation in forecast.citations
            if citation.get("wiki_entry_id") in counts
        }
        for entry_id in referenced:
            counts[entry_id] += 1
    for entry in entries:
        entry.referenced_by_count = counts[entry.id]


def _aware_api_datetime(value: datetime, timezone: str) -> datetime:
    zone = ZoneInfo(timezone)
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)
