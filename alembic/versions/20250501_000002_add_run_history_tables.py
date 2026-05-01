"""add run history tables

Revision ID: 20250501_000002
Revises: 20250501_000001
Create Date: 2026-05-01 18:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20250501_000002"
down_revision = "20250501_000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_runtime_summaries",
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("provider", sa.Text(), nullable=False, server_default=""),
        sa.Column("total_cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "total_input_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "total_output_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "total_thinking_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "total_duration_seconds", sa.Float(), nullable=False, server_default="0"
        ),
        sa.Column("step_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("registered_source_count", sa.Integer(), nullable=True),
        sa.Column("scored_source_count", sa.Integer(), nullable=True),
        sa.Column("above_threshold_source_count", sa.Integer(), nullable=True),
        sa.Column("selected_source_count", sa.Integer(), nullable=True),
        sa.Column("selected_full_text_count", sa.Integer(), nullable=True),
        sa.Column("selected_abstract_only_count", sa.Integer(), nullable=True),
        sa.Column("cited_source_count", sa.Integer(), nullable=True),
        sa.Column("target_words", sa.Integer(), nullable=True),
        sa.Column("draft_words", sa.Integer(), nullable=True),
        sa.Column("final_words", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_index(
        "ix_job_runtime_summaries_status",
        "job_runtime_summaries",
        ["status"],
    )

    op.create_table(
        "job_step_metrics",
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("step_name", sa.String(length=128), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="completed"
        ),
        sa.Column("model", sa.Text(), nullable=False, server_default=""),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("call_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("thinking_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("step_index", sa.Integer(), nullable=True),
        sa.Column("step_count", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("job_id", "step_name"),
    )
    op.create_index(
        "ix_job_step_metrics_job_id",
        "job_step_metrics",
        ["job_id"],
    )

    op.create_table(
        "job_artifacts",
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column(
            "artifact_type",
            sa.String(length=64),
            nullable=False,
            server_default="artifact",
        ),
        sa.Column(
            "mime_type",
            sa.String(length=128),
            nullable=False,
            server_default="application/octet-stream",
        ),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "is_available", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("deleted_at", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("job_id", "relative_path"),
    )
    op.create_index(
        "ix_job_artifacts_job_id",
        "job_artifacts",
        ["job_id"],
    )
    op.create_index(
        "ix_job_artifacts_availability",
        "job_artifacts",
        ["job_id", "is_available"],
    )


def downgrade() -> None:
    op.drop_index("ix_job_artifacts_availability", table_name="job_artifacts")
    op.drop_index("ix_job_artifacts_job_id", table_name="job_artifacts")
    op.drop_table("job_artifacts")
    op.drop_index("ix_job_step_metrics_job_id", table_name="job_step_metrics")
    op.drop_table("job_step_metrics")
    op.drop_index("ix_job_runtime_summaries_status", table_name="job_runtime_summaries")
    op.drop_table("job_runtime_summaries")
