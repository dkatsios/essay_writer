# AI Guidance — Essay Writer

This is the canonical AI guidance file for the essay writer project. See `.github/instructions/documentation-sync.instructions.md` for the documentation sync policy.

## Commands

```bash
# Install dependencies
uv sync

# Run the agent — point at a file or directory with assignment materials
uv run python -m src.runner /path/to/assignment/
uv run python -m src.runner /path/to/brief.pdf
uv run python -m src.runner /path/to/files/ -p "Focus on economic aspects"

# Prompt-only mode (no files)
uv run python -m src.runner -p "Write a 3000-word essay on climate change"

# Custom config
uv run python -m src.runner /path/to/files/ --config my_config.yaml

# Import check (no tests yet)
uv run python -c "from src.agent import create_essay_agent"
```

## Architecture

Multi-agent essay writing system built on the `deepagents` framework (LangChain/LangGraph). Produces academic essays in Greek as formatted `.docx` files.

### Pipeline

A single **orchestrator** agent drives an 8-phase pipeline by delegating to 7 specialized **subagents** via the `task` tool:

1. **Intake** — parse user input + any provided documents (cataloguer)
2. **Draft Plan** — section breakdown with word targets (planner)
3. **Research** — parallel academic source search (researcher, parallel)
4. **Plan Refinement** — adjust plan based on found sources (planner)
5. **Extraction** — one-pass per source, fan-out to per-section VFS entries (extractor, parallel)
6. **Sequential Writing** — sections written one at a time in order (writer, sequential)
7. **Review** — coherence, citations, polish, intro revision (reviewer)
8. **Export** — build `.docx` with cover page, TOC, formatting (builder)

### VFS (Virtual File System)

Agents exchange data through backend file operations (`write_file`/`read_file`), not conversation messages. Key paths:

- `/brief/` — assignment brief
- `/plan/` — draft and final plans, source mapping
- `/sources/metadata/` — structured source catalog entries
- `/sections/section_XX/` — per-section drafts, summaries, and source extracts
- `/essay/` — assembled and reviewed essay
- `/input/` — staged input files (temp dir, routed via `CompositeBackend`)
- `/output/` — final `.docx` output (routed to real filesystem)

**Critical constraint**: `write_file` errors on existing files. Rewrites (e.g., word count retries) must use `edit_file`.

### Input Handling

The CLI accepts a file or directory path. The intake module (`src/intake.py`):
- Scans and categorizes files by extension
- Extracts text from `.md`, `.txt`, `.pdf`, `.docx`, `.pptx`
- For scanned PDFs (sparse text extraction), falls back to rendering pages as images
- Encodes images (`.png`, `.jpg`, etc.) as base64 for multimodal LLM consumption
- Stages originals into a temp directory for the agent's `/input/` backend route

Supported: `.md`, `.txt`, `.text`, `.rst`, `.csv`, `.tsv`, `.log`, `.pdf`, `.docx`, `.pptx`, `.png`, `.jpg`, `.jpeg`, `.gif`, `.bmp`, `.tiff`, `.webp`, `.svg`

### Configuration

Uses `pydantic-settings` (`BaseSettings`) with three layers (highest wins):

1. **Environment variables** — prefix `ESSAY_WRITER_`, nested with `__` (e.g., `ESSAY_WRITER_MODELS__ORCHESTRATOR=anthropic:claude-opus-4-6`)
2. **YAML config file** — `config/default.yaml` by default, override with `--config`
3. **Field defaults** — in the Pydantic models at `config/schemas.py`

### Key Invariants

- **Jinja2 templates** (`src/templates/*.j2`) render system prompts with config-driven conditionals (checkpoints, intro strategy, long essay thresholds). Templates are rendered *before* subagent creation.
- **Skills** (`src/skills/*/SKILL.md`) provide detailed phase-specific instructions via progressive disclosure — agents see name/description only, then `read_file` the full skill when needed.
- **Sequential section writing** is intentional — each writer gets prior sections as context for coherence. For essays > `long_essay_threshold` words, prior sections are passed as summaries instead of full text.
- **Subagent independence** — subagents have NO conversation history from the parent. Every `task` call must include all necessary context in the `description` parameter. Multiple `task` calls in one message run in parallel. Files written by subagents propagate back to the parent via state updates.
- **CompositeBackend** routes `/input/` to a temp staging dir and `/output/` to `FilesystemBackend` (real disk); everything else goes to `StateBackend` (LangGraph state, checkpointed).

### Test Fixtures

Place test assignment directories under `examples/`. Each subdirectory is a self-contained test case with assignment files (PDFs, images, text).

## Design Documents

- `docs/DESIGN.md` — high-level requirements and decisions
- `docs/TECHNICAL_DESIGN.md` — implementation blueprint mapping design to deepagents constructs
- `docs/DEEPAGENTS_REFERENCE.md` — framework API reference from source code analysis
