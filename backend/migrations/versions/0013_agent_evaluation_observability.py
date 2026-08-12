"""Add Agent evaluation, trace, and bad-case governance records.

Revision ID: 0013_agent_eval_observability
Revises: 0012_user_judgment_revisions
Create Date: 2026-08-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_agent_eval_observability"
down_revision = "0012_user_judgment_revisions"
branch_labels = None
depends_on = None


_TABLES = {
    "agent_traces",
    "agent_trace_spans",
    "agent_eval_experiments",
    "agent_eval_tasks",
    "agent_eval_results",
    "agent_bad_cases",
    "agent_bad_case_events",
}


def upgrade() -> None:
    present = set(sa.inspect(op.get_bind()).get_table_names()).intersection(_TABLES)
    if present and present != _TABLES:
        missing = ", ".join(sorted(_TABLES - present))
        raise RuntimeError(
            f"partial Agent evaluation and observability schema; missing: {missing}"
        )
    if present:
        return
    _create_tables()


def _create_tables() -> None:
    op.create_table(
        "agent_traces",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("workflow_kind", sa.String(24), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(24), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_hash", sa.String(64), nullable=True),
        sa.Column("trace_policy_version", sa.String(32), nullable=False),
        sa.Column("telemetry_complete", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(120), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.UniqueConstraint("workflow_kind", "subject_id", name="uq_agent_trace_subject"),
        sa.CheckConstraint(
            "workflow_kind IN ('prediction', 'reflection', 'agent_eval')",
            name="ck_agent_trace_workflow_kind",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'degraded')",
            name="ck_agent_trace_status",
        ),
    )
    for column in ("workflow_kind", "subject_id", "mode", "status", "started_at", "input_hash"):
        op.create_index(f"ix_agent_traces_{column}", "agent_traces", [column])

    op.create_table(
        "agent_trace_spans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "trace_id",
            sa.String(32),
            sa.ForeignKey("agent_traces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("span_id", sa.String(16), nullable=False),
        sa.Column("parent_span_id", sa.String(16), nullable=True),
        sa.Column("node_id", sa.String(120), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("span_kind", sa.String(24), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("agent_id", sa.String(64), nullable=True),
        sa.Column("agent_version", sa.String(32), nullable=True),
        sa.Column("model_name", sa.String(160), nullable=True),
        sa.Column("prompt_version", sa.String(80), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=True),
        sa.Column("input_digest", sa.String(64), nullable=True),
        sa.Column("output_digest", sa.String(64), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(120), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.UniqueConstraint("trace_id", "span_id", name="uq_agent_trace_span_identity"),
        sa.CheckConstraint(
            "span_kind IN ('workflow', 'agent', 'llm', 'validator', 'persistence', 'external')",
            name="ck_agent_trace_span_kind",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="ck_agent_trace_span_status",
        ),
    )
    for column in (
        "trace_id",
        "span_id",
        "node_id",
        "span_kind",
        "status",
        "started_at",
        "agent_id",
        "model_name",
    ):
        op.create_index(f"ix_agent_trace_spans_{column}", "agent_trace_spans", [column])

    op.create_table(
        "agent_eval_experiments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("suite_id", sa.String(120), nullable=False),
        sa.Column("suite_version", sa.String(32), nullable=False),
        sa.Column("suite_hash", sa.String(64), nullable=False),
        sa.Column("baseline_target_id", sa.String(120), nullable=False),
        sa.Column("baseline_target_hash", sa.String(64), nullable=False),
        sa.Column("candidate_target_id", sa.String(120), nullable=False),
        sa.Column("candidate_target_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("release_decision", sa.String(32), nullable=False),
        sa.Column("policy_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("report_hash", sa.String(64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')",
            name="ck_agent_eval_experiment_status",
        ),
        sa.CheckConstraint(
            "release_decision IN ('pending', 'pass', 'fail', 'insufficient_sample')",
            name="ck_agent_eval_release_decision",
        ),
    )
    for column in ("suite_id", "suite_hash", "status", "release_decision", "created_at"):
        op.create_index(f"ix_agent_eval_experiments_{column}", "agent_eval_experiments", [column])

    op.create_table(
        "agent_eval_tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "experiment_id",
            sa.String(36),
            sa.ForeignKey("agent_eval_experiments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(120), nullable=True),
        sa.Column("lease_token", sa.String(36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("experiment_id", name="uq_agent_eval_task_experiment"),
        sa.UniqueConstraint("idempotency_key", name="uq_agent_eval_task_idempotency"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'completed', 'failed')",
            name="ck_agent_eval_task_status",
        ),
    )
    for column in ("experiment_id", "status", "available_at", "lease_expires_at"):
        op.create_index(f"ix_agent_eval_tasks_{column}", "agent_eval_tasks", [column])

    op.create_table(
        "agent_eval_results",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "experiment_id",
            sa.String(36),
            sa.ForeignKey("agent_eval_experiments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("arm", sa.String(16), nullable=False),
        sa.Column("case_id", sa.String(160), nullable=False),
        sa.Column("evaluator_id", sa.String(160), nullable=False),
        sa.Column("evaluator_version", sa.String(32), nullable=False),
        sa.Column("metric_kind", sa.String(64), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("label", sa.String(120), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("output_hash", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(32), sa.ForeignKey("agent_traces.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "experiment_id",
            "arm",
            "case_id",
            "evaluator_id",
            name="uq_agent_eval_result_identity",
        ),
        sa.CheckConstraint("arm IN ('baseline', 'candidate')", name="ck_agent_eval_arm"),
        sa.CheckConstraint(
            "status IN ('passed', 'failed', 'not_applicable', 'error')",
            name="ck_agent_eval_result_status",
        ),
    )
    for column in (
        "experiment_id",
        "arm",
        "case_id",
        "evaluator_id",
        "metric_kind",
        "status",
        "trace_id",
        "created_at",
    ):
        op.create_index(f"ix_agent_eval_results_{column}", "agent_eval_results", [column])

    op.create_table(
        "agent_bad_cases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("trace_id", sa.String(32), sa.ForeignKey("agent_traces.id"), nullable=False),
        sa.Column("span_id", sa.String(16), nullable=True),
        sa.Column(
            "eval_result_id", sa.String(36), sa.ForeignKey("agent_eval_results.id"), nullable=True
        ),
        sa.Column("workflow_kind", sa.String(24), nullable=False),
        sa.Column("issue_type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("expected_behavior", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=True),
        sa.Column("dedupe_hash", sa.String(64), nullable=False),
        sa.Column("dataset_id", sa.String(120), nullable=True),
        sa.Column("dataset_version", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dedupe_hash", name="uq_agent_bad_case_dedupe"),
        sa.CheckConstraint(
            "status IN ('detected', 'triaged', 'confirmed', 'materialized', "
            "'resolved', 'rejected')",
            name="ck_agent_bad_case_status",
        ),
        sa.CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')",
            name="ck_agent_bad_case_severity",
        ),
    )
    for column in (
        "trace_id",
        "workflow_kind",
        "issue_type",
        "severity",
        "status",
        "created_at",
        "updated_at",
    ):
        op.create_index(f"ix_agent_bad_cases_{column}", "agent_bad_cases", [column])

    op.create_table(
        "agent_bad_case_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "bad_case_id",
            sa.String(36),
            sa.ForeignKey("agent_bad_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(24), nullable=False),
        sa.Column("from_status", sa.String(24), nullable=True),
        sa.Column("to_status", sa.String(24), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("previous_event_hash", sa.String(64), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("bad_case_id", "sequence_number", name="uq_bad_case_event_seq"),
        sa.UniqueConstraint("bad_case_id", "idempotency_key", name="uq_bad_case_event_key"),
        sa.CheckConstraint(
            "event_type IN ('detected', 'triaged', 'confirmed', 'materialized', "
            "'resolved', 'rejected')",
            name="ck_agent_bad_case_event_type",
        ),
    )
    for column in ("bad_case_id", "event_type", "content_hash", "occurred_at"):
        op.create_index(f"ix_agent_bad_case_events_{column}", "agent_bad_case_events", [column])


def downgrade() -> None:
    op.drop_table("agent_bad_case_events")
    op.drop_table("agent_bad_cases")
    op.drop_table("agent_eval_results")
    op.drop_table("agent_eval_tasks")
    op.drop_table("agent_eval_experiments")
    op.drop_table("agent_trace_spans")
    op.drop_table("agent_traces")
