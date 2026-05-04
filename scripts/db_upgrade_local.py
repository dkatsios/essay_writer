#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import MetaData, Table, create_engine, inspect, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import load_config


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Upgrade a local Postgres web_jobs database to the current Alembic head. "
            "If a legacy pre-Alembic web_jobs table is found, its rows are backed up, "
            "the table is recreated through Alembic, and the rows are restored."
        )
    )
    parser.add_argument(
        "--database-url",
        help=(
            "SQLAlchemy database URL to migrate. Defaults to "
            "ESSAY_WRITER_DATABASE__URL / config.settings."
        ),
    )
    return parser.parse_args()


def _resolve_database_url(explicit_url: str | None) -> str:
    url = explicit_url or load_config().database.url
    if not url.startswith("postgresql"):
        raise SystemExit(
            "db_upgrade_local.py only supports PostgreSQL URLs. "
            f"Got: {url!r}"
        )
    return url


def _run_alembic_upgrade(database_url: str) -> None:
    env = os.environ.copy()
    env["ESSAY_WRITER_DATABASE__URL"] = database_url
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        check=True,
        env=env,
    )


def _load_rows(database_url: str) -> list[dict[str, object]]:
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


def _restore_rows(database_url: str, rows: list[dict[str, object]]) -> None:
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
        has_web_jobs = inspector.has_table("web_jobs", schema="public")
        has_alembic_version = inspector.has_table("alembic_version", schema="public")
        return has_web_jobs, has_alembic_version
    finally:
        engine.dispose()


def _print_final_state(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            version = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            row_count = connection.execute(
                text("SELECT COUNT(*) FROM web_jobs")
            ).scalar_one()
    finally:
        engine.dispose()

    print(f"Alembic revision: {version}")
    print(f"web_jobs rows: {row_count}")


def main() -> int:
    args = _parse_args()
    database_url = _resolve_database_url(args.database_url)
    has_web_jobs, has_alembic_version = _inspect_state(database_url)

    if has_alembic_version:
        print("Database is already Alembic-managed; applying any pending migrations.")
        _run_alembic_upgrade(database_url)
        _print_final_state(database_url)
        return 0

    rows: list[dict[str, object]] = []
    if has_web_jobs:
        rows = _load_rows(database_url)
        print(f"Backing up {len(rows)} existing web_jobs row(s).")
        _drop_legacy_tables(database_url)
        print("Dropped legacy web_jobs table; recreating schema through Alembic.")
    else:
        print("No existing web_jobs table found; creating schema through Alembic.")

    _run_alembic_upgrade(database_url)

    if rows:
        _restore_rows(database_url, rows)
        print(f"Restored {len(rows)} web_jobs row(s).")

    _print_final_state(database_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())