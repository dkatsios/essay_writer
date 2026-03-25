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

# Run tests
uv run python -m pytest tests/ -v

# Import check
uv run python -c "from src.agent import create_essay_agent"
```

## Architecture

Coordinator-subagent essay writing system built on the `deepagents` framework (LangChain/LangGraph). Produces academic essays in Greek as formatted `.docx` files. The orchestrator is a thin coordinator that delegates to two subagent types — **worker** (fast/cheap) and **writer** (quality) — directing them via skill files.

### Flow

1. **Intake** (worker + intake skill) — reads user documents, writes `/brief/assignment.md`
2. **Plan** (worker + essay-planning skill) — creates sections, word targets, research queries → `/plan/plan.md`
3. **Research** (tool) — orchestrator calls `research_sources` with queries from the plan → `/sources/registry.json`
4. **Read sources** (worker + source-reading skill, parallel) — fetches full-text sources → `/sources/notes/{source_id}.md`
5. **Write** (writer + essay-writing skill) — writes the complete essay → `/essay/draft.md`
6. **Review** (writer + essay-review skill) — reviews and polishes draft via `edit_file`
7. **Export** — orchestrator calls `build_docx` → `/output/essay.docx`

### Two-Tier Subagent Architecture

Two subagent types share the same system prompt (`assistant.j2`) but use different models:

- **worker** — fast, cheap model (default: `gemini-2.5-flash`) for intake, planning, and source reading.
- **writer** — quality model (default: `gemini-3-flash-preview`) for essay writing and reviewing.

The orchestrator directs each subagent by specifying which skill file to read in the task description.

| Skill | Subagent | Purpose | VFS output |
|-------|----------|---------|------------|
| `intake` | worker | Synthesize pre-extracted document content into a structured brief | `/brief/assignment.md` |
| `essay-planning` | worker | Create essay plan with sections, word targets, research queries | `/plan/plan.md` |
| `source-reading` | worker | Fetch/read a single source, write condensed notes | `/sources/notes/{source_id}.md` |
| `essay-writing` | writer | Write the complete essay using plan and source notes | `/essay/draft.md` |
| `essay-review` | writer | Review and polish the draft with targeted edits | `/essay/draft.md` (via `edit_file`) |
| `docx-export` | — | Reference for orchestrator's export step | (used by orchestrator) |

### VFS (Virtual File System)

Key paths:

- `/brief/assignment.md` — assignment brief
- `/plan/plan.md` — essay plan
- `/sources/registry.json` — source metadata (from `research_sources` tool)
- `/essay/draft.md` — complete essay draft
- `/sources/notes/{source_id}.md` — reader notes, one file per source
- `/input/` — staged input files (temp dir, routed via `CompositeBackend`)
- `/sources/` — downloaded source PDFs (routed to `.output/run_*/sources/` on disk)
- `/output/essay.docx` — final formatted document (routed to real filesystem)

**Critical constraint**: `write_file` errors on existing files. Modifications must use `edit_file`.

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

- **Jinja2 templates** (`src/templates/*.j2`) render system prompts. 2 templates: `orchestrator.j2` (orchestrator), `assistant.j2` (single assistant subagent).
- **Skills** (`src/skills/*/SKILL.md`) provide detailed task-specific instructions via progressive disclosure — the assistant reads the relevant skill via `read_file` when starting each task. 6 skills: intake, essay-planning, source-reading, essay-writing, essay-review, docx-export.
- **Retry middleware** — `_RetryMalformedMiddleware` retries on `MALFORMED_FUNCTION_CALL` / zero-output `STOP` (Gemini glitch). `ModelRetryMiddleware` retries on transient 503/429 errors with exponential backoff. Both applied to all agents.
- **Thin orchestrator** — the orchestrator is a lightweight coordinator. Research is handled by the `research_sources` tool (deterministic Python, no LLM). Heavy writing and review work is delegated to the writer subagent.
- **`research_sources` tool** — fans out queries across Semantic Scholar, OpenAlex, and Crossref in parallel, deduplicates by DOI/title, writes registry JSON. Zero LLM tokens consumed.
- **Subagent independence** — subagents have NO conversation history from the orchestrator. They read what they need from VFS. Multiple `task` calls in one message run in parallel.
- **CompositeBackend** routes `/input/` to a temp staging dir, `/output/`, `/sources/`, and `/essay/` to `FilesystemBackend` (real disk); everything else goes to `StateBackend` (in-memory LangGraph state).
- **Custom AI endpoint** — when `AI_BASE_URL` is set in `.env`, all models route through an OpenAI-compatible endpoint using `AI_API_KEY` and `AI_MODEL`.
- **Deterministic docx fallback** — if the orchestrator doesn't call `build_docx`, the runner nudges it and, as a last resort, calls `_build_document` directly from `/essay/draft.md`.

### Test Fixtures

Place test assignment directories under `examples/`. Each subdirectory is a self-contained test case with assignment files (PDFs, images, text).

## Design Documents

- `docs/DESIGN.md` — high-level requirements and decisions
- `docs/TECHNICAL_DESIGN.md` — implementation blueprint mapping design to deepagents constructs
- `docs/DEEPAGENTS_REFERENCE.md` — framework API reference from source code analysis
