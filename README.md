# Essay Writer

AI-powered academic essay generator for Greek university students. Uses a multi-agent pipeline to research, plan, write, review, and export formatted `.docx` essays.

## Features

- Multi-agent pipeline: orchestrator, planner, researcher, extractor, writer, reviewer, builder
- Academic source research via Semantic Scholar
- Sequential section writing with source integration and citation management
- Formatted `.docx` output with cover page, table of contents, and page numbers
- Supports multiple input formats: PDF, DOCX, PPTX, images, text files
- Scanned PDF detection with automatic image fallback
- Configurable via YAML, environment variables, or both

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

Default settings are in `config/default.yaml`. Override with `--config` or environment variables (prefix `ESSAY_WRITER_`).

## Documentation

- [Design Document](docs/DESIGN.md) — high-level requirements and decisions
- [Technical Design](docs/TECHNICAL_DESIGN.md) — implementation blueprint
- [Deepagents Reference](docs/DEEPAGENTS_REFERENCE.md) — framework API reference
