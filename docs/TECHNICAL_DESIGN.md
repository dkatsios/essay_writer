# Essay Writer — Technical Design

This document maps the high-level design (see `DESIGN.md`) to concrete implementation decisions using the `deepagents` framework (see `DEEPAGENTS_REFERENCE.md`).

---

## 1. Project Structure

```
essay_writer/
├── pyproject.toml
├── config/
│   ├── default.yaml                 # Default configuration
│   └── schemas.py                   # Pydantic config models
├── src/
│   ├── __init__.py
│   ├── agent.py                     # create_deep_agent() setup, main entry point
│   ├── subagents.py                 # SubAgent definitions for all roles
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── academic_search.py       # Google Scholar / Semantic Scholar tool
│   │   ├── pdf_reader.py            # PDF text extraction tool
│   │   ├── docx_reader.py           # DOCX text extraction tool
│   │   ├── docx_builder.py          # DOCX document assembly tool
│   │   ├── word_counter.py          # Word count tool
│   │   └── web_fetcher.py           # URL content fetching tool
│   ├── templates/
│   │   ├── orchestrator.j2          # Main orchestrator system prompt
│   │   ├── planner.j2               # Planner subagent system prompt
│   │   ├── researcher.j2            # Research agent system prompt
│   │   ├── cataloguer.j2            # Source cataloguer system prompt
│   │   ├── extractor.j2             # Source extractor system prompt
│   │   ├── writer.j2                # Section writer system prompt
│   │   ├── reviewer.j2              # Reviewer/polisher system prompt
│   │   └── builder.j2               # Document builder system prompt
│   ├── skills/
│   │   ├── essay-planning/
│   │   │   └── SKILL.md
│   │   ├── source-extraction/
│   │   │   └── SKILL.md
│   │   ├── section-writing/
│   │   │   └── SKILL.md
│   │   ├── essay-review/
│   │   │   └── SKILL.md
│   │   └── docx-export/
│   │       └── SKILL.md
│   ├── rendering.py                 # Jinja template loading and rendering
│   └── runner.py                    # CLI / programmatic runner
├── input/                           # User-provided source documents (PDFs, DOCX)
├── output/                          # Generated .docx files
└── docs/
    ├── DESIGN.md
    ├── DEEPAGENTS_REFERENCE.md
    └── TECHNICAL_DESIGN.md
```

---

## 2. Configuration

### 2.1 Approach: YAML + Pydantic

Configuration uses YAML files validated by Pydantic models. This gives us human-readable defaults (YAML) with strict type checking and IDE support (Pydantic).

### 2.2 Configuration Schema

```yaml
# config/default.yaml

models:
  orchestrator: "anthropic:claude-sonnet-4-6"
  planner: "anthropic:claude-sonnet-4-6"
  researcher: "anthropic:claude-sonnet-4-6"
  cataloguer: "google_genai:gemini-2.5-flash"
  extractor: "anthropic:claude-sonnet-4-6"
  writer: "anthropic:claude-sonnet-4-6"
  reviewer: "anthropic:claude-sonnet-4-6"
  builder: "anthropic:claude-sonnet-4-6"

pipeline:
  checkpoint_after_draft_plan: false
  checkpoint_after_final_plan: true
  checkpoint_after_review: false
  default_mode: "autonomous"        # "autonomous" or "interactive"

writing:
  word_count_tolerance: 0.10        # ±10%
  max_word_count_retries: 2
  long_essay_threshold: 3000        # words — above this, use summaries for prior context
  intro_strategy: "placeholder"     # "placeholder" (write first, revise after) or "write_last"

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
  input_dir: "./input"
  output_dir: "./output"
  skills_dir: "/skills/"
```

### 2.3 Pydantic Models

Defined in `config/schemas.py`:

