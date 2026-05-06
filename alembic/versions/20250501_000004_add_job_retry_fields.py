"""add job retry fields

Revision ID: 20250501_000004
Revises: 20250501_000003
Create Date: 2026-05-05 18:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20250501_000004"
down_revision = "20250501_000003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_jobs",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "web_jobs",
        sa.Column("not_before", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("web_jobs", "not_before")
    op.drop_column("web_jobs", "retry_count")
