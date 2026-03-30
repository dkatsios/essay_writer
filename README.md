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
- Long essays use bounded section-local context instead of repeatedly sending the whole draft during section review
- Input extraction writes a single `input/extracted.md` artifact directly into each run directory
- Formatted `.docx` output with cover page, table of contents, and page numbers
- Supports multiple input formats: PDF, DOCX, PPTX, images, text files
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
```

Output is written to the `output/` directory as a `.docx` file.

## Configuration

Default settings are defined in `config/schemas.py`. Override with `--config path/to/custom.yaml` or environment variables (prefix `ESSAY_WRITER_`).
