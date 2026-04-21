# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

For full architecture, conventions, and invariants, see `.github/copilot-instructions.md`.

## Commands

```bash
# Install dependencies
uv sync

# Enable the repo-managed pre-push hook once per clone
git config core.hooksPath .githooks

# Run the web UI
uv run uvicorn src.web:app --reload
# Web: optional ESSAY_WEB_JOB_TTL_SECONDS (default 86400, 0=disable stale-job sweeps), ESSAY_WEB_JOB_SWEEP_INTERVAL_SECONDS, ESSAY_WEB_INTERACTION_TIMEOUT_SECONDS (default 1800)
# Optional PDF prompt: ESSAY_WRITER_SEARCH__OPTIONAL_PDF_PROMPT_TOP_N (default 5, 0=off), ESSAY_WRITER_SEARCH__OPTIONAL_PDF_MIN_BODY_WORDS
# Source filtering: ESSAY_WRITER_SEARCH__TRIAGE_BATCH_SIZE (default 50), ESSAY_WRITER_SEARCH__MIN_RELEVANCE_SCORE (default 3)

# Docker
docker build -t essay-writer .
docker run -p 8000:8000 --env-file .env essay-writer

# Lint
uv run ruff check src/ tests/

# Run tests
uv run python -m pytest tests/ -v

# Import check
uv run python -c "from src.agent import create_client, _retry_with_backoff"
```

## Documentation Sync

See `.github/instructions/documentation-sync.instructions.md`. On important changes, review CLAUDE.md, README.md, and `.github/copilot-instructions.md` together.
