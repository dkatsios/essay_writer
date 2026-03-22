# Essay Writer — Technical Design

This document maps the high-level design (see `DESIGN.md`) to concrete implementation decisions using the `deepagents` framework (see `DEEPAGENTS_REFERENCE.md`).

---

## 1. Project Structure

```
essay_writer/
├── pyproject.toml
├── config/
│   ├── __init__.py
│   ├── default.yaml                 # Default configuration
│   └── schemas.py                   # Pydantic config models
├── src/
│   ├── __init__.py
│   ├── agent.py                     # create_essay_agent() — main entry point
│   ├── subagents.py                 # SubAgent definitions (3 types)
│   ├── intake.py                    # Input scanning and content extraction
│   ├── rendering.py                 # Jinja template loading and rendering
│   ├── runner.py                    # CLI / programmatic runner
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── _http.py                 # Shared HTTP error helpers
│   │   ├── academic_search.py       # Semantic Scholar search
│   │   ├── openalex_search.py       # OpenAlex search
│   │   ├── crossref_search.py       # Crossref search
│   │   ├── pdf_reader.py            # PDF text extraction (pymupdf)
│   │   ├── docx_reader.py           # DOCX text extraction (python-docx)
│   │   ├── docx_builder.py          # DOCX document assembly
│   │   ├── web_fetcher.py           # URL content fetching (httpx)
│   │   └── word_counter.py          # Word count tool
│   ├── templates/
│   │   ├── orchestrator.j2          # Orchestrator system prompt
│   │   ├── intake.j2                # Intake subagent system prompt
│   │   ├── reader.j2                # Reader subagent system prompt
│   │   └── reviewer.j2              # Reviewer subagent system prompt
│   └── skills/
│       ├── essay-writing/
│       │   └── SKILL.md             # Writing guidance for the orchestrator
│       ├── essay-review/
│       │   └── SKILL.md             # Review checklist for the reviewer
│       └── docx-export/
│           └── SKILL.md             # Export instructions for the orchestrator
├── tests/
│   └── test_refactoring.py          # Unit tests (30 tests)
├── examples/                        # Test assignment directories
├── output/                          # Default output directory
└── docs/
    ├── DESIGN.md                    # High-level design
    ├── DEEPAGENTS_REFERENCE.md      # Framework API reference
    └── TECHNICAL_DESIGN.md          # This file
```

---

## 2. Configuration

### 2.1 Approach: YAML + Pydantic Settings

Configuration uses `pydantic-settings` (`BaseSettings`) with three layers (highest wins):

1. **Environment variables** — prefix `ESSAY_WRITER_`, nested with `__`
2. **YAML config file** — `config/default.yaml` by default, override with `--config`
3. **Field defaults** — in the Pydantic models

### 2.2 Configuration Schema

```yaml
# config/default.yaml

models:
  orchestrator: "google_genai:gemini-2.5-flash"
  intake: "google_genai:gemini-2.5-flash"
  reader: "google_genai:gemini-2.5-flash"
  reviewer: "google_genai:gemini-2.5-flash"

writing:
  word_count_tolerance: 0.10        # ±10%

formatting:
  font: "Times New Roman"
  font_size: 12
  line_spacing: 1.5
  margins_cm: 2.5
  citation_style: "apa7"
  page_numbers: "bottom_center"
  paragraph_indent: true

search:
  max_sources_per_direction: 5
  prefer_greek_sources: true
  search_language: ["el", "en"]

paths:
  output_dir: "./output"
  skills_dir: "/skills/"
```

### 2.3 Pydantic Models (`config/schemas.py`)

```python
class ModelsConfig(BaseModel):
    orchestrator: str = "google_genai:gemini-2.5-flash"
    intake: str = "google_genai:gemini-2.5-flash"
    reader: str = "google_genai:gemini-2.5-flash"
    reviewer: str = "google_genai:gemini-2.5-flash"

class WritingConfig(BaseModel):
    word_count_tolerance: float = 0.10

class FormattingConfig(BaseModel):
    font: str = "Times New Roman"
    font_size: int = 12
    line_spacing: float = 1.5
    margins_cm: float = 2.5
    citation_style: str = "apa7"
    page_numbers: str = "bottom_center"
    paragraph_indent: bool = True

class SearchConfig(BaseModel):
    max_sources_per_direction: int = 5
    prefer_greek_sources: bool = True
    search_language: list[str] = ["el", "en"]

class PathsConfig(BaseModel):
    output_dir: str = "./output"
    skills_dir: str = "/skills/"

class EssayWriterConfig(BaseSettings):
    models: ModelsConfig = ModelsConfig()
    writing: WritingConfig = WritingConfig()
    formatting: FormattingConfig = FormattingConfig()
    search: SearchConfig = SearchConfig()
    paths: PathsConfig = PathsConfig()
```

### 2.4 Custom AI Endpoint

