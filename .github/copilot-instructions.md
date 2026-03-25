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

Coordinator-subagent essay writing system built on the `deepagents` framework (LangChain/LangGraph). Produces academic essays in Greek as formatted `.docx` files. The orchestrator is a thin coordinator that delegates all work to two subagent types — **worker** and **writer** — each with dedicated templates, tools, and skills.

### Flow

1. **Intake** (worker) — reads user documents, writes `/brief/assignment.md`
2. **Plan** (worker) — creates sections, word targets, research queries → `/plan/plan.md`
3. **Research** (worker) — extracts queries from plan, calls `research_sources` → `/sources/registry.json`
4. **Read sources** (worker, parallel) — fetches full-text sources → `/sources/notes/{source_id}.md`
5. **Write** (writer) — writes the complete essay → `/essay/draft.md`
6. **Review** (writer) — reviews draft, writes polished version to `/essay/reviewed.md`
7. **Export** — orchestrator calls `build_docx` → `/output/essay.docx`

### Three-Agent Architecture

| Agent | Model | Template | Tools | Skills dir |
|-------|-------|----------|-------|------------|
| **orchestrator** | `gemini-3-flash-preview` | `orchestrator.j2` | `build_docx` | — |
| **worker** | `gemini-2.5-flash` | `worker.j2` | `read_pdf`, `read_docx`, `fetch_url`, `research_sources` | `/skills/worker/` |
| **writer** | `gemini-3-flash-preview` | `writer.j2` | *(none)* | `/skills/writer/` |

The orchestrator coordinates by dispatching `task` calls. It has no research or reading tools — it delegates everything.

### Skills (organized by agent)

Worker skills (`src/skills/worker/`):

| Skill | Purpose | VFS output |
|-------|---------|------------|
| `intake` | Synthesize pre-extracted document content into a structured brief | `/brief/assignment.md` |
| `essay-planning` | Create essay plan with sections, word targets, research queries | `/plan/plan.md` |
| `research` | Extract queries from plan, call `research_sources` once | `/sources/registry.json` |
| `source-reading` | Fetch/read a single source, write condensed notes | `/sources/notes/{source_id}.md` |

Writer skills (`src/skills/writer/`):

| Skill | Purpose | VFS output |
|-------|---------|------------|
| `essay-writing` | Write the complete essay using plan and source notes | `/essay/draft.md` |
| `essay-review` | Review and polish the draft | `/essay/reviewed.md` |

### VFS (Virtual File System)

Key paths:

- `/brief/assignment.md` — assignment brief
- `/plan/plan.md` — essay plan
- `/sources/registry.json` — source metadata (from `research_sources` tool)
- `/essay/draft.md` — initial essay draft
- `/essay/reviewed.md` — reviewed/polished essay (used for export)
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

Uses `pydantic-settings` (`BaseSettings`) with two layers (highest wins):

1. **Environment variables** — prefix `ESSAY_WRITER_`, nested with `__` (e.g., `ESSAY_WRITER_MODELS__ORCHESTRATOR=google_genai:gemini-2.5-flash`)
2. **Custom YAML config file** — override with `--config path/to/custom.yaml`
3. **Field defaults** — in the Pydantic models at `config/schemas.py`

No `default.yaml` exists; field defaults in `schemas.py` are canonical.

### Key Invariants

- **Jinja2 templates** (`src/templates/*.j2`) render system prompts. 3 templates: `orchestrator.j2`, `worker.j2`, `writer.j2` — one per agent type.
- **Skills** (`src/skills/{worker,writer}/*/SKILL.md`) provide task-specific instructions via progressive disclosure. 6 skills total: 4 worker, 2 writer. Each subagent only sees its own skills directory.
- **Tool separation** — orchestrator has only `build_docx`; worker has `read_pdf`, `read_docx`, `fetch_url`, `research_sources`; writer has no custom tools (uses only framework-provided `read_file`/`write_file`). The orchestrator delegates; subagents perform.
- **Retry middleware** — `_RetryMalformedMiddleware` retries on `MALFORMED_FUNCTION_CALL` / zero-output `STOP` (Gemini glitch). `ModelRetryMiddleware` retries on transient 503/429 errors with exponential backoff. Both applied to all agents.
- **History trimming** — `_TrimHistoryMiddleware` keeps only the first message + last 6 messages before each LLM call, preventing token accumulation across turns. Applied to orchestrator only. Full history stays in LangGraph state; only the LLM's view is trimmed.
- **Thin orchestrator** — receives only a text-only summary of input files (no multimodal content). Has no research or reading tools. Delegates all work to subagents via `task` calls.
- **Input flow** — `build_message_content()` returns a text-only summary for the orchestrator and writes extracted content to `/input/extracted.md` for the worker. Multimodal content (scanned PDF images) stays in `/input/` for the worker to access via `read_pdf`. The orchestrator never sees base64 images.
- **`research_sources` tool** — owned by the worker, fans out queries across Semantic Scholar, OpenAlex, and Crossref in parallel, deduplicates by DOI/title, writes registry JSON. Zero LLM tokens consumed by the tool itself.
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
