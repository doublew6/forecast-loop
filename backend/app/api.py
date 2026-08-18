"""FastAPI routes for forecasts, meetings, user judgments and audit views."""

from __future__ import annotations

import base64
import hashlib
import time as time_module
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import PlainTextResponse
from sqlalchemy import and_, exists, func, or_, select
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
    AgentBadCase,
    AgentEvalExperiment,
    AgentTrace,
    AgentTraceSpan,
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
from .research_v2 import DEFAULT_RESEARCH_PROGRAM_V2, ResearchProgramV2
from .schemas import (
    AgentBadCaseListResponse,
    AgentBadCaseRead,
    AgentBadCaseTransitionCreate,
    AgentEvalExperimentCreate,
    AgentEvalExperimentListResponse,
    AgentEvalExperimentRead,
    AgentEvalSuiteListResponse,
    AgentEvalV2JobListResponse,
    AgentEvalV2JobRead,
    AgentListResponse,
    AgentObservabilitySummary,
    AgentRead,
    AgentTraceListResponse,
    AgentTraceRead,
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
    V2AgentScorecardsResponse,
    V2LatestForecastsResponse,
    V2ReasoningReviewListResponse,
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
from .services.agent_evaluation import (
    AgentEvalError,
    AgentEvalStore,
    BadCaseTransition,
    EvalRunRequest,
    enqueue_experiment,
    run_next_eval_task,
    transition_bad_case,
)
from .services.agent_evaluation_v2 import (
    AgentEvalV2Error,
    latest_agent_eval_v2_ablation_values,
    list_agent_eval_v2_jobs,
)
from .services.agent_trace_projection import build_trace_view_projection
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
from .services.premarket import PremarketServiceError, load_premarket_history
from .services.reflection_sources import load_frozen_source_timeline
from .services.research_v2 import (
    ResearchV2Error,
    agent_scorecards_v2,
    latest_forecasts_v2,
    reasoning_reviews_v2,
)
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
from .services.v1_run_admission import V1RunAdmissionError, assert_v1_run_creation_allowed

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


@router.get("/v2/research-program", response_model=ResearchProgramV2)
def research_program_v2() -> ResearchProgramV2:
    return DEFAULT_RESEARCH_PROGRAM_V2


@router.get("/v2/forecasts/latest", response_model=V2LatestForecastsResponse)
def latest_v2_forecasts(db: DBSession) -> V2LatestForecastsResponse:
    return V2LatestForecastsResponse.model_validate(latest_forecasts_v2(db))


@router.get("/v2/agent-scorecards", response_model=V2AgentScorecardsResponse)
def v2_agent_scorecards(request: Request, db: DBSession) -> V2AgentScorecardsResponse:
    generated_at = datetime.now(ZoneInfo(request.app.state.settings.timezone))
    payload = agent_scorecards_v2(
        db,
        generated_at=generated_at,
        ablation_values=latest_agent_eval_v2_ablation_values(request.app.state.settings),
    )
    try:
        payload["premarket_history"] = load_premarket_history(request.app.state.settings)
    except PremarketServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return V2AgentScorecardsResponse.model_validate(payload)


@router.get("/v2/reasoning-reviews", response_model=V2ReasoningReviewListResponse)
def v2_reasoning_reviews(
    db: DBSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> V2ReasoningReviewListResponse:
    try:
        payload = reasoning_reviews_v2(db, limit=limit, cursor=cursor)
    except ResearchV2Error as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return V2ReasoningReviewListResponse.model_validate(payload)


@router.get("/agent-evals/jobs-v2", response_model=AgentEvalV2JobListResponse)
def list_agent_eval_v2_job_views(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AgentEvalV2JobListResponse:
    try:
        jobs = list_agent_eval_v2_jobs(request.app.state.settings, limit=limit)
    except AgentEvalV2Error as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return AgentEvalV2JobListResponse(
        items=[
            AgentEvalV2JobRead(
                id=job.job_id,
                suite_id=job.suite_id,
                suite_version=job.suite_version,
                suite_hash=job.suite_hash,
                baseline_target_id=job.baseline_arm_id,
                candidate_target_id=job.candidate_arm_id,
                status=job.status,
                release_decision=job.release_decision,
                policy_version=job.policy_version,
                created_at=job.prepared_at,
                started_at=job.prepared_at,
                completed_at=job.completed_at,
                report_hash=job.report_hash,
                summary={
                    "release_decision": job.release_decision,
                    "pending_arms": job.pending_arms,
                    "pending_tasks": job.pending_tasks,
                    "targets": {
                        key: value.model_dump(mode="json")
                        for key, value in job.targets.items()
                    },
                },
            )
            for job in jobs
        ]
    )


@router.get("/agent-evals/suites", response_model=AgentEvalSuiteListResponse)
def list_agent_eval_suites(request: Request) -> AgentEvalSuiteListResponse:
    return AgentEvalSuiteListResponse(
        items=[
            {**descriptor.model_dump(mode="json"), "arm_ids": descriptor.target_ids}
            for descriptor in AgentEvalStore(request.app.state.settings).list_suites()
        ]
    )


@router.post("/agent-evals/experiments", response_model=AgentEvalExperimentRead, status_code=202)
def create_agent_eval_experiment(
    payload: AgentEvalExperimentCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AgentEvalExperimentRead:
    del background_tasks
    request_value = EvalRunRequest.model_validate(payload.model_dump(mode="json"))
    payload_hash = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
    key = idempotency_key or f"agent-eval:{payload_hash}"
    try:
        validate_idempotency_key(key)
        row = enqueue_experiment(
            request.app.state.database,
            request.app.state.settings,
            request_value,
            idempotency_key=key,
        )
    except (AgentEvalError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    completed = run_next_eval_task(
        request.app.state.database,
        request.app.state.settings,
        worker_id=f"agent-eval-api-{row.id[:8]}",
    )
    return _agent_eval_experiment_read(completed or row)


@router.get("/agent-evals/experiments", response_model=AgentEvalExperimentListResponse)
def list_agent_eval_experiments(
    db: DBSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AgentEvalExperimentListResponse:
    rows = db.scalars(
        select(AgentEvalExperiment)
        .options(selectinload(AgentEvalExperiment.results))
        .order_by(AgentEvalExperiment.created_at.desc())
        .limit(limit)
    ).all()
    return AgentEvalExperimentListResponse(items=[_agent_eval_experiment_read(row) for row in rows])


@router.get(
    "/agent-evals/experiments/{experiment_id}",
    response_model=AgentEvalExperimentRead,
)
def agent_eval_experiment_detail(
    experiment_id: str,
    db: DBSession,
) -> AgentEvalExperimentRead:
    row = db.scalar(
        select(AgentEvalExperiment)
        .options(selectinload(AgentEvalExperiment.results))
        .where(AgentEvalExperiment.id == experiment_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Agent eval experiment not found")
    return _agent_eval_experiment_read(row, include_results=True)


@router.get("/agent-bad-cases", response_model=AgentBadCaseListResponse)
def list_agent_bad_cases(
    db: DBSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    bad_case_status: Annotated[str | None, Query(alias="status")] = None,
) -> AgentBadCaseListResponse:
    statement = (
        select(AgentBadCase)
        .options(selectinload(AgentBadCase.events))
        .order_by(AgentBadCase.updated_at.desc())
    )
    if bad_case_status:
        statement = statement.where(AgentBadCase.status == bad_case_status)
    rows = db.scalars(statement.limit(limit)).all()
    return AgentBadCaseListResponse(items=[AgentBadCaseRead.model_validate(row) for row in rows])


@router.post("/agent-bad-cases/{bad_case_id}/transitions", response_model=AgentBadCaseRead)
def transition_agent_bad_case(
    bad_case_id: str,
    payload: AgentBadCaseTransitionCreate,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AgentBadCaseRead:
    payload_hash = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
    key = idempotency_key or f"bad-case-transition:{payload_hash}"
    try:
        validate_idempotency_key(key)
        row = transition_bad_case(
            request.app.state.database,
            request.app.state.settings,
            bad_case_id,
            BadCaseTransition.model_validate(payload.model_dump(mode="json")),
            idempotency_key=key,
        )
    except (AgentEvalError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _reload_bad_case(request, row.id)


@router.get("/agent-traces", response_model=AgentTraceListResponse)
def list_agent_traces(
    request: Request,
    db: DBSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    workflow_kind: Annotated[str | None, Query()] = None,
    trace_status: Annotated[str | None, Query(alias="status")] = None,
    target_id: Annotated[str | None, Query()] = None,
    agent_id: Annotated[str | None, Query()] = None,
    horizon: Annotated[str | None, Query()] = None,
    started_from: Annotated[datetime | None, Query()] = None,
    started_to: Annotated[datetime | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
) -> AgentTraceListResponse:
    statement = (
        select(AgentTrace)
        .options(
            selectinload(AgentTrace.spans),
            selectinload(AgentTrace.artifact_links),
        )
        .order_by(AgentTrace.started_at.desc(), AgentTrace.id.desc())
    )
    if workflow_kind:
        statement = statement.where(AgentTrace.workflow_kind == workflow_kind)
    if trace_status:
        statement = statement.where(AgentTrace.status == trace_status)
    if target_id:
        statement = statement.where(AgentTrace.target_id == target_id)
    if horizon:
        statement = statement.where(AgentTrace.horizon == horizon)
    if agent_id:
        statement = statement.where(
            exists().where(
                AgentTraceSpan.trace_id == AgentTrace.id,
                AgentTraceSpan.agent_id == agent_id,
            )
        )
    if started_from:
        statement = statement.where(AgentTrace.started_at >= started_from)
    if started_to:
        statement = statement.where(AgentTrace.started_at <= started_to)
    if cursor:
        cursor_started_at, cursor_id = _decode_trace_cursor(cursor)
        statement = statement.where(
            or_(
                AgentTrace.started_at < cursor_started_at,
                and_(AgentTrace.started_at == cursor_started_at, AgentTrace.id < cursor_id),
            )
        )
    rows = db.scalars(statement.limit(limit + 1)).all()
    page = rows[:limit]
    return AgentTraceListResponse(
        items=[_agent_trace_read(row, request=request) for row in page],
        next_cursor=_encode_trace_cursor(page[-1]) if len(rows) > limit and page else None,
    )


@router.get("/agent-observability/summary", response_model=AgentObservabilitySummary)
def agent_observability_summary(
    request: Request,
    db: DBSession,
    hours: Annotated[int, Query(ge=1, le=24 * 90)] = 24,
) -> AgentObservabilitySummary:
    now = datetime.now(ZoneInfo(request.app.state.settings.timezone))
    cutoff = now - timedelta(hours=hours)
    rows = db.scalars(
        select(AgentTrace)
        .options(selectinload(AgentTrace.spans))
        .where(AgentTrace.started_at >= cutoff)
        .order_by(AgentTrace.started_at.desc())
    ).all()
    terminal = [row for row in rows if row.status != "running"]
    by_kind: dict[str, int] = {}
    for row in rows:
        by_kind[row.workflow_kind] = by_kind.get(row.workflow_kind, 0) + 1
    storage = request.app.state.trace_recorder.storage_metrics()
    durations = [value for row in rows if (value := _trace_duration_ms(row)) is not None]
    return AgentObservabilitySummary(
        window_hours=hours,
        total_traces=len(rows),
        running_traces=sum(row.status == "running" for row in rows),
        completed_traces=sum(row.status == "completed" for row in rows),
        failed_traces=sum(row.status == "failed" for row in rows),
        degraded_traces=sum(row.status == "degraded" or not row.telemetry_complete for row in rows),
        telemetry_complete_rate=(
            sum(row.telemetry_complete for row in rows) / len(rows) if rows else None
        ),
        completion_rate=(
            sum(row.status == "completed" for row in terminal) / len(terminal)
            if terminal
            else None
        ),
        p95_duration_ms=_p95(durations),
        by_workflow_kind=by_kind,
        recent=[_agent_trace_read(row, request=request) for row in rows[:10]],
        database_size_bytes=storage.database_size_bytes,
        trace_storage_bytes=storage.trace_storage_bytes,
        stored_span_count=storage.span_count,
        stored_artifact_link_count=storage.artifact_link_count,
        storage_warning_bytes=request.app.state.settings.agent_trace_storage_warning_bytes,
        storage_warning=storage.warning,
    )


@router.get("/agent-traces/{trace_id}", response_model=AgentTraceRead)
def agent_trace_detail(trace_id: str, request: Request, db: DBSession) -> AgentTraceRead:
    row = db.scalar(
        select(AgentTrace)
        .options(
            selectinload(AgentTrace.spans),
            selectinload(AgentTrace.artifact_links),
        )
        .where(AgentTrace.id == trace_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Agent trace not found")
    return _agent_trace_read(row, request=request, include_spans=True, session=db)


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
    run_statement = (
        select(WorkflowRun)
        .where(
            WorkflowRun.status == RunStatus.COMPLETED.value,
            WorkflowRun.mode
            == ("demo" if request.app.state.settings.use_demo_provider else "live"),
        )
        .order_by(WorkflowRun.as_of.desc(), WorkflowRun.completed_at.desc())
    )
    if horizon is not None:
        run_statement = run_statement.where(
            WorkflowRun.forecasts.any(Forecast.horizon == horizon.value)
        )
    run = _first_run_for_universe(
        db,
        run_statement,
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
    horizon: Annotated[Horizon, Query()] = Horizon.D1,
) -> ScorecardRead:
    if agent_id not in AGENT_BY_ID:
        raise HTTPException(status_code=404, detail="Agent not found")
    universe = _current_market_universe(request)
    historical_partition = (
        horizon is Horizon.D2 and agent_id != "user_judgment_agent"
    )
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
            model_name=(
                None
                if historical_partition
                else request.app.state.workflow.model_name_for_agent(agent_id)
            ),
            forecast_model_version=(
                None
                if historical_partition
                else request.app.state.workflow.workflow_version
            ),
            latest_frozen_partition=historical_partition,
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
    try:
        assert_v1_run_creation_allowed(db)
    except V1RunAdmissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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


def _agent_eval_experiment_read(
    row: AgentEvalExperiment,
    *,
    include_results: bool = False,
) -> AgentEvalExperimentRead:
    results = sorted(
        row.__dict__.get("results", []),
        key=lambda result: (result.case_id, result.arm, result.evaluator_id),
    )
    return AgentEvalExperimentRead(
        id=row.id,
        suite_id=row.suite_id,
        suite_version=row.suite_version,
        suite_hash=row.suite_hash,
        baseline_target_id=row.baseline_target_id,
        baseline_target_hash=row.baseline_target_hash,
        candidate_target_id=row.candidate_target_id,
        candidate_target_hash=row.candidate_target_hash,
        status=row.status,
        release_decision=row.release_decision,
        policy_version=row.policy_version,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        report_hash=row.report_hash,
        error=row.error,
        summary=row.summary or {},
        result_count=len(results),
        results=(
            [
                {
                    "id": result.id,
                    "arm": result.arm,
                    "case_id": result.case_id,
                    "evaluator_id": result.evaluator_id,
                    "evaluator_version": result.evaluator_version,
                    "metric_kind": result.metric_kind,
                    "score": result.score,
                    "passed": result.passed,
                    "status": result.status,
                    "label": result.label,
                    "explanation": result.explanation,
                    "output_hash": result.output_hash,
                    "trace_id": result.trace_id,
                    "created_at": result.created_at,
                }
                for result in results
            ]
            if include_results
            else []
        ),
    )


def _reload_bad_case(request: Request, bad_case_id: str) -> AgentBadCaseRead:
    with request.app.state.database.session_factory() as session:
        row = session.scalar(
            select(AgentBadCase)
            .options(selectinload(AgentBadCase.events))
            .where(AgentBadCase.id == bad_case_id)
        )
        assert row is not None
        return AgentBadCaseRead.model_validate(row)


def _agent_trace_read(
    row: AgentTrace,
    *,
    request: Request,
    include_spans: bool = False,
    session: Session | None = None,
) -> AgentTraceRead:
    loaded_spans = sorted(
        row.__dict__.get("spans", []),
        key=lambda span: (span.started_at, span.span_id),
    )
    projection = (
        build_trace_view_projection(session, row, loaded_spans)
        if include_spans and session is not None
        else None
    )
    spans = []
    if include_spans:
        for span in loaded_spans:
            payload = {
                "span_id": span.span_id,
                "parent_span_id": span.parent_span_id,
                "node_id": span.node_id,
                "name": span.name,
                "span_kind": span.span_kind,
                "status": span.status,
                "started_at": span.started_at,
                "completed_at": span.completed_at,
                "duration_ms": span.duration_ms,
                "agent_id": span.agent_id,
                "agent_version": span.agent_version,
                "model_name": span.model_name,
                "prompt_version": span.prompt_version,
                "input_tokens": span.input_tokens,
                "output_tokens": span.output_tokens,
                "total_tokens": span.total_tokens,
                "estimated_cost_usd": span.estimated_cost_usd,
                "input_digest": span.input_digest,
                "output_digest": span.output_digest,
                "tool_name": None,
                "input_summary": None,
                "output_summary": None,
                "summary": span.summary,
                "error_code": span.error_code,
                "error_summary": span.error_summary,
                "attributes": span.attributes or {},
                "references": [],
            }
            if projection is not None:
                payload.update(projection.span_overrides.get(span.span_id, {}))
            spans.append(payload)
        if projection is not None:
            spans.extend(projection.synthetic_spans)
    external_url = request.app.state.settings.agent_trace_external_url
    if external_url:
        external_url = (
            external_url.replace("{trace_id}", row.id)
            if "{trace_id}" in external_url
            else f"{external_url.rstrip('/')}/{row.id}"
        )
    return AgentTraceRead(
        id=row.id,
        workflow_kind=row.workflow_kind,
        subject_id=row.subject_id,
        attempt_number=row.attempt_number,
        target_id=row.target_id,
        horizon=row.horizon,
        mode=row.mode,
        status=row.status,
        started_at=row.started_at,
        completed_at=row.completed_at,
        duration_ms=_trace_duration_ms(row),
        input_hash=row.input_hash,
        trace_policy_version=row.trace_policy_version,
        telemetry_complete=row.telemetry_complete,
        error_code=row.error_code,
        error_summary=row.error_summary,
        attributes=row.attributes or {},
        span_count=len(spans) if include_spans else len(loaded_spans),
        spans=spans,
        artifact_links=(
            [
                {
                    "id": link.id,
                    "span_id": link.span_id,
                    "artifact_kind": link.artifact_kind,
                    "artifact_id": link.artifact_id,
                    "relation": link.relation,
                    "content_hash": link.content_hash,
                    "created_at": link.created_at,
                }
                for link in sorted(
                    row.__dict__.get("artifact_links", []),
                    key=lambda item: (item.created_at, item.id),
                )
            ]
            if include_spans
            else []
        ),
        external_url=external_url,
        audit_url=projection.audit_url if projection is not None else None,
        audit_label=projection.audit_label if projection is not None else None,
    )


def _trace_duration_ms(row: AgentTrace) -> float | None:
    if row.completed_at is None:
        return None
    started_at = row.started_at
    completed_at = row.completed_at
    if started_at.tzinfo is None and completed_at.tzinfo is not None:
        started_at = started_at.replace(tzinfo=completed_at.tzinfo)
    elif completed_at.tzinfo is None and started_at.tzinfo is not None:
        completed_at = completed_at.replace(tzinfo=started_at.tzinfo)
    return max(0.0, (completed_at - started_at).total_seconds() * 1000)


def _encode_trace_cursor(row: AgentTrace) -> str:
    raw = f"{row.started_at.isoformat()}|{row.id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_trace_cursor(value: str) -> tuple[datetime, str]:
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(value + padding).decode("utf-8")
        started_at_value, trace_id = raw.rsplit("|", 1)
        started_at = datetime.fromisoformat(started_at_value)
        if not trace_id or len(trace_id) > 32:
            raise ValueError
        return started_at, trace_id
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid Agent trace cursor") from exc


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * 0.95 + 0.999999))
    return ordered[index]
