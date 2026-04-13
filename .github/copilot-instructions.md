# AI Guidance — Essay Writer

This is the canonical AI guidance file for the essay writer project. See `.github/instructions/documentation-sync.instructions.md` for the documentation sync policy.

## Commands

```bash
# Install dependencies
uv sync

# Run the pipeline — point at a file or directory with assignment materials
uv run python -m src.runner /path/to/assignment/
uv run python -m src.runner /path/to/brief.pdf
uv run python -m src.runner /path/to/files/ -p "Focus on economic aspects"

# Prompt-only mode (no files)
uv run python -m src.runner -p "Write a 3000-word essay on climate change"

# Provide your own reference sources
uv run python -m src.runner /path/to/files/ --sources /path/to/my/papers/

# Custom config
uv run python -m src.runner /path/to/files/ --config my_config.yaml

# Run the web UI
uv run uvicorn src.web:app --reload

# Docker
docker build -t essay-writer .
docker run -p 8000:8000 --env-file .env essay-writer

# Run tests
uv run python -m pytest tests/ -v

# Import check
uv run python -c "from src.agent import create_client, _retry_with_backoff"
```

## Architecture

Deterministic Python pipeline for academic essay writing using the OpenAI SDK + Instructor rather than a deepagents/LangGraph orchestration layer. Produces academic essays as formatted `.docx` files in the language specified by the assignment brief (defaults to Greek). The orchestration entrypoint in `src/pipeline.py` delegates to focused helper modules: shared execution helpers in `src/pipeline_support.py`, source-processing steps in `src/pipeline_sources.py`, and writing/export steps in `src/pipeline_writing.py`. Three LLM model roles — **worker**, **writer**, and **reviewer** — perform the language tasks.

### Flow

1. **Intake** (worker) — reads extracted text, produces `AssignmentBrief` via Instructor structured output
2. **Validate** (worker) — checks brief completeness; if gaps found, prompts user interactively and updates brief JSON
3. **Plan** (worker) — creates sections, word targets, research queries → `EssayPlan` via structured output
4. **Research** — pure Python: extracts queries from plan, calls `run_research()` → `registry.json`
5. **Read sources** (worker, parallel) — pipeline fetches URLs via `fetch_url_content()`, then worker extracts `SourceNote` via structured output
5.5. **Assign sources** (worker, long path only) — assigns selected sources to sections based on content fit → `source_assignments.json`
6. **Write** (writer) — writes the complete essay via OpenAI SDK `chat.completions.create()` → `draft.md`
7. **Review** (reviewer) — reviews draft, writes polished version → `reviewed.md`
8. **Export** — pure Python `build_document()` call → `essay.docx`

### Three-Model Architecture

| Role | Google (default) | OpenAI | Anthropic | Templates | Purpose |
|------|-----------------|--------|-----------|-----------|----------|
| **worker** | `gemini-2.5-flash` | `gpt-5.4-nano` | `claude-haiku-4-5` | `intake.j2`, `validate.j2`, `plan.j2`, `source_reading.j2`, `source_assignment.j2` | Structured data extraction (brief, plan, notes, source assignment) |
| **writer** | `gemini-3.1-pro-preview` | `gpt-5.4` | `claude-sonnet-4-6` | `essay_writing.j2`, `section_writing.j2` | Essay text generation |
| **reviewer** | `gemini-3.1-pro-preview` | `gpt-5.4` | `claude-opus-4-6` | `essay_review.j2`, `section_review.j2` | Essay review and polish |

Set `models.provider` (or `ESSAY_WRITER_MODELS__PROVIDER`) to `google`, `openai`, or `anthropic` to switch all three roles at once. Individual role overrides still take precedence.

This runtime is a direct pipeline, not a deepagents/LangGraph system. The pipeline calls models via:
- Instructor `client.chat.completions.create(response_model=PydanticSchema)` for JSON steps (auto-retry on validation failure)
- OpenAI SDK `client.chat.completions.create(messages=[...])` for text steps (essays)

All tool calls (research, URL fetching, PDF reading) are plain Python functions called by the pipeline, not by the LLM.

### Templates (per-task Jinja2)

9 templates in `src/templates/`, one per pipeline task. Each template receives specific context variables and renders the complete prompt for that step:

