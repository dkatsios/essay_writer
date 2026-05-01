"""Persistent storage for web job state.

Phase 1 keeps local run artifacts on disk and stores only job metadata/state
in a SQL database. The store is Postgres-compatible and defaults to a local
SQLite file for development and tests when no database URL is configured.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, Float, Integer, MetaData, String, Table, Text
from sqlalchemy import Column, create_engine, delete, insert, select, update
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.orm import Session, sessionmaker

from config.settings import load_config
from src.runtime import TokenTracker

if TYPE_CHECKING:
    from src.web_jobs import Job


_metadata = MetaData()

_jobs_table = Table(
    "web_jobs",
    _metadata,
    Column("job_id", String(32), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("run_dir", Text, nullable=False),
    Column("questions", JSON, nullable=True),
    Column("answers", Text, nullable=False, default=""),
    Column("optional_pdf_items", JSON, nullable=True),
    Column("optional_pdf_allowed_ids", JSON, nullable=True),
    Column("source_shortfall", JSON, nullable=True),
    Column("source_shortfall_decision", Text, nullable=False, default=""),
    Column("source_shortfall_added_ids", JSON, nullable=False, default=list),
    Column("error", Text, nullable=False, default=""),
    Column("academic_level", Text, nullable=False, default=""),
    Column("submit_prompt", Text, nullable=False, default=""),
    Column("target_words", Integer, nullable=True),
    Column("min_sources", Integer, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("finished_at", Float, nullable=True),
    Column("clarification_rounds", JSON, nullable=False, default=list),
    Column("optional_pdf_rounds", JSON, nullable=False, default=list),
    Column("optional_pdf_choices", JSON, nullable=False, default=dict),
    Column("fast_track", Boolean, nullable=False, default=False),
    Column("provider", Text, nullable=False, default=""),
)


@dataclass
class JobTransients:
    answers_event: asyncio.Event = field(default_factory=asyncio.Event)
    optional_pdf_event: asyncio.Event = field(default_factory=asyncio.Event)
    source_shortfall_event: asyncio.Event = field(default_factory=asyncio.Event)
    sse_event: asyncio.Event = field(default_factory=asyncio.Event)
    tracker: TokenTracker | None = None


class JobStore:
    """Mapping-like interface backed by SQL storage for durable job state."""

    def __init__(self) -> None:
        self._engine: Engine | None = None
        self._session_factory: sessionmaker[Session] | None = None
        self._engine_url: str | None = None
        self._transients: dict[str, JobTransients] = {}
        self._live_jobs: dict[str, Job] = {}

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

    def _remember_transients(self, job: Job) -> None:
        transient = self._transients.setdefault(job.job_id, JobTransients())
        transient.answers_event = job.answers_event
        transient.optional_pdf_event = job.optional_pdf_event
        transient.source_shortfall_event = job.source_shortfall_event
        transient.sse_event = job._sse_event
        transient.tracker = job.tracker
        self._live_jobs[job.job_id] = job

    def _serialize_job(self, job: Job) -> dict[str, Any]:
        self._remember_transients(job)
        return {
            "job_id": job.job_id,
            "status": job.status,
            "run_dir": str(job.run_dir),
            "questions": job.questions,
            "answers": job.answers,
            "optional_pdf_items": job.optional_pdf_items,
            "optional_pdf_allowed_ids": sorted(job.optional_pdf_allowed_ids)
            if job.optional_pdf_allowed_ids is not None
            else None,
            "source_shortfall": job.source_shortfall,
            "source_shortfall_decision": job.source_shortfall_decision,
            "source_shortfall_added_ids": list(job.source_shortfall_added_ids),
            "error": job.error,
            "academic_level": job.academic_level,
            "submit_prompt": job.submit_prompt,
            "target_words": job.target_words,
            "min_sources": job.min_sources,
            "created_at": job.created_at,
            "finished_at": job.finished_at,
            "clarification_rounds": list(job.clarification_rounds),
            "optional_pdf_rounds": list(job.optional_pdf_rounds),
            "optional_pdf_choices": dict(job.optional_pdf_choices),
            "fast_track": job.fast_track,
            "provider": job.provider,
        }

    def _hydrate_job(self, row: RowMapping) -> Job:
        from src.web_jobs import Job

        transient = self._transients.setdefault(row["job_id"], JobTransients())
        return Job(
            job_id=row["job_id"],
            status=row["status"],
            run_dir=Path(row["run_dir"]),
            questions=row["questions"],
            answers_event=transient.answers_event,
            answers=row["answers"] or "",
            optional_pdf_items=row["optional_pdf_items"],
            optional_pdf_allowed_ids=frozenset(row["optional_pdf_allowed_ids"])
            if row["optional_pdf_allowed_ids"]
            else None,
            optional_pdf_event=transient.optional_pdf_event,
            source_shortfall=row["source_shortfall"],
            source_shortfall_event=transient.source_shortfall_event,
            source_shortfall_decision=row["source_shortfall_decision"] or "",
            source_shortfall_added_ids=list(row["source_shortfall_added_ids"] or []),
            error=row["error"] or "",
            academic_level=row["academic_level"] or "",
            submit_prompt=row["submit_prompt"] or "",
            target_words=row["target_words"],
            min_sources=row["min_sources"],
            tracker=transient.tracker,
            created_at=float(row["created_at"]),
            finished_at=(
                float(row["finished_at"]) if row["finished_at"] is not None else None
            ),
            clarification_rounds=list(row["clarification_rounds"] or []),
            optional_pdf_rounds=list(row["optional_pdf_rounds"] or []),
            optional_pdf_choices=dict(row["optional_pdf_choices"] or {}),
            fast_track=bool(row["fast_track"]),
            provider=row["provider"] or "",
            _sse_event=transient.sse_event,
        )

    def save(self, job: Job) -> Job:
        payload = self._serialize_job(job)
        with self._session() as session:
            existing = session.execute(
                select(_jobs_table.c.job_id).where(_jobs_table.c.job_id == job.job_id)
            ).scalar_one_or_none()
            if existing is None:
                session.execute(insert(_jobs_table).values(**payload))
            else:
                session.execute(
                    update(_jobs_table)
                    .where(_jobs_table.c.job_id == job.job_id)
                    .values(**payload)
                )
            session.commit()
        return job

    def get(self, job_id: str, default: Job | None = None) -> Job | None:
        live_job = self._live_jobs.get(job_id)
        if live_job is not None:
            return live_job
        with self._session() as session:
            row = (
                session.execute(
                    select(_jobs_table).where(_jobs_table.c.job_id == job_id)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return default
        job = self._hydrate_job(row)
        self._live_jobs[job_id] = job
        return job

    def __getitem__(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def __setitem__(self, job_id: str, job: Job) -> None:
        if job_id != job.job_id:
            raise KeyError(job_id)
        self.save(job)

    def pop(self, job_id: str, default: Job | None = None) -> Job | None:
        job = self.get(job_id)
        if job is None:
            return default
        with self._session() as session:
            session.execute(delete(_jobs_table).where(_jobs_table.c.job_id == job_id))
            session.commit()
        self._transients.pop(job_id, None)
        self._live_jobs.pop(job_id, None)
        return job

    def __contains__(self, job_id: object) -> bool:
        if not isinstance(job_id, str):
            return False
        with self._session() as session:
            existing = session.execute(
                select(_jobs_table.c.job_id).where(_jobs_table.c.job_id == job_id)
            ).scalar_one_or_none()
        return existing is not None

    def expired_finished_jobs(
        self, *, current_time: float, ttl_seconds: int
    ) -> list[Job]:
        if ttl_seconds <= 0:
            return []
        cutoff = current_time - ttl_seconds
        with self._session() as session:
            rows = (
                session.execute(
                    select(_jobs_table)
                    .where(_jobs_table.c.status.in_(("done", "error")))
                    .where(_jobs_table.c.finished_at.is_not(None))
                    .where(_jobs_table.c.finished_at < cutoff)
                    .order_by(_jobs_table.c.finished_at.asc())
                )
                .mappings()
                .all()
            )
        return [self._hydrate_job(row) for row in rows]

    def mark_stale_active_jobs(self, message: str) -> int:
        """Fail active jobs left behind by a previous process restart."""
        stale_statuses = ("running", "questions", "optional_pdfs", "source_shortfall")
        finished_at = time.time()
        with self._session() as session:
            rows = session.execute(
                select(_jobs_table.c.job_id).where(
                    _jobs_table.c.status.in_(stale_statuses)
                )
            ).all()
            if not rows:
                return 0
            stale_job_ids = [str(row[0]) for row in rows]
            session.execute(
                update(_jobs_table)
                .where(_jobs_table.c.status.in_(stale_statuses))
                .values(
                    status="error",
                    error=message,
                    finished_at=finished_at,
                    questions=None,
                    optional_pdf_items=None,
                    optional_pdf_allowed_ids=None,
                    source_shortfall=None,
                    source_shortfall_decision="",
                    source_shortfall_added_ids=[],
                )
            )
            session.commit()
        for job_id in stale_job_ids:
            self._live_jobs.pop(job_id, None)
        return len(stale_job_ids)

    def reset_for_tests(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
        self._engine = None
        self._session_factory = None
        self._engine_url = None
        self._transients.clear()
        self._live_jobs.clear()