When `AI_BASE_URL` is set (with `AI_API_KEY` and optionally `AI_MODEL`), all models route through an OpenAI-compatible custom endpoint. The `_resolve_model()` function in `agent.py` handles this:
- If `AI_BASE_URL` is set, creates a `ChatOpenAI` instance via `init_chat_model("openai:<model_name>", base_url=..., api_key=...)`
- `AI_MODEL` overrides the model name; if unset, the model name from the config spec is used

---

## 3. Backend Setup

### 3.1 CompositeBackend

`CompositeBackend` routes file operations to different backends based on path prefix:

```python
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend

def _create_backend(config, input_staging_dir=None, sources_dir=None):
    def factory(runtime):
        routes = {
            "/output/": FilesystemBackend(root_dir=config.paths.output_dir, virtual_mode=True),
        }
        if input_staging_dir is not None:
            routes["/input/"] = FilesystemBackend(root_dir=input_staging_dir, virtual_mode=True)
        if sources_dir is not None:
            routes["/sources/"] = FilesystemBackend(root_dir=sources_dir, virtual_mode=True)
        return CompositeBackend(default=StateBackend(runtime), routes=routes)
    return factory
```

**Routing logic**:
- `/input/` → `FilesystemBackend` — user-provided files staged in a temp directory
- `/output/` → `FilesystemBackend` — the final `.docx` is written to disk
- `/sources/` → `FilesystemBackend` — downloaded source PDFs persist to `.output/run_*/sources/`
- Everything else (`/brief/`, `/plan/`, `/essay/`, `/skills/`) → `StateBackend` — VFS artifacts in LangGraph state, checkpointed automatically

### 3.2 Input Handling

The CLI (`src/runner.py`) and intake module (`src/intake.py`) handle input before the agent starts:

1. `scan()` — scans the input path, classifies files by extension, extracts text from PDFs/DOCX/PPTX, encodes images as base64
2. `stage_files()` — copies recognized files into a temp directory for the `/input/` backend route
3. `build_message_content()` — assembles extracted content into a `HumanMessage` (plain text or multimodal with image blocks)

The intake subagent receives the pre-extracted content in its task description — it does not re-read files from `/input/`.

---

## 4. Jinja Template Rendering

### 4.1 Template Engine (`src/rendering.py`)

```python
from jinja2 import Environment, FileSystemLoader

def render_prompt(template_name: str, **context) -> str:
    env = _get_env("src/templates")
    template = env.get_template(template_name)
    return template.render(**context)
```

The environment is cached via `@lru_cache`.

### 4.2 Template Usage

Templates are rendered at agent creation time. Each template receives the full `config` object and uses Jinja2 expressions to inject settings:

- `{{ config.search.max_sources_per_direction }}` — number of sources to find
- `{{ config.writing.word_count_tolerance }}` — word count tolerance
- `{{ config.formatting.font }}` — font name
- `{% if config.search.prefer_greek_sources %}` — conditional blocks

---

## 5. Subagent Definitions

All subagents are defined in `src/subagents.py` via a data-driven spec table. Each entry maps to a `SubAgent` TypedDict.

### 5.1 Spec Table

```python
_SUBAGENT_SPECS = [
    ("intake",   "intake.j2",   "intake",   "Synthesizes pre-extracted document content...", False),
    ("reader",   "reader.j2",   "reader",   "Reads a single academic source...",             False),
    ("reviewer", "reviewer.j2", "reviewer", "Reviews and polishes the essay draft...",        True),
]
```

The boolean flag indicates whether the subagent gets `skills` (access to `/skills/` directory).

### 5.2 Factory Function

```python
def make_subagent(name, config, tools):
    agent = {
        "name": name,
        "description": description,
        "system_prompt": render_prompt(template, config=config),
        "model": getattr(config.models, model_attr),
        "tools": tools,
    }
    if has_skills:
        agent["skills"] = [config.paths.skills_dir]
    return agent
```

### 5.3 Tool Assignment

- **intake**: No custom tools (`tools=[]`). Receives all content via task description. VFS tools (read_file, write_file) are added by framework middleware.
- **reader**: Document reading tools (`read_pdf`, `read_docx`, `fetch_url`, `count_words`).
- **reviewer**: Same document reading tools plus skills access for the review checklist.

---

## 6. Tool Definitions

### 6.1 Search Tools

Three academic search tools, each returning JSON-formatted results:

- **academic_search** — Queries Semantic Scholar API. Supports language parameter.
- **openalex_search** — Queries OpenAlex API. Good coverage of open-access papers.
- **crossref_search** — Queries Crossref API. Strong for DOI resolution and metadata.

All search tools handle HTTP errors gracefully (return JSON error response instead of crashing).

### 6.2 Document Tools

