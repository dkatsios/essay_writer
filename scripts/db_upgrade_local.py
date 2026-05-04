#!/usr/bin/env python3

from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import load_config
from src.db_upgrade import upgrade_database


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
            f"db_upgrade_local.py only supports PostgreSQL URLs. Got: {url!r}"
        )
    return url


def main() -> int:
    args = _parse_args()
    database_url = _resolve_database_url(args.database_url)
    return upgrade_database(database_url, status=print)


if __name__ == "__main__":
    sys.exit(main())
