"""create web_jobs table

Revision ID: 20250501_000001
Revises:
Create Date: 2026-05-01 00:00:01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20250501_000001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_jobs",
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("run_dir", sa.Text(), nullable=False),
        sa.Column("questions", sa.JSON(), nullable=True),
        sa.Column("answers", sa.Text(), nullable=False, server_default=""),
        sa.Column("optional_pdf_items", sa.JSON(), nullable=True),
        sa.Column("optional_pdf_allowed_ids", sa.JSON(), nullable=True),
        sa.Column("source_shortfall", sa.JSON(), nullable=True),
        sa.Column(
            "source_shortfall_decision", sa.Text(), nullable=False, server_default=""
        ),
        sa.Column("source_shortfall_added_ids", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("academic_level", sa.Text(), nullable=False, server_default=""),
        sa.Column("submit_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("target_words", sa.Integer(), nullable=True),
        sa.Column("min_sources", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.Column("clarification_rounds", sa.JSON(), nullable=False),
        sa.Column("optional_pdf_rounds", sa.JSON(), nullable=False),
        sa.Column("optional_pdf_choices", sa.JSON(), nullable=False),
        sa.Column(
            "fast_track", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("provider", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("job_id"),
    )


def downgrade() -> None:
    op.drop_table("web_jobs")
