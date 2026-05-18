"""add writers table and writer_id to jobs

Revision ID: 20260518_000006
Revises: 20260506_000005
Create Date: 2026-05-18 12:00:00
"""

from __future__ import annotations

import os
import time
import uuid

from alembic import op
import sqlalchemy as sa


revision = "20260518_000006"
down_revision = "20260506_000005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Create writers table ---
    op.create_table(
        "writers",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )

    # --- Add writer_id to web_jobs ---
    op.add_column(
        "web_jobs",
        sa.Column("writer_id", sa.String(length=32), nullable=True),
    )

    # --- Add writer_id to job_runtime_summaries ---
    op.add_column(
        "job_runtime_summaries",
        sa.Column("writer_id", sa.String(length=32), nullable=True),
    )

    # --- Backfill: create default writer and assign all existing jobs ---
    default_email = os.environ.get(
        "ESSAY_WRITER_DEFAULT_WRITER_EMAIL", "admin@essaywriter.local"
    )
    default_name = os.environ.get("ESSAY_WRITER_DEFAULT_WRITER_NAME", "Admin")
    default_id = uuid.uuid4().hex[:32]
    now = time.time()

    bind = op.get_bind()
    bind.execute(
        sa.text(
            "INSERT INTO writers (id, email, name, is_active, created_at, updated_at) "
            "VALUES (:id, :email, :name, :is_active, :created_at, :updated_at)"
        ),
        {
            "id": default_id,
            "email": default_email.strip().lower(),
            "name": default_name.strip(),
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        },
    )
    bind.execute(
        sa.text("UPDATE web_jobs SET writer_id = :wid WHERE writer_id IS NULL"),
        {"wid": default_id},
    )
    bind.execute(
        sa.text(
            "UPDATE job_runtime_summaries SET writer_id = :wid WHERE writer_id IS NULL"
        ),
        {"wid": default_id},
    )


def downgrade() -> None:
    op.drop_column("job_runtime_summaries", "writer_id")
    op.drop_column("web_jobs", "writer_id")
    op.drop_table("writers")
