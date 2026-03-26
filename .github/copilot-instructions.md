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
uv run python -c "from src.agent import create_worker, create_writer"
```

## Architecture

Deterministic Python pipeline for academic essay writing, built on the `deepagents` framework (LangChain/LangGraph). Produces academic essays in Greek as formatted `.docx` files. A Python pipeline (`src/pipeline.py`) controls the 8-step workflow; two LLM agent types — **worker** and **writer** — perform the actual language tasks.

### Flow

1. **Intake** (worker) — reads user documents, writes `/brief/assignment.md`
2. **Validate** (worker) — checks brief completeness; if gaps found, prompts user interactively and appends answers to brief
3. **Plan** (worker) — creates sections, word targets, research queries → `/plan/plan.md`
4. **Research** (worker) — extracts queries from plan, calls `research_sources` → `/sources/registry.json`
5. **Read sources** (worker, parallel) — fetches full-text sources → `/sources/notes/{source_id}.md`
6. **Write** (writer) — writes the complete essay → `/essay/draft.md`
7. **Review** (writer) — reviews draft, writes polished version to `/essay/reviewed.md`
8. **Export** — pure Python `_build_document` call → `essay.docx`

### Two-Agent Architecture

| Agent | Model | Template | Custom Tools | Blocked Tools | Skills dir |
|-------|-------|----------|-------------|---------------|------------|
| **worker** | `gemini-2.5-flash` | `worker.j2` | `read_pdf`, `read_docx`, `fetch_url`, `research_sources` | *(none)* | `/skills/worker/` |
| **writer** | `gemini-3-flash-preview` | `writer.j2` | *(none)* | `edit_file`, `grep`, `glob`, `write_todos` | `/skills/writer/` |

There is no LLM orchestrator. The Python pipeline (`src/pipeline.py`) controls flow deterministically. Each agent is a standalone `create_deep_agent` instance. Each pipeline step uses a unique `thread_id` for clean agent state.

### Skills (organized by agent)

Worker skills (`src/skills/worker/`):

| Skill | Purpose | VFS output |
|-------|---------|------------|
| `intake` | Synthesize pre-extracted document content into a structured brief | `/brief/assignment.md` |
| `validate` | Evaluate brief completeness; write PASS or structured questions | `/brief/validation.md` |
| `essay-planning` | Create essay plan with sections, word targets, research queries | `/plan/plan.md` |
| `research` | Extract queries from plan, call `research_sources` once | `/sources/registry.json` |
| `source-reading` | Fetch/read a single source, write condensed notes | `/sources/notes/{source_id}.md` |

Writer skills (`src/skills/writer/`):

| Skill | Purpose | VFS output |
|-------|---------|------------|
| `essay-writing` | Write the complete essay using plan and source notes | `/essay/draft.md` |
| `essay-review` | Review and polish the draft | `/essay/reviewed.md` |

### VFS (Virtual File System)

All VFS paths are disk-backed via `CompositeBackend` with `FilesystemBackend` routes. Each path maps to a subdirectory of the run directory:

- `/brief/assignment.md` — assignment brief
- `/brief/validation.md` — validation result (PASS or questions)
- `/plan/plan.md` — essay plan
- `/sources/registry.json` — source metadata (from `research_sources` tool)
- `/sources/notes/{source_id}.md` — reader notes, one file per source
- `/essay/draft.md` — initial essay draft
- `/essay/reviewed.md` — reviewed/polished essay (used for export)
- `/input/extracted.md` — pre-extracted document text for the worker
- `/skills/` — routes to `src/skills/` on disk (agents read SKILL.md via VFS)

Files persist between agent invocations because they're on disk. `StateBackend` is the default fallback for unrouted paths.

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

1. **Environment variables** — prefix `ESSAY_WRITER_`, nested with `__` (e.g., `ESSAY_WRITER_MODELS__WORKER=google_genai:gemini-2.5-flash`)
2. **Custom YAML config file** — override with `--config path/to/custom.yaml`
3. **Field defaults** — in the Pydantic models at `config/schemas.py`

No `default.yaml` exists; field defaults in `schemas.py` are canonical.

### Key Invariants

- **Deterministic pipeline** — `src/pipeline.py` runs 8 fixed steps in sequence. No LLM decides the flow. Steps 1–5 use the worker; steps 6–7 use the writer; step 8 is pure Python.
- **Jinja2 templates** (`src/templates/*.j2`) render system prompts. 2 templates: `worker.j2`, `writer.j2` — one per agent type.
- **Skills** (`src/skills/{worker,writer}/*/SKILL.md`) provide task-specific instructions via progressive disclosure. 7 skills total: 5 worker, 2 writer. Each agent only sees its own skills directory.
- **Tool separation** — worker has `read_pdf`, `read_docx`, `fetch_url`, `research_sources`; writer has no custom tools (uses only framework-provided `read_file`/`write_file`). `_BlockToolsMiddleware` blocks `edit_file`, `grep`, `glob`, `write_todos` on the writer at code level.
- **Retry middleware** — `_RetryMalformedMiddleware` retries on `MALFORMED_FUNCTION_CALL` / zero-output `STOP` (Gemini glitch). `ModelRetryMiddleware` retries on transient 503/429 errors with exponential backoff. Both applied to all agents.
- **Input flow** — `build_message_content()` writes extracted text to `/input/extracted.md` for the worker. Multimodal content (scanned PDF images) stays in `/input/` for the worker to access via `read_pdf`.
- **`research_sources` tool** — owned by the worker, fans out queries across Semantic Scholar, OpenAlex, and Crossref in parallel, deduplicates by DOI/title, writes registry JSON. Zero LLM tokens consumed by the tool itself.
- **Agent independence** — each pipeline step uses a unique `thread_id`, giving agents a clean conversation. Agents read what they need from VFS (disk).
- **Parallel source reading** — step 4 uses `ThreadPoolExecutor(max_workers=5)` to read multiple sources concurrently.
- **CompositeBackend** routes `/brief/`, `/plan/`, `/sources/`, `/essay/`, `/output/`, `/input/` to `FilesystemBackend` (run_dir subdirectories on disk); `/skills/` routes to `src/skills/` on disk.
- **Custom AI endpoint** — when `AI_BASE_URL` is set in `.env`, all models route through an OpenAI-compatible endpoint using `AI_API_KEY` and `AI_MODEL`.
- **Deterministic export** — step 8 calls `_build_document` directly from Python. No LLM involved. Prefers `/essay/reviewed.md`, falls back to `/essay/draft.md`.
- **Validate step** — after intake, the worker evaluates the brief for significant gaps. If found, prints numbered questions with options and collects answers via `input()`. Answers are appended to `assignment.md` as a `## Clarifications` section. If no gaps, pipeline continues automatically. The `on_questions` callback in `run_pipeline` makes this interactive behavior pluggable.

### Test Fixtures

Place test assignment directories under `examples/`. Each subdirectory is a self-contained test case with assignment files (PDFs, images, text).

## Design Documents

- `docs/DEEPAGENTS_REFERENCE.md` — framework API reference from source code analysis
