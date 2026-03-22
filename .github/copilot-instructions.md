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

Single-orchestrator essay writing system built on the `deepagents` framework (LangChain/LangGraph). Produces academic essays in Greek as formatted `.docx` files.

### Flow

The **orchestrator** handles planning, searching, writing, and export directly. It delegates to 3 specialized **subagent** types only when isolated context is needed:

1. **Intake** (subagent) — reads user documents, writes `/brief/assignment.md`
2. **Plan** — orchestrator plans sections, word targets, research topics → `/plan/plan.md`
3. **Search** — orchestrator uses `academic_search`, `openalex_search`, `crossref_search` directly
4. **Read sources** (subagent, parallel) — reader subagents fetch full-text sources, return condensed notes
5. **Write** — orchestrator writes the complete essay → `/essay/draft.md`
6. **Review** (subagent) — reviewer polishes the essay → `/essay/final.md`
7. **Export** — orchestrator calls `build_docx` → `/output/essay.docx`

### Subagents

| Type | Purpose | VFS output |
|------|---------|------------|
| **intake** | Synthesize pre-extracted document content into a structured brief | `/brief/assignment.md` |
| **reader** | Fetch/read a single source, return condensed notes | None (message return) |
| **reviewer** | Review + polish the draft | `/essay/final.md` |

### VFS (Virtual File System)

Key paths:

- `/brief/assignment.md` — assignment brief (from intake subagent)
- `/plan/plan.md` — essay plan (from orchestrator)
- `/essay/draft.md` — complete essay draft (from orchestrator)
- `/essay/final.md` — polished essay (from reviewer subagent)
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

- **Jinja2 templates** (`src/templates/*.j2`) render system prompts. 4 templates: `orchestrator.j2`, `intake.j2`, `reader.j2`, `reviewer.j2`.
- **Skills** (`src/skills/*/SKILL.md`) provide detailed instructions via progressive disclosure — agents read the full skill via `read_file` when needed. 3 skills: essay-writing, essay-review, docx-export.
- **Single-pass writing** — the orchestrator writes the complete essay in one go, using its own message history (plan, search results, reader notes).
- **Subagent independence** — subagents have NO conversation history from the parent. Every `task` call must include all necessary context in the `description` parameter. Multiple `task` calls in one message run in parallel.
- **CompositeBackend** routes `/input/` to a temp staging dir, `/output/` and `/sources/` to `FilesystemBackend` (real disk); everything else goes to `StateBackend` (LangGraph state, checkpointed).
- **Custom AI endpoint** — when `AI_BASE_URL` is set in `.env`, all models route through an OpenAI-compatible endpoint using `AI_API_KEY` and `AI_MODEL`.

### Test Fixtures

Place test assignment directories under `examples/`. Each subdirectory is a self-contained test case with assignment files (PDFs, images, text).

## Design Documents

- `docs/DESIGN.md` — high-level requirements and decisions
- `docs/TECHNICAL_DESIGN.md` — implementation blueprint mapping design to deepagents constructs
- `docs/DEEPAGENTS_REFERENCE.md` — framework API reference from source code analysis
