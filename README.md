# Essay Writer

AI-powered academic essay generator for Greek university students. Uses a deterministic Python pipeline with OpenAI SDK + Instructor to research, plan, write, review, and export formatted `.docx` essays.

## Features

- Deterministic Python pipeline for intake, planning, research, writing, review, and export
- Three model roles: worker (fast model for research/planning), writer (quality model for essay text), and reviewer (highest-quality model for polishing)
- Writer prompts keep style guidance compact and positive, while reviewer prompts carry most of the active cleanup for templated academic prose
- OpenAI SDK + Instructor for model calls with no orchestration framework or middleware layer
- Instructor-powered structured output with automatic Pydantic validation and retry
- Interactive validation: catches incomplete assignments and prompts for missing info before proceeding
- Preserves explicit user-provided essay structure and headings more strongly when they appear in the prompt or assignment materials
- Deterministic academic source research via Semantic Scholar, OpenAlex, and Crossref
- Staged source filtering: cheap metadata pretrim first, then batch-triages title+abstract candidates, then extracts only the final selected sources
- Downloaded run ZIPs include source triage and scoring metadata for auditability alongside the selected-source outputs
- If usable selected sources fall below the target, runs one broader recovery search pass before asking whether to continue with fewer sources
- Long essays draft most body sections in parallel, defer introduction/synthesis/conclusion sections until full context is available, then run a reconciliation pass before review
- Input extraction writes a single `input/extracted.md` artifact directly into each run directory
- Search and fetch requests share one HTTP transport with pooled connections and centralized retry behavior
- Research queries run with bounded query-level concurrency while preserving deterministic merge order
- Search ranking honors the configured language preference and per-API source cap, and review prompts honor the configured word-count tolerance
- Cost reporting via `genai-prices` with automatic model/provider detection
- Formatted `.docx` output with cover page, table of contents, and page numbers
- Supports multiple input formats: PDF, DOCX, PPTX, images, text files
- User-provided reference sources (separate from assignment files) are prioritized and cited in the essay
- Source PDFs saved alongside run artifacts for inspection
- Configurable via environment variables
- Custom AI endpoint support via `AI_BASE_URL` / `AI_API_KEY` / `AI_MODEL`

## Quick Start

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

# Run the background worker in a second terminal
uv run python -m src.worker

