# Essay Writer

AI-powered academic essay generator for Greek university students. Uses a deterministic Python pipeline with two specialized LLM agents (worker and writer) to research, plan, write, review, and export formatted `.docx` essays.

## Features

- Deterministic 8-step Python pipeline (intake → validate → plan → research → read → write → review → export)
- Two agent types: worker (fast model for research/planning) and writer (quality model for essay text)
- Interactive validation: catches incomplete assignments and prompts for missing info before proceeding
- Deterministic academic source research via Semantic Scholar, OpenAlex, and Crossref
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

## Documentation

- [Deepagents Reference](docs/DEEPAGENTS_REFERENCE.md) — framework API reference
