"""add job execution ownership fields

Revision ID: 20250501_000003
Revises: 20250501_000002
Create Date: 2026-05-01 23:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20250501_000003"
down_revision = "20250501_000002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("web_jobs", sa.Column("worker_id", sa.Text(), nullable=True))
    op.add_column("web_jobs", sa.Column("leased_at", sa.Float(), nullable=True))
    op.add_column(
        "web_jobs",
        sa.Column("lease_expires_at", sa.Float(), nullable=True),
    )
    op.add_column(
        "web_jobs",
        sa.Column("current_step", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column("web_jobs", sa.Column("step_index", sa.Integer(), nullable=True))
    op.add_column("web_jobs", sa.Column("step_count", sa.Integer(), nullable=True))
    op.create_index(
        "ix_web_jobs_lease_expires_at",
        "web_jobs",
        ["lease_expires_at"],
    )
    op.create_index(
        "ix_web_jobs_status_created_at",
        "web_jobs",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_web_jobs_status_created_at", table_name="web_jobs")
    op.drop_index("ix_web_jobs_lease_expires_at", table_name="web_jobs")
    op.drop_column("web_jobs", "step_count")
    op.drop_column("web_jobs", "step_index")
    op.drop_column("web_jobs", "current_step")
    op.drop_column("web_jobs", "lease_expires_at")
    op.drop_column("web_jobs", "leased_at")
    op.drop_column("web_jobs", "worker_id")
