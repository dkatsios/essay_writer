"""add job control request fields

Revision ID: 20260506_000005
Revises: 20250501_000004
Create Date: 2026-05-06 12:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260506_000005"
down_revision = "20250501_000004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_jobs",
        sa.Column(
            "cancel_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "web_jobs",
        sa.Column(
            "delete_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("web_jobs", "delete_requested")
    op.drop_column("web_jobs", "cancel_requested")
