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
uv run python -c "from src.agent import create_model, invoke_with_retry"
```

## Architecture

Deterministic Python pipeline for academic essay writing using direct LangChain model calls (no agent framework). Produces academic essays in Greek as formatted `.docx` files. A Python pipeline (`src/pipeline.py`) controls the 8-step workflow; three LLM model roles — **worker**, **writer**, and **reviewer** — perform the language tasks.

### Flow

1. **Intake** (worker) — reads extracted text, produces `AssignmentBrief` via `model.with_structured_output()`
2. **Validate** (worker) — checks brief completeness; if gaps found, prompts user interactively and updates brief JSON
3. **Plan** (worker) — creates sections, word targets, research queries → `EssayPlan` via structured output
4. **Research** — pure Python: extracts queries from plan, calls `run_research()` → `registry.json`
5. **Read sources** (worker, parallel) — pipeline fetches URLs via `fetch_url_content()`, then worker extracts `SourceNote` via structured output
6. **Write** (writer) — writes the complete essay via `model.invoke()` → `draft.md`
7. **Review** (reviewer) — reviews draft, writes polished version → `reviewed.md`
8. **Export** — pure Python `build_document()` call → `essay.docx`

### Three-Model Architecture

| Role | Model | Templates | Purpose |
|------|-------|-----------|---------|
| **worker** | `gemini-2.5-flash` | `intake.j2`, `validate.j2`, `plan.j2`, `source_reading.j2` | Structured data extraction (brief, plan, notes) |
| **writer** | `gemini-2.5-pro` | `essay_writing.j2`, `section_writing.j2` | Essay text generation |
| **reviewer** | `gemini-3.1-pro-preview` | `essay_review.j2`, `section_review.j2` | Essay review and polish |

No agents, no VFS, no middleware. The pipeline calls models directly:
- `model.with_structured_output(PydanticSchema)` for JSON steps (auto-retry on validation failure)
- `model.invoke([SystemMessage, HumanMessage])` for text steps (essays)

All tool calls (research, URL fetching, PDF reading) are plain Python functions called by the pipeline, not by the LLM.

### Templates (per-task Jinja2)

8 templates in `src/templates/`, one per pipeline task. Each template receives specific context variables and renders the complete prompt for that step:

| Template | Context variables | Output |
|----------|-------------------|--------|
| `intake.j2` | `extracted_text`, `extra_prompt` | `AssignmentBrief` JSON |
| `validate.j2` | `brief_json` | `ValidationResult` JSON |
| `plan.j2` | `brief_json` | `EssayPlan` JSON |
| `source_reading.j2` | `source_id`, `title`, `authors`, `year`, `doi`, `abstract`, `content` | `SourceNote` JSON |
| `essay_writing.j2` | `brief_json`, `plan_json`, `source_notes`, `target_words` | Essay markdown |
| `essay_review.j2` | `brief_json`, `plan_json`, `draft_text`, `target_words` | Reviewed markdown |
| `section_writing.j2` | `plan_json`, `source_notes`, `section`, `prior_sections` | Section markdown |
| `section_review.j2` | `section`, `full_essay` | Reviewed section markdown |

### File Layout (run directory)

Each run uses a directory with these subdirectories:

- `brief/assignment.json` — assignment brief (Pydantic `AssignmentBrief`)
- `brief/validation.json` — validation result (Pydantic `ValidationResult`)
- `plan/plan.json` — essay plan (Pydantic `EssayPlan`)
- `sources/registry.json` — source metadata (from `run_research()`)
- `sources/notes/{source_id}.json` — reader notes, one file per source (Pydantic `SourceNote`)
- `sources/selected.json` — best N sources selected for the essay
- `essay/draft.md` — initial essay draft
- `essay/reviewed.md` — reviewed/polished essay (used for export)
- `input/extracted.md` — pre-extracted document text

All file I/O is done by the pipeline Python code — LLMs never read or write files.

### Input Handling

The CLI accepts a file or directory path. The intake module (`src/intake.py`):
- Scans and categorizes files by extension
- Extracts text from `.md`, `.txt`, `.pdf`, `.docx`, `.pptx`
- For scanned PDFs (sparse text extraction), falls back to rendering pages as images
- Encodes images (`.png`, `.jpg`, etc.) as base64 for multimodal LLM consumption

Supported: `.md`, `.txt`, `.text`, `.rst`, `.csv`, `.tsv`, `.log`, `.pdf`, `.docx`, `.pptx`, `.png`, `.jpg`, `.jpeg`, `.gif`, `.bmp`, `.tiff`, `.webp`, `.svg`

### Configuration

Uses `pydantic-settings` (`BaseSettings`) with two layers (highest wins):

1. **Environment variables** — prefix `ESSAY_WRITER_`, nested with `__` (e.g., `ESSAY_WRITER_MODELS__WORKER=google_genai:gemini-2.5-flash`)
2. **Custom YAML config file** — override with `--config path/to/custom.yaml`
3. **Field defaults** — in the Pydantic models at `config/schemas.py`

No `default.yaml` exists; field defaults in `schemas.py` are canonical.

### Key Invariants

- **Deterministic pipeline** — `src/pipeline.py` runs steps in sequence. No LLM decides the flow. Steps 1–5 use the worker model; step 6 uses the writer model; step 7 uses the reviewer model; step 8 is pure Python.
- **Direct model calls** — no agent framework. `_structured_call()` uses `model.with_structured_output(Schema)` for JSON steps with auto-retry on validation failure. `_text_call()` uses `model.invoke()` for text steps.
- **Jinja2 templates** (`src/templates/*.j2`) render prompts. 8 templates, one per pipeline task.
- **Plain function tools** — `run_research()`, `fetch_url_content()`, `read_pdf_text()`, `read_docx_text()` are plain Python functions called by the pipeline. No `@tool` decorators.
- **Retry logic** — `invoke_with_retry()` in `src/agent.py` handles transient 429/503 API errors with exponential backoff. `_structured_call()` retries on Pydantic `ValidationError`.
- **Input flow** — `build_message_content()` extracts text and writes to `input/extracted.md`. The pipeline reads this and passes it to the intake template.
- **`run_research()`** — fans out queries across Semantic Scholar, OpenAlex, and Crossref in parallel, deduplicates by DOI/title, writes registry JSON. Zero LLM tokens consumed.
- **Parallel source reading** — `ThreadPoolExecutor(max_workers=3)` reads multiple sources concurrently.
- **Selected sources drive writing** — after source reading, `sources/selected.json` is the preferred source set for essay generation. If the selected set has no accessible notes, the pipeline falls back to all accessible notes.
- **Short vs long path** — essays ≤ `long_essay_threshold` (default 4000 words) use full-essay write/review. Longer essays use section-by-section write/review.
- **Custom AI endpoint** — when `AI_BASE_URL` is set in `.env`, all models route through an OpenAI-compatible endpoint using `AI_API_KEY` and `AI_MODEL`.
- **Deterministic export** — step 8 calls `build_document()` directly from Python. No LLM involved. Prefers `reviewed.md`, falls back to `draft.md`.
- **Validate step** — after intake, the worker evaluates the brief for significant gaps. If found, prints numbered questions with options and collects answers via `input()`. Answers are stored as `clarifications` in `assignment.json`. The `on_questions` callback in `run_pipeline` makes this interactive behavior pluggable.
- **Structured outputs** — brief, validation, plan, and source notes are JSON files validated by Pydantic models in `src/schemas.py`. Essays remain markdown.

### Test Fixtures

Place test assignment directories under `examples/`. Each subdirectory is a self-contained test case with assignment files (PDFs, images, text).
