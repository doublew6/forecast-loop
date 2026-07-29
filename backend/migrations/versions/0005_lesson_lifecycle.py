"""Add append-only Lesson replay and lifecycle audit records.

Revision ID: 0005_lesson_lifecycle
Revises: 0004_reflection_review_gate
Create Date: 2026-07-17
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op

revision = "0005_lesson_lifecycle"
down_revision = "0004_reflection_review_gate"
branch_labels = None
depends_on = None

_TABLES = {
    "lesson_episodes",
    "lesson_replay_batches",
    "lesson_lifecycle_events",
}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    present = set(inspector.get_table_names()) & _TABLES
    if present:
        if present != _TABLES:
            missing = ", ".join(sorted(_TABLES - present))
            raise RuntimeError(
                f"partial Lesson lifecycle schema detected; missing: {missing}"
            )
        return
    lesson_indexes = {
        item["name"] for item in inspector.get_indexes("lesson_proposals")
    }
    if "uq_lesson_active_cluster_head" not in lesson_indexes:
        op.create_index(
            "uq_lesson_active_cluster_head",
            "lesson_proposals",
            ["cluster_key"],
            unique=True,
            sqlite_where=sa.text("status IN ('active', 'challenged')"),
            postgresql_where=sa.text("status IN ('active', 'challenged')"),
        )
    op.create_table(
        "lesson_episodes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("cluster_key", sa.String(length=240), nullable=False),
        sa.Column("episode_key", sa.String(length=120), nullable=False),
        sa.Column("first_reflection_run_id", sa.String(length=36), nullable=False),
        sa.Column("evidence_set_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["first_reflection_run_id"], ["reflection_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cluster_key",
            "episode_key",
            name="uq_lesson_episode_cluster_date",
        ),
    )
    for column in (
        "cluster_key",
        "episode_key",
        "first_reflection_run_id",
        "evidence_set_hash",
        "created_at",
    ):
        op.create_index(
            op.f(f"ix_lesson_episodes_{column}"),
            "lesson_episodes",
            [column],
            unique=False,
        )
    bind = op.get_bind()
    episode_groups = bind.execute(
        sa.text(
            "SELECT DISTINCT cluster_key, episode_key FROM lesson_proposals"
        )
    ).mappings()
    for group in episode_groups:
        row = bind.execute(
            sa.text(
                "SELECT lesson_proposals.reflection_run_id AS reflection_run_id, "
                "reflection_runs.evaluation_set_hash AS evidence_set_hash, "
                "lesson_proposals.created_at AS created_at "
                "FROM lesson_proposals "
                "JOIN reflection_runs "
                "ON reflection_runs.id = lesson_proposals.reflection_run_id "
                "WHERE lesson_proposals.cluster_key = :cluster_key "
                "AND lesson_proposals.episode_key = :episode_key "
                "ORDER BY lesson_proposals.created_at, lesson_proposals.id LIMIT 1"
            ),
            {
                "cluster_key": group["cluster_key"],
                "episode_key": group["episode_key"],
            },
        ).mappings().one()
        deterministic_id = str(
            uuid5(
                NAMESPACE_URL,
                "vericouncil:lesson-episode:"
                f"{group['cluster_key']}:{group['episode_key']}",
            )
        )
        bind.execute(
            sa.text(
                "INSERT INTO lesson_episodes "
                "(id, cluster_key, episode_key, first_reflection_run_id, "
                "evidence_set_hash, created_at) "
                "VALUES (:id, :cluster_key, :episode_key, :reflection_run_id, "
                ":evidence_set_hash, :created_at)"
            ),
            {
                "id": deterministic_id,
                "cluster_key": group["cluster_key"],
                "episode_key": group["episode_key"],
                "reflection_run_id": row["reflection_run_id"],
                "evidence_set_hash": row["evidence_set_hash"],
                "created_at": row["created_at"],
            },
        )

    op.create_table(
        "lesson_replay_batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("lesson_proposal_id", sa.String(length=36), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("observations", sa.JSON(), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("distinct_target_dates", sa.Integer(), nullable=False),
        sa.Column("aggregate_metrics", sa.JSON(), nullable=False),
        sa.Column("submitted_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["lesson_proposal_id"], ["lesson_proposals.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "lesson_proposal_id",
            "content_hash",
            name="uq_lesson_replay_batch_content",
        ),
    )
    for column in ("lesson_proposal_id", "content_hash", "created_at"):
        op.create_index(
            op.f(f"ix_lesson_replay_batches_{column}"),
            "lesson_replay_batches",
            [column],
            unique=False,
        )

    op.create_table(
        "lesson_lifecycle_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("lesson_proposal_id", sa.String(length=36), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("event_key", sa.String(length=160), nullable=False),
        sa.Column("from_status", sa.String(length=24), nullable=False),
        sa.Column("to_status", sa.String(length=24), nullable=False),
        sa.Column("actor", sa.String(length=120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurred_at_canonical", sa.String(length=40), nullable=False),
        sa.CheckConstraint(
            "event_type IN "
            "('replay_recorded', 'approved', 'revalidated', "
            "'challenged', 'retired', 'superseded')",
            name="ck_lesson_lifecycle_event_type",
        ),
        sa.CheckConstraint(
            "from_status IN "
            "('candidate', 'active', 'challenged', 'retired', 'superseded')",
            name="ck_lesson_lifecycle_event_from_status",
        ),
        sa.CheckConstraint(
            "to_status IN "
            "('candidate', 'active', 'challenged', 'retired', 'superseded')",
            name="ck_lesson_lifecycle_event_to_status",
        ),
        sa.ForeignKeyConstraint(["lesson_proposal_id"], ["lesson_proposals.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "lesson_proposal_id",
            "event_key",
            name="uq_lesson_lifecycle_event_key",
        ),
        sa.UniqueConstraint(
            "lesson_proposal_id",
            "sequence_number",
            name="uq_lesson_lifecycle_event_sequence",
        ),
    )
    for column in ("lesson_proposal_id", "event_type", "occurred_at"):
        op.create_index(
            op.f(f"ix_lesson_lifecycle_events_{column}"),
            "lesson_lifecycle_events",
            [column],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("lesson_lifecycle_events")
    op.drop_table("lesson_replay_batches")
    op.drop_table("lesson_episodes")
    op.drop_index("uq_lesson_active_cluster_head", table_name="lesson_proposals")
