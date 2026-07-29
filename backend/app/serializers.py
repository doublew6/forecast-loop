"""Explicit ORM-to-API mappings keep wire contracts independent of table layout."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .market_universe import DEFAULT_MARKET_UNIVERSE
from .models import (
    AgentOpinion,
    EvaluationResult,
    Forecast,
    ForecastDiagnostic,
    LessonProposal,
    MarketSessionSnapshot,
    ReflectionFinding,
    ReflectionRun,
    UserJudgment,
    WorkflowRun,
    WorkflowTask,
)
from .schemas import (
    AgentOpinionRead,
    Citation,
    EvaluationRead,
    ForecastDiagnosticRead,
    ForecastRead,
    LessonLifecycleEventRead,
    LessonProposalRead,
    MarketSessionSnapshotRead,
    Probabilities,
    ReflectionDetailRead,
    ReflectionFindingRead,
    ReflectionMetricsRead,
    ReflectionOutcomeRead,
    ReflectionRunRead,
    ReflectionSourceRead,
    StrategyContext,
    UserJudgmentEvaluationRead,
    UserJudgmentRead,
    WorkflowRunRead,
    WorkflowTaskRead,
)
from .services.lesson_lifecycle import lesson_revalidation_due_reasons


def forecast_read(
    row: Forecast,
    *,
    timezone: str = "Asia/Shanghai",
) -> ForecastRead:
    timezone = _workflow_run_timezone(row.run, fallback=timezone)
    evaluation = (
        evaluation_result_read(row.evaluation, timezone=timezone)
        if row.evaluation
        else None
    )
    return ForecastRead(
        id=row.id,
        run_id=row.run_id,
        index_code=row.index_code,
        index_name=row.index_name,
        horizon=row.horizon,
        base_trade_date=row.base_trade_date,
        target_date=row.target_date,
        as_of=_aware(row.as_of, timezone),
        data_cutoff=_aware(row.data_cutoff, timezone),
        direction=row.direction,
        probabilities=Probabilities(
            up=row.probability_up,
            neutral=row.probability_neutral,
            down=row.probability_down,
        ),
        threshold=row.threshold,
        confidence=row.confidence,
        rationale=row.rationale,
        counter_evidence=row.counter_evidence,
        invalidation_conditions=row.invalidation_conditions,
        citations=[Citation.model_validate(item) for item in row.citations],
        abstain=row.abstain,
        model_name=row.model_name,
        model_version=row.model_version,
        wiki_version=row.wiki_version,
        input_hash=row.input_hash,
        evaluation=evaluation,
    )


def opinion_read(row: AgentOpinion) -> AgentOpinionRead:
    raw_strategy_context = (row.raw_response or {}).get("strategy_context")
    return AgentOpinionRead(
        id=row.id,
        run_id=row.run_id,
        agent_id=row.agent_id,
        agent_name=row.agent_name,
        role=row.role,
        agent_version=row.agent_version,
        model_name=row.model_name,
        status=row.status,
        index_code=row.index_code,
        horizon=row.horizon,
        target_date=row.target_date,
        direction=row.direction,
        probabilities=Probabilities(
            up=row.probability_up,
            neutral=row.probability_neutral,
            down=row.probability_down,
        ),
        summary=row.summary,
        evidence=row.evidence,
        counter_evidence=row.counter_evidence,
        invalidation_conditions=row.invalidation_conditions,
        citations=[Citation.model_validate(item) for item in row.citations],
        contribution=row.contribution,
        weight=row.weight,
        strategy_context=(
            StrategyContext.model_validate(raw_strategy_context)
            if raw_strategy_context is not None
            else None
        ),
    )


def run_read(
    row: WorkflowRun,
    *,
    forecasts_count: int,
    task: WorkflowTask | None = None,
) -> WorkflowRunRead:
    timezone = _workflow_run_timezone(row)
    return WorkflowRunRead(
        id=row.id,
        as_of=_aware(row.as_of, timezone),
        data_cutoff=_aware(row.data_cutoff, timezone),
        status=row.status,
        mode=row.mode,
        started_at=_aware(row.started_at, timezone),
        completed_at=(
            _aware(row.completed_at, timezone) if row.completed_at else None
        ),
        duration_seconds=row.duration_seconds,
        error=row.error,
        data_quality=row.data_quality,
        workflow_steps=row.workflow_steps,
        input_hash=row.input_hash,
        forecasts_count=forecasts_count,
        task=(
            workflow_task_read(task, timezone=timezone)
            if task is not None
            else None
        ),
    )


def workflow_task_read(
    row: WorkflowTask,
    *,
    timezone: str = "Asia/Shanghai",
) -> WorkflowTaskRead:
    return WorkflowTaskRead(
        id=row.id,
        status=row.status,
        stage=row.stage,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        available_at=_aware(row.available_at, timezone),
        attempt_started_at=(
            _aware(row.attempt_started_at, timezone)
            if row.attempt_started_at is not None
            else None
        ),
        lease_expires_at=(
            _aware(row.lease_expires_at, timezone)
            if row.lease_expires_at is not None
            else None
        ),
        last_error=row.last_error,
        updated_at=_aware(row.updated_at, timezone),
    )


def evaluation_result_read(
    row: EvaluationResult,
    *,
    timezone: str | None = None,
) -> EvaluationRead:
    timezone = timezone or _workflow_run_timezone(row.forecast.run)
    return EvaluationRead(
        actual_return=row.actual_return,
        actual_label=row.actual_label,
        correct=row.correct,
        brier_score=row.brier_score,
        evaluated_at=_aware(row.evaluated_at, timezone),
        price_source=row.price_source,
        observed_at=_aware(row.observed_at, timezone),
        start_trade_date=row.start_trade_date,
        start_close=row.start_close,
        start_source_url=row.start_source_url,
        start_source_hash=row.start_source_hash,
        end_trade_date=row.end_trade_date,
        end_close=row.end_close,
        end_source_url=row.end_source_url,
        end_source_hash=row.end_source_hash,
        observation_hash=row.observation_hash,
    )


def user_judgment_read(
    row: UserJudgment,
    *,
    timezone: str = "Asia/Shanghai",
) -> UserJudgmentRead:
    timezone = _workflow_run_timezone(row.forecast.run, fallback=timezone)
    evaluation = row.evaluation
    return UserJudgmentRead(
        id=row.id,
        actor_id=row.actor_id,
        agent_id=row.agent_id,
        agent_version=row.agent_version,
        forecast_id=row.forecast_id,
        run_id=row.run_id,
        mode=row.mode,
        index_code=row.index_code,
        index_name=row.forecast.index_name,
        horizon=row.horizon,
        target_date=row.target_date,
        direction=row.direction,
        confidence=row.confidence,
        rationale=row.rationale,
        counter_evidence=row.counter_evidence,
        invalidation_condition=row.invalidation_condition,
        blind_attestation=row.blind_attestation,
        submitted_at=_aware(row.submitted_at, timezone),
        submission_deadline=(
            _aware(row.submission_deadline, timezone)
            if row.submission_deadline is not None
            else None
        ),
        formal_score_eligible=row.formal_score_eligible,
        run_input_hash=row.run_input_hash,
        forecast_input_hash=row.forecast_input_hash,
        policy_version=row.policy_version,
        content_hash=row.content_hash,
        wiki_path=row.wiki_path,
        wiki_artifact_hash=row.wiki_artifact_hash,
        wiki_url=f"/api/user-judgments/{row.id}/wiki",
        committee_direction=row.forecast.direction,
        committee_agreement=row.direction == row.forecast.direction,
        evaluation=(
            UserJudgmentEvaluationRead(
                actual_return=evaluation.actual_return,
                actual_label=evaluation.actual_label,
                sign_correct=evaluation.sign_correct,
                material_direction_correct=evaluation.material_direction_correct,
                observation_hash=evaluation.observation_hash,
                policy_version=evaluation.policy_version,
                evaluated_at=_aware_from_utc(evaluation.evaluated_at, timezone),
                content_hash=evaluation.content_hash,
            )
            if evaluation is not None
            else None
        ),
    )


def reflection_run_read(row: ReflectionRun) -> ReflectionRunRead:
    diagnostics = list(row.source_batch.diagnostics)
    market_summary = next(
        (
            item.summary
            for item in row.findings
            if item.scope_type == "market_event"
        ),
        None,
    )
    return ReflectionRunRead(
        id=row.id,
        source_run_id=row.source_run_id,
        source_batch_id=row.source_batch_id,
        horizon=row.horizon,
        target_date=row.target_date,
        schema_version=row.schema_version,
        evaluation_set_hash=row.evaluation_set_hash,
        status=row.status,
        supersedes_id=row.supersedes_id,
        created_at=_aware(row.created_at),
        completed_at=_aware(row.completed_at) if row.completed_at else None,
        error=row.error,
        input_hash=row.input_hash,
        source_snapshot_hash=row.source_snapshot_hash,
        output_hash=row.output_hash,
        receipt_hash=row.receipt_hash,
        prediction_cutoff=_aware(row.source_run.data_cutoff),
        reflection_cutoff=_aware(row.completed_at) if row.completed_at else None,
        data_quality=row.source_batch.data_quality,
        summary=market_summary or _reflection_status_summary(row.status),
        finding_count=len(row.findings),
        lesson_candidate_count=sum(
            item.status == "candidate" for item in row.lesson_proposals
        ),
        overall_severity=_overall_severity(diagnostics),
        metrics=_reflection_metrics(diagnostics),
    )


def reflection_detail_read(
    row: ReflectionRun,
    *,
    source_timeline: list[ReflectionSourceRead] | None = None,
) -> ReflectionDetailRead:
    summary = reflection_run_read(row)
    snapshots = {
        item.index_code: market_session_snapshot_read(item)
        for item in row.source_batch.market_snapshots
    }
    outcomes = []
    for diagnostic in sorted(
        row.source_batch.diagnostics,
        key=lambda item: item.forecast.index_code,
    ):
        forecast = diagnostic.forecast
        evaluation = diagnostic.evaluation
        outcomes.append(
            ReflectionOutcomeRead(
                forecast_id=forecast.id,
                index_code=forecast.index_code,
                index_name=forecast.index_name,
                horizon=forecast.horizon,
                target_date=forecast.target_date,
                predicted_direction=forecast.direction,
                probabilities=Probabilities(
                    up=forecast.probability_up,
                    neutral=forecast.probability_neutral,
                    down=forecast.probability_down,
                ),
                threshold=forecast.threshold,
                actual_return=evaluation.actual_return,
                actual_label=evaluation.actual_label,
                diagnostic=forecast_diagnostic_read(diagnostic),
                market_snapshot=snapshots.get(forecast.index_code),
            )
        )
    findings = [
        reflection_finding_read(item)
        for item in sorted(
            row.findings,
            key=lambda item: (item.scope_type, item.index_code or "", item.subject_id),
        )
    ]
    decision_order = {
        "macro_policy_agent": 0,
        "market_news_agent": 1,
        "ai_storage_industry_agent": 2,
        "strategy_agent": 3,
        "risk_critic_agent": 4,
        "cio_agent": 5,
        "committee": 6,
    }
    decision_chain = sorted(
        [
            item
            for item in findings
            if item.scope_type in {"agent", "committee"}
        ],
        key=lambda item: (
            item.index_code or "",
            decision_order.get(item.subject_id, len(decision_order)),
        ),
    )
    return ReflectionDetailRead(
        **summary.model_dump(),
        outcomes=outcomes,
        diagnostics=[
            forecast_diagnostic_read(item)
            for item in sorted(
                row.source_batch.diagnostics,
                key=lambda item: item.forecast.index_code,
            )
        ],
        findings=findings,
        decision_chain=decision_chain,
        source_timeline=source_timeline or [],
        lesson_proposals=[
            lesson_proposal_read(item)
            for item in sorted(row.lesson_proposals, key=lambda item: item.created_at)
        ],
    )


def market_session_snapshot_read(row: MarketSessionSnapshot) -> MarketSessionSnapshotRead:
    return MarketSessionSnapshotRead(
        id=row.id,
        batch_id=row.batch_id,
        index_code=row.index_code,
        index_name=row.index_name,
        target_date=row.target_date,
        base_trade_date=row.base_trade_date,
        base_close=row.base_close,
        target_close=row.target_close,
        actual_return=row.actual_return,
        amount=row.amount,
        advancers=row.advancers,
        decliners=row.decliners,
        unchanged=row.unchanged,
        limit_down_count=row.limit_down_count,
        breadth_down_ratio=row.breadth_down_ratio,
        sector_contributions=row.sector_contributions,
        weight_contributions=row.weight_contributions,
        historical_abs_return_percentile=row.historical_abs_return_percentile,
        history_sample_size=row.history_sample_size,
        source_url=row.source_url,
        source_hash=row.source_hash,
        captured_at=_aware(row.captured_at),
        content_hash=row.content_hash,
    )


def forecast_diagnostic_read(row: ForecastDiagnostic) -> ForecastDiagnosticRead:
    return ForecastDiagnosticRead(
        id=row.id,
        forecast_id=row.forecast_id,
        evaluation_result_id=row.evaluation_result_id,
        index_code=row.forecast.index_code,
        index_name=row.forecast.index_name,
        horizon=row.forecast.horizon,
        target_date=row.forecast.target_date,
        predicted_direction=row.forecast.direction,
        actual_return=row.evaluation.actual_return,
        actual_label=row.evaluation.actual_label,
        threshold=row.forecast.threshold,
        signed_sigma=row.signed_sigma,
        severity=row.severity,
        systemic_extreme_down=row.systemic_extreme_down,
        historical_abs_return_percentile=row.historical_abs_return_percentile,
        history_sample_size=row.history_sample_size,
        data_incomplete=row.data_incomplete,
        sign_correct=row.sign_correct,
        material_direction_correct=row.material_direction_correct,
        brier_score=row.brier_score,
        policy_version=row.policy_version,
        created_at=_aware(row.created_at),
    )


def reflection_finding_read(row: ReflectionFinding) -> ReflectionFindingRead:
    counterfactual = row.counterfactual or {}
    raw_metadata = counterfactual.get("reflection_metadata", {})
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}

    def metadata_list(name: str) -> list[str]:
        value = metadata.get(name, [])
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str)]

    direction_correct = {
        "right_reason": True,
        "lucky_correct": True,
        "wrong": False,
    }.get(row.verdict)
    return ReflectionFindingRead(
        id=row.id,
        reflection_run_id=row.reflection_run_id,
        scope_type=row.scope_type,
        subject_id=row.subject_id,
        index_code=row.index_code,
        horizon=row.horizon,
        direction_correct=direction_correct,
        verdict=row.verdict,
        primary_error_type=row.primary_error_type,
        secondary_error_types=row.secondary_error_types,
        evidence_ids=row.evidence_ids,
        what_was_right=metadata_list("what_was_right"),
        what_was_wrong=metadata_list("what_was_wrong"),
        original_evidence_item_ids=metadata_list("original_evidence_item_ids"),
        missed_evidence_item_ids=metadata_list("missed_evidence_item_ids"),
        source_ids=metadata_list("source_ids"),
        invalidation_conditions_triggered=metadata_list(
            "invalidation_conditions_triggered"
        ),
        availability_class=row.availability_class,
        causal_status=row.causal_status,
        counterfactual=counterfactual,
        remediation=row.remediation,
        confidence=row.confidence,
        summary=row.summary,
        created_at=_aware(row.created_at),
    )


def lesson_proposal_read(row: LessonProposal) -> LessonProposalRead:
    replay_batches = sorted(
        row.replay_batches,
        key=lambda item: (item.created_at, item.id),
    )
    lifecycle_events = sorted(
        row.lifecycle_events,
        key=lambda item: item.sequence_number,
    )
    replay_metrics = row.replay_metrics or {}
    due_reasons = (
        lesson_revalidation_due_reasons(row, as_of=datetime.now(UTC))
        if row.status in {"active", "challenged"}
        else []
    )
    return LessonProposalRead(
        id=row.id,
        reflection_run_id=row.reflection_run_id,
        episode_key=row.episode_key,
        cluster_key=row.cluster_key,
        title=row.title,
        summary=row.summary,
        status=row.status,
        proposal_type=row.proposal_type,
        evidence_finding_ids=row.evidence_finding_ids,
        independent_episode_count=row.independent_episode_count,
        replay_target_dates=row.replay_target_dates,
        replay_metrics=replay_metrics,
        half_life_sessions=row.half_life_sessions,
        created_at=_aware(row.created_at),
        reviewed_at=_aware(row.reviewed_at) if row.reviewed_at else None,
        supersedes_id=row.supersedes_id,
        superseded_by_id=replay_metrics.get("superseded_by_id"),
        replay_batch_count=len(replay_batches),
        latest_replay_hash=(
            replay_batches[-1].content_hash if replay_batches else None
        ),
        revalidation_due=bool(due_reasons),
        revalidation_due_reasons=due_reasons,
        lifecycle_history=[
            LessonLifecycleEventRead(
                id=item.id,
                sequence_number=item.sequence_number,
                event_type=item.event_type,
                from_status=item.from_status,
                to_status=item.to_status,
                actor=item.actor,
                reason=item.reason,
                payload_hash=item.payload_hash,
                occurred_at=_aware(item.occurred_at),
            )
            for item in lifecycle_events
        ],
    )


def _reflection_metrics(
    diagnostics: list[ForecastDiagnostic],
) -> ReflectionMetricsRead:
    sign_values = [item.sign_correct for item in diagnostics if item.sign_correct is not None]
    material_values = [
        item.material_direction_correct
        for item in diagnostics
        if item.material_direction_correct is not None
    ]
    brier = [item.brier_score for item in diagnostics]
    sign_correct = sum(bool(value) for value in sign_values)
    material_correct = sum(bool(value) for value in material_values)
    return ReflectionMetricsRead(
        outcome_count=len(diagnostics),
        sign_sample_size=len(sign_values),
        sign_correct=sign_correct,
        sign_accuracy=sign_correct / len(sign_values) if sign_values else None,
        material_sample_size=len(material_values),
        material_correct=material_correct,
        material_direction_accuracy=(
            material_correct / len(material_values) if material_values else None
        ),
        average_brier=sum(brier) / len(brier) if brier else None,
    )


def _overall_severity(diagnostics: list[ForecastDiagnostic]) -> str:
    if any(item.systemic_extreme_down for item in diagnostics):
        return "systemic_extreme_down"
    priority = {"noise": 0, "directional": 1, "large": 2, "extreme": 3}
    if not diagnostics:
        return "unknown"
    return max(diagnostics, key=lambda item: priority[item.severity]).severity


def _reflection_status_summary(status: str) -> str:
    return {
        "awaiting_sources": "等待冻结事后来源。",
        "awaiting_analysis": "来源已冻结，等待 Codex 反省草稿。",
        "completed": "反省已完成，原因与经验候选见详情。",
        "failed": "反省失败，未形成可用经验。",
        "blocked_upstream": "可信上游数据未就绪，反省已阻断。",
    }.get(status, "反省状态未知。")


def _aware(value: datetime, timezone: str = "Asia/Shanghai") -> datetime:
    zone = ZoneInfo(timezone)
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def _aware_from_utc(
    value: datetime,
    timezone: str = "Asia/Shanghai",
) -> datetime:
    instant = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return instant.astimezone(ZoneInfo(timezone))


def _workflow_run_timezone(
    row: WorkflowRun,
    *,
    fallback: str = "Asia/Shanghai",
) -> str:
    del fallback  # Rows without a seal predate configurable universes.
    quality = row.data_quality if isinstance(row.data_quality, dict) else {}
    raw_universe = quality.get("market_universe")
    if raw_universe is None:
        identity_hash = getattr(row, "market_universe_hash", None)
        if identity_hash not in {
            None,
            DEFAULT_MARKET_UNIVERSE.content_hash,
        }:
            raise ValueError("workflow run market-universe timezone is missing")
        return DEFAULT_MARKET_UNIVERSE.timezone
    if not isinstance(raw_universe, dict):
        raise ValueError("workflow run market-universe timezone is invalid")
    if raw_universe.get("content_hash") != row.market_universe_hash:
        raise ValueError("workflow run market-universe timezone seal is invalid")
    timezone = raw_universe.get("timezone")
    if not isinstance(timezone, str) or not timezone:
        raise ValueError("workflow run market-universe timezone is invalid")
    try:
        ZoneInfo(timezone)
    except (KeyError, ValueError) as exc:
        raise ValueError("workflow run market-universe timezone is invalid") from exc
    return timezone