# Run 6 workers only (or pass a different count)
uv run python -m src.start_workers 6
```

Open http://localhost:8000. Upload assignment files, optionally upload your own reference sources, enter a prompt, set a target word count, and download the result as a ZIP.

The web process now enqueues jobs into the SQL store and the worker process claims them for execution. The browser UI downloads the ZIP first and then asks the server to clean up that completed job, which keeps failed or interrupted transfers retryable. Web job state is stored in a SQL database: local development defaults to a SQLite file in the repo, while production should set `ESSAY_WRITER_DATABASE__URL` to Postgres. Run artifacts are stored via a pluggable backend: set `ESSAY_WRITER_STORAGE__BACKEND=local` for local filesystem (development) or `r2` for Cloudflare R2 (production, configure via `ESSAY_WRITER_STORAGE__R2_*` env vars). Jobs that are never cleaned up are removed after **24 hours** by default (database row plus R2 artifacts). Override with `ESSAY_WEB_JOB_TTL_SECONDS` (seconds; set to `0` to disable only this automatic cleanup). Sweeps run every **300** seconds by default (`ESSAY_WEB_JOB_SWEEP_INTERVAL_SECONDS`, not below **60**). If a job is waiting on clarification answers or optional PDF input, it times out after **1800** seconds by default (`ESSAY_WEB_INTERACTION_TIMEOUT_SECONDS`). Worker ownership is lease-based, so a different worker can reclaim a stuck job after the lease expires.


Source handling is intentionally staged: the system first builds a broad candidate pool, then applies a cheap deterministic metadata pretrim using weighted title overlap, abstract overlap, citations, and direct-PDF availability to cap the scoring pool to roughly `target_sources × 5`. It then runs the title+abstract LLM triage pass on that shortlist before fetching/extracting the final selected set. The final selected set is usable-only; if the usable pool is smaller than the original target, the writer and reviewer prompts are capped to the actually available selected sources.

If the first source-reading pass still produces too few usable selected sources, the pipeline performs one automatic recovery rerun with a larger fetch budget and full-text-biased filters where the upstream APIs support them. If the usable selected pool is still below target after that recovery pass, the app pauses and asks whether to continue with the smaller evidence set.

**Docker:**

```bash
docker build -t essay-writer .
docker run -p 8000:8000 --env-file .env essay-writer
```

The container now starts both the web server and the worker launcher. Worker count comes from `config/settings.py` and can be overridden from `.env` or the deployment environment with `ESSAY_WORKER_COUNT` (or `ESSAY_WRITER_WORKER_COUNT`). The default is `6`. For example:

```bash
docker run -p 8000:8000 --env-file .env -e ESSAY_WORKER_COUNT=4 essay-writer
```

## Local Git Hook

This repo includes a tracked pre-push hook at `.githooks/pre-push` that runs the test suite locally before `git push`.

Enable it once per clone:

```bash
git config core.hooksPath .githooks
```

After that, each push runs:

```bash
uv run python -m pytest tests/ -v
```

## Deployment

### Render

The repo includes a `render.yaml` Blueprint for one-click deployment to [Render](https://render.com):

1. Connect this repo in the Render dashboard.
2. Apply the Blueprint (or create a Web Service pointing at the Dockerfile).
3. Set the required environment variables. For direct Google provider usage, `GOOGLE_API_KEY` can be either a classic Gemini Developer API key or a Vertex AI `AQ.` key. Vertex AI keys also require `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION`. Optionally set `SEMANTIC_SCHOLAR_API_KEY`.
4. Set `ESSAY_WRITER_DATABASE__URL` to your Postgres connection string in production. If unset, the web layer falls back to a local SQLite file, which is suitable only for local development and tests.
5. Set `ESSAY_WRITER_STORAGE__BACKEND=r2` and configure `ESSAY_WRITER_STORAGE__R2_ENDPOINT_URL`, `ESSAY_WRITER_STORAGE__R2_BUCKET`, `ESSAY_WRITER_STORAGE__R2_ACCESS_KEY_ID`, and `ESSAY_WRITER_STORAGE__R2_SECRET_ACCESS_KEY` for artifact storage in Cloudflare R2. For local development, set `ESSAY_WRITER_STORAGE__BACKEND=local` to store artifacts on the local filesystem under `runs/`.

The web process and worker processes need the same database and storage credentials. Outside Docker, start the web app explicitly with `uv run uvicorn src.web:app --reload` and start workers explicitly with `uv run python -m src.start_workers 6` (or another count). Inside the Docker image, the default container entrypoint starts the web server first, then launches workers after the port is bound. Override the settings-backed `worker_count` from `.env` or the deployment environment with `ESSAY_WORKER_COUNT` or `ESSAY_WRITER_WORKER_COUNT`.

The checked-in Render blueprint pins `ESSAY_WORKER_COUNT=1` on the free plan because the combined web-plus-workers container can exceed 512 MiB if you let it boot the default `6` workers. If you deploy the Docker image manually in Render instead of using the blueprint, set that env var yourself.


The service exposes the web UI on the port assigned by Render. Set `ESSAY_WEB_JOB_TTL_SECONDS` / `ESSAY_WEB_JOB_SWEEP_INTERVAL_SECONDS` if you need different retention for undownloaded jobs.

## Configuration

Default settings are defined in `config/settings.py`. Override them with environment variables using the `ESSAY_WRITER_` prefix. `EssayWriterConfig` also reads the repo-root `.env`, including direct provider variables such as `GOOGLE_API_KEY`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `AI_BASE_URL`, and `AI_API_KEY`, so the runtime no longer depends on ad hoc `os.environ` reads or entrypoint-specific dotenv loading.

The web layer database URL lives at `ESSAY_WRITER_DATABASE__URL`. The database stores web job state, runtime summaries, per-step metrics, and artifact metadata in SQL. Run artifacts (`.md`, `.docx`, uploaded files, logs) are stored via the configured storage backend (`ESSAY_WRITER_STORAGE__BACKEND`): `local` for filesystem or `r2` for Cloudflare R2.

Worker process count lives in `EssayWriterConfig.worker_count` with a default of `6`. Set `ESSAY_WORKER_COUNT` or `ESSAY_WRITER_WORKER_COUNT` in `.env` or your deployment environment to override it for `src.start_workers`, `scripts/start_workers.sh`, and the combined Docker entrypoint.

For inspection/debugging, the web server exposes a browser history page at `GET /history` plus JSON history endpoints: `GET /history/jobs` lists persisted run summaries, including active jobs immediately after submission, and `GET /history/jobs/{job_id}` returns the summary, step metrics, artifact manifest, and live status (when the job is still active).

Database schema changes are managed through Alembic. Run `uv run alembic upgrade head` before starting the app in a fresh environment or after pulling schema changes.

If your local Postgres database was created before Alembic support and already contains a `web_jobs` table, use `uv run python scripts/db_upgrade_local.py` for the one-time upgrade path. It backs up existing `web_jobs` rows, recreates the table through Alembic, and restores the saved rows. Avoid `alembic stamp head` unless you have verified that the existing schema matches the migration exactly.

PDF proxy credentials are no longer hardcoded in code. Set `ESSAY_WRITER_SEARCH__PROXY_PREFIX`, `ESSAY_WRITER_SEARCH__PROXY_USERNAME`, and `ESSAY_WRITER_SEARCH__PROXY_PASSWORD` in `.env` or the deployment environment when proxy access is needed.

When the direct Google provider path is used, the runtime auto-detects the credential format. Classic `GOOGLE_API_KEY` values stay on the Gemini Developer API path, while `AQ.` keys are routed through Vertex AI. In the web UI, the same credential field also accepts pasted Vertex service-account JSON; when that JSON includes `project_id`, it is used if `GOOGLE_CLOUD_PROJECT` is unset. `GOOGLE_CLOUD_LOCATION` is still required for all direct Vertex paths. If `AI_BASE_URL` is set, the runtime uses the gateway credentials instead and ignores direct Google Vertex credentials.