```python
class ModelsConfig(BaseModel):
    orchestrator: str = "anthropic:claude-sonnet-4-6"
    planner: str = "anthropic:claude-sonnet-4-6"
    researcher: str = "anthropic:claude-sonnet-4-6"
    cataloguer: str = "google_genai:gemini-2.5-flash"
    extractor: str = "anthropic:claude-sonnet-4-6"
    writer: str = "anthropic:claude-sonnet-4-6"
    reviewer: str = "anthropic:claude-sonnet-4-6"
    builder: str = "anthropic:claude-sonnet-4-6"

class PipelineConfig(BaseModel):
    checkpoint_after_draft_plan: bool = False
    checkpoint_after_final_plan: bool = True
    checkpoint_after_review: bool = False
    default_mode: Literal["autonomous", "interactive"] = "autonomous"

class WritingConfig(BaseModel):
    word_count_tolerance: float = 0.10
    max_word_count_retries: int = 2
    long_essay_threshold: int = 3000
    intro_strategy: Literal["placeholder", "write_last"] = "placeholder"

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
    input_dir: str = "./input"
    output_dir: str = "./output"
    skills_dir: str = "/skills/"

class EssayWriterConfig(BaseModel):
    models: ModelsConfig = ModelsConfig()
    pipeline: PipelineConfig = PipelineConfig()
    writing: WritingConfig = WritingConfig()
    formatting: FormattingConfig = FormattingConfig()
    search: SearchConfig = SearchConfig()
    paths: PathsConfig = PathsConfig()
```

Config loading: read `config/default.yaml`, optionally merge with a user-provided override file, validate through `EssayWriterConfig`.

---

## 3. Backend Setup

### 3.1 CompositeBackend

We use `CompositeBackend` to route file operations:

```python
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend

def create_backend(runtime, config: EssayWriterConfig):
    return CompositeBackend(
        default=StateBackend(runtime),
        routes={
            "/input/": FilesystemBackend(
                root_dir=config.paths.input_dir,
                virtual_mode=True,
            ),
            "/output/": FilesystemBackend(
                root_dir=config.paths.output_dir,
                virtual_mode=True,
            ),
        },
    )
```

**Routing logic**:
- `/input/` → `FilesystemBackend` — user-provided PDFs and documents are read from disk.
- `/output/` → `FilesystemBackend` — the final `.docx` is written to disk.
- Everything else (`/brief/`, `/plan/`, `/sources/`, `/sections/`, `/essay/`, `/review/`) → `StateBackend` — intermediate VFS artifacts live in LangGraph state. Checkpointed automatically.

### 3.2 Pre-loading User Files

User-provided source PDFs are placed in `input/` before running the agent. The `FilesystemBackend` makes them accessible at `/input/source_01.pdf`, etc. The orchestrator's system prompt instructs it to check `/input/` for provided materials during Phase 1.

---

## 4. Jinja Template Rendering

### 4.1 Template Engine

```python
# src/rendering.py
from jinja2 import Environment, FileSystemLoader

def create_template_env(templates_dir: str = "src/templates") -> Environment:
    return Environment(
        loader=FileSystemLoader(templates_dir),
        trim_blocks=True,
        lstrip_blocks=True,
    )

def render_prompt(template_name: str, **context) -> str:
    env = create_template_env()
    template = env.get_template(template_name)
    return template.render(**context)
```

### 4.2 Template Usage

Templates are rendered **before** creating subagent definitions. The rendered output becomes the `system_prompt` string in the `SubAgent` dict:

```python
writer_prompt = render_prompt(
    "writer.j2",
    section=section_plan,
    academic_level="graduate",
    citation_style="apa7",
    word_target=800,
    total_essay_words=5000,
    long_essay_threshold=config.writing.long_essay_threshold,
    prior_sections=prior_summaries,   # or prior_full_text for short essays
    source_paths=["/sections/section_03/sources/"],
)
```

### 4.3 Template Example (writer.j2)