| Template | Context variables | Output |
|----------|-------------------|--------|
| `intake.j2` | `extracted_text`, `extra_prompt` | `AssignmentBrief` JSON |
| `validate.j2` | `brief_json` | `ValidationResult` JSON |
| `plan.j2` | `brief_json` | `EssayPlan` JSON |
| `source_reading.j2` | `source_id`, `title`, `authors`, `year`, `doi`, `abstract`, `content`, `essay_topic` | `SourceNote` JSON |
| `source_assignment.j2` | `plan_json`, `source_notes`, `min_per_section` | `SourceAssignmentPlan` JSON |
| `essay_writing.j2` | `brief_json`, `plan_json`, `source_notes`, `target_words` | Essay markdown |
| `essay_review.j2` | `brief_json`, `plan_json`, `draft_text`, `target_words` | Reviewed markdown |
| `section_writing.j2` | `plan_json`, `source_notes`, `section`, `prior_sections`, `assigned_source_ids` | Section markdown |
| `section_review.j2` | `section`, `full_essay` | Reviewed section markdown |

### File Layout (run directory)

Each run uses a directory with these subdirectories:

- `brief/assignment.json` — assignment brief (Pydantic `AssignmentBrief`)
- `brief/validation.json` — validation result (Pydantic `ValidationResult`); each `ValidationQuestion` includes `suggested_option_index` (0-based default for UI/CLI)
- `plan/plan.json` — essay plan (Pydantic `EssayPlan`)
- `plan/source_assignments.json` — source-to-section assignments (Pydantic `SourceAssignmentPlan`; long-path only)
- `sources/registry.json` — source metadata (from `run_research()`)
- `sources/notes/{source_id}.json` — reader notes, one file per source (Pydantic `SourceNote`)
- `sources/selected.json` — best N sources selected for the essay
- `sources/user/` — user-provided source files and their extracted text
- `essay/draft.md` — initial essay draft
- `essay/reviewed.md` — reviewed/polished essay (used for export)
- `input/extracted.md` — pre-extracted document text

All file I/O is done by the pipeline Python code — LLMs never read or write files.

### Input Handling

The CLI accepts a file or directory path. The intake module (`src/intake.py`):
- Scans and categorizes files by extension
- Extracts text from `.md`, `.txt`, `.pdf`, `.docx`, `.pptx`
- For scanned PDFs (sparse text extraction), inserts a short text placeholder in `extracted.md` (no page rasterization; no OCR)
- Encodes images (`.png`, `.jpg`, etc.) as base64 for multimodal LLM consumption

Supported: `.md`, `.txt`, `.text`, `.rst`, `.csv`, `.tsv`, `.log`, `.pdf`, `.docx`, `.pptx`, `.png`, `.jpg`, `.jpeg`, `.gif`, `.bmp`, `.tiff`, `.webp`, `.svg`

### Configuration

Uses `pydantic-settings` (`BaseSettings`) with two layers (highest wins):

1. **Environment variables** — prefix `ESSAY_WRITER_`, nested with `__` (e.g., `ESSAY_WRITER_MODELS__PROVIDER=openai` or `ESSAY_WRITER_MODELS__WORKER=google_genai:gemini-2.5-flash`)
2. **Custom YAML config file** — override with `--config path/to/custom.yaml`
3. **Field defaults** — in the Pydantic models at `config/schemas.py`

No `default.yaml` exists; field defaults in `schemas.py` are canonical.

**Provider presets** — `_PROVIDER_PRESETS` in `config/schemas.py` maps `google`, `openai`, `anthropic` to default (worker, writer, reviewer) model specs. When `models.provider` is set, roles not explicitly overridden get the preset values. Explicit role settings always win.

**Google credentials** — for the direct Google provider path, `GOOGLE_API_KEY` may be either a classic Gemini Developer API key or a Vertex AI `AQ.` API key. `AQ.` keys must also have `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` set so `src/agent.py` can route the request through the Vertex provider alias. When `AI_BASE_URL` is set, model calls use the gateway credentials instead of direct Google credential autodetection.

### Key Invariants

