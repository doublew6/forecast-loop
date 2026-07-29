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
                "mode = 'live' AND status IN "
                "('awaiting_draft', 'queued', 'running', 'completed')"
            ),
            postgresql_where=text(
                "mode = 'live' AND status IN "
                "('awaiting_draft', 'queued', 'running', 'completed')"
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
            "attempt_count >= 0 AND max_attempts >= 1 "
            "AND attempt_count <= max_attempts",
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


class UserJudgment(Base):
    """One immutable human judgment bound to a completed committee forecast."""

    __tablename__ = "user_judgments"
    __table_args__ = (
        UniqueConstraint(
            "actor_id",
            "forecast_id",
            name="uq_user_judgment_actor_forecast",
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
    forecast_id: Mapped[str] = mapped_column(ForeignKey("forecasts.id"), index=True)
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
    forecast_input_hash: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(32))
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    wiki_path: Mapped[str] = mapped_column(Text)
    wiki_artifact_hash: Mapped[str] = mapped_column(String(64))

    forecast: Mapped[Forecast] = relationship(back_populates="user_judgments")
    agent_spec_record: Mapped[AgentSpecRecord | None] = relationship(lazy="joined")
    evaluation: Mapped[UserJudgmentEvaluation | None] = relationship(
        back_populates="judgment",
        uselist=False,
    )


class UserJudgmentEvaluation(Base):
    """Immutable score derived only from a trusted forecast evaluation batch."""

    __tablename__ = "user_judgment_evaluations"
    __table_args__ = (
        CheckConstraint(
            "actual_label IN ('up', 'neutral', 'down')",
            name="ck_user_judgment_evaluation_label",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_judgment_id: Mapped[str] = mapped_column(
        ForeignKey("user_judgments.id"),
        unique=True,
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
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

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
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
    historical_abs_return_percentile: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    history_sample_size: Mapped[int] = mapped_column(Integer, default=0)
    source_url: Mapped[str] = mapped_column(Text)
    source_hash: Mapped[str] = mapped_column(String(64))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

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
    agent_opinion_id: Mapped[str] = mapped_column(
        ForeignKey("agent_opinions.id"), index=True
    )
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
    historical_abs_return_percentile: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
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
    source_batch_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_batches.id"), index=True
    )
    horizon: Mapped[str] = mapped_column(String(8), index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    schema_version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    evaluation_set_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("reflection_runs.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
    reflection_run_id: Mapped[str] = mapped_column(
        ForeignKey("reflection_runs.id"), index=True
    )
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
    reflection_run_id: Mapped[str] = mapped_column(
        ForeignKey("reflection_runs.id"), index=True
    )
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
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("lesson_proposals.id"), nullable=True
    )

    reflection_run: Mapped[ReflectionRun] = relationship(back_populates="lesson_proposals")
    supersedes: Mapped[LessonProposal | None] = relationship(
        remote_side="LessonProposal.id", uselist=False
    )
    replay_batches: Mapped[list[LessonReplayBatch]] = relationship(
        back_populates="lesson_proposal"
    )
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
    lesson_proposal_id: Mapped[str] = mapped_column(
        ForeignKey("lesson_proposals.id"), index=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON)
    observations: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    observation_count: Mapped[int] = mapped_column(Integer)
    distinct_target_dates: Mapped[int] = mapped_column(Integer)
    aggregate_metrics: Mapped[dict[str, Any]] = mapped_column(JSON)
    submitted_by: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    lesson_proposal: Mapped[LessonProposal] = relationship(
        back_populates="replay_batches"
    )


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
            "from_status IN "
            "('candidate', 'active', 'challenged', 'retired', 'superseded')",
            name="ck_lesson_lifecycle_event_from_status",
        ),
        CheckConstraint(
            "to_status IN "
            "('candidate', 'active', 'challenged', 'retired', 'superseded')",
            name="ck_lesson_lifecycle_event_to_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    lesson_proposal_id: Mapped[str] = mapped_column(
        ForeignKey("lesson_proposals.id"), index=True
    )
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

    lesson_proposal: Mapped[LessonProposal] = relationship(
        back_populates="lifecycle_events"
    )