```jinja
You are an academic essay section writer. You write in Modern Greek (Δημοτική) at the {{ academic_level }} level.

## Your Task
Write section "{{ section.title }}" of the essay.
Target word count: {{ word_target }} words (±{{ (word_count_tolerance * 100)|int }}%).

## Essay Plan
{{ section.plan_context }}

## Prior Sections
{% if total_essay_words <= long_essay_threshold %}
The following are the full texts of all previously written sections:
{% for s in prior_sections %}
### {{ s.title }}
{{ s.full_text }}
{% endfor %}
{% else %}
The following are concise summaries of all previously written sections:
{% for s in prior_sections %}
### {{ s.title }}
{{ s.summary }}
{% endfor %}
{% endif %}

## Source Material
Read the following VFS paths for your source material:
{% for path in source_paths %}
- {{ path }}
{% endfor %}

## Citation Style
Use {{ citation_style }} format for all in-text citations.

## Output
Write ONLY the section content. Write it to VFS at {{ section.draft_path }}.
{% if total_essay_words > long_essay_threshold %}
Also write a concise summary (3-5 sentences) of this section to {{ section.summary_path }}.
{% endif %}
```

---

## 5. Subagent Definitions

All subagents are defined in `src/subagents.py`. Each returns a `SubAgent` TypedDict.

### 5.1 Planner

```python
def make_planner(config: EssayWriterConfig, tools: list) -> SubAgent:
    prompt = render_prompt("planner.j2", config=config)
    return {
        "name": "planner",
        "description": (
            "Creates and refines essay plans. Produces section breakdowns with "
            "word count targets, research directions, and source-to-section mappings. "
            "Use for both draft planning (before research) and plan refinement (after research)."
        ),
        "system_prompt": prompt,
        "model": config.models.planner,
        "tools": tools,
        "skills": [config.paths.skills_dir],
    }
```

### 5.2 Research Agent

```python
def make_researcher(config: EssayWriterConfig, tools: list) -> SubAgent:
    prompt = render_prompt("researcher.j2", config=config)
    return {
        "name": "researcher",
        "description": (
            "Searches academic databases for credible sources on specific topics. "
            "Writes structured source metadata to VFS. Use for targeted research "
            "based on specific research directions from the essay plan."
        ),
        "system_prompt": prompt,
        "model": config.models.researcher,
        "tools": tools,
    }
```

### 5.3 Source Cataloguer

```python
def make_cataloguer(config: EssayWriterConfig, tools: list) -> SubAgent:
    prompt = render_prompt("cataloguer.j2", config=config)
    return {
        "name": "cataloguer",
        "description": (
            "Reads a raw PDF source and produces a lightweight structured metadata "
            "entry (title, authors, abstract, introduction summary). Uses a cheaper "
            "model. Use when a source lacks structured metadata."
        ),
        "system_prompt": prompt,
        "model": config.models.cataloguer,
        "tools": tools,
    }
```

### 5.4 Source Extractor

```python
def make_extractor(config: EssayWriterConfig, tools: list) -> SubAgent:
    prompt = render_prompt("extractor.j2", config=config)
    return {
        "name": "extractor",
        "description": (
            "Reads a single source document in full and produces exhaustive, "
            "self-contained VFS entries for each section that uses this source. "
            "Includes quotes with page numbers, data, citation keys, and full "
            "bibliographic information. This is the sole access point for the source."
        ),
        "system_prompt": prompt,
        "model": config.models.extractor,
        "tools": tools,
        "skills": [config.paths.skills_dir],
    }
```

### 5.5 Section Writer

The writer is special — its system prompt is rendered per-invocation with section-specific context (prior sections, source paths, word target). The orchestrator renders the template and passes the result as the `description` in the `task` tool call, not as a static system prompt.

```python
def make_writer(config: EssayWriterConfig, tools: list) -> SubAgent:
    # Static base prompt — section-specific context comes via task description
    prompt = render_prompt("writer.j2", config=config, static_only=True)
    return {
        "name": "writer",
        "description": (
            "Writes a single essay section in academic Greek. Receives the full "
            "plan, prior sections context, and pre-extracted source material. "
            "Respects the word count target for the section."
        ),
        "system_prompt": prompt,
        "model": config.models.writer,
        "tools": tools,
        "skills": [config.paths.skills_dir],
    }
```

The orchestrator renders section-specific instructions and passes them in the `task` call's `description` parameter. The writer's system prompt contains static instructions (language, style, general behavior), while the `description` contains the dynamic per-section context.