- **Deterministic pipeline** — `src/pipeline.py` builds and runs the step sequence, while `src/pipeline_support.py`, `src/pipeline_sources.py`, and `src/pipeline_writing.py` hold the shared helpers and phase-specific implementations. No LLM decides the flow. Steps 1–5 use the worker model; step 6 uses the writer model; step 7 uses the reviewer model; step 8 is pure Python.
- **Direct model calls** — no agent framework. `_structured_call()` uses Instructor `client.chat.completions.create(response_model=Schema)` for JSON steps with auto-retry on validation failure. `_text_call()` uses OpenAI SDK `client.chat.completions.create()` for text steps.
- **Jinja2 templates** (`src/templates/*.j2`) render prompts. 8 templates, one per pipeline task.
- **Explicit structure preservation** — when the user provides a concrete outline, named headings, or required section order in the prompt or assignment materials, intake should capture it and planning/writing/review prompts should preserve it rather than normalizing it into a generic essay template.
- **Plain function tools** — `run_research()`, `fetch_url_content()`, `read_pdf_text()`, `read_docx_text()` are plain Python functions called by the pipeline. No `@tool` decorators.
- **Retry logic** — `_retry_with_backoff()` in `src/agent.py` handles transient 429/503 API errors and timeouts with exponential backoff. All clients are created with a 300-second request timeout (`_REQUEST_TIMEOUT`) to prevent hung connections. Instructor handles validation retries.
- **Input flow** — `scan()` extracts content and `build_extracted_text()` writes `input/extracted.md` directly into the run directory. The pipeline reads that file and passes it to the intake template.
- **`run_research()`** — runs queries with bounded query-level concurrency, fans each query out across Semantic Scholar, OpenAlex, and Crossref, deduplicates by DOI/title, and writes registry JSON. Zero LLM tokens consumed.
- **Config-backed search controls** — `search.fetch_per_api` sets the per-API-per-query fetch limit (default 20), independent of how many sources the essay ultimately uses.
- **Citation-aware ranking** — after deduplication, sources are sorted by citation count (higher first), then accessibility tier (OA PDF > DOI > metadata-only) as tiebreaker. This ensures highly-cited paywalled papers outrank low-citation OA papers. OpenAlex requests use sort `relevance_score:desc`: that field is **OpenAlex’s API relevance** (how well each work matches that search request). It is unrelated to **`SourceNote.relevance_score`** in `schemas.py`, which is a **1–5 topic-fit score** assigned by the worker during source reading from the assignment brief’s `topic` (`source_reading.j2`).
- **Shared HTTP transport** — search APIs and URL fetching use a shared `httpx.Client` with centralized retry behavior and connection pooling in `src/tools/_http.py`.
- **Pricing** — shared runtime helpers in `src/runtime.py` use the `genai-prices` package for automatic per-model pricing across all providers; `src/runner.py` and the web job flow both consume that shared logic.
- **Parallel source reading** — `asyncio.gather()` with `Semaphore(6)` reads **every** API row in `registry.json` (plus all user uploads) concurrently, bounded only by registry size. URL fetching runs in `asyncio.to_thread()`, LLM calls use async Instructor client.
- **`_select_best_sources`** — builds `sources/selected.json` with up to `target_sources` IDs. Sources with `relevance_score < 2` (barely relevant) are filtered out before ranking. Only sources whose `SourceNote` has `is_accessible` are ranked; the rest are “inaccessible” for ordering. Sort keys (all descending where numeric, `reverse=True` on the composite tuple): **`user_provided`** in registry (user uploads first), **`SourceNote.relevance_score`**, registry row has at least one non-blank author string, registry **`citation_count`**, then **`content_word_count`** on the note. If fewer than `target_sources` are accessible, remaining slots are filled from inaccessible IDs in encounter order (no re-ranking).
- **Source target scaling** — `target_sources` uses log-based scaling (`sources_per_1k_words × 3 × log2(1 + words/1000)`) for diminishing marginal growth, floored by `search.min_sources` or the brief/user minimum. The web UI auto-fills the suggested source count when the user enters a word target. `fetch_sources` is `max(target_sources, target × overfetch_multiplier)`. After plan, `brief.min_sources` is set to `max(target_sources, user/brief minimum)` so writers always see a citation floor aligned with the selected-source target.
- **Selected sources drive writing** — after source reading, `sources/selected.json` is the preferred source set for essay generation. If the selected set has no accessible notes, the pipeline falls back to all accessible notes.
- **Long-path writer context** — writer prompts include full summaries for a **lexically ranked** subset (`search.section_source_full_detail_max`) plus a **compact catalog** of every selected source (id, authors, year, title). Review steps see essay text only (refiner; no source bodies).
- **User-provided sources** — users can supply their own reference PDFs/documents via `--sources` (CLI) or the "Your Sources" upload (web UI). These are saved to `sources/user/`, injected into `registry.json` with `user_provided: true` and placeholder metadata. The source-reading step extracts both notes and bibliographic metadata (title, authors, year, DOI) from the content via the LLM, then backfills the registry. User sources are always read first and are prioritized in `_select_best_sources`.
- **Optional PDF uploads (web)** — after the first source-read pass, API sources with `SourceNote.fetched_fulltext=False` (abstract-only or short fetch) may be offered to the user (up to `search.optional_pdf_prompt_top_n`, lexical relevance from brief/plan + citation count). The UI accepts a local PDF file or an http(s) URL to a PDF; text is saved under `sources/supplement/` and `registry[id].content_path` is set, then those IDs are re-read before `selected.json`. CLI runs print a stderr hint instead of blocking. Config: `optional_pdf_prompt_top_n`, `optional_pdf_min_body_words`.
- **Short vs long path** — essays ≤ `long_essay_threshold` (default 4000 words) use full-essay write/review. Longer essays use section-by-section write/review with a source-assignment step (5.5) that distributes sources across sections.
- **Source assignment (long path)** — after source reading, the worker assigns each selected source to the sections where it is most relevant (`source_assignment.j2` → `SourceAssignmentPlan`). During section writing, assigned sources are boosted to the top of the detail window so the writer sees their full summaries, and the template requires citing each assigned source at least once.
- **Bounded long-essay context** — section writing includes only the most recent prior sections, and section review includes only adjacent sections with the current section delimited. This keeps prompt growth roughly linear instead of resending the full essay on every section review.
- **Config-backed word tolerance** — `writing.word_count_tolerance` (default 10%) controls the under-target tolerance in writing and review prompts. `writing.word_count_tolerance_over` (default 20%) controls the over-target tolerance for reviewers only. Reviewers use asymmetric thresholds: they are told to cut only when significantly over the target, and always see a hard floor forbidding output below `target × (1 - word_count_tolerance)`. Writers still use symmetric tolerance.
- **Custom AI endpoint** — when `AI_BASE_URL` is set in `.env`, all models route through an OpenAI-compatible endpoint using `AI_API_KEY` and `AI_MODEL`.
- **Deterministic export** — step 8 calls `build_document()` directly from Python. No LLM involved. Prefers `reviewed.md`, falls back to `draft.md`.
- **Markdown table support** — `_parse_and_add_content()` detects standard markdown tables (pipe-delimited with `|---|` separator) and renders them as native Word tables via `doc.add_table()`. Writing templates instruct the LLM to use tables sparingly when they improve clarity.
- **Validate step** — after intake, the worker evaluates the brief for significant gaps. If found, prints numbered questions with options and collects answers via `input()`. Answers are stored as structured `clarifications` in `assignment.json`, one entry per answered question. The `on_questions` callback in `run_pipeline` makes this interactive behavior pluggable.
- **Structured outputs** — brief, validation, plan, and source notes are JSON files validated by Pydantic models in `src/schemas.py`. Essays remain markdown.

