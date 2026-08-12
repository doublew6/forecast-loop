"""Add focused multi-horizon research protocol v2 tables.

Revision ID: 0015_research_program_v2
Revises: 0014_trace_attempts
Create Date: 2026-08-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_research_program_v2"
down_revision = "0014_trace_attempts"
branch_labels = None
depends_on = None

V2_TABLES = (
    "research_runs_v2",
    "agent_signals_v2",
    "forecasts_v2",
    "outcome_observations_v2",
    "signal_evaluations_v2",
    "forecast_evaluations_v2",
    "reasoning_reviews_v2",
    "reasoning_review_human_events_v2",
    "reflections_v2",
    "reflection_review_events_v2",
    "research_activation_events_v2",
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing_v2_tables = set(inspector.get_table_names()).intersection(V2_TABLES)
    if existing_v2_tables:
        missing_v2_tables = set(V2_TABLES).difference(existing_v2_tables)
        if missing_v2_tables:
            raise RuntimeError(
                "partial research v2 schema detected; missing tables: "
                + ", ".join(sorted(missing_v2_tables))
            )
        # Revision 0001 historically used current metadata.create_all().  A fresh
        # database can therefore already contain every v2 table before Alembic
        # reaches this revision.  The tables alone are not sufficient: the
        # append-only and terminal-state guards are part of the v2 contract.
        _install_v2_guards()
        return

    op.create_table(
        "research_runs_v2",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("program_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=True),
        sa.Column("mode", sa.String(24), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("anchor_date", sa.Date(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("program", sa.JSON(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("receipt", sa.JSON(), nullable=False),
        sa.UniqueConstraint("input_hash", name="uq_research_run_v2_input_hash"),
        sa.CheckConstraint(
            "status IN ('awaiting_draft', 'completed', 'failed')",
            name="ck_research_run_v2_status",
        ),
        sa.CheckConstraint("mode IN ('demo', 'live')", name="ck_research_run_v2_mode"),
    )
    _indexes(
        "research_runs_v2",
        "program_hash",
        "snapshot_hash",
        "input_hash",
        "request_hash",
        "mode",
        "status",
        "anchor_date",
        "as_of",
    )

    op.create_table(
        "agent_signals_v2",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("research_runs_v2.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("agent_version", sa.String(32), nullable=False),
        sa.Column("model_name", sa.String(160), nullable=False),
        sa.Column("prompt_version", sa.String(80), nullable=False),
        sa.Column("target_id", sa.String(120), nullable=False),
        sa.Column("signal_kind", sa.String(32), nullable=False),
        sa.Column("natural_horizon", sa.String(8), nullable=False),
        sa.Column("decision_horizon", sa.String(8), nullable=True),
        sa.Column("anchor_date", sa.Date(), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("evidence_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("program_hash", sa.String(64), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column("baseline_probabilities", sa.JSON(), nullable=True),
        sa.Column("state_available", sa.Boolean(), nullable=False),
        sa.Column("abstain", sa.Boolean(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("envelope", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("content_hash", name="uq_agent_signal_v2_content_hash"),
        sa.UniqueConstraint(
            "run_id",
            "agent_id",
            "target_id",
            "signal_kind",
            name="uq_agent_signal_v2_run_identity",
        ),
        sa.CheckConstraint(
            "signal_kind IN ('natural_view', 'd1_impact', 'strategy_forecast', "
            "'risk_critique', 'decision_forecast')",
            name="ck_agent_signal_v2_kind",
        ),
        sa.CheckConstraint(
            "natural_horizon IN ('D1', 'W1', 'D20')",
            name="ck_agent_signal_v2_natural_horizon",
        ),
        sa.CheckConstraint(
            "decision_horizon IS NULL OR decision_horizon IN ('D1', 'W1', 'D20')",
            name="ck_agent_signal_v2_decision_horizon",
        ),
    )
    _indexes(
        "agent_signals_v2",
        "run_id",
        "agent_id",
        "agent_version",
        "model_name",
        "target_id",
        "signal_kind",
        "natural_horizon",
        "decision_horizon",
        "anchor_date",
        "target_date",
        "evidence_cutoff",
        "program_hash",
        "input_hash",
        "content_hash",
        "created_at",
    )

    op.create_table(
        "forecasts_v2",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("research_runs_v2.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_signal_id",
            sa.String(64),
            sa.ForeignKey("agent_signals_v2.id"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("program_hash", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(120), nullable=False),
        sa.Column("horizon", sa.String(8), nullable=False),
        sa.Column("configured_lane", sa.String(16), nullable=False),
        sa.Column("effective_lane", sa.String(16), nullable=False),
        sa.Column("anchor_date", sa.Date(), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("probability_up", sa.Float(), nullable=False),
        sa.Column("probability_neutral", sa.Float(), nullable=False),
        sa.Column("probability_down", sa.Float(), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("baseline_probabilities", sa.JSON(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("counter_evidence", sa.JSON(), nullable=False),
        sa.Column("invalidation_conditions", sa.JSON(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "target_id", name="uq_forecast_v2_run_target"),
        sa.UniqueConstraint("source_signal_id", name="uq_forecast_v2_source_signal"),
        sa.UniqueConstraint("content_hash", name="uq_forecast_v2_content_hash"),
        sa.CheckConstraint("horizon IN ('D1', 'W1')", name="ck_forecast_v2_horizon"),
        sa.CheckConstraint(
            "configured_lane IN ('formal', 'shadow')",
            name="ck_forecast_v2_configured_lane",
        ),
        sa.CheckConstraint(
            "effective_lane IN ('formal', 'shadow')",
            name="ck_forecast_v2_effective_lane",
        ),
    )
    _indexes(
        "forecasts_v2",
        "run_id",
        "source_signal_id",
        "program_hash",
        "target_id",
        "horizon",
        "configured_lane",
        "effective_lane",
        "anchor_date",
        "target_date",
        "input_hash",
        "content_hash",
        "created_at",
    )

    op.create_table(
        "outcome_observations_v2",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("program_hash", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(24), nullable=False),
        sa.Column("target_id", sa.String(120), nullable=False),
        sa.Column("anchor_date", sa.Date(), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("actual_value", sa.Float(), nullable=False),
        sa.Column("actual_label", sa.String(16), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("observation", sa.JSON(), nullable=False),
        sa.UniqueConstraint("content_hash", name="uq_outcome_observation_v2_hash"),
        sa.UniqueConstraint(
            "program_hash",
            "target_id",
            "anchor_date",
            "target_date",
            "mode",
            name="uq_outcome_observation_v2_episode",
        ),
        sa.CheckConstraint(
            "mode IN ('demo', 'live')", name="ck_outcome_observation_v2_mode"
        ),
    )
    _indexes(
        "outcome_observations_v2",
        "program_hash",
        "mode",
        "target_id",
        "anchor_date",
        "target_date",
        "actual_label",
        "observed_at",
        "content_hash",
    )

    op.create_table(
        "signal_evaluations_v2",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "signal_id",
            sa.String(64),
            sa.ForeignKey("agent_signals_v2.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "observation_id",
            sa.String(36),
            sa.ForeignKey("outcome_observations_v2.id"),
            nullable=False,
        ),
        sa.Column("actual_label", sa.String(16), nullable=False),
        sa.Column("brier_score", sa.Float(), nullable=False),
        sa.Column("baseline_brier_score", sa.Float(), nullable=False),
        sa.Column("direction_correct", sa.Boolean(), nullable=True),
        sa.Column("evaluator_version", sa.String(32), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("signal_id", name="uq_signal_evaluation_v2_signal"),
        sa.UniqueConstraint("content_hash", name="uq_signal_evaluation_v2_hash"),
    )
    _indexes(
        "signal_evaluations_v2",
        "signal_id",
        "observation_id",
        "actual_label",
        "evaluated_at",
        "content_hash",
    )

    op.create_table(
        "forecast_evaluations_v2",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "forecast_id",
            sa.String(36),
            sa.ForeignKey("forecasts_v2.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "signal_evaluation_id",
            sa.String(36),
            sa.ForeignKey("signal_evaluations_v2.id"),
            nullable=False,
        ),
        sa.Column("actual_value", sa.Float(), nullable=False),
        sa.Column("actual_label", sa.String(16), nullable=False),
        sa.Column("brier_score", sa.Float(), nullable=False),
        sa.Column("baseline_brier_score", sa.Float(), nullable=False),
        sa.Column("direction_correct", sa.Boolean(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("forecast_id", name="uq_forecast_evaluation_v2_forecast"),
        sa.UniqueConstraint(
            "signal_evaluation_id", name="uq_forecast_evaluation_v2_signal_evaluation"
        ),
        sa.UniqueConstraint("content_hash", name="uq_forecast_evaluation_v2_hash"),
    )
    _indexes(
        "forecast_evaluations_v2",
        "forecast_id",
        "signal_evaluation_id",
        "actual_label",
        "evaluated_at",
        "content_hash",
    )

    op.create_table(
        "reasoning_reviews_v2",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "signal_id",
            sa.String(64),
            sa.ForeignKey("agent_signals_v2.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("review_input_hash", sa.String(64), nullable=False),
        sa.Column("deterministic_checks", sa.JSON(), nullable=False),
        sa.Column("rubric", sa.JSON(), nullable=False),
        sa.Column("total_score", sa.Integer(), nullable=False),
        sa.Column("human_review_required", sa.Boolean(), nullable=False),
        sa.Column("human_review_status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("signal_id", name="uq_reasoning_review_v2_signal"),
        sa.UniqueConstraint("review_input_hash", name="uq_reasoning_review_v2_input"),
        sa.UniqueConstraint("content_hash", name="uq_reasoning_review_v2_hash"),
        sa.CheckConstraint(
            "human_review_status IN ('not_required', 'pending', 'approved', 'rejected')",
            name="ck_reasoning_review_v2_human_status",
        ),
    )
    _indexes(
        "reasoning_reviews_v2",
        "signal_id",
        "review_input_hash",
        "human_review_required",
        "human_review_status",
        "created_at",
        "content_hash",
    )

    op.create_table(
        "reasoning_review_human_events_v2",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "review_id",
            sa.String(36),
            sa.ForeignKey("reasoning_reviews_v2.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reviewer", sa.String(120), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("review_id", name="uq_reasoning_review_human_event_v2_review"),
        sa.UniqueConstraint("content_hash", name="uq_reasoning_review_human_event_v2_hash"),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected')", name="ck_reasoning_human_decision"
        ),
    )
    _indexes(
        "reasoning_review_human_events_v2",
        "review_id",
        "decision",
        "occurred_at",
        "content_hash",
    )

    op.create_table(
        "reflections_v2",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "forecast_id",
            sa.String(36),
            sa.ForeignKey("forecasts_v2.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("forecast_hash", sa.String(64), nullable=False),
        sa.Column(
            "evaluation_id",
            sa.String(36),
            sa.ForeignKey("forecast_evaluations_v2.id"),
            nullable=False,
        ),
        sa.Column("evaluation_hash", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(120), nullable=False),
        sa.Column("anchor_date", sa.Date(), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("actual_label", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("verdict", sa.String(64), nullable=False),
        sa.Column("findings", sa.JSON(), nullable=False),
        sa.Column("envelope", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("forecast_id", name="uq_reflection_v2_forecast"),
        sa.UniqueConstraint("evaluation_id", name="uq_reflection_v2_evaluation"),
        sa.UniqueConstraint("content_hash", name="uq_reflection_v2_hash"),
        sa.CheckConstraint(
            "status IN ('completed', 'failed')", name="ck_reflection_v2_status"
        ),
    )
    _indexes(
        "reflections_v2",
        "forecast_id",
        "forecast_hash",
        "evaluation_id",
        "evaluation_hash",
        "target_id",
        "anchor_date",
        "target_date",
        "actual_label",
        "status",
        "created_at",
        "content_hash",
    )

    op.create_table(
        "reflection_review_events_v2",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "reflection_id",
            sa.String(36),
            sa.ForeignKey("reflections_v2.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reviewer", sa.String(120), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("reflection_id", name="uq_reflection_review_event_v2_reflection"),
        sa.UniqueConstraint("content_hash", name="uq_reflection_review_event_v2_hash"),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="ck_reflection_review_v2_decision",
        ),
    )
    _indexes(
        "reflection_review_events_v2",
        "reflection_id",
        "decision",
        "occurred_at",
        "content_hash",
    )

    op.create_table(
        "research_activation_events_v2",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("program_hash", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(120), nullable=False),
        sa.Column("event_type", sa.String(16), nullable=False),
        sa.Column("policy_version", sa.String(32), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("previous_event_hash", sa.String(64), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("content_hash", name="uq_research_activation_v2_hash"),
        sa.CheckConstraint(
            "event_type IN ('activated', 'retired')", name="ck_activation_v2_type"
        ),
    )
    _indexes(
        "research_activation_events_v2",
        "program_hash",
        "target_id",
        "event_type",
        "occurred_at",
        "content_hash",
    )
    _install_v2_guards()


def downgrade() -> None:
    _drop_v2_guards()
    for table in reversed(V2_TABLES):
        op.drop_table(table)


def _indexes(table: str, *columns: str) -> None:
    for column in columns:
        op.create_index(f"ix_{table}_{column}", table, [column])


def _install_v2_guards() -> None:
    connection = op.get_bind()
    append_only = (
        "agent_signals_v2",
        "forecasts_v2",
        "outcome_observations_v2",
        "signal_evaluations_v2",
        "forecast_evaluations_v2",
        "reasoning_reviews_v2",
        "reasoning_review_human_events_v2",
        "reflections_v2",
        "reflection_review_events_v2",
        "research_activation_events_v2",
    )
    if connection.dialect.name == "sqlite":
        for table in append_only:
            for operation in ("UPDATE", "DELETE"):
                connection.execute(
                    sa.text(
                        f"CREATE TRIGGER IF NOT EXISTS trg_{table}_reject_{operation.lower()} "
                        f"BEFORE {operation} ON {table} BEGIN "
                        "SELECT RAISE(ABORT, 'immutable v2 research record'); END"
                    )
                )
        connection.execute(
            sa.text(
                "CREATE TRIGGER IF NOT EXISTS trg_research_runs_v2_reject_delete "
                "BEFORE DELETE ON research_runs_v2 BEGIN "
                "SELECT RAISE(ABORT, 'v2 research runs are retained'); END"
            )
        )
        connection.execute(
            sa.text(
                "CREATE TRIGGER IF NOT EXISTS trg_research_runs_v2_reject_terminal_update "
                "BEFORE UPDATE ON research_runs_v2 WHEN OLD.status != 'awaiting_draft' BEGIN "
                "SELECT RAISE(ABORT, 'terminal v2 research run is immutable'); END"
            )
        )
        return
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                "CREATE OR REPLACE FUNCTION forecast_loop_reject_v2_mutation() "
                "RETURNS trigger AS $$ BEGIN RAISE EXCEPTION "
                "'immutable v2 research record'; END; $$ LANGUAGE plpgsql"
            )
        )
        for table in append_only:
            connection.execute(
                sa.text(
                    f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                    "FOR EACH ROW EXECUTE FUNCTION forecast_loop_reject_v2_mutation()"
                )
            )
        connection.execute(
            sa.text(
                "CREATE OR REPLACE FUNCTION forecast_loop_guard_v2_research_run() "
                "RETURNS trigger AS $$ BEGIN "
                "IF TG_OP = 'DELETE' OR OLD.status != 'awaiting_draft' THEN "
                "RAISE EXCEPTION 'terminal v2 research run is immutable'; END IF; "
                "RETURN NEW; END; $$ LANGUAGE plpgsql"
            )
        )
        connection.execute(
            sa.text(
                "CREATE TRIGGER trg_research_runs_v2_guard BEFORE UPDATE OR DELETE "
                "ON research_runs_v2 FOR EACH ROW EXECUTE FUNCTION "
                "forecast_loop_guard_v2_research_run()"
            )
        )


def _drop_v2_guards() -> None:
    connection = op.get_bind()
    tables = (
        "agent_signals_v2",
        "forecasts_v2",
        "outcome_observations_v2",
        "signal_evaluations_v2",
        "forecast_evaluations_v2",
        "reasoning_reviews_v2",
        "reasoning_review_human_events_v2",
        "reflections_v2",
        "reflection_review_events_v2",
        "research_activation_events_v2",
    )
    if connection.dialect.name == "sqlite":
        for table in tables:
            for operation in ("update", "delete"):
                connection.execute(
                    sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_reject_{operation}")
                )
        for trigger in (
            "trg_research_runs_v2_reject_delete",
            "trg_research_runs_v2_reject_terminal_update",
        ):
            connection.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger}"))
        return
    if connection.dialect.name == "postgresql":
        for table in tables:
            connection.execute(
                sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
            )
        connection.execute(
            sa.text("DROP FUNCTION IF EXISTS forecast_loop_reject_v2_mutation()")
        )
        connection.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS trg_research_runs_v2_guard ON research_runs_v2"
            )
        )
        connection.execute(
            sa.text("DROP FUNCTION IF EXISTS forecast_loop_guard_v2_research_run()")
        )
