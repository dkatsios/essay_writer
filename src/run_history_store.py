"""Persistent storage for run summaries, step metrics, and artifact metadata."""

from __future__ import annotations

import mimetypes
import time
from pathlib import Path
from typing import Any

from sqlalchemy import Boolean, Column, Float, Integer, String, Table, Text
from sqlalchemy import create_engine, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from config.settings import load_config
from src.job_store import _metadata


_runtime_summaries_table = Table(
    "job_runtime_summaries",
    _metadata,
    Column("job_id", String(32), primary_key=True),
    Column("status", String(32), nullable=False, default=""),
    Column("provider", Text, nullable=False, default=""),
    Column("total_cost_usd", Float, nullable=False, default=0.0),
    Column("total_input_tokens", Integer, nullable=False, default=0),
    Column("total_output_tokens", Integer, nullable=False, default=0),
    Column("total_thinking_tokens", Integer, nullable=False, default=0),
    Column("total_duration_seconds", Float, nullable=False, default=0.0),
    Column("step_count", Integer, nullable=False, default=0),
    Column("registered_source_count", Integer, nullable=True),
    Column("scored_source_count", Integer, nullable=True),
    Column("above_threshold_source_count", Integer, nullable=True),
    Column("selected_source_count", Integer, nullable=True),
    Column("selected_full_text_count", Integer, nullable=True),
    Column("selected_abstract_only_count", Integer, nullable=True),
    Column("cited_source_count", Integer, nullable=True),
    Column("target_words", Integer, nullable=True),
    Column("draft_words", Integer, nullable=True),
    Column("final_words", Integer, nullable=True),
    Column("updated_at", Float, nullable=False),
)

_step_metrics_table = Table(
    "job_step_metrics",
    _metadata,
    Column("job_id", String(32), primary_key=True),
    Column("step_name", String(128), primary_key=True),
    Column("status", String(16), nullable=False, default="completed"),
    Column("model", Text, nullable=False, default=""),
    Column("cost_usd", Float, nullable=False, default=0.0),
    Column("call_count", Integer, nullable=False, default=0),
    Column("input_tokens", Integer, nullable=False, default=0),
    Column("output_tokens", Integer, nullable=False, default=0),
    Column("thinking_tokens", Integer, nullable=False, default=0),
    Column("duration_seconds", Float, nullable=False, default=0.0),
    Column("step_index", Integer, nullable=True),
    Column("step_count", Integer, nullable=True),
    Column("updated_at", Float, nullable=False),
)