### 5.6 Reviewer / Polisher

```python
def make_reviewer(config: EssayWriterConfig, tools: list) -> SubAgent:
    prompt = render_prompt("reviewer.j2", config=config)
    return {
        "name": "reviewer",
        "description": (
            "Reviews the assembled essay for coherence, language quality, "
            "citation correctness, and completeness. Can refine earlier sections. "
            "Produces feedback and a polished version of the essay."
        ),
        "system_prompt": prompt,
        "model": config.models.reviewer,
        "tools": tools,
        "skills": [config.paths.skills_dir],
    }
```

### 5.7 Document Builder

```python
def make_builder(config: EssayWriterConfig, tools: list) -> SubAgent:
    prompt = render_prompt("builder.j2", config=config)
    return {
        "name": "builder",
        "description": (
            "Converts the final essay text into a formatted .docx file with "
            "cover page, table of contents, headings, citations, references, "
            "and page numbers. Writes to /output/."
        ),
        "system_prompt": prompt,
        "model": config.models.builder,
        "tools": [docx_builder_tool],  # only needs the DOCX builder tool
    }
```

---

## 6. Tool Definitions

### 6.1 Academic Search Tool

Queries academic search APIs and returns structured results.

```python
@tool
def academic_search(
    query: str,
    language: str = "en",
    max_results: int = 5,
) -> str:
    """Search Google Scholar or Semantic Scholar for academic papers.
    Returns structured metadata: title, authors, year, DOI, abstract."""
```

**Implementation options** (to be evaluated):
- **SerpAPI** with Google Scholar engine — most reliable, paid API, returns structured data.
- **Semantic Scholar API** — free, good structured data, English-focused.
- **scholarly** (Python library) — free, scrapes Google Scholar, fragile.

The tool returns structured results that the research agent writes to VFS as source metadata.

### 6.2 PDF Reader Tool

```python
@tool
def read_pdf(
    file_path: str,
    pages: str | None = None,
) -> str:
    """Extract text from a PDF file. Supports page ranges (e.g., '1-5', '3,7-10').
    Returns extracted text with page markers."""
```

**Library**: `pymupdf` (PyMuPDF) — fast, reliable, handles Greek text well.

### 6.3 DOCX Reader Tool

```python
@tool
def read_docx(file_path: str) -> str:
    """Extract text and structure from a .docx file.
    Returns text with heading markers preserved."""
```

**Library**: `python-docx`.

### 6.4 DOCX Builder Tool

This is the most complex tool. It takes structured essay content and produces a formatted `.docx`.

```python
@tool
def build_docx(
    essay_path: str,
    output_path: str,
    config_json: str,
) -> str:
    """Build a formatted .docx document from the essay text.

    Args:
        essay_path: VFS path to the final essay markdown.
        output_path: Output file path (e.g., /output/essay.docx).
        config_json: JSON string with formatting config and metadata
            (title, author, course, date, citation_style, font, etc.)
    """
```

**Library**: `python-docx` for document construction.

