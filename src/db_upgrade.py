"""Helpers for upgrading the SQL schema, including legacy pre-Alembic Postgres DBs."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from sqlalchemy import MetaData, Table, create_engine, inspect, text

from config.settings import load_config


def _resolve_database_url(explicit_url: str | None = None) -> str:
    return explicit_url or load_config().database.url


def _run_alembic_upgrade(database_url: str) -> int:
    env = os.environ.copy()
    env["ESSAY_WRITER_DATABASE__URL"] = database_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=False,
        env=env,
    ).returncode


def _load_rows(database_url: str) -> list[dict[str, Any]]:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            result = connection.execute(
                text("SELECT * FROM web_jobs ORDER BY created_at, job_id")
            )
            return [dict(row._mapping) for row in result]
    finally:
        engine.dispose()


def _drop_legacy_tables(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS web_jobs"))
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    finally:
        engine.dispose()


def _restore_rows(database_url: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        web_jobs = Table("web_jobs", metadata, autoload_with=engine)
        with engine.begin() as connection:
            connection.execute(web_jobs.insert(), rows)
    finally:
        engine.dispose()


def _inspect_state(database_url: str) -> tuple[bool, bool]:
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        schema = "public" if database_url.startswith("postgresql") else None
        has_web_jobs = inspector.has_table("web_jobs", schema=schema)
        has_alembic_version = inspector.has_table("alembic_version", schema=schema)
        return has_web_jobs, has_alembic_version
    finally:
        engine.dispose()


def upgrade_database(
    explicit_url: str | None = None,
    *,
    status: Callable[[str], None] | None = None,
) -> int:
    database_url = _resolve_database_url(explicit_url)
    emit = status or (lambda _: None)

    if not database_url.startswith("postgresql"):
        return _run_alembic_upgrade(database_url)

    has_web_jobs, has_alembic_version = _inspect_state(database_url)

    if has_alembic_version:
        emit("Database is already Alembic-managed; applying any pending migrations.")
        return _run_alembic_upgrade(database_url)

    rows: list[dict[str, Any]] = []
    if has_web_jobs:
        rows = _load_rows(database_url)
        emit(f"Backing up {len(rows)} existing web_jobs row(s).")
        _drop_legacy_tables(database_url)
        emit("Dropped legacy web_jobs table; recreating schema through Alembic.")
    else:
        emit("No existing web_jobs table found; creating schema through Alembic.")

    exit_code = _run_alembic_upgrade(database_url)
    if exit_code != 0:
        return exit_code

    if rows:
        _restore_rows(database_url, rows)
        emit(f"Restored {len(rows)} web_jobs row(s).")

    return 0
