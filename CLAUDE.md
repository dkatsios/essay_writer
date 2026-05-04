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
# Web DB: ESSAY_WRITER_DATABASE__URL (production Postgres; local dev falls back to repo SQLite). Stores job state plus runtime summaries, step metrics, and artifact metadata.
# Artifact storage backend: ESSAY_WRITER_STORAGE__BACKEND ("r2" or "local", default "r2")
# Local backend: ESSAY_WRITER_STORAGE__LOCAL_DIR (default "runs")
# R2 backend: ESSAY_WRITER_STORAGE__R2_ENDPOINT_URL, ESSAY_WRITER_STORAGE__R2_BUCKET, ESSAY_WRITER_STORAGE__R2_ACCESS_KEY_ID, ESSAY_WRITER_STORAGE__R2_SECRET_ACCESS_KEY
# Worker/web split: both processes must use the same DB and storage credentials/paths.
# Optional PDF prompt: ESSAY_WRITER_SEARCH__OPTIONAL_PDF_PROMPT_TOP_N (default 5, 0=off), ESSAY_WRITER_SEARCH__OPTIONAL_PDF_MIN_BODY_WORDS
# Source filtering: ESSAY_WRITER_SEARCH__TRIAGE_BATCH_SIZE (default 50), ESSAY_WRITER_SEARCH__MIN_RELEVANCE_SCORE (default 3)
# Institutional proxy: ESSAY_WRITER_SEARCH__PROXY_PREFIX (e.g. 'https://login.proxy.eap.gr/login?url=')
# Proxy auth (Shibboleth/EZProxy): ESSAY_WRITER_SEARCH__PROXY_USERNAME, ESSAY_WRITER_SEARCH__PROXY_PASSWORD

# Docker
docker build -t essay-writer .
docker run -p 8000:8000 --env-file .env essay-writer
# Combined container entrypoint starts web + workers together
# Override worker count with ESSAY_WORKER_COUNT or ESSAY_WRITER_WORKER_COUNT (default 6)

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

The Docker image now uses a combined container entrypoint that starts the web app plus `src.start_workers` together. Worker count comes from `EssayWriterConfig.worker_count`; override it with `ESSAY_WORKER_COUNT` or `ESSAY_WRITER_WORKER_COUNT` (default `6`). Outside Docker, start the web app with `uv run uvicorn src.web:app --reload` and workers with `uv run python -m src.start_workers 6` (or another count). Both sides need the same DB credentials. With R2 backend, both need R2 credentials; with local backend (`ESSAY_WRITER_STORAGE__BACKEND=local`), both need access to the same `local_dir` path.

For Render free-plan deployments, keep the combined-container worker count at `1` unless you move workers into a separate service or upgrade memory. The checked-in `render.yaml` sets `ESSAY_WORKER_COUNT=1` for that reason and runs `python -m alembic upgrade head` as a pre-deploy command before the container starts. The combined runtime entrypoint also runs Alembic before launching the web and worker processes so migrations happen in the same runtime environment.
