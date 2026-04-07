# Essay Writer

AI-powered academic essay generator for Greek university students. Uses a deterministic Python pipeline with direct LangChain model calls to research, plan, write, review, and export formatted `.docx` essays.

## Features

- Deterministic 8-step Python pipeline (intake → validate → plan → research → read → write → review → export)
- Three model roles: worker (fast model for research/planning), writer (quality model for essay text), and reviewer (highest-quality model for polishing)
- Direct LangChain model calls with no orchestration framework or middleware layer
- `model.with_structured_output()` for automatic Pydantic-validated JSON outputs with retry
- Interactive validation: catches incomplete assignments and prompts for missing info before proceeding
- Deterministic academic source research via Semantic Scholar, OpenAlex, and Crossref
- Selects the best source subset and uses that selection during essay generation
- Bounds source reading to the top ranked candidates instead of LLM-reading every fetched result
- Long essays use bounded section-local context instead of repeatedly sending the whole draft during section review
- Input extraction writes a single `input/extracted.md` artifact directly into each run directory
- Search and fetch requests share one HTTP transport with pooled connections and centralized retry behavior
- Research queries run with bounded query-level concurrency while preserving deterministic merge order
- Search ranking honors the configured language preference and per-API source cap, and review prompts honor the configured word-count tolerance
- Cost reporting loads model pricing from `config/gemini_pricing.json`
- Formatted `.docx` output with cover page, table of contents, and page numbers
- Supports multiple input formats: PDF, DOCX, PPTX, images, text files
- User-provided reference sources (separate from assignment files) are prioritized and cited in the essay
- Source PDFs saved alongside run artifacts for inspection
- Configurable via YAML, environment variables, or both
- Custom AI endpoint support via `AI_BASE_URL` / `AI_API_KEY` / `AI_MODEL`

## Quick Start

```bash
# Install dependencies
uv sync

# Run with assignment files
uv run python -m src.runner /path/to/assignment/

# Run with a single file
uv run python -m src.runner /path/to/brief.pdf

# Run with a text prompt
uv run python -m src.runner -p "Write a 3000-word essay on climate change"

# Provide your own reference sources
uv run python -m src.runner /path/to/assignment/ --sources /path/to/my/papers/
```

Output is written to the `output/` directory as a `.docx` file.

## Web UI

A browser-based interface is available as an alternative to the CLI.

```bash
uv run uvicorn src.web:app --reload
```

Open http://localhost:8000. Upload assignment files, optionally upload your own reference sources, enter a prompt, set a target word count, and download the result as a ZIP.

**Docker:**

```bash
docker build -t essay-writer .
docker run -p 8000:8000 --env-file .env essay-writer
```

## Deployment

### Render

The repo includes a `render.yaml` Blueprint for one-click deployment to [Render](https://render.com):

1. Connect this repo in the Render dashboard.
2. Apply the Blueprint (or create a Web Service pointing at the Dockerfile).
3. Set the required environment variables (`GOOGLE_API_KEY`, and optionally `SEMANTIC_SCHOLAR_API_KEY`, `LANGSMITH_API_KEY`).

The service exposes the web UI on the port assigned by Render.

## Configuration

Default settings are defined in `config/schemas.py`. Override with `--config path/to/custom.yaml` or environment variables (prefix `ESSAY_WRITER_`).
