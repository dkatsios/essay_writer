# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

For full architecture, conventions, and invariants, see `.github/copilot-instructions.md`.

## Commands

```bash
# Install dependencies
uv sync

# Run the pipeline — point at a file or directory with assignment materials
uv run python -m src.runner /path/to/assignment/
uv run python -m src.runner /path/to/brief.pdf
uv run python -m src.runner /path/to/files/ -p "Focus on economic aspects"

# Prompt-only mode (no files)
uv run python -m src.runner -p "Write a 3000-word essay on climate change"

# Custom config
uv run python -m src.runner /path/to/files/ --config my_config.yaml

# Run the web UI
uv run uvicorn src.web:app --reload

# Docker
docker build -t essay-writer .
docker run -p 8000:8000 --env-file .env essay-writer

# Run tests
uv run python -m pytest tests/ -v

# Import check
uv run python -c "from src.agent import create_model, invoke_with_retry"
```

## Documentation Sync

See `.github/instructions/documentation-sync.instructions.md`. On important changes, review CLAUDE.md, README.md, and `.github/copilot-instructions.md` together.