### Test Fixtures

Place test assignment directories under `examples/`. Each subdirectory is a self-contained test case with assignment files (PDFs, images, text).

### Web UI

`src/web.py` is a thin FastAPI route layer that wraps the same pipeline used by the CLI. Shared web job state, TTL cleanup, optional-PDF handling, and background execution live in `src/web_jobs.py`. A single HTML page (`src/templates/web/index.html`) provides a form (prompt, file upload, target word count). Jobs run in background threads; validation questions pause the pipeline thread via `threading.Event` and are pushed to the browser via Server-Sent Events (SSE). The web path respects `writing.interactive_validation` the same way as the CLI. Results are returned as a ZIP containing the docx, markdown, and sources metadata.

- **SSE (`/stream/{job_id}`)** — the primary status channel. The server holds a long-lived `text/event-stream` connection and pushes `data: {JSON}\n\n` events whenever `job.status` or the pipeline stage changes. The client uses the native `EventSource` API which handles automatic reconnection. `_notify_job(job)` sets a `threading.Event` on each state transition; the SSE generator wakes on that signal or after a 2-second timeout (to catch stage changes within `running`). Terminal states (`done`, `error`, `gone`) close the generator so `EventSource` does not reconnect.
- **Download lifecycle** — `GET /download/{job_id}` returns the ZIP without deleting it so interrupted transfers can retry safely. The browser UI follows that with `POST /download/{job_id}/cleanup` after it has received the blob, which deletes the temp directory and removes the job from memory. Jobs that finish (`done` or `error`) but are never cleaned up are removed automatically after a TTL (default 24h). Optional environment variables: `ESSAY_WEB_JOB_TTL_SECONDS` (default `86400`, use `0` to disable TTL cleanup only), `ESSAY_WEB_JOB_SWEEP_INTERVAL_SECONDS` (default `300`, minimum `60` between sweeps), and `ESSAY_WEB_INTERACTION_TIMEOUT_SECONDS` (default `1800`) for paused `questions` / `optional_pdfs` waits before the job fails.
