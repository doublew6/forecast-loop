"""Add append-only daily reflection entities.

Revision ID: 0003_daily_reflection
Revises: 0002_handoff_waiting_index
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_daily_reflection"
down_revision = "0002_handoff_waiting_index"
branch_labels = None
depends_on = None

_TABLES = {
    "evaluation_batches",
    "market_session_snapshots",
    "opinion_evaluations",
    "forecast_diagnostics",
    "reflection_runs",
    "reflection_findings",
    "lesson_proposals",
}


def upgrade() -> None:
    # Revision 0001 intentionally uses current Base.metadata.create_all. A new
    # database therefore already contains these tables before Alembic reaches
    # this revision, while an existing 0002 database contains none of them.
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    present = existing & _TABLES
    if present:
        if present != _TABLES:
            missing = ", ".join(sorted(_TABLES - present))
            raise RuntimeError(f"partial reflection schema detected; missing: {missing}")
        return
    op.create_table(
        "evaluation_batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("horizon", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("evaluation_set_hash", sa.String(length=64), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("data_quality", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('completed', 'failed', 'blocked_upstream')",
            name="ck_evaluation_batch_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "target_date",
            "horizon",
            "evaluation_set_hash",
            "status",
            name="uq_evaluation_batch_identity",
        ),
    )
    op.create_index(
        op.f("ix_evaluation_batches_target_date"),
        "evaluation_batches",
        ["target_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_evaluation_batches_horizon"),
        "evaluation_batches",
        ["horizon"],
        unique=False,
    )
    op.create_index(
        op.f("ix_evaluation_batches_status"),
        "evaluation_batches",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_evaluation_batches_evaluation_set_hash"),
        "evaluation_batches",
        ["evaluation_set_hash"],
        unique=False,
    )

    op.create_table(
        "market_session_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("index_code", sa.String(length=24), nullable=False),
        sa.Column("index_name", sa.String(length=64), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("base_trade_date", sa.Date(), nullable=False),
        sa.Column("base_close", sa.Float(), nullable=False),
        sa.Column("target_close", sa.Float(), nullable=False),
        sa.Column("actual_return", sa.Float(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=True),
        sa.Column("advancers", sa.Integer(), nullable=True),
        sa.Column("decliners", sa.Integer(), nullable=True),
        sa.Column("unchanged", sa.Integer(), nullable=True),
        sa.Column("limit_down_count", sa.Integer(), nullable=True),
        sa.Column("breadth_down_ratio", sa.Float(), nullable=True),
        sa.Column("sector_contributions", sa.JSON(), nullable=False),
        sa.Column("weight_contributions", sa.JSON(), nullable=False),
        sa.Column("historical_abs_return_percentile", sa.Float(), nullable=True),
        sa.Column("history_sample_size", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["evaluation_batches.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "batch_id",
            "index_code",
            name="uq_market_snapshot_batch_index",
        ),
        sa.UniqueConstraint("content_hash"),
    )
    for column in ("batch_id", "index_code", "target_date", "content_hash"):
        op.create_index(
            op.f(f"ix_market_session_snapshots_{column}"),
            "market_session_snapshots",
            [column],
            unique=False,
        )

    op.create_table(
        "opinion_evaluations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("agent_opinion_id", sa.String(length=36), nullable=False),
        sa.Column("evaluation_result_id", sa.String(length=36), nullable=False),
        sa.Column("sign_correct", sa.Boolean(), nullable=True),
        sa.Column("material_direction_correct", sa.Boolean(), nullable=True),
        sa.Column("brier_score", sa.Float(), nullable=False),
        sa.Column("included_in_direction_score", sa.Boolean(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_opinion_id"], ["agent_opinions.id"]),
        sa.ForeignKeyConstraint(["batch_id"], ["evaluation_batches.id"]),
        sa.ForeignKeyConstraint(["evaluation_result_id"], ["evaluation_results.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "batch_id",
            "agent_opinion_id",
            name="uq_opinion_evaluation_identity",
        ),
    )
    for column in ("batch_id", "agent_opinion_id", "evaluation_result_id"):
        op.create_index(
            op.f(f"ix_opinion_evaluations_{column}"),
            "opinion_evaluations",
            [column],
            unique=False,
        )

    op.create_table(
        "forecast_diagnostics",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("forecast_id", sa.String(length=36), nullable=False),
        sa.Column("evaluation_result_id", sa.String(length=36), nullable=False),
        sa.Column("signed_sigma", sa.Float(), nullable=False),
        sa.Column("severity", sa.String(length=24), nullable=False),
        sa.Column("systemic_extreme_down", sa.Boolean(), nullable=False),
        sa.Column("historical_abs_return_percentile", sa.Float(), nullable=True),
        sa.Column("history_sample_size", sa.Integer(), nullable=False),
        sa.Column("data_incomplete", sa.Boolean(), nullable=False),
        sa.Column("sign_correct", sa.Boolean(), nullable=True),
        sa.Column("material_direction_correct", sa.Boolean(), nullable=True),
        sa.Column("brier_score", sa.Float(), nullable=False),
        sa.Column("policy_version", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "severity IN ('noise', 'directional', 'large', 'extreme')",
            name="ck_forecast_diagnostic_severity",
        ),
        sa.ForeignKeyConstraint(["batch_id"], ["evaluation_batches.id"]),
        sa.ForeignKeyConstraint(["evaluation_result_id"], ["evaluation_results.id"]),
        sa.ForeignKeyConstraint(["forecast_id"], ["forecasts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "batch_id",
            "forecast_id",
            name="uq_forecast_diagnostic_identity",
        ),
    )
    for column in (
        "batch_id",
        "forecast_id",
        "evaluation_result_id",
        "severity",
    ):
        op.create_index(
            op.f(f"ix_forecast_diagnostics_{column}"),
            "forecast_diagnostics",
            [column],
            unique=False,
        )

    op.create_table(
        "reflection_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_run_id", sa.String(length=36), nullable=False),
        sa.Column("source_batch_id", sa.String(length=36), nullable=False),
        sa.Column("horizon", sa.String(length=8), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("evaluation_set_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("supersedes_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("source_snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column("output_hash", sa.String(length=64), nullable=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "status IN "
            "('awaiting_sources', 'awaiting_analysis', 'completed', "
            "'failed', 'blocked_upstream')",
            name="ck_reflection_run_status",
        ),
        sa.ForeignKeyConstraint(["source_batch_id"], ["evaluation_batches.id"]),
        sa.ForeignKeyConstraint(["source_run_id"], ["workflow_runs.id"]),
        sa.ForeignKeyConstraint(["supersedes_id"], ["reflection_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_run_id",
            "horizon",
            "target_date",
            "schema_version",
            "evaluation_set_hash",
            name="uq_reflection_run_identity",
        ),
    )
    for column in (
        "source_run_id",
        "source_batch_id",
        "horizon",
        "target_date",
        "evaluation_set_hash",
        "status",
    ):
        op.create_index(
            op.f(f"ix_reflection_runs_{column}"),
            "reflection_runs",
            [column],
            unique=False,
        )

    op.create_table(
        "reflection_findings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("reflection_run_id", sa.String(length=36), nullable=False),
        sa.Column("scope_type", sa.String(length=24), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("index_code", sa.String(length=24), nullable=True),
        sa.Column("horizon", sa.String(length=8), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("primary_error_type", sa.String(length=40), nullable=False),
        sa.Column("secondary_error_types", sa.JSON(), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("availability_class", sa.String(length=40), nullable=False),
        sa.Column("causal_status", sa.String(length=24), nullable=False),
        sa.Column("counterfactual", sa.JSON(), nullable=False),
        sa.Column("remediation", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "scope_type IN ('agent', 'committee', 'market_event')",
            name="ck_reflection_finding_scope",
        ),
        sa.CheckConstraint(
            "verdict IN "
            "('right_reason', 'lucky_correct', 'wrong', 'wrong_noise', "
            "'right_but_noise', 'not_applicable', 'unresolved')",
            name="ck_reflection_finding_verdict",
        ),
        sa.CheckConstraint(
            "availability_class IN "
            "('available_used', 'available_missed', 'coverage_gap_pre_cutoff', "
            "'post_cutoff_event', 'after_close_explanation', 'unresolved')",
            name="ck_reflection_finding_availability",
        ),
        sa.CheckConstraint(
            "causal_status IN ('verified', 'supported', 'hypothesis', 'unresolved')",
            name="ck_reflection_finding_causal_status",
        ),
        sa.CheckConstraint(
            "primary_error_type IN "
            "('data_coverage_failure', 'attention_omission', "
            "'reasoning_or_weighting_failure', 'transmission_mapping', "
            "'horizon_timing', 'post_cutoff_shock', 'risk_plan_failure', "
            "'market_noise', 'unresolved')",
            name="ck_reflection_finding_error_type",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_reflection_finding_confidence",
        ),
        sa.ForeignKeyConstraint(["reflection_run_id"], ["reflection_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "reflection_run_id",
        "scope_type",
        "subject_id",
        "index_code",
        "horizon",
        "verdict",
        "primary_error_type",
        "availability_class",
        "causal_status",
    ):
        op.create_index(
            op.f(f"ix_reflection_findings_{column}"),
            "reflection_findings",
            [column],
            unique=False,
        )

    op.create_table(
        "lesson_proposals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("reflection_run_id", sa.String(length=36), nullable=False),
        sa.Column("episode_key", sa.String(length=120), nullable=False),
        sa.Column("cluster_key", sa.String(length=240), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("proposal_type", sa.String(length=40), nullable=False),
        sa.Column("evidence_finding_ids", sa.JSON(), nullable=False),
        sa.Column("independent_episode_count", sa.Integer(), nullable=False),
        sa.Column("replay_target_dates", sa.Integer(), nullable=False),
        sa.Column("replay_metrics", sa.JSON(), nullable=False),
        sa.Column("half_life_sessions", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("supersedes_id", sa.String(length=36), nullable=True),
        sa.CheckConstraint(
            "status IN ('candidate', 'active', 'challenged', 'retired', 'superseded')",
            name="ck_lesson_proposal_status",
        ),
        sa.ForeignKeyConstraint(["reflection_run_id"], ["reflection_runs.id"]),
        sa.ForeignKeyConstraint(["supersedes_id"], ["lesson_proposals.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "reflection_run_id",
            "cluster_key",
            name="uq_lesson_proposal_episode_cluster",
        ),
    )
    for column in (
        "reflection_run_id",
        "episode_key",
        "cluster_key",
        "status",
        "proposal_type",
    ):
        op.create_index(
            op.f(f"ix_lesson_proposals_{column}"),
            "lesson_proposals",
            [column],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("lesson_proposals")
    op.drop_table("reflection_findings")
    op.drop_table("reflection_runs")
    op.drop_table("forecast_diagnostics")
    op.drop_table("opinion_evaluations")
    op.drop_table("market_session_snapshots")
    op.drop_table("evaluation_batches")
