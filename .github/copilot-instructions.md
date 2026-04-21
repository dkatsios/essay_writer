# AI Guidance — Essay Writer

This is the canonical AI guidance file for the essay writer project. See `.github/instructions/documentation-sync.instructions.md` for the documentation sync policy.

## Commands

```bash
# Install dependencies
uv sync

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
5. **Read sources** (worker) — staged: (a) score title+abstract relevance via `source_triage.j2` → `SourceScoreBatch` (1–5 scale), filter by `min_relevance_score`, (b) fetch PDF content only for sources above threshold (non-PDF URLs skipped), (c) select top T, (d) full extraction on selected sources via `source_reading.j2` → `SourceNote`
5.5. **Assign sources** (worker, long path only) — assigns selected sources to sections based on content fit → `source_assignments.json`
6. **Write** (writer) — short path writes a full draft; long path drafts most sections in parallel, then writes full-context sections (for example intro/conclusion) after the rest of the draft exists → `essay/sections/*.md`, `draft.md`
6.5. **Reconcile sections** (worker, long path only) — inspects all drafted sections and produces per-section correction notes for overlap, transitions, and boundary cleanup → `essay/reconciliation.json`
7. **Review** (reviewer) — reviews draft, writes polished version; long-path review receives only the current section plus its own reconciliation notes → `reviewed.md`
8. **Export** — pure Python `build_document()` call → `essay.docx`

### Three-Model Architecture

| Role | Google (default) | OpenAI | Anthropic | Templates | Purpose |
|------|-----------------|--------|-----------|-----------|----------|
| **worker** | `gemini-2.5-flash` | `gpt-5.4-nano` | `claude-haiku-4-5` | `intake.j2`, `validate.j2`, `plan.j2`, `source_triage.j2`, `source_reading.j2`, `source_assignment.j2` | Structured data extraction (brief, plan, source scoring, notes, source assignment) |
| **writer** | `gemini-3.1-pro-preview` | `gpt-5.4` | `claude-sonnet-4-6` | `essay_writing.j2`, `section_writing.j2` | Essay text generation |
| **reviewer** | `gemini-3.1-pro-preview` | `gpt-5.4` | `claude-opus-4-6` | `essay_review.j2`, `section_review.j2` | Essay review and polish |

Set `models.provider` (or `ESSAY_WRITER_MODELS__PROVIDER`) to `google`, `openai`, or `anthropic` to switch all three roles at once. Individual role overrides still take precedence.

This runtime is a direct pipeline, not a deepagents/LangGraph system. The pipeline calls models via:
- Instructor `client.chat.completions.create(response_model=PydanticSchema)` for JSON steps (auto-retry on validation failure)
- OpenAI SDK `client.chat.completions.create(messages=[...])` for text steps (essays)

All tool calls (research, URL fetching, PDF reading) are plain Python functions called by the pipeline, not by the LLM.

### Templates (per-task Jinja2)

11 templates in `src/templates/`, one per pipeline task. Each template contains a `<!-- SPLIT -->` marker that separates **system instructions** (role identity, behavioral rules, style guidelines) from **user content** (variable data, task description). `render_prompt()` returns a `PromptPair(system, user)` namedtuple; the pipeline helpers route these into separate `system` and `user` messages in the API call. Templates without a marker produce `system=None` (user-only message).

| Template | Context variables | Output |
|----------|-------------------|--------|
| `intake.j2` | `extracted_text`, `extra_prompt` | `AssignmentBrief` JSON |
| `validate.j2` | `brief_json` | `ValidationResult` JSON |
| `plan.j2` | `brief_json` | `EssayPlan` JSON |
| `source_triage.j2` | `essay_topic`, `thesis`, `sections` (list of {title, key_points}), `sources` (list of {source_id, title, abstract}) | `SourceScoreBatch` JSON |
| `source_reading.j2` | `source_id`, `title`, `authors`, `year`, `doi`, `abstract`, `content`, `essay_topic` | `SourceNote` JSON |
| `source_assignment.j2` | `sections`, `source_notes`, `min_per_section` | `SourceAssignmentPlan` JSON |
| `essay_writing.j2` | `brief_json`, `plan_json`, `source_notes`, `target_words` | Essay markdown |
| `essay_review.j2` | `brief_json`, `plan_json`, `draft_text`, `target_words` | Reviewed markdown |
| `section_writing.j2` | `plan_json`, `source_notes`, `section`, `assigned_source_ids`, `has_full_context`, `essay_context` | Section markdown |
| `section_reconciliation.j2` | `plan_json`, `drafted_sections`, `language` | `EssayReconciliationPlan` JSON |
| `section_review.j2` | `section`, `full_essay`, `reconciliation_instructions` | Reviewed section markdown |

### File Layout (run directory)

Each run uses a directory with these subdirectories:

- `brief/assignment.json` — assignment brief (Pydantic `AssignmentBrief`)
- `brief/validation.json` — validation result (Pydantic `ValidationResult`); each `ValidationQuestion` includes `suggested_option_index` (0-based default for the web UI)
- `plan/plan.json` — essay plan (Pydantic `EssayPlan`); each section may set `requires_full_context` for intro/conclusion/synthesis sections that should be drafted after the rest of the essay, and `deferred_order` to control the writing sequence among deferred sections
- `plan/source_assignments.json` — source-to-section assignments (Pydantic `SourceAssignmentPlan`; long-path only)
- `sources/registry.json` — source metadata (from `run_research()`)
- `sources/scores.json` — 1–5 relevance scores for all scored candidates plus `selected_for_writing`
- `sources/notes/{source_id}.json` — reader notes for selected sources only (Pydantic `SourceNote`)
- `sources/selected.json` — best N sources selected for the essay
- `sources/user/` — user-provided source files and their extracted text
- `essay/sections/{position:02d}.md` — long-path section drafts, keyed by internal section position
- `essay/draft.md` — initial essay draft
- `essay/reconciliation.json` — long-path per-section reconciliation notes (Pydantic `EssayReconciliationPlan`)
- `essay/reviewed/{position:02d}.md` — long-path reviewed section files keyed by internal section position
- `essay/reviewed.md` — reviewed/polished essay (used for export)
- `input/extracted.md` — pre-extracted document text

All file I/O is done by the pipeline Python code — LLMs never read or write files.

### Input Handling

The web UI accepts uploaded files plus an optional prompt. The intake module (`src/intake.py`):
- Scans and categorizes files by extension
- Extracts text from `.md`, `.txt`, `.pdf`, `.docx`, `.pptx`
- For scanned PDFs (sparse text extraction), inserts a short text placeholder in `extracted.md` (no page rasterization; no OCR)
- Encodes images (`.png`, `.jpg`, etc.) as base64 for multimodal LLM consumption

Supported: `.md`, `.txt`, `.text`, `.rst`, `.csv`, `.tsv`, `.log`, `.pdf`, `.docx`, `.pptx`, `.png`, `.jpg`, `.jpeg`, `.gif`, `.bmp`, `.tiff`, `.webp`, `.svg`

### Configuration

Uses `pydantic-settings` (`BaseSettings`) with two layers (highest wins):

1. **Environment variables** — prefix `ESSAY_WRITER_`, nested with `__` (e.g., `ESSAY_WRITER_MODELS__PROVIDER=openai` or `ESSAY_WRITER_MODELS__WORKER=google_genai:gemini-2.5-flash`)
2. **Field defaults** — in the Pydantic models at `config/schemas.py`

No `default.yaml` exists; field defaults in `schemas.py` are canonical.

**Provider presets** — `_PROVIDER_PRESETS` in `config/schemas.py` maps `google`, `openai`, `anthropic` to default (worker, writer, reviewer) model specs. When `models.provider` is set, roles not explicitly overridden get the preset values. Explicit role settings always win.

**Google credentials** — for the direct Google provider path, `GOOGLE_API_KEY` may be either a classic Gemini Developer API key or a Vertex AI `AQ.` API key. The web UI's explicit credential field also accepts pasted Vertex service-account JSON. `AQ.` keys must have `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` set so `src/agent.py` can route the request through the Vertex provider alias. For pasted service-account JSON, `src/agent.py` may use the JSON `project_id` when `GOOGLE_CLOUD_PROJECT` is unset, but `GOOGLE_CLOUD_LOCATION` is still required. When `AI_BASE_URL` is set, model calls use the gateway credentials instead of direct Google credential autodetection.

### Key Invariants

- **Deterministic pipeline** — `src/pipeline.py` builds and runs the step sequence, while `src/pipeline_support.py`, `src/pipeline_sources.py`, and `src/pipeline_writing.py` hold the shared helpers and phase-specific implementations. No LLM decides the flow. Steps 1–5 use the worker model; step 6 uses the writer model; step 7 uses the reviewer model; step 8 is pure Python.
- **Direct model calls** — no agent framework. `_structured_call()` uses Instructor `client.chat.completions.create(response_model=Schema)` for JSON steps with auto-retry on validation failure. `_text_call()` uses OpenAI SDK `client.chat.completions.create()` for text steps. Both accept a `PromptPair` (or plain `str`) and route `system`/`user` into separate messages via `_build_messages()`.
- **Jinja2 templates** (`src/templates/*.j2`) render prompts. 10 templates, one per pipeline task. Each uses a `<!-- SPLIT -->` marker to separate system instructions from user content; `render_prompt()` returns `PromptPair(system, user)`. `style_common.j2` and `style_review.j2` are shared partials included in the system portion of writer/reviewer templates.
- **Explicit structure preservation** — when the user provides a concrete outline, named headings, or required section order in the prompt or assignment materials, intake should capture it and planning/writing/review prompts should preserve it rather than normalizing it into a generic essay template.
- **Plain function tools** — `run_research()`, `fetch_url_content()`, `read_pdf_text()`, `read_docx_text()` are plain Python functions called by the pipeline. No `@tool` decorators.
- **Retry logic** — `_retry_with_backoff()` in `src/agent.py` handles transient 429/503 API errors and timeouts with exponential backoff. All clients are created with a 300-second request timeout (`_REQUEST_TIMEOUT`) to prevent hung connections. Instructor handles validation retries.
- **Input flow** — `scan()` extracts content and `build_extracted_text()` writes `input/extracted.md` directly into the run directory. The pipeline reads that file and passes it to the intake template.
- **`run_research()`** — runs queries with bounded query-level concurrency, fans each query out across Semantic Scholar, OpenAlex, and Crossref, deduplicates by DOI/title, and writes registry JSON. Zero LLM tokens consumed.
- **Config-backed search controls** — `search.fetch_per_api` sets the per-API-per-query fetch limit (default 20), independent of how many sources the essay ultimately uses. `search.triage_batch_size` controls the 1–5 relevance scoring batch size (default 50). `search.min_relevance_score` controls the minimum score for source selection (default 3).
- **Recovery search controls** — `search.recovery_overfetch_multiplier`, `search.recovery_fetch_per_api_multiplier`, and `search.recovery_prefer_fulltext` control the automatic recovery rerun. Recovery can trigger at two points: (1) after scoring, if sources above threshold are below `target_sources`, and (2) after selection, if selected sources are below target and no scoring recovery already ran. Only one research rerun happens per pipeline run. Already-scored source IDs plus DOI/title dedup sets prevent re-processing.
- **Citation-aware ranking** — after deduplication, sources are sorted by citation count (higher first), then accessibility tier (OA PDF > DOI > metadata-only) as tiebreaker. This ensures highly-cited paywalled papers outrank low-citation OA papers. OpenAlex requests use sort `relevance_score:desc`: that field is **OpenAlex’s API relevance** (how well each work matches that search request). It is unrelated to **`SourceNote.relevance_score`** in `schemas.py`, which is a **1–5 topic-fit score** assigned by the worker during source reading from the assignment brief’s `topic` (`source_reading.j2`).
- **Shared HTTP transport** — search APIs and URL fetching use a shared `httpx.Client` with centralized retry behavior and connection pooling in `src/tools/_http.py`.
- **Pricing** — shared runtime helpers in `src/runtime.py` use the `genai-prices` package for automatic per-model pricing across all providers; the web job flow consumes that shared logic.
- **Staged source filtering** — Phase 1: filter out sources with no useful abstract or no authors. Phase 2: batch-score title+abstract relevance via `source_triage.j2` (batches of `search.triage_batch_size`, default 50), producing 1–5 scores; sources below `min_relevance_score` are filtered out. Phase 2b (conditional): if sources above threshold < `target_sources`, recovery research rerun + score new candidates (skipping already-scored IDs and DOI/title dupes). Phase 3: fetch PDF content only for above-threshold API sources (non-PDF URLs are skipped — analysis showed 89% are garbage HTML). Phase 4: select top T. Phase 4b (conditional): if selected < target and no scoring recovery ran, selection recovery rerun + score/fetch new candidates. Phase 5: full LLM extraction (`source_reading.j2`) on selected sources only. `SourceNote` files are produced only for selected sources.
- **`_select_top_sources`** — ranks batch-scored candidates and returns top `target_sources` IDs. Sources with `relevance_score < search.min_relevance_score` are filtered out (default 3). Sort keys (all descending, `reverse=True`): **`user_provided`** → **batch relevance_score** → **`citation_count`** → **has_fulltext**. If the usable pool is smaller than `target_sources`, the selected set is smaller — not padded.
- **Source target scaling** — `target_sources` uses log-based scaling (`sources_per_1k_words × 3 × log2(1 + words/1000)`) for diminishing marginal growth, floored by `search.min_sources` or the brief/user minimum. The web UI auto-fills the suggested source count when the user enters a word target. `fetch_sources` is `max(target_sources, target × overfetch_multiplier)`. Writing and review prompts clamp the minimum distinct-source requirement to the number of actually selected usable sources, so the model is never asked to cite more sources than exist in the usable selected pool.
- **Selected sources drive writing** — after source reading, `sources/selected.json` contains the usable source set intended for essay generation. If the file is missing or stale, the pipeline can still fall back to all accessible notes, but an intentionally empty selected set remains empty.
- **Source shortfall handling** — after the first score + select pass, if `selected.json` still contains fewer usable sources than the target, the pipeline performs one deterministic recovery rerun with a larger fetch budget and full-text-biased API filters where available. If the usable selected pool is still below target, the pipeline pauses before writing and asks the user whether to continue with the smaller source set.
- **Long-path writer context** — writer prompts include full summaries for a **lexically ranked** subset (`search.section_source_full_detail_max`) plus a **compact catalog** of every selected source (id, authors, year, title). Review steps see essay text only (refiner; no source bodies).
- **User-provided sources** — users can supply their own reference PDFs/documents via the "Your Sources" upload (web UI). These are saved to `sources/user/`, injected into `registry.json` with `user_provided: true` and placeholder metadata. User sources skip batch scoring and go directly to full extraction, which extracts both notes and bibliographic metadata (title, authors, year, DOI) from the content via the LLM, then backfills the registry. User sources are always prioritized in `_select_top_sources`.
- **Optional PDF uploads (web)** — after the first source-read pass, API sources with `SourceNote.fetched_fulltext=False` (abstract-only or short fetch) may be offered to the user (up to `search.optional_pdf_prompt_top_n`, lexical relevance from brief/plan + citation count). The UI accepts a local PDF file or an http(s) URL to a PDF; text is saved under `sources/supplement/` and `registry[id].content_path` is set, then those IDs are re-read before `selected.json`. If no optional-PDF callback is configured, the pipeline logs a hint instead of blocking. Config: `optional_pdf_prompt_top_n`, `optional_pdf_min_body_words`.
- **Short vs long path** — essays ≤ `long_essay_threshold` (default 4000 words) use full-essay write/review. Longer essays use section-by-section write/review with a source-assignment step (5.5), hybrid section drafting, and a reconciliation step before review.
- **Source assignment (long path)** — after source reading, the worker assigns each selected source to the sections where it is most relevant (`source_assignment.j2` → `SourceAssignmentPlan`). During section writing, assigned sources are boosted to the top of the detail window so the writer sees their full summaries, and the template requires citing each assigned source at least once.
- **Hybrid long-essay drafting** — body sections without `requires_full_context` are drafted in parallel. Sections marked `requires_full_context` are drafted afterward sorted by `deferred_order` (ascending, then by position) so later sections see more context. Both fields are set by the LLM in the plan — no heuristic overrides.
- **Long-essay reconciliation** — after drafting, the worker emits `essay/reconciliation.json` with section-position-keyed notes for overlap, transition, scope, and intro/conclusion alignment fixes. Reviewers receive only the current section’s note bundle.
- **Long-path identifiers** — use `Section.position` (plan order) as the runtime key for section draft files, review files, source assignment routing, and reconciliation routing. `section.number` may repeat in user-visible headings and must not be used as the unique runtime identifier. LLM-facing templates (`source_assignment.j2`, `section_reconciliation.j2`) expose and request `section_position`, not `section_number`.
- **Bounded review context** — section review includes only adjacent sections with the current section delimited. Deferred writing may see the full current draft; review remains section-local.
- **Config-backed word tolerance** — `writing.word_count_tolerance` (default 10%) controls the under-target tolerance in writing and review prompts. `writing.word_count_tolerance_over` (default 20%) controls the over-target tolerance for reviewers only. Reviewers use asymmetric thresholds: they are told to cut only when significantly over the target, and always see a hard floor forbidding output below `target × (1 - word_count_tolerance)`. Writers still use symmetric tolerance.
- **Custom AI endpoint** — when `AI_BASE_URL` is set in `.env`, all models route through an OpenAI-compatible endpoint using `AI_API_KEY` and `AI_MODEL`.
- **Deterministic export** — step 8 calls `build_document()` directly from Python. No LLM involved. Prefers `reviewed.md`, falls back to `draft.md`.
- **Markdown table support** — `_parse_and_add_content()` detects standard markdown tables (pipe-delimited with `|---|` separator) and renders them as native Word tables via `doc.add_table()`. Writing templates instruct the LLM to use tables sparingly when they improve clarity.
- **Validate step** — after intake, the worker evaluates the brief for significant gaps. If found, the `on_questions` callback receives those questions and the chosen answers are stored as structured `clarifications` in `assignment.json`, one entry per answered question. Validation options and persisted clarification answers must be standalone and self-explanatory; do not use options like "all of the above" / "Συνδυασμός των παραπάνω" that lose meaning once stored without the full choice list.
- **Structured outputs** — brief, validation, plan, and source notes are JSON files validated by Pydantic models in `src/schemas.py`. Essays remain markdown.

### Test Fixtures

Place test assignment directories under `examples/`. Each subdirectory is a self-contained test case with assignment files (PDFs, images, text).

### Web UI

`src/web.py` is a thin FastAPI route layer that wraps the pipeline. Shared web job state, TTL cleanup, optional-PDF handling, and background execution live in `src/web_jobs.py`. A single HTML page (`src/templates/web/index.html`) provides a form (prompt, file upload, target word count). Jobs run in background threads; validation questions pause the pipeline thread via `threading.Event` and are pushed to the browser via Server-Sent Events (SSE). The web path respects `writing.interactive_validation` through the validation callback flow. Results are returned as a ZIP containing the docx, markdown, and sources metadata.

- **SSE (`/stream/{job_id}`)** — the primary status channel. The server holds a long-lived `text/event-stream` connection and pushes `data: {JSON}\n\n` events whenever `job.status` or the pipeline stage changes. The client uses the native `EventSource` API which handles automatic reconnection. `_notify_job(job)` sets a `threading.Event` on each state transition; the SSE generator wakes on that signal or after a 2-second timeout (to catch stage changes within `running`). Terminal states (`done`, `error`, `gone`) close the generator so `EventSource` does not reconnect.
- **Download lifecycle** — `GET /download/{job_id}` returns the ZIP without deleting it so interrupted transfers can retry safely. The browser UI follows that with `POST /download/{job_id}/cleanup` after it has received the blob, which deletes the temp directory and removes the job from memory. Jobs that finish (`done` or `error`) but are never cleaned up are removed automatically after a TTL (default 24h). Optional environment variables: `ESSAY_WEB_JOB_TTL_SECONDS` (default `86400`, use `0` to disable TTL cleanup only), `ESSAY_WEB_JOB_SWEEP_INTERVAL_SECONDS` (default `300`, minimum `60` between sweeps), and `ESSAY_WEB_INTERACTION_TIMEOUT_SECONDS` (default `1800`) for paused `questions` / `optional_pdfs` waits before the job fails.