- **read_pdf** — Extracts text from PDF files via `pymupdf`. Supports page ranges.
- **read_docx** — Extracts text and structure from `.docx` files via `python-docx`. Preserves heading markers.
- **fetch_url** — Fetches URL content via `httpx`, strips HTML. Handles HTTP errors gracefully.

### 6.3 Output Tools

- **build_docx** — Constructs a formatted `.docx` from essay text + config JSON (title, author, font, spacing, etc.). Produces cover page, TOC, headings, page numbers.
- **count_words** — Simple word count by splitting on whitespace/punctuation.

---

## 7. Orchestrator Design

### 7.1 System Prompt

Rendered from `templates/orchestrator.j2`. Defines the 7-step workflow where the orchestrator handles planning, searching, writing, and export directly. Key sections:

- **Role definition**: Academic essay writing agent producing Greek essays.
- **Workflow steps**: Detailed instructions for all 7 steps.
- **Subagent reference**: When to call each of the 3 subagent types.
- **VFS structure**: Directory layout and file naming.
- **Rules**: Language, source integrity, word discipline, file operations, efficiency.

### 7.2 Agent Assembly (`src/agent.py`)

```python
def create_essay_agent(config, input_staging_dir=None, sources_dir=None, checkpointer=None):
    all_tools = [academic_search, openalex_search, crossref_search,
                 fetch_url, read_pdf, read_docx, count_words, build_docx]
    doc_tools = [read_pdf, read_docx, fetch_url, count_words]

    subagents = [
        make_intake(config, []),
        make_reader(config, doc_tools),
        make_reviewer(config, doc_tools),
    ]

    return create_deep_agent(
        model=_resolve_model(config.models.orchestrator),
        tools=all_tools,
        system_prompt=render_prompt("orchestrator.j2", config=config),
        subagents=subagents,
        skills=[config.paths.skills_dir],
        backend=_create_backend(config, input_staging_dir, sources_dir),
        checkpointer=checkpointer,
        name="essay-orchestrator",
    )
```

The orchestrator gets ALL tools (search + document + export). Subagents get only what they need.

---

## 8. Skills

Skills provide detailed, on-demand workflow instructions. Agents read the full skill via `read_file` when entering the relevant step. 3 skills:

### 8.1 essay-writing

Writing guidance for the orchestrator (Step 5). Covers: reading materials, source integration, APA7 citations in Greek, academic register, word count verification, common pitfalls.

### 8.2 essay-review

Review checklist for the reviewer subagent (Step 6). Covers: structural review, language review, citation audit, completeness check, references formatting. Output: polished essay at `/essay/final.md`.

### 8.3 docx-export

Export instructions for the orchestrator (Step 7). Covers: metadata extraction, config JSON preparation, heading mapping, cover page layout, TOC insertion.

---

## 9. Runner (`src/runner.py`)

### 9.1 CLI Entry Point

```bash
uv run python -m src.runner /path/to/files/              # File/directory input
uv run python -m src.runner -p "Write an essay on X"      # Prompt-only mode
uv run python -m src.runner /path/ --dump-vfs             # With VFS dump
uv run python -m src.runner --resume .output/run_*/       # Resume from checkpoint
```

### 9.2 Run Flow

1. `scan()` and `build_message_content()` extract content from input files
2. `stage_files()` copies files to a temp directory for `/input/` route
3. `_ExecutionContext` sets up checkpointing (SqliteSaver) and file logging
4. `create_essay_agent()` builds the agent with the appropriate backend routes
5. `agent.invoke()` runs the orchestrator
6. `dump_vfs()` writes VFS contents to disk (if `--dump-vfs`)

### 9.3 Output

- `--dump-vfs` creates a timestamped directory under `.output/` with:
  - `run.log` — full debug log
  - `checkpoints.db` — SQLite checkpoint DB (for resume)
  - `sources/` — downloaded source PDFs
  - `vfs/` — dumped VFS contents (brief, plan, essay, etc.)

---

## 10. Failure Handling

### 10.1 Search Failures

Search tools return JSON error responses instead of crashing. The orchestrator sees the error and can retry with different terms or skip that source.

### 10.2 URL Fetch Failures

`fetch_url` catches HTTP errors and returns error strings. The reader subagent reports inaccessible sources clearly.

### 10.3 Subagent Failures

If a `task` call fails, the orchestrator sees the error in the tool result and decides how to proceed — retry, skip, or adjust the approach. This is handled by the orchestrator's system prompt rather than framework-level retry logic.

---

## 11. Dependencies

Key dependencies (see `pyproject.toml` for full list):

- `deepagents>=0.4.11` — LangGraph-based multi-agent framework
- `langchain-openai` — for custom AI endpoint routing
- `jinja2` — template rendering
- `python-docx` — DOCX reading and building
- `pymupdf` — PDF text extraction
- `python-pptx` — PPTX text extraction
- `httpx` — HTTP requests for search tools and URL fetching
- `pydantic-settings` — configuration management
- `pyyaml` — YAML config loading