_artifacts_table = Table(
    "job_artifacts",
    _metadata,
    Column("job_id", String(32), primary_key=True),
    Column("relative_path", Text, primary_key=True),
    Column("artifact_type", String(64), nullable=False, default="artifact"),
    Column(
        "mime_type",
        String(128),
        nullable=False,
        default="application/octet-stream",
    ),
    Column("size_bytes", Integer, nullable=False, default=0),
    Column("is_available", Boolean, nullable=False, default=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("deleted_at", Float, nullable=True),
)


def _artifact_type_for_path(relative_path: str) -> str:
    if relative_path == "checkpoint.json":
        return "checkpoint"
    if relative_path == "report.md":
        return "report"
    if relative_path == "run.log":
        return "log"
    if relative_path == "essay.docx":
        return "document"
    if relative_path == "brief/assignment.json":
        return "assignment_brief"
    if relative_path == "brief/validation.json":
        return "validation"
    if relative_path == "plan/plan.json":
        return "essay_plan"
    if relative_path == "plan/source_assignments.json":
        return "source_assignments"
    if relative_path == "sources/registry.json":
        return "source_registry"
    if relative_path == "sources/scores.json":
        return "source_scores"
    if relative_path == "sources/selected.json":
        return "selected_sources"
    if relative_path == "essay/draft.md":
        return "draft"
    if relative_path == "essay/reviewed.md":
        return "reviewed"
    if relative_path == "essay/reconciliation.json":
        return "reconciliation"
    if relative_path.startswith("sources/notes/") and relative_path.endswith(".json"):
        return "source_note"
    if relative_path.startswith("sources/user/"):
        return "user_source"
    if relative_path.startswith("sources/supplement/"):
        return "supplemental_source"
    if relative_path.startswith("essay/sections/") and relative_path.endswith(".md"):
        return "section_draft"
    if relative_path.startswith("essay/reviewed/") and relative_path.endswith(".md"):
        return "section_reviewed"
    if relative_path.startswith("uploads/"):
        return "upload"
    if relative_path.startswith("user_sources/"):
        return "user_source_upload"
    return "artifact"


def _mime_type_for_path(relative_path: str) -> str:
    guessed, _ = mimetypes.guess_type(relative_path)
    return guessed or "application/octet-stream"


class RunHistoryStore:
    """SQL-backed store for run history metadata."""

    def __init__(self) -> None:
        self._engine: Engine | None = None
        self._session_factory: sessionmaker[Session] | None = None
        self._engine_url: str | None = None

    def _ensure_session_factory(self) -> sessionmaker[Session]:
        config = load_config()
        url = config.database.url
        if self._session_factory is not None and self._engine_url == url:
            return self._session_factory

        if self._engine is not None:
            self._engine.dispose()

        connect_args: dict[str, Any] = {}
        if url.startswith("sqlite"):
            connect_args["check_same_thread"] = False

        self._engine = create_engine(
            url,
            future=True,
            echo=config.database.echo,
            connect_args=connect_args,
        )
        self._engine_url = url
        self._session_factory = sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            future=True,
        )
        return self._session_factory

    def _session(self) -> Session:
        return self._ensure_session_factory()()

    def save_runtime_summary(self, job_id: str, **payload: Any) -> None:
        current_time = float(payload.pop("updated_at", time.time()))
        record = {"job_id": job_id, "updated_at": current_time, **payload}
        with self._session() as session:
            existing = session.execute(
                select(_runtime_summaries_table.c.job_id).where(
                    _runtime_summaries_table.c.job_id == job_id
                )
            ).scalar_one_or_none()
            if existing is None:
                session.execute(insert(_runtime_summaries_table).values(**record))
            else:
                session.execute(
                    update(_runtime_summaries_table)
                    .where(_runtime_summaries_table.c.job_id == job_id)
                    .values(**record)
                )
            session.commit()

    def get_runtime_summary(self, job_id: str) -> dict[str, Any] | None:
        with self._session() as session:
            row = (
                session.execute(
                    select(_runtime_summaries_table).where(
                        _runtime_summaries_table.c.job_id == job_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return dict(row) if row is not None else None

    def save_step_metric(self, job_id: str, step_name: str, **payload: Any) -> None:
        current_time = float(payload.pop("updated_at", time.time()))
        record = {
            "job_id": job_id,
            "step_name": step_name,
            "updated_at": current_time,
            **payload,
        }
        with self._session() as session:
            existing = session.execute(
                select(_step_metrics_table.c.job_id).where(
                    _step_metrics_table.c.job_id == job_id,
                    _step_metrics_table.c.step_name == step_name,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.execute(insert(_step_metrics_table).values(**record))
            else:
                session.execute(
                    update(_step_metrics_table)
                    .where(_step_metrics_table.c.job_id == job_id)
                    .where(_step_metrics_table.c.step_name == step_name)
                    .values(**record)
                )
            session.commit()

    def list_step_metrics(self, job_id: str) -> list[dict[str, Any]]:
        with self._session() as session:
            rows = (
                session.execute(
                    select(_step_metrics_table)
                    .where(_step_metrics_table.c.job_id == job_id)
                    .order_by(_step_metrics_table.c.step_name.asc())
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    def sync_artifacts(
        self,
        job_id: str,
        run_dir: Path,
        *,
        current_time: float | None = None,
    ) -> list[dict[str, Any]]:
        synced_at = time.time() if current_time is None else current_time
        discovered = []
        if run_dir.exists():
            for path in sorted(run_dir.rglob("*")):
                if not path.is_file():
                    continue
                relative_path = path.relative_to(run_dir).as_posix()
                discovered.append(
                    {
                        "relative_path": relative_path,
                        "artifact_type": _artifact_type_for_path(relative_path),
                        "mime_type": _mime_type_for_path(relative_path),
                        "size_bytes": path.stat().st_size,
                        "is_available": True,
                        "created_at": synced_at,
                        "updated_at": synced_at,
                        "deleted_at": None,
                    }
                )

        discovered_by_path = {item["relative_path"]: item for item in discovered}

        with self._session() as session:
            existing_rows = (
                session.execute(
                    select(_artifacts_table).where(_artifacts_table.c.job_id == job_id)
                )
                .mappings()
                .all()
            )
            existing_by_path = {
                str(row["relative_path"]): dict(row) for row in existing_rows
            }

            for relative_path, payload in discovered_by_path.items():
                existing = existing_by_path.get(relative_path)
                values = {"job_id": job_id, **payload}
                if existing is None:
                    session.execute(insert(_artifacts_table).values(**values))
                else:
                    values["created_at"] = float(existing["created_at"])
                    session.execute(
                        update(_artifacts_table)
                        .where(_artifacts_table.c.job_id == job_id)
                        .where(_artifacts_table.c.relative_path == relative_path)
                        .values(**values)
                    )

            for relative_path, existing in existing_by_path.items():
                if relative_path in discovered_by_path or not existing["is_available"]:
                    continue
                session.execute(
                    update(_artifacts_table)
                    .where(_artifacts_table.c.job_id == job_id)
                    .where(_artifacts_table.c.relative_path == relative_path)
                    .values(
                        is_available=False,
                        updated_at=synced_at,
                        deleted_at=synced_at,
                    )
                )

            session.commit()

        return self.list_artifacts(job_id)

    def mark_artifacts_deleted(
        self, job_id: str, *, current_time: float | None = None
    ) -> None:
        deleted_at = time.time() if current_time is None else current_time
        with self._session() as session:
            session.execute(
                update(_artifacts_table)
                .where(_artifacts_table.c.job_id == job_id)
                .where(_artifacts_table.c.is_available.is_(True))
                .values(
                    is_available=False,
                    updated_at=deleted_at,
                    deleted_at=deleted_at,
                )
            )
            session.commit()

    def list_artifacts(self, job_id: str) -> list[dict[str, Any]]:
        with self._session() as session:
            rows = (
                session.execute(
                    select(_artifacts_table)
                    .where(_artifacts_table.c.job_id == job_id)
                    .order_by(_artifacts_table.c.relative_path.asc())
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    def reset_for_tests(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
        self._engine = None
        self._session_factory = None
        self._engine_url = None


run_history = RunHistoryStore()
