"""Persistent entities for immutable forecasts and their evaluations."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .market_universe import DEFAULT_MARKET_UNIVERSE


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index(
            "uq_active_live_run_as_of",
            "market_universe_hash",
            "as_of",
            unique=True,
            sqlite_where=text(
                "mode = 'live' AND status IN ('awaiting_draft', 'queued', 'running', 'completed')"
            ),
            postgresql_where=text(
                "mode = 'live' AND status IN ('awaiting_draft', 'queued', 'running', 'completed')"
            ),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    data_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), index=True)
    mode: Mapped[str] = mapped_column(String(24), default="demo")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_quality: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    workflow_steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    market_universe_hash: Mapped[str] = mapped_column(
        String(64),
        default=DEFAULT_MARKET_UNIVERSE.content_hash,
        server_default=DEFAULT_MARKET_UNIVERSE.content_hash,
        nullable=False,
        index=True,
    )

    opinions: Mapped[list[AgentOpinion]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    forecasts: Mapped[list[Forecast]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    user_judgment_targets: Mapped[list[UserJudgmentTarget]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    task: Mapped[WorkflowTask | None] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        uselist=False,
    )


class WorkflowTask(Base):
    """Durable queue record for one frozen committee run."""

    __tablename__ = "workflow_tasks"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_workflow_task_run"),
        UniqueConstraint("idempotency_key", name="uq_workflow_task_idempotency"),
        CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'completed', 'failed')",
            name="ck_workflow_task_status",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1 AND attempt_count <= max_attempts",
            name="ck_workflow_task_attempts",
        ),
        CheckConstraint(
            "(status = 'running' AND lease_owner IS NOT NULL "
            "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND attempt_started_at IS NOT NULL) OR "
            "(status != 'running' AND lease_owner IS NULL "
            "AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_workflow_task_lease",
        ),
        Index(
            "ix_workflow_tasks_claim",
            "status",
            "available_at",
            "created_at",
        ),
        Index(
            "ix_workflow_tasks_expired_lease",
            "status",
            "lease_expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(48), default="committee_run")
    status: Mapped[str] = mapped_column(String(24), index=True)
    stage: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    attempt_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    version: Mapped[int] = mapped_column(Integer, default=0)

    run: Mapped[WorkflowRun] = relationship(back_populates="task")


class AgentOpinion(Base):
    __tablename__ = "agent_opinions"
    __table_args__ = (
        UniqueConstraint("run_id", "agent_id", "index_code", "horizon", name="uq_opinion_identity"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    agent_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(Text)
    agent_version: Mapped[str] = mapped_column(String(32), default="0.1.0")
    model_name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(24), default="completed")
    index_code: Mapped[str] = mapped_column(String(24), index=True)
    horizon: Mapped[str] = mapped_column(String(8), index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    direction: Mapped[str] = mapped_column(String(16))
    probability_up: Mapped[float] = mapped_column(Float)
    probability_neutral: Mapped[float] = mapped_column(Float)
    probability_down: Mapped[float] = mapped_column(Float)
    summary: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    counter_evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    invalidation_conditions: Mapped[list[str]] = mapped_column(JSON, default=list)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    contribution: Mapped[str] = mapped_column(Text, default="")
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    raw_response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    run: Mapped[WorkflowRun] = relationship(back_populates="opinions")


class AgentSpecRecord(Base):
    """Append-only, content-addressed snapshot for historical signal verification."""

    __tablename__ = "agent_specs"

    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64))
    agent_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_version: Mapped[str] = mapped_column(String(32), index=True)
    source_type: Mapped[str] = mapped_column(String(24), index=True)
    participation_policy_id: Mapped[str] = mapped_column(String(120))
    participation_policy_version: Mapped[str] = mapped_column(String(32))
    participation_mode: Mapped[str] = mapped_column(String(24), index=True)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)


class SignalEnvelopeRecord(Base):
    """Append-only projection of a validated v1 SignalEnvelope.

    Historical AgentOpinion, Forecast and UserJudgment rows are intentionally
    not backfilled because their original accepted-at and provenance fields
    cannot be reconstructed without inventing audit facts.
    """

    __tablename__ = "signal_envelopes"
    __table_args__ = (
        Index(
            "ux_signal_envelopes_content_hash",
            "content_hash",
            unique=True,
        ),
        UniqueConstraint(
            "source_record_type",
            "source_record_id",
            name="uq_signal_envelope_source_record",
        ),
        CheckConstraint(
            "mode IN ('demo', 'live')",
            name="ck_signal_envelope_mode",
        ),
        CheckConstraint(
            "source_type IN ('ai', 'manual', 'quant', 'deterministic')",
            name="ck_signal_envelope_source_type",
        ),
        CheckConstraint(
            "participation_mode IN ('formal', 'shadow', 'disabled')",
            name="ck_signal_envelope_participation_mode",
        ),
        CheckConstraint(
            "routing_lane IN "
            "('formal_input', 'formal_advisory', 'formal_decision', "
            "'shadow_benchmark')",
            name="ck_signal_envelope_routing_lane",
        ),
        CheckConstraint(
            "(formal_aggregation AND NOT shadow_benchmark) OR "
            "(NOT formal_aggregation AND shadow_benchmark)",
            name="ck_signal_envelope_route_flags",
        ),
        CheckConstraint(
            "(source_record_type IS NULL AND source_record_id IS NULL) OR "
            "(source_record_type IS NOT NULL AND source_record_id IS NOT NULL)",
            name="ck_signal_envelope_source_record_pair",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64))
    agent_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_version: Mapped[str] = mapped_column(String(32))
    agent_spec_hash: Mapped[str] = mapped_column(
        ForeignKey("agent_specs.content_hash"),
        index=True,
    )
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflow_runs.id"),
        index=True,
    )
    mode: Mapped[str] = mapped_column(String(24), index=True)
    source_type: Mapped[str] = mapped_column(String(24), index=True)
    index_code: Mapped[str] = mapped_column(String(32), index=True)
    horizon: Mapped[str] = mapped_column(String(16), index=True)
    base_trade_date: Mapped[date] = mapped_column(Date)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    participation_policy_id: Mapped[str] = mapped_column(String(120))
    participation_policy_version: Mapped[str] = mapped_column(String(32))
    participation_mode: Mapped[str] = mapped_column(String(24), index=True)
    routing_lane: Mapped[str] = mapped_column(String(32), index=True)
    formal_aggregation: Mapped[bool] = mapped_column(Boolean)
    shadow_benchmark: Mapped[bool] = mapped_column(Boolean)
    source_record_type: Mapped[str | None] = mapped_column(String(48), nullable=True)
    source_record_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    envelope: Mapped[dict[str, Any]] = mapped_column(JSON)

    spec_record: Mapped[AgentSpecRecord] = relationship(lazy="joined")


class Forecast(Base):
    __tablename__ = "forecasts"
    __table_args__ = (
        UniqueConstraint("run_id", "index_code", "horizon", name="uq_forecast_identity"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    index_code: Mapped[str] = mapped_column(String(24), index=True)
    index_name: Mapped[str] = mapped_column(String(64))
    horizon: Mapped[str] = mapped_column(String(8), index=True)
    base_trade_date: Mapped[date] = mapped_column(Date, index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    data_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    direction: Mapped[str] = mapped_column(String(16), index=True)
    probability_up: Mapped[float] = mapped_column(Float)
    probability_neutral: Mapped[float] = mapped_column(Float)
    probability_down: Mapped[float] = mapped_column(Float)
    threshold: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    rationale: Mapped[str] = mapped_column(Text)
    counter_evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    invalidation_conditions: Mapped[list[str]] = mapped_column(JSON, default=list)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    abstain: Mapped[bool] = mapped_column(Boolean, default=False)
    model_name: Mapped[str] = mapped_column(String(120))
    model_version: Mapped[str] = mapped_column(String(32), default="0.1.0")
    wiki_version: Mapped[str] = mapped_column(String(64), default="snapshot")
    input_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    run: Mapped[WorkflowRun] = relationship(back_populates="forecasts")
    evaluation: Mapped[EvaluationResult | None] = relationship(
        back_populates="forecast", cascade="all, delete-orphan", uselist=False
    )
    user_judgments: Mapped[list[UserJudgment]] = relationship(
        back_populates="forecast",
    )


class UserJudgmentTarget(Base):
    """Immutable blind target published as soon as a run is prepared."""

    __tablename__ = "user_judgment_targets"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "index_code",
            "horizon",
            name="uq_user_judgment_target_identity",
        ),
        UniqueConstraint("content_hash"),
        CheckConstraint(
            "mode IN ('demo', 'live')",
            name="ck_user_judgment_target_mode",
        ),
        CheckConstraint(
            "opens_at < locks_at",
            name="ck_user_judgment_target_window",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        index=True,
    )
    mode: Mapped[str] = mapped_column(String(24), index=True)
    index_code: Mapped[str] = mapped_column(String(24), index=True)
    index_name: Mapped[str] = mapped_column(String(64))
    horizon: Mapped[str] = mapped_column(String(8), index=True)
    base_trade_date: Mapped[date] = mapped_column(Date, index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    data_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    opens_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    locks_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    run_input_hash: Mapped[str] = mapped_column(String(64))
    market_universe_hash: Mapped[str] = mapped_column(String(64), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    run: Mapped[WorkflowRun] = relationship(back_populates="user_judgment_targets")
    judgments: Mapped[list[UserJudgment]] = relationship(back_populates="target")


class EvaluationResult(Base):
    __tablename__ = "evaluation_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    forecast_id: Mapped[str] = mapped_column(ForeignKey("forecasts.id"), unique=True, index=True)
    actual_return: Mapped[float] = mapped_column(Float)
    actual_label: Mapped[str] = mapped_column(String(16))
    correct: Mapped[bool] = mapped_column(Boolean)
    brier_score: Mapped[float] = mapped_column(Float)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    price_source: Mapped[str] = mapped_column(String(120))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    start_trade_date: Mapped[date] = mapped_column(Date)
    start_close: Mapped[float] = mapped_column(Float)
    start_source_url: Mapped[str] = mapped_column(Text)
    start_source_hash: Mapped[str] = mapped_column(String(64))
    end_trade_date: Mapped[date] = mapped_column(Date)
    end_close: Mapped[float] = mapped_column(Float)
    end_source_url: Mapped[str] = mapped_column(Text)
    end_source_hash: Mapped[str] = mapped_column(String(64))
    observation_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    forecast: Mapped[Forecast] = relationship(back_populates="evaluation")


class ResearchRunV2(Base):
    """One focused v2 research run without reinterpreting a legacy workflow run."""

    __tablename__ = "research_runs_v2"
    __table_args__ = (
        UniqueConstraint("input_hash", name="uq_research_run_v2_input_hash"),
        CheckConstraint(
            "status IN ('awaiting_draft', 'completed', 'failed')",
            name="ck_research_run_v2_status",
        ),
        CheckConstraint("mode IN ('demo', 'live')", name="ck_research_run_v2_mode"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64))
    program_hash: Mapped[str] = mapped_column(String(64), index=True)
    snapshot_hash: Mapped[str] = mapped_column(String(64), index=True)
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    mode: Mapped[str] = mapped_column(String(24), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    anchor_date: Mapped[date] = mapped_column(Date, index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    data_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    prepared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    program: Mapped[dict[str, Any]] = mapped_column(JSON)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    receipt: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    signals: Mapped[list[AgentSignalV2Record]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )
    forecasts: Mapped[list[ForecastV2]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )


class AgentSignalV2Record(Base):
    """Append-only v2 Agent signal with explicit natural and decision horizons."""

    __tablename__ = "agent_signals_v2"
    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_agent_signal_v2_content_hash"),
        UniqueConstraint(
            "run_id",
            "agent_id",
            "target_id",
            "signal_kind",
            name="uq_agent_signal_v2_run_identity",
        ),
        CheckConstraint(
            "signal_kind IN ('natural_view', 'd1_impact', 'strategy_forecast', "
            "'risk_critique', 'decision_forecast')",
            name="ck_agent_signal_v2_kind",
        ),
        CheckConstraint(
            "natural_horizon IN ('D1', 'W1', 'D20')",
            name="ck_agent_signal_v2_natural_horizon",
        ),
        CheckConstraint(
            "decision_horizon IS NULL OR decision_horizon IN ('D1', 'W1', 'D20')",
            name="ck_agent_signal_v2_decision_horizon",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("research_runs_v2.id", ondelete="CASCADE"), index=True
    )
    schema_version: Mapped[str] = mapped_column(String(64))
    agent_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_version: Mapped[str] = mapped_column(String(32), index=True)
    model_name: Mapped[str] = mapped_column(String(160), index=True)
    prompt_version: Mapped[str] = mapped_column(String(80))
    target_id: Mapped[str] = mapped_column(String(120), index=True)
    signal_kind: Mapped[str] = mapped_column(String(32), index=True)
    natural_horizon: Mapped[str] = mapped_column(String(8), index=True)
    decision_horizon: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    anchor_date: Mapped[date] = mapped_column(Date, index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    evidence_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    program_hash: Mapped[str] = mapped_column(String(64), index=True)
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_probabilities: Mapped[dict[str, float] | None] = mapped_column(JSON, nullable=True)
    state_available: Mapped[bool] = mapped_column(Boolean, default=True)
    abstain: Mapped[bool] = mapped_column(Boolean, default=False)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    envelope: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    run: Mapped[ResearchRunV2] = relationship(back_populates="signals")
    evaluations: Mapped[list[SignalEvaluationV2]] = relationship(
        back_populates="signal",
        cascade="all, delete-orphan",
    )
    reasoning_reviews: Mapped[list[ReasoningReviewV2]] = relationship(
        back_populates="signal",
        cascade="all, delete-orphan",
    )


class ForecastV2(Base):
    """Append-only v2 decision forecast for one explicit target."""

    __tablename__ = "forecasts_v2"
    __table_args__ = (
        UniqueConstraint("run_id", "target_id", name="uq_forecast_v2_run_target"),
        UniqueConstraint("source_signal_id", name="uq_forecast_v2_source_signal"),
        UniqueConstraint("content_hash", name="uq_forecast_v2_content_hash"),
        CheckConstraint("horizon IN ('D1', 'W1')", name="ck_forecast_v2_horizon"),
        CheckConstraint(
            "configured_lane IN ('formal', 'shadow')",
            name="ck_forecast_v2_configured_lane",
        ),
        CheckConstraint(
            "effective_lane IN ('formal', 'shadow')",
            name="ck_forecast_v2_effective_lane",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("research_runs_v2.id", ondelete="CASCADE"), index=True
    )
    source_signal_id: Mapped[str] = mapped_column(ForeignKey("agent_signals_v2.id"), index=True)
    schema_version: Mapped[str] = mapped_column(String(64))
    program_hash: Mapped[str] = mapped_column(String(64), index=True)
    target_id: Mapped[str] = mapped_column(String(120), index=True)
    horizon: Mapped[str] = mapped_column(String(8), index=True)
    configured_lane: Mapped[str] = mapped_column(String(16), index=True)
    effective_lane: Mapped[str] = mapped_column(String(16), index=True)
    anchor_date: Mapped[date] = mapped_column(Date, index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    probability_up: Mapped[float] = mapped_column(Float)
    probability_neutral: Mapped[float] = mapped_column(Float)
    probability_down: Mapped[float] = mapped_column(Float)
    threshold: Mapped[float] = mapped_column(Float)
    baseline_probabilities: Mapped[dict[str, float]] = mapped_column(JSON)
    rationale: Mapped[str] = mapped_column(Text)
    counter_evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    invalidation_conditions: Mapped[list[str]] = mapped_column(JSON, default=list)
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    run: Mapped[ResearchRunV2] = relationship(back_populates="forecasts")
    source_signal: Mapped[AgentSignalV2Record] = relationship()
    evaluation: Mapped[ForecastEvaluationV2 | None] = relationship(
        back_populates="forecast",
        cascade="all, delete-orphan",
        uselist=False,
    )
    reflection: Mapped[ReflectionV2 | None] = relationship(
        back_populates="forecast",
        cascade="all, delete-orphan",
        uselist=False,
    )


class OutcomeObservationV2Record(Base):
    """Immutable, source-bound v2 market outcome shared by signal evaluations."""

    __tablename__ = "outcome_observations_v2"
    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_outcome_observation_v2_hash"),
        UniqueConstraint(
            "program_hash",
            "target_id",
            "anchor_date",
            "target_date",
            "mode",
            name="uq_outcome_observation_v2_episode",
        ),
        CheckConstraint("mode IN ('demo', 'live')", name="ck_outcome_observation_v2_mode"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64))
    program_hash: Mapped[str] = mapped_column(String(64), index=True)
    mode: Mapped[str] = mapped_column(String(24), index=True)
    target_id: Mapped[str] = mapped_column(String(120), index=True)
    anchor_date: Mapped[date] = mapped_column(Date, index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    actual_value: Mapped[float] = mapped_column(Float)
    actual_label: Mapped[str] = mapped_column(String(16), index=True)
    threshold: Mapped[float] = mapped_column(Float)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    observation: Mapped[dict[str, Any]] = mapped_column(JSON)


class SignalEvaluationV2(Base):
    """Immutable outcome score for one probabilistic v2 signal."""

    __tablename__ = "signal_evaluations_v2"
    __table_args__ = (
        UniqueConstraint("signal_id", name="uq_signal_evaluation_v2_signal"),
        UniqueConstraint("content_hash", name="uq_signal_evaluation_v2_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    signal_id: Mapped[str] = mapped_column(
        ForeignKey("agent_signals_v2.id", ondelete="CASCADE"), index=True
    )
    observation_id: Mapped[str] = mapped_column(
        ForeignKey("outcome_observations_v2.id"), index=True
    )
    actual_label: Mapped[str] = mapped_column(String(16), index=True)
    brier_score: Mapped[float] = mapped_column(Float)
    baseline_brier_score: Mapped[float] = mapped_column(Float)
    direction_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    evaluator_version: Mapped[str] = mapped_column(String(32))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)

    signal: Mapped[AgentSignalV2Record] = relationship(back_populates="evaluations")
    observation: Mapped[OutcomeObservationV2Record] = relationship()


class ForecastEvaluationV2(Base):
    """Immutable projection of the source decision signal evaluation."""

    __tablename__ = "forecast_evaluations_v2"
    __table_args__ = (
        UniqueConstraint("forecast_id", name="uq_forecast_evaluation_v2_forecast"),
        UniqueConstraint(
            "signal_evaluation_id",
            name="uq_forecast_evaluation_v2_signal_evaluation",
        ),
        UniqueConstraint("content_hash", name="uq_forecast_evaluation_v2_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    forecast_id: Mapped[str] = mapped_column(
        ForeignKey("forecasts_v2.id", ondelete="CASCADE"), index=True
    )
    signal_evaluation_id: Mapped[str] = mapped_column(
        ForeignKey("signal_evaluations_v2.id"), index=True
    )
    actual_value: Mapped[float] = mapped_column(Float)
    actual_label: Mapped[str] = mapped_column(String(16), index=True)
    brier_score: Mapped[float] = mapped_column(Float)
    baseline_brier_score: Mapped[float] = mapped_column(Float)
    direction_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)

    forecast: Mapped[ForecastV2] = relationship(back_populates="evaluation")
    signal_evaluation: Mapped[SignalEvaluationV2] = relationship()


class ReasoningReviewV2(Base):
    """Append-only pre-outcome review; human decisions live in separate events."""

    __tablename__ = "reasoning_reviews_v2"
    __table_args__ = (
        UniqueConstraint("signal_id", name="uq_reasoning_review_v2_signal"),
        UniqueConstraint("review_input_hash", name="uq_reasoning_review_v2_input"),
        UniqueConstraint("content_hash", name="uq_reasoning_review_v2_hash"),
        CheckConstraint(
            "human_review_status IN ('not_required', 'pending', 'approved', 'rejected')",
            name="ck_reasoning_review_v2_human_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    signal_id: Mapped[str] = mapped_column(
        ForeignKey("agent_signals_v2.id", ondelete="CASCADE"), index=True
    )
    schema_version: Mapped[str] = mapped_column(String(64))
    review_input_hash: Mapped[str] = mapped_column(String(64), index=True)
    deterministic_checks: Mapped[dict[str, bool]] = mapped_column(JSON)
    rubric: Mapped[dict[str, Any]] = mapped_column(JSON)
    total_score: Mapped[int] = mapped_column(Integer)
    human_review_required: Mapped[bool] = mapped_column(Boolean, index=True)
    human_review_status: Mapped[str] = mapped_column(String(24), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)

    signal: Mapped[AgentSignalV2Record] = relationship(back_populates="reasoning_reviews")
    human_events: Mapped[list[ReasoningReviewHumanEventV2]] = relationship(
        back_populates="review",
        cascade="all, delete-orphan",
    )


class ReasoningReviewHumanEventV2(Base):
    """Append-only human decision; consumers derive effective status from it."""

    __tablename__ = "reasoning_review_human_events_v2"
    __table_args__ = (
        UniqueConstraint("review_id", name="uq_reasoning_review_human_event_v2_review"),
        UniqueConstraint("content_hash", name="uq_reasoning_review_human_event_v2_hash"),
        CheckConstraint("decision IN ('approved', 'rejected')", name="ck_reasoning_human_decision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_id: Mapped[str] = mapped_column(
        ForeignKey("reasoning_reviews_v2.id", ondelete="CASCADE"), index=True
    )
    decision: Mapped[str] = mapped_column(String(16), index=True)
    reviewer: Mapped[str] = mapped_column(String(120))
    notes: Mapped[str] = mapped_column(Text, default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)

    review: Mapped[ReasoningReviewV2] = relationship(back_populates="human_events")


class ReflectionV2(Base):
    """Target-scoped v2 reflection; no fixed five-index matrix is implied."""

    __tablename__ = "reflections_v2"
    __table_args__ = (
        UniqueConstraint("forecast_id", name="uq_reflection_v2_forecast"),
        UniqueConstraint("evaluation_id", name="uq_reflection_v2_evaluation"),
        UniqueConstraint("content_hash", name="uq_reflection_v2_hash"),
        CheckConstraint(
            "status IN ('completed', 'failed')",
            name="ck_reflection_v2_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    forecast_id: Mapped[str] = mapped_column(
        ForeignKey("forecasts_v2.id", ondelete="CASCADE"), index=True
    )
    forecast_hash: Mapped[str] = mapped_column(String(64), index=True)
    evaluation_id: Mapped[str] = mapped_column(ForeignKey("forecast_evaluations_v2.id"), index=True)
    evaluation_hash: Mapped[str] = mapped_column(String(64), index=True)
    schema_version: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str] = mapped_column(String(120), index=True)
    anchor_date: Mapped[date] = mapped_column(Date, index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    actual_label: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    verdict: Mapped[str] = mapped_column(String(64))
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    envelope: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)

    forecast: Mapped[ForecastV2] = relationship(back_populates="reflection")
    review_events: Mapped[list[ReflectionReviewEventV2]] = relationship(
        back_populates="reflection",
        cascade="all, delete-orphan",
    )


class ReflectionReviewEventV2(Base):
    """Append-only approval used by the v2 activation gate."""

    __tablename__ = "reflection_review_events_v2"
    __table_args__ = (
        UniqueConstraint("reflection_id", name="uq_reflection_review_event_v2_reflection"),
        UniqueConstraint("content_hash", name="uq_reflection_review_event_v2_hash"),
        CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="ck_reflection_review_v2_decision",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    reflection_id: Mapped[str] = mapped_column(
        ForeignKey("reflections_v2.id", ondelete="CASCADE"), index=True
    )
    decision: Mapped[str] = mapped_column(String(16), index=True)
    reviewer: Mapped[str] = mapped_column(String(120))
    notes: Mapped[str] = mapped_column(Text, default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)

    reflection: Mapped[ReflectionV2] = relationship(back_populates="review_events")


class ResearchActivationEventV2(Base):
    """Append-only activation event; absence means every v2 target remains shadow."""

    __tablename__ = "research_activation_events_v2"
    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_research_activation_v2_hash"),
        CheckConstraint("event_type IN ('activated', 'retired')", name="ck_activation_v2_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64))
    program_hash: Mapped[str] = mapped_column(String(64), index=True)
    target_id: Mapped[str] = mapped_column(String(120), index=True)
    event_type: Mapped[str] = mapped_column(String(16), index=True)
    policy_version: Mapped[str] = mapped_column(String(32))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(120))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    previous_event_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)


class UserJudgment(Base):
    """One immutable revision of a human judgment."""

    __tablename__ = "user_judgments"
    __table_args__ = (
        UniqueConstraint(
            "actor_id",
            "forecast_id",
            name="uq_user_judgment_actor_forecast",
        ),
        UniqueConstraint(
            "actor_id",
            "target_id",
            "revision_number",
            name="uq_user_judgment_actor_target_revision",
        ),
        UniqueConstraint("content_hash", name="uq_user_judgments_content_hash"),
        CheckConstraint(
            "(target_id IS NULL AND revision_number = 1) OR "
            "(target_id IS NOT NULL AND revision_number >= 1)",
            name="ck_user_judgment_revision_binding",
        ),
        CheckConstraint(
            "direction IN ('up', 'down')",
            name="ck_user_judgment_direction",
        ),
        CheckConstraint(
            "confidence >= 0.5 AND confidence <= 1.0",
            name="ck_user_judgment_confidence",
        ),
        CheckConstraint(
            "mode IN ('demo', 'live')",
            name="ck_user_judgment_mode",
        ),
        CheckConstraint(
            "NOT formal_score_eligible OR "
            "(mode = 'live' AND blind_attestation "
            "AND submission_deadline IS NOT NULL "
            "AND submitted_at < submission_deadline)",
            name="ck_user_judgment_formal_eligibility",
        ),
        CheckConstraint(
            "length(trim(rationale)) >= 20",
            name="ck_user_judgment_rationale",
        ),
        CheckConstraint(
            "length(trim(counter_evidence)) >= 10",
            name="ck_user_judgment_counter_evidence",
        ),
        CheckConstraint(
            "length(trim(invalidation_condition)) >= 10",
            name="ck_user_judgment_invalidation",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(120), index=True)
    agent_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_version: Mapped[str] = mapped_column(String(32))
    agent_spec_hash: Mapped[str | None] = mapped_column(
        ForeignKey("agent_specs.content_hash"),
        nullable=True,
        index=True,
    )
    forecast_id: Mapped[str | None] = mapped_column(
        ForeignKey("forecasts.id"), nullable=True, index=True
    )
    target_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_judgment_targets.id"), nullable=True, index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_judgments.id"), nullable=True, index=True
    )
    target_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    mode: Mapped[str] = mapped_column(String(24), index=True)
    index_code: Mapped[str] = mapped_column(String(24), index=True)
    horizon: Mapped[str] = mapped_column(String(8), index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    direction: Mapped[str] = mapped_column(String(16), index=True)
    confidence: Mapped[float] = mapped_column(Float)
    rationale: Mapped[str] = mapped_column(Text)
    counter_evidence: Mapped[str] = mapped_column(Text)
    invalidation_condition: Mapped[str] = mapped_column(Text)
    blind_attestation: Mapped[bool] = mapped_column(Boolean)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    submission_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    formal_score_eligible: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    run_input_hash: Mapped[str] = mapped_column(String(64))
    forecast_input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_version: Mapped[str] = mapped_column(String(32))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    wiki_path: Mapped[str] = mapped_column(Text)
    wiki_artifact_hash: Mapped[str] = mapped_column(String(64))

    forecast: Mapped[Forecast | None] = relationship(back_populates="user_judgments")
    target: Mapped[UserJudgmentTarget | None] = relationship(
        back_populates="judgments",
        foreign_keys=[target_id],
    )
    supersedes: Mapped[UserJudgment | None] = relationship(
        remote_side=[id],
        foreign_keys=[supersedes_id],
    )
    agent_spec_record: Mapped[AgentSpecRecord | None] = relationship(lazy="joined")
    evaluation: Mapped[UserJudgmentEvaluation | None] = relationship(
        back_populates="judgment",
        uselist=False,
    )


class UserJudgmentEvaluation(Base):
    """Immutable score derived only from a trusted forecast evaluation batch."""

    __tablename__ = "user_judgment_evaluations"
    __table_args__ = (
        UniqueConstraint("user_judgment_id"),
        UniqueConstraint("content_hash"),
        CheckConstraint(
            "actual_label IN ('up', 'neutral', 'down')",
            name="ck_user_judgment_evaluation_label",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_judgment_id: Mapped[str] = mapped_column(
        ForeignKey("user_judgments.id"),
        index=True,
    )
    batch_id: Mapped[str] = mapped_column(ForeignKey("evaluation_batches.id"), index=True)
    evaluation_result_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_results.id"),
        index=True,
    )
    actual_return: Mapped[float] = mapped_column(Float)
    actual_label: Mapped[str] = mapped_column(String(16))
    sign_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    material_direction_correct: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
    )
    observation_hash: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(32))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)

    judgment: Mapped[UserJudgment] = relationship(back_populates="evaluation")
    batch: Mapped[EvaluationBatch] = relationship()
    evaluation_result: Mapped[EvaluationResult] = relationship()


class PriceObservation(Base):
    __tablename__ = "price_observations"
    __table_args__ = (
        UniqueConstraint("mode", "index_code", "trade_date", name="uq_price_mode_index_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(24), index=True)
    index_code: Mapped[str] = mapped_column(String(24), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    close: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(120), default="demo")
    source_url: Mapped[str] = mapped_column(Text)
    source_hash: Mapped[str] = mapped_column(String(64))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvaluationBatch(Base):
    """One immutable evaluation attempt for a target session and horizon."""

    __tablename__ = "evaluation_batches"
    __table_args__ = (
        UniqueConstraint(
            "target_date",
            "horizon",
            "evaluation_set_hash",
            "status",
            name="uq_evaluation_batch_identity",
        ),
        CheckConstraint(
            "status IN ('completed', 'failed', 'blocked_upstream')",
            name="ck_evaluation_batch_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    horizon: Mapped[str] = mapped_column(String(8), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    evaluation_set_hash: Mapped[str] = mapped_column(String(64), index=True)
    source_hash: Mapped[str] = mapped_column(String(64))
    data_quality: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    market_snapshots: Mapped[list[MarketSessionSnapshot]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )
    diagnostics: Mapped[list[ForecastDiagnostic]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )
    opinion_evaluations: Mapped[list[OpinionEvaluation]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class MarketSessionSnapshot(Base):
    """Source-bound close and market breadth facts copied from a read-only owner."""

    __tablename__ = "market_session_snapshots"
    __table_args__ = (
        UniqueConstraint("batch_id", "index_code", name="uq_market_snapshot_batch_index"),
        UniqueConstraint("content_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("evaluation_batches.id"), index=True)
    index_code: Mapped[str] = mapped_column(String(24), index=True)
    index_name: Mapped[str] = mapped_column(String(64))
    target_date: Mapped[date] = mapped_column(Date, index=True)
    base_trade_date: Mapped[date] = mapped_column(Date)
    base_close: Mapped[float] = mapped_column(Float)
    target_close: Mapped[float] = mapped_column(Float)
    actual_return: Mapped[float] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    advancers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decliners: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unchanged: Mapped[int | None] = mapped_column(Integer, nullable=True)
    limit_down_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    breadth_down_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    sector_contributions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    weight_contributions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    historical_abs_return_percentile: Mapped[float | None] = mapped_column(Float, nullable=True)
    history_sample_size: Mapped[int] = mapped_column(Integer, default=0)
    source_url: Mapped[str] = mapped_column(Text)
    source_hash: Mapped[str] = mapped_column(String(64))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)

    batch: Mapped[EvaluationBatch] = relationship(back_populates="market_snapshots")


class OpinionEvaluation(Base):
    """Persisted per-opinion score; critics and placeholders are explicitly excluded."""

    __tablename__ = "opinion_evaluations"
    __table_args__ = (
        UniqueConstraint(
            "batch_id",
            "agent_opinion_id",
            name="uq_opinion_evaluation_identity",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("evaluation_batches.id"), index=True)
    agent_opinion_id: Mapped[str] = mapped_column(ForeignKey("agent_opinions.id"), index=True)
    evaluation_result_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_results.id"), index=True
    )
    sign_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    material_direction_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    brier_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    included_in_direction_score: Mapped[bool] = mapped_column(Boolean, default=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    batch: Mapped[EvaluationBatch] = relationship(back_populates="opinion_evaluations")
    opinion: Mapped[AgentOpinion] = relationship()
    evaluation: Mapped[EvaluationResult] = relationship()


class ForecastDiagnostic(Base):
    """Versioned deterministic labels used by reflection, independent of model prose."""

    __tablename__ = "forecast_diagnostics"
    __table_args__ = (
        UniqueConstraint("batch_id", "forecast_id", name="uq_forecast_diagnostic_identity"),
        CheckConstraint(
            "severity IN ('noise', 'directional', 'large', 'extreme')",
            name="ck_forecast_diagnostic_severity",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("evaluation_batches.id"), index=True)
    forecast_id: Mapped[str] = mapped_column(ForeignKey("forecasts.id"), index=True)
    evaluation_result_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_results.id"), index=True
    )
    signed_sigma: Mapped[float] = mapped_column(Float)
    severity: Mapped[str] = mapped_column(String(24), index=True)
    systemic_extreme_down: Mapped[bool] = mapped_column(Boolean, default=False)
    historical_abs_return_percentile: Mapped[float | None] = mapped_column(Float, nullable=True)
    history_sample_size: Mapped[int] = mapped_column(Integer, default=0)
    data_incomplete: Mapped[bool] = mapped_column(Boolean, default=False)
    sign_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    material_direction_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    brier_score: Mapped[float] = mapped_column(Float)
    policy_version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    batch: Mapped[EvaluationBatch] = relationship(back_populates="diagnostics")
    forecast: Mapped[Forecast] = relationship()
    evaluation: Mapped[EvaluationResult] = relationship()


class ReflectionRun(Base):
    """Append-only Codex reflection over one source run, horizon and outcome set."""

    __tablename__ = "reflection_runs"
    __table_args__ = (
        Index(
            "uq_reflection_active_successor",
            "supersedes_id",
            unique=True,
            sqlite_where=text(
                "supersedes_id IS NOT NULL AND status IN "
                "('awaiting_sources', 'awaiting_analysis', 'completed')"
            ),
            postgresql_where=text(
                "supersedes_id IS NOT NULL AND status IN "
                "('awaiting_sources', 'awaiting_analysis', 'completed')"
            ),
        ),
        UniqueConstraint(
            "source_run_id",
            "horizon",
            "target_date",
            "schema_version",
            "evaluation_set_hash",
            name="uq_reflection_run_identity",
        ),
        CheckConstraint(
            "status IN "
            "('awaiting_sources', 'awaiting_analysis', 'completed', "
            "'failed', 'blocked_upstream')",
            name="ck_reflection_run_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    source_batch_id: Mapped[str] = mapped_column(ForeignKey("evaluation_batches.id"), index=True)
    horizon: Mapped[str] = mapped_column(String(8), index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    schema_version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    evaluation_set_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("reflection_runs.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    source_snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    receipt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    source_run: Mapped[WorkflowRun] = relationship()
    source_batch: Mapped[EvaluationBatch] = relationship()
    supersedes: Mapped[ReflectionRun | None] = relationship(
        remote_side="ReflectionRun.id", uselist=False
    )
    findings: Mapped[list[ReflectionFinding]] = relationship(
        back_populates="reflection_run", cascade="all, delete-orphan"
    )
    lesson_proposals: Mapped[list[LessonProposal]] = relationship(
        back_populates="reflection_run", cascade="all, delete-orphan"
    )


class ReflectionFinding(Base):
    __tablename__ = "reflection_findings"
    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('agent', 'committee', 'market_event')",
            name="ck_reflection_finding_scope",
        ),
        CheckConstraint(
            "verdict IN "
            "('right_reason', 'lucky_correct', 'wrong', 'wrong_noise', "
            "'right_but_noise', 'not_applicable', 'unresolved')",
            name="ck_reflection_finding_verdict",
        ),
        CheckConstraint(
            "availability_class IN "
            "('available_used', 'available_missed', 'coverage_gap_pre_cutoff', "
            "'post_cutoff_event', 'after_close_explanation', 'unresolved')",
            name="ck_reflection_finding_availability",
        ),
        CheckConstraint(
            "causal_status IN ('verified', 'supported', 'hypothesis', 'unresolved')",
            name="ck_reflection_finding_causal_status",
        ),
        CheckConstraint(
            "primary_error_type IN "
            "('data_coverage_failure', 'attention_omission', "
            "'reasoning_or_weighting_failure', 'transmission_mapping', "
            "'horizon_timing', 'post_cutoff_shock', 'risk_plan_failure', "
            "'market_noise', 'unresolved')",
            name="ck_reflection_finding_error_type",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_reflection_finding_confidence",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    reflection_run_id: Mapped[str] = mapped_column(ForeignKey("reflection_runs.id"), index=True)
    scope_type: Mapped[str] = mapped_column(String(24), index=True)
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    index_code: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    horizon: Mapped[str] = mapped_column(String(8), index=True)
    verdict: Mapped[str] = mapped_column(String(32), index=True)
    primary_error_type: Mapped[str] = mapped_column(String(40), index=True)
    secondary_error_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    availability_class: Mapped[str] = mapped_column(String(40), index=True)
    causal_status: Mapped[str] = mapped_column(String(24), index=True)
    counterfactual: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    remediation: Mapped[list[str]] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float)
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    reflection_run: Mapped[ReflectionRun] = relationship(back_populates="findings")


class ReflectionHumanReview(Base):
    """Append-only operator decision used to open the Lesson proposal gate."""

    __tablename__ = "reflection_human_reviews"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="ck_reflection_human_review_decision",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    reflection_run_id: Mapped[str] = mapped_column(
        ForeignKey("reflection_runs.id"), unique=True, index=True
    )
    decision: Mapped[str] = mapped_column(String(16), index=True)
    reviewer: Mapped[str] = mapped_column(String(120))
    notes: Mapped[str] = mapped_column(Text, default="")
    notes_hash: Mapped[str] = mapped_column(String(64))
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    reflection_run: Mapped[ReflectionRun] = relationship()


class LessonProposal(Base):
    """A candidate lesson; publication to Wiki always remains a separate action."""

    __tablename__ = "lesson_proposals"
    __table_args__ = (
        Index(
            "uq_lesson_active_cluster_head",
            "cluster_key",
            unique=True,
            sqlite_where=text("status IN ('active', 'challenged')"),
            postgresql_where=text("status IN ('active', 'challenged')"),
        ),
        UniqueConstraint(
            "reflection_run_id",
            "cluster_key",
            name="uq_lesson_proposal_episode_cluster",
        ),
        CheckConstraint(
            "status IN ('candidate', 'active', 'challenged', 'retired', 'superseded')",
            name="ck_lesson_proposal_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    reflection_run_id: Mapped[str] = mapped_column(ForeignKey("reflection_runs.id"), index=True)
    episode_key: Mapped[str] = mapped_column(String(120), index=True)
    cluster_key: Mapped[str] = mapped_column(String(240), index=True)
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="candidate", index=True)
    proposal_type: Mapped[str] = mapped_column(String(40), index=True)
    evidence_finding_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    independent_episode_count: Mapped[int] = mapped_column(Integer, default=1)
    replay_target_dates: Mapped[int] = mapped_column(Integer, default=0)
    replay_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    half_life_sessions: Mapped[int] = mapped_column(Integer, default=60)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("lesson_proposals.id"), nullable=True
    )

    reflection_run: Mapped[ReflectionRun] = relationship(back_populates="lesson_proposals")
    supersedes: Mapped[LessonProposal | None] = relationship(
        remote_side="LessonProposal.id", uselist=False
    )
    replay_batches: Mapped[list[LessonReplayBatch]] = relationship(back_populates="lesson_proposal")
    lifecycle_events: Mapped[list[LessonLifecycleEvent]] = relationship(
        back_populates="lesson_proposal"
    )


class LessonEpisode(Base):
    """One independent market episode per recurrence cluster and target date."""

    __tablename__ = "lesson_episodes"
    __table_args__ = (
        UniqueConstraint(
            "cluster_key",
            "episode_key",
            name="uq_lesson_episode_cluster_date",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cluster_key: Mapped[str] = mapped_column(String(240), index=True)
    episode_key: Mapped[str] = mapped_column(String(120), index=True)
    first_reflection_run_id: Mapped[str] = mapped_column(
        ForeignKey("reflection_runs.id"), index=True
    )
    evidence_set_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    first_reflection_run: Mapped[ReflectionRun] = relationship()


class LessonReplayBatch(Base):
    """Immutable replay observations plus deterministic aggregate metrics."""

    __tablename__ = "lesson_replay_batches"
    __table_args__ = (
        UniqueConstraint(
            "lesson_proposal_id",
            "content_hash",
            name="uq_lesson_replay_batch_content",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    lesson_proposal_id: Mapped[str] = mapped_column(ForeignKey("lesson_proposals.id"), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON)
    observations: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    observation_count: Mapped[int] = mapped_column(Integer)
    distinct_target_dates: Mapped[int] = mapped_column(Integer)
    aggregate_metrics: Mapped[dict[str, Any]] = mapped_column(JSON)
    submitted_by: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    lesson_proposal: Mapped[LessonProposal] = relationship(back_populates="replay_batches")


class LessonLifecycleEvent(Base):
    """Append-only audit event behind the mutable Lesson status projection."""

    __tablename__ = "lesson_lifecycle_events"
    __table_args__ = (
        UniqueConstraint(
            "lesson_proposal_id",
            "event_key",
            name="uq_lesson_lifecycle_event_key",
        ),
        UniqueConstraint(
            "lesson_proposal_id",
            "sequence_number",
            name="uq_lesson_lifecycle_event_sequence",
        ),
        CheckConstraint(
            "event_type IN "
            "('replay_recorded', 'approved', 'revalidated', "
            "'challenged', 'retired', 'superseded')",
            name="ck_lesson_lifecycle_event_type",
        ),
        CheckConstraint(
            "from_status IN ('candidate', 'active', 'challenged', 'retired', 'superseded')",
            name="ck_lesson_lifecycle_event_from_status",
        ),
        CheckConstraint(
            "to_status IN ('candidate', 'active', 'challenged', 'retired', 'superseded')",
            name="ck_lesson_lifecycle_event_to_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    lesson_proposal_id: Mapped[str] = mapped_column(ForeignKey("lesson_proposals.id"), index=True)
    sequence_number: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    event_key: Mapped[str] = mapped_column(String(160))
    from_status: Mapped[str] = mapped_column(String(24))
    to_status: Mapped[str] = mapped_column(String(24))
    actor: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    occurred_at_canonical: Mapped[str] = mapped_column(String(40))

    lesson_proposal: Mapped[LessonProposal] = relationship(back_populates="lifecycle_events")


class AgentTrace(Base):
    """One private, non-authoritative execution attempt for a workflow subject."""

    __tablename__ = "agent_traces"
    __table_args__ = (
        UniqueConstraint(
            "workflow_kind",
            "subject_id",
            "attempt_number",
            name="uq_agent_trace_attempt",
        ),
        CheckConstraint(
            "workflow_kind IN ('prediction', 'reflection', 'agent_eval')",
            name="ck_agent_trace_workflow_kind",
        ),
        CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'degraded')",
            name="ck_agent_trace_status",
        ),
        CheckConstraint("attempt_number >= 1", name="ck_agent_trace_attempt_number"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workflow_kind: Mapped[str] = mapped_column(String(24), index=True)
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    target_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    horizon: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    mode: Mapped[str] = mapped_column(String(24), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    trace_policy_version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    telemetry_complete: Mapped[bool] = mapped_column(Boolean, default=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    spans: Mapped[list[AgentTraceSpan]] = relationship(
        back_populates="trace", cascade="all, delete-orphan"
    )
    artifact_links: Mapped[list[AgentTraceArtifactLink]] = relationship(
        back_populates="trace", cascade="all, delete-orphan"
    )


class AgentTraceSpan(Base):
    """One sanitized workflow, agent, model, validator, or persistence span."""

    __tablename__ = "agent_trace_spans"
    __table_args__ = (
        UniqueConstraint("trace_id", "span_id", name="uq_agent_trace_span_identity"),
        ForeignKeyConstraint(
            ["trace_id", "parent_span_id"],
            ["agent_trace_spans.trace_id", "agent_trace_spans.span_id"],
            name="fk_agent_trace_span_parent",
        ),
        CheckConstraint(
            "span_kind IN ('workflow', 'agent', 'llm', 'validator', 'persistence', 'external')",
            name="ck_agent_trace_span_kind",
        ),
        CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="ck_agent_trace_span_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    trace_id: Mapped[str] = mapped_column(
        ForeignKey("agent_traces.id", ondelete="CASCADE"), index=True
    )
    span_id: Mapped[str] = mapped_column(String(16), index=True)
    parent_span_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    node_id: Mapped[str] = mapped_column(String(120), index=True)
    name: Mapped[str] = mapped_column(String(200))
    span_kind: Mapped[str] = mapped_column(String(24), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    agent_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    prompt_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    trace: Mapped[AgentTrace] = relationship(back_populates="spans")


class AgentTraceArtifactLink(Base):
    """Append-only identity link from a trace or span to an audited artifact."""

    __tablename__ = "agent_trace_artifact_links"
    __table_args__ = (
        UniqueConstraint(
            "trace_id",
            "span_id",
            "artifact_kind",
            "artifact_id",
            "relation",
            name="uq_agent_trace_artifact_link",
        ),
        ForeignKeyConstraint(
            ["trace_id", "span_id"],
            ["agent_trace_spans.trace_id", "agent_trace_spans.span_id"],
            name="fk_agent_trace_artifact_link_span",
        ),
        CheckConstraint(
            "artifact_kind IN ('signal', 'forecast', 'evaluation', 'reasoning_review', "
            "'reflection', 'bad_case')",
            name="ck_agent_trace_artifact_kind",
        ),
        CheckConstraint(
            "relation IN ('input', 'output', 'reused', 'diagnostic')",
            name="ck_agent_trace_artifact_relation",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    trace_id: Mapped[str] = mapped_column(
        ForeignKey("agent_traces.id", ondelete="CASCADE"), index=True
    )
    span_id: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    artifact_kind: Mapped[str] = mapped_column(String(32), index=True)
    artifact_id: Mapped[str] = mapped_column(String(160), index=True)
    relation: Mapped[str] = mapped_column(String(24), index=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    trace: Mapped[AgentTrace] = relationship(back_populates="artifact_links")


class AgentEvalExperiment(Base):
    """One immutable-suite comparison between a baseline and candidate target."""

    __tablename__ = "agent_eval_experiments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')",
            name="ck_agent_eval_experiment_status",
        ),
        CheckConstraint(
            "release_decision IN ('pending', 'pass', 'fail', 'insufficient_sample')",
            name="ck_agent_eval_release_decision",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    suite_id: Mapped[str] = mapped_column(String(120), index=True)
    suite_version: Mapped[str] = mapped_column(String(32))
    suite_hash: Mapped[str] = mapped_column(String(64), index=True)
    baseline_target_id: Mapped[str] = mapped_column(String(120))
    baseline_target_hash: Mapped[str] = mapped_column(String(64))
    candidate_target_id: Mapped[str] = mapped_column(String(120))
    candidate_target_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), index=True)
    release_decision: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    policy_version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    report_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    task: Mapped[AgentEvalTask | None] = relationship(
        back_populates="experiment", cascade="all, delete-orphan", uselist=False
    )
    results: Mapped[list[AgentEvalResult]] = relationship(
        back_populates="experiment", cascade="all, delete-orphan"
    )


class AgentEvalTask(Base):
    """Durable queue item for an offline Agent evaluation experiment."""

    __tablename__ = "agent_eval_tasks"
    __table_args__ = (
        UniqueConstraint("experiment_id", name="uq_agent_eval_task_experiment"),
        UniqueConstraint("idempotency_key", name="uq_agent_eval_task_idempotency"),
        CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'completed', 'failed')",
            name="ck_agent_eval_task_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("agent_eval_experiments.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(24), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=0)

    experiment: Mapped[AgentEvalExperiment] = relationship(back_populates="task")


class AgentEvalResult(Base):
    """Per-case evaluator result for one experiment arm."""

    __tablename__ = "agent_eval_results"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "arm",
            "case_id",
            "evaluator_id",
            name="uq_agent_eval_result_identity",
        ),
        CheckConstraint("arm IN ('baseline', 'candidate')", name="ck_agent_eval_arm"),
        CheckConstraint(
            "status IN ('passed', 'failed', 'not_applicable', 'error')",
            name="ck_agent_eval_result_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("agent_eval_experiments.id", ondelete="CASCADE"), index=True
    )
    arm: Mapped[str] = mapped_column(String(16), index=True)
    case_id: Mapped[str] = mapped_column(String(160), index=True)
    evaluator_id: Mapped[str] = mapped_column(String(160), index=True)
    evaluator_version: Mapped[str] = mapped_column(String(32))
    metric_kind: Mapped[str] = mapped_column(String(64), index=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    explanation: Mapped[str] = mapped_column(Text, default="")
    output_hash: Mapped[str] = mapped_column(String(64))
    trace_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_traces.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    experiment: Mapped[AgentEvalExperiment] = relationship(back_populates="results")


class AgentBadCase(Base):
    """Mutable projection backed by append-only bad-case lifecycle events."""

    __tablename__ = "agent_bad_cases"
    __table_args__ = (
        UniqueConstraint("dedupe_hash", name="uq_agent_bad_case_dedupe"),
        CheckConstraint(
            "status IN ('detected', 'triaged', 'confirmed', 'materialized', "
            "'resolved', 'rejected')",
            name="ck_agent_bad_case_status",
        ),
        CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')",
            name="ck_agent_bad_case_severity",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    trace_id: Mapped[str] = mapped_column(ForeignKey("agent_traces.id"), index=True)
    span_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    eval_result_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_eval_results.id"), nullable=True
    )
    workflow_kind: Mapped[str] = mapped_column(String(24), index=True)
    issue_type: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text)
    expected_behavior: Mapped[str] = mapped_column(Text, default="")
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dedupe_hash: Mapped[str] = mapped_column(String(64))
    dataset_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    dataset_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    events: Mapped[list[AgentBadCaseEvent]] = relationship(
        back_populates="bad_case", cascade="all, delete-orphan"
    )


class AgentBadCaseEvent(Base):
    """One immutable transition in the bad-case governance hash chain."""

    __tablename__ = "agent_bad_case_events"
    __table_args__ = (
        UniqueConstraint("bad_case_id", "sequence_number", name="uq_bad_case_event_seq"),
        UniqueConstraint("bad_case_id", "idempotency_key", name="uq_bad_case_event_key"),
        CheckConstraint(
            "event_type IN ('detected', 'triaged', 'confirmed', 'materialized', "
            "'resolved', 'rejected')",
            name="ck_agent_bad_case_event_type",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    bad_case_id: Mapped[str] = mapped_column(
        ForeignKey("agent_bad_cases.id", ondelete="CASCADE"), index=True
    )
    sequence_number: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(24), index=True)
    from_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_status: Mapped[str] = mapped_column(String(24))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    actor: Mapped[str] = mapped_column(String(120))
    notes: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    previous_event_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    bad_case: Mapped[AgentBadCase] = relationship(back_populates="events")