The builder parses the essay markdown, applying:
- Heading styles (mapped from `#`, `##`, `###`)
- Body text formatting (font, size, spacing, margins)
- Cover page generation
- Table of contents (via Word's built-in TOC field)
- Page numbers
- References section formatting

Greek character support is native in `python-docx` since it works with Unicode.

### 6.5 Word Counter Tool

```python
@tool
def count_words(text: str) -> int:
    """Count the number of words in the given text.
    Handles Greek text correctly by splitting on whitespace."""
```

Simple tool — splits on whitespace, counts tokens. Exposed so the orchestrator can verify section lengths with exact numbers.

### 6.6 Web Fetcher Tool

```python
@tool
def fetch_url(url: str) -> str:
    """Fetch content from a URL and return as text.
    Strips HTML tags, returns plain text content."""
```

**Library**: `httpx` (already installed) + basic HTML-to-text stripping.

---

## 7. Orchestrator Design

### 7.1 System Prompt

The orchestrator's system prompt is rendered from `templates/orchestrator.j2` and defines the full pipeline. It instructs the agent to follow the 8 phases sequentially, using specific subagents and VFS paths at each step.

Key sections of the orchestrator prompt:
- **Role definition**: You are an essay writing orchestrator.
- **Pipeline phases**: Detailed instructions for each of the 8 phases.
- **VFS conventions**: The directory structure and naming conventions.
- **Subagent usage**: When to call which subagent, what to pass.
- **Decision logic**: When to retry, when to adjust the plan, when to proceed.
- **Word count management**: How to check and enforce word targets.

### 7.2 Orchestrator Flow (Pseudocode)

```
Phase 1: INTAKE
  - Read /input/ for user-provided documents
  - If documents exist: spawn parser subagents (parallel, one per doc)
  - Else: use the user's prompt directly
  - Assemble assignment brief → write to /brief/assignment.md

Phase 2: DRAFT PLAN
  - Call planner subagent with assignment brief
  - Planner reads /brief/assignment.md, writes /plan/draft.md
  - [CHECKPOINT if config.pipeline.checkpoint_after_draft_plan]

Phase 3: RESEARCH
  - Read /plan/draft.md to get research directions
  - Spawn N researcher subagents in parallel (one per research direction)
  - Each writes to /sources/metadata/source_XX.md
  - For user-provided sources: spawn cataloguer subagents in parallel
  - Each cataloguer writes to /sources/metadata/source_XX.md (with provided: true)

Phase 4: PLAN REFINEMENT
  - Call planner subagent with /brief/assignment.md + /sources/metadata/*.md
  - Planner writes /plan/final.md and /plan/source_mapping.md
  - [CHECKPOINT if config.pipeline.checkpoint_after_final_plan]

Phase 5: EXTRACTION
  - Read /plan/source_mapping.md
  - Spawn M extractor subagents in parallel (one per source)
  - Each extractor reads the source, writes to /sections/section_XX/sources/source_YY.md

Phase 6: SEQUENTIAL WRITING
  - Read /plan/final.md for section list and word targets
  - For each section (in writing order — see intro strategy):
    a. Render writer prompt with section context, prior sections, source paths
    b. Call writer subagent
    c. Read written draft, count words
    d. If outside tolerance: call writer again with adjustment instructions
    e. Append to running essay body
    f. [If long essay: also read summary for next iteration]

Phase 7: REVIEW
  - Assemble all section drafts → write /essay/assembled.md
  - Call reviewer subagent
  - Reviewer reads assembled essay + assignment brief
  - Writes /review/feedback.md and /essay/reviewed.md
  - [CHECKPOINT if config.pipeline.checkpoint_after_review]

Phase 8: EXPORT
  - Call builder subagent with /essay/reviewed.md and formatting config
  - Builder writes /output/essay.docx
  - Report completion to user
```

### 7.3 Introduction Strategy

**Decision: "placeholder" strategy** (configurable via `writing.intro_strategy`).

- **placeholder** (default): The introduction is written first in the sequential loop, but with an explicit instruction that it's a preliminary version. The reviewer/polisher revises it during Phase 7 once the full body exists — adding foreshadowing, correcting the structure preview, harmonizing tone.
- **write_last**: The writing order is: body sections first (in order), then introduction, then conclusion. The final document is reordered to the correct structure during assembly. This produces a better first-draft introduction but adds complexity.

The orchestrator's Jinja template uses `{% if config.writing.intro_strategy == 'write_last' %}` to adjust the section loop order.

---

## 8. Skills

Skills provide detailed, on-demand workflow instructions. They follow the deepagents SKILL.md format with progressive disclosure — the agent sees name/description in the system prompt and reads full instructions only when entering the relevant phase.

### 8.1 essay-planning

```markdown
---
name: essay-planning
description: Structured approach to creating academic essay plans with section breakdowns, word allocation, and research directions
---
# Essay Planning Skill
## When to Use
- When creating a draft plan from an assignment brief (Phase 2)
- When refining a plan after research (Phase 4)
## Draft Plan Instructions
1. Read the assignment brief from /brief/assignment.md
2. Identify the thesis statement and core arguments
3. Break the essay into sections...
[detailed instructions for plan structure, word allocation, research directions]
## Plan Refinement Instructions
1. Read source metadata from /sources/metadata/*.md
2. Compare available sources against planned sections...
[detailed instructions for adjusting plan based on sources]
```

### 8.2 source-extraction

```markdown
---
name: source-extraction
description: Extract section-specific content from academic sources with quotes, page numbers, and citation keys
---
# Source Extraction Skill
## Output Format
For each section this source is mapped to, write a VFS entry containing:
### Bibliographic Information
- Authors, title, year, journal/publisher, DOI/URL
- Citation key (e.g., "Παπαδόπουλος, 2021")
### Relevant Content
- Key arguments (bulleted, with page references)
- Direct quotes (with exact page numbers)
- Data/statistics/case studies
- Counter-arguments or limitations
### Relevance Assessment
- How this content supports the section's planned argument
```

### 8.3 section-writing

Detailed instructions for writing in academic Greek, integrating sources, managing word count, producing summaries for long essays.

### 8.4 essay-review

Detailed checklist for coherence, language quality, citation audit, completeness. Instructions for backward refinement of earlier sections.

### 8.5 docx-export

Instructions for document assembly, formatting rules, TOC generation, cover page layout.

---

## 9. Agent Assembly

### 9.1 Main Entry Point (`src/agent.py`)

```python
from deepagents import create_deep_agent
from config.schemas import EssayWriterConfig
from src.subagents import (
    make_planner, make_researcher, make_cataloguer,
    make_extractor, make_writer, make_reviewer, make_builder,
)
from src.tools import (
    academic_search, read_pdf, read_docx,
    build_docx, count_words, fetch_url,
)
from src.rendering import render_prompt

def create_essay_agent(config: EssayWriterConfig):
    # Tools available to most subagents (file ops are built-in)
    common_tools = [read_pdf, read_docx, count_words, fetch_url]
    research_tools = [academic_search, fetch_url]
    builder_tools = [build_docx]

    # Render orchestrator prompt
    orchestrator_prompt = render_prompt("orchestrator.j2", config=config)

    # Define subagents
    subagents = [
        make_planner(config, common_tools),
        make_researcher(config, research_tools),
        make_cataloguer(config, common_tools),
        make_extractor(config, common_tools),
        make_writer(config, common_tools),
        make_reviewer(config, common_tools),
        make_builder(config, builder_tools),
    ]

    # Build interrupt_on config from pipeline settings
    interrupt_on = {}
    # Checkpoints are handled by the orchestrator writing specific files
    # and the human reviewing them — not by tool-level interrupts

    return create_deep_agent(
        model=config.models.orchestrator,
        tools=common_tools,
        system_prompt=orchestrator_prompt,
        subagents=subagents,
        skills=[config.paths.skills_dir],
        backend=lambda rt: create_backend(rt, config),
        name="essay-orchestrator",
    )
```

### 9.2 Runner (`src/runner.py`)

```python
import yaml
from langchain_core.messages import HumanMessage
from config.schemas import EssayWriterConfig
from src.agent import create_essay_agent

def run(prompt: str, config_path: str = "config/default.yaml"):
    # Load config
    with open(config_path) as f:
        raw = yaml.safe_load(f)
    config = EssayWriterConfig(**raw)

    # Create agent
    agent = create_essay_agent(config)

    # Run
    result = agent.invoke({
        "messages": [HumanMessage(content=prompt)],
    })

    return result
```

---

## 10. Human-in-the-Loop Checkpoints

### 10.1 Implementation Approach

Rather than using deepagents' `interrupt_on` (which pauses at tool-level granularity), checkpoints are implemented as **explicit pauses in the orchestrator's workflow**. The orchestrator:

1. Writes the artifact to VFS (e.g., `/plan/draft.md`).
2. Outputs a message to the user summarizing the plan and asking for approval.
3. Waits for the user's response before proceeding.

This works naturally in a conversational interface — the agent simply asks the user and waits for the next message. In autonomous mode, the orchestrator skips the question and proceeds.

The orchestrator prompt template includes conditional blocks:

```jinja
{% if config.pipeline.checkpoint_after_final_plan %}
After writing the final plan, present it to the user and ask:
"The essay plan is ready. Would you like to review it before I proceed with writing?"
Wait for the user's response before continuing.
{% endif %}
```

### 10.2 Checkpointer

For multi-turn conversations (needed for checkpoints), a LangGraph checkpointer is required:

```python
from langgraph.checkpoint.memory import MemorySaver

checkpointer = MemorySaver()
agent = create_deep_agent(..., checkpointer=checkpointer)
```

---

## 11. Failure Handling

### 11.1 Research Finds No Sources

The orchestrator prompt instructs: if a research agent returns no sources for a section's research direction, note it. After all research completes, if any section has zero sources, instruct the planner during refinement to either merge the section into another, drop it, or flag it for writing from general knowledge with a disclaimer.

### 11.2 Corrupted / Unreadable Source

The extractor writes a VFS entry at the expected path with an error flag:

```markdown
# Source: source_03
## Status: UNREADABLE
The PDF could not be parsed. Error: [description]
```

The orchestrator checks for these flags after extraction and decides whether to proceed without the source or find a replacement.

### 11.3 Empty Extraction

If an extractor writes a "no useful content" entry for a section, the orchestrator counts how many sources each section has. If a section has no usable extracts, the orchestrator either re-invokes the planner to adjust or instructs the writer to use general knowledge with a note.

### 11.4 Subagent Failure

If a `task` call fails entirely (API error, timeout), the orchestrator:
1. Logs the failure.
2. Retries once.
3. If the retry fails, proceeds without that subagent's output and notes the gap.

This is handled by the orchestrator's system prompt rather than framework-level retry logic — the agent sees the error in the tool result and decides how to proceed.

---

## 12. User-Provided Sources

### 12.1 Flow

1. User places PDF/DOCX files in `input/` directory before running.
2. During Phase 1, the orchestrator lists `/input/` and identifies source files.
3. For each source file, the orchestrator spawns a cataloguer subagent (in parallel) to produce metadata. The metadata entry includes a `provided: true` flag.
4. During Phase 4 (plan refinement), the planner treats these as first-class sources. They are always included in the source-to-section mapping — the planner does not question their relevance.
5. Extraction and writing proceed normally.

### 12.2 Metadata Flag

```markdown
# Source: source_01
## Status: PROVIDED
## Provided: true

- **Title**: [extracted title]
- **Authors**: [extracted authors]
...
```

The `provided: true` flag tells the system:
- Do not question credibility.
- Do not try to find alternatives.
- These sources should appear in the final references.

---

## 13. Dependencies to Add

```toml
# pyproject.toml additions
[project]
dependencies = [
    "deepagents>=0.4.11",
    "jinja2>=3.1",
    "python-docx>=1.1",
    "pymupdf>=1.25",
    "pyyaml>=6.0",
    # Academic search (pick one during implementation):
    # "serpapi>=0.1"        # paid, reliable
    # "scholarly>=1.7"      # free, fragile
]
```

---

## 14. Resolved Decisions from DESIGN.md Section 13

| Decision | Resolution | Rationale |
|---|---|---|
| **Intro/conclusion order** | "placeholder" strategy (configurable) | Write intro first as preliminary, revise during review. Simpler loop design, reviewer handles polish. |
| **Human checkpoints** | Conversational pauses, config-controlled | Default: pause after final plan only. No tool-level interrupts needed. |
| **User-provided sources** | `provided: true` flag in metadata | Skip research, always include, don't question credibility. |
| **Failure handling** | Degrade and inform via orchestrator prompt | No framework-level retry. Agent sees errors and decides. |
| **VFS backend** | CompositeBackend (State + Filesystem) | State for intermediates, filesystem for input/output. |
| **Config approach** | YAML + Pydantic | Human-readable defaults, strict validation, IDE support. |
