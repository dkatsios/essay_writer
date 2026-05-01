# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

For full architecture, conventions, and invariants, see `.github/copilot-instructions.md`.

## Commands

```bash
# Install dependencies
uv sync

# Apply database migrations
# Existing local DB from before Alembic: back up or remove the old web_jobs table first
uv run alembic upgrade head

# One-time local upgrade helper for pre-Alembic Postgres DBs
uv run python scripts/db_upgrade_local.py

# Enable the repo-managed pre-push hook once per clone
git config core.hooksPath .githooks

# Run the web UI
uv run uvicorn src.web:app --reload
# Run the background worker in a second terminal/process
uv run python -m src.worker
# Run multiple workers only (default 6 if omitted)
uv run python -m src.start_workers 6
# Web: optional ESSAY_WEB_JOB_TTL_SECONDS (default 86400, 0=disable stale-job sweeps), ESSAY_WEB_JOB_SWEEP_INTERVAL_SECONDS, ESSAY_WEB_INTERACTION_TIMEOUT_SECONDS (default 1800)
# Web DB: ESSAY_WRITER_DATABASE__URL (production Postgres; local dev falls back to repo SQLite). Stores job state plus runtime summaries, step metrics, and artifact metadata; file bytes remain local.
# Worker/web split: both processes must use the same DB and share the local run-artifact filesystem.
# Optional PDF prompt: ESSAY_WRITER_SEARCH__OPTIONAL_PDF_PROMPT_TOP_N (default 5, 0=off), ESSAY_WRITER_SEARCH__OPTIONAL_PDF_MIN_BODY_WORDS
# Source filtering: ESSAY_WRITER_SEARCH__TRIAGE_BATCH_SIZE (default 50), ESSAY_WRITER_SEARCH__MIN_RELEVANCE_SCORE (default 3)
# Institutional proxy: ESSAY_WRITER_SEARCH__PROXY_PREFIX (e.g. 'https://login.proxy.eap.gr/login?url=')
# Proxy auth (Shibboleth/EZProxy): ESSAY_WRITER_SEARCH__PROXY_USERNAME, ESSAY_WRITER_SEARCH__PROXY_PASSWORD

# Docker
docker build -t essay-writer .
docker run -p 8000:8000 --env-file .env essay-writer

# Lint
uv run ruff check src/ tests/

# Run tests
uv run python -m pytest tests/ -v

# Import check
uv run python -c "from src.agent import create_client, retry_with_backoff"
```

## Documentation Sync

See `.github/instructions/documentation-sync.instructions.md`. On important changes, review CLAUDE.md, README.md, and `.github/copilot-instructions.md` together.

## Deployment Note

The repo no longer includes a combined web+worker startup script. Start the web app with `uv run uvicorn src.web:app --reload` and workers with `uv run python -m src.start_workers 6` (or another count). Both sides still need the same DB and shared local artifact filesystem.
