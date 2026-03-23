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

Coordinator-subagent essay writing system built on the `deepagents` framework (LangChain/LangGraph). Produces academic essays in Greek as formatted `.docx` files. The orchestrator is a thin coordinator that delegates heavy work to specialized subagents, keeping each conversation context small.

### Flow

1. **Intake** (subagent) — reads user documents, writes `/brief/assignment.md`
2. **Plan** — orchestrator plans sections, word targets, research topics → `/plan/plan.md`
3. **Research** (subagent) — searches for academic sources → `/sources/registry.json`
4. **Read sources** (subagent, parallel) — reader subagents fetch full-text sources → `/sources/notes/{source_id}.md`
5. **Write** (subagent) — writes the complete essay → `/essay/draft.md`
6. **Review** (subagent) — reviews and polishes draft via `edit_file`
7. **Export** — orchestrator calls `build_docx` → `/output/essay.docx`

### Subagents

| Type | Purpose | VFS output |
|------|---------|------------|
| **intake** | Synthesize pre-extracted document content into a structured brief | `/brief/assignment.md` |
| **researcher** | Search for academic sources, build source registry | `/sources/registry.json` |
| **reader** | Fetch/read a single source, write condensed notes | `/sources/notes/{source_id}.md` |
| **writer** | Write the complete essay using plan and source notes | `/essay/draft.md` |
| **reviewer** | Review and polish the draft with targeted edits | `/essay/draft.md` (via `edit_file`) |

### VFS (Virtual File System)

Key paths:

- `/brief/assignment.md` — assignment brief (from intake subagent)
- `/plan/plan.md` — essay plan (from orchestrator)
- `/sources/registry.json` — source metadata (from researcher subagent)
- `/essay/draft.md` — complete essay draft (from writer, polished by reviewer)
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

- **Jinja2 templates** (`src/templates/*.j2`) render system prompts. 6 templates: `orchestrator.j2`, `intake.j2`, `researcher.j2`, `reader.j2`, `writer.j2`, `reviewer.j2`.
- **Skills** (`src/skills/*/SKILL.md`) provide detailed instructions via progressive disclosure — agents read the full skill via `read_file` when needed. 3 skills: essay-writing, essay-review, docx-export.
- **Retry middleware** (`_RetryMalformedMiddleware` in `src/agent.py`) — retries model calls that return `MALFORMED_FUNCTION_CALL` or zero-output-token `STOP` from Google Gemini. Applied to all agents.
- **Thin orchestrator** — the orchestrator is a lightweight coordinator. Heavy work (research, writing, review) is delegated to subagents that each get a clean context. This prevents token accumulation across phases.
- **Subagent independence** — subagents have NO conversation history from the parent. They read what they need from VFS. Multiple `task` calls in one message run in parallel.
- **CompositeBackend** routes `/input/` to a temp staging dir, `/output/` and `/sources/` to `FilesystemBackend` (real disk); everything else goes to `StateBackend` (in-memory LangGraph state).
- **Custom AI endpoint** — when `AI_BASE_URL` is set in `.env`, all models route through an OpenAI-compatible endpoint using `AI_API_KEY` and `AI_MODEL`.
- **Deterministic docx fallback** — if the orchestrator doesn't call `build_docx`, the runner nudges it and, as a last resort, calls `_build_document` directly from `/essay/draft.md`.

### Test Fixtures

Place test assignment directories under `examples/`. Each subdirectory is a self-contained test case with assignment files (PDFs, images, text).

## Design Documents

- `docs/DESIGN.md` — high-level requirements and decisions
- `docs/TECHNICAL_DESIGN.md` — implementation blueprint mapping design to deepagents constructs
- `docs/DEEPAGENTS_REFERENCE.md` — framework API reference from source code analysis
