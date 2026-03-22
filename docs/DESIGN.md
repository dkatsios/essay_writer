# Essay Writer Agent — Project Design Document

## 1. Overview

Essay Writer is a deep agent system that produces high-quality academic essays for Greek university students at both undergraduate and graduate levels. The system accepts various input documents and instructions, uses an AI orchestrator with specialized subagents, and delivers a properly formatted `.docx` document ready for submission.

The primary output language is **Greek**, though research and sources may be drawn from both Greek and international academic literature.

---

## 2. Target Users & Academic Levels

- **Undergraduate students** at Greek universities — essays tend to be shorter, more introductory, with lighter citation requirements.
- **Graduate / postgraduate students** — longer, more analytical essays with stricter academic standards, heavier use of primary sources, and more rigorous argumentation.

The system should adapt its tone, depth, vocabulary, and citation density based on the specified academic level.

---

## 3. Inputs

The system accepts a flexible combination of inputs:

### 3.1 Simple Mode
- A single **text prompt** describing the essay topic and any constraints (word count, style, etc.).

### 3.2 Document Mode
One or more input documents that may include:
- **Topic description** — the subject matter, research question, or thesis statement.
- **Assignment instructions** — word count, formatting rules, required structure, grading rubric.
- **Source materials** — PDFs, articles, or references that must be incorporated.
- **Style guidelines** — citation format (APA, Harvard, Chicago, custom university style), font, margins, language register.
- **Additional notes** — professor preferences, specific angles to cover, mandatory keywords or concepts.

### 3.3 Supported Input Formats
- Plain text / prompt
- PDF documents
- Word documents (.docx)
- PowerPoint presentations (.pptx)
- Images (.png, .jpg, .gif, .bmp, .tiff, .webp)
- Plain text formats (.md, .txt, .rst, .csv)

---

## 4. Pipeline — High-Level Workflow

The essay production follows a 7-step workflow driven by a single **orchestrator** agent. The orchestrator handles planning, searching, writing, and export directly. It delegates to **3 specialized subagent types** only when isolated context is needed — to keep large documents out of the orchestrator's context window.

Agents exchange data through a **Virtual File System (VFS)** — a shared, structured storage layer. Search results and reader notes flow via the orchestrator's message history (not VFS), keeping the design simple and efficient.

### Step 1: INTAKE (subagent)
- The CLI scans input files, extracts text from PDFs/DOCX/PPTX, and encodes images as base64 before the agent starts.
- The **intake** subagent receives this pre-extracted content in its task description.
- It synthesizes a structured **assignment brief** — topic, word count target, academic level, course details, specific instructions.
- The brief is written to VFS at `/brief/assignment.md`.

### Step 2: PLAN (orchestrator)
- The orchestrator reads the assignment brief and plans the essay directly.
- The plan includes: thesis statement, section breakdown with headings (typically 4-6 sections), word count targets per section, and research topics (in both Greek and English).
- Written to VFS at `/plan/plan.md`.

### Step 3: RESEARCH (orchestrator)
- The orchestrator searches for academic sources itself using `academic_search` (Semantic Scholar), `openalex_search` (OpenAlex), and `crossref_search` (Crossref).
- Searches in both Greek and English.
- Prioritizes Greek-language sources when available, but includes high-quality English sources too.
- Results stay in the orchestrator's message history — no intermediate VFS files needed.

### Step 4: READ SOURCES (subagent, parallel)
- For sources that need full-text access (to extract quotes, data, or detailed arguments), the orchestrator calls **reader** subagents.
- Each reader fetches a single source (URL or document), extracts relevant content, and returns condensed notes (200-500 words).
- Multiple readers can run in parallel (multiple `task` calls in one message).
- Reader notes return as messages — they stay in the orchestrator's history for use during writing.

### Step 5: WRITE (orchestrator)
- The orchestrator writes the complete essay in a single pass, using its own message history (plan, search results, reader notes).
- Follows the plan's section structure and word targets.
- Integrates sources with APA7 citations and includes a References section.
- Uses `count_words` to verify the total is within ±10% (configurable) of the target.
- Written to VFS at `/essay/draft.md`.

### Step 6: REVIEW (subagent)
- The **reviewer** subagent reads `/brief/assignment.md` and `/essay/draft.md`.
- It checks structure, thesis, language quality, citations, completeness, and introduction coherence.
- It applies corrections and writes the polished essay to `/essay/final.md`.

### Step 7: EXPORT (orchestrator)
- The orchestrator reads `/essay/final.md` and `/brief/assignment.md`.
- It calls `build_docx` with the essay text and formatting configuration.
- The `.docx` is written to `/output/essay.docx` (routed to real filesystem).

---

## 5. Agent Architecture

### 5.1 Orchestrator
The single orchestrator agent. It:
- Receives the user's message (with pre-extracted document content).
- Plans the essay, searches for sources, writes the draft, and exports the `.docx` — all directly.
- Delegates to subagents only for: intake (synthesize assignment brief), reading sources (isolate large documents from context), and review (fresh context with structured evaluation).
- Holds search results and reader notes in its message history.
- Reads/writes VFS for persistent artifacts (brief, plan, draft, final essay).

### 5.2 Subagents

| Subagent | Purpose | Parallelism |
|---|---|---|
| **intake** | Synthesizes pre-extracted document content into a structured assignment brief at `/brief/assignment.md`. | Single instance, once at start |
| **reader** | Reads a single academic source (URL or document), returns condensed notes as a message. Keeps large source text out of the orchestrator's context. | Multiple in parallel, one per source |
| **reviewer** | Reviews and polishes the essay draft. Reads `/brief/assignment.md` and `/essay/draft.md`, writes `/essay/final.md`. | Single instance, once after writing |

### 5.3 Tools

| Tool | Used by | Purpose |
|---|---|---|
| **academic_search** | Orchestrator | Queries Semantic Scholar for academic papers |
| **openalex_search** | Orchestrator | Queries OpenAlex for academic papers |
| **crossref_search** | Orchestrator | Queries Crossref for academic papers |
| **fetch_url** | Orchestrator, Reader | Fetches content from URLs |
| **read_pdf** | Orchestrator, Reader | Extracts text from PDF files |
| **read_docx** | Orchestrator, Reader | Extracts text and structure from Word documents |
| **build_docx** | Orchestrator | Constructs the final `.docx` with formatting |
| **count_words** | Orchestrator, Reviewer | Counts words in text |
| **VFS tools** | All agents (via framework) | `read_file`, `write_file`, `edit_file`, `ls` — provided by `deepagents` middleware |

---

## 6. Language Considerations

### 6.1 Output Language
- All essay text is written in **Modern Greek (Δημοτική)**.
- Academic register should match the level — more formal and technical for graduate work, clear and structured for undergraduate.
- Correct use of accents (τονισμός) and Greek punctuation (e.g., semicolon as question mark `;`, middle dot as semicolon `·`).

### 6.2 Source Language
- Sources can be in **Greek or English** (or other languages where relevant).
- When citing non-Greek sources in a Greek essay, follow the citation convention specified by the assignment (some professors want transliterated titles, others want originals).
- Quotes from English sources may need to be presented in the original language with a Greek translation, or paraphrased in Greek — depending on the style guide.

### 6.3 Terminology
- Use established Greek academic terminology where it exists.
- For terms that are commonly used in English even in Greek academic writing (e.g., "feedback", "marketing", "stakeholders"), follow the convention of the specific field.

---

## 7. Source Credibility Standards

Sources must meet academic credibility criteria:

### Acceptable
- Peer-reviewed journal articles
- Conference papers from recognized academic conferences
- Books and book chapters from academic publishers
- Theses and dissertations from university repositories
- Official reports from recognized institutions (EU, World Bank, OECD, Greek government bodies, etc.)
- Greek legal texts and court decisions (for law essays)
- Primary sources (historical documents, data sets) from trusted archives

### Use with Caution
- Wikipedia — only for initial orientation, never cited directly
- News articles — acceptable only as evidence of events, not as analytical sources
- Non-peer-reviewed preprints — note their status if used

### Not Acceptable
- Blog posts and opinion pieces (unless analyzing them as primary sources)
- Generic web content without clear authorship
- Marketing materials
- AI-generated content presented as a source

---

## 8. Output Specification

### 8.1 File Format
- Primary output: `.docx` (Microsoft Word format)
- The file must be fully self-contained — no external dependencies, no broken references.

### 8.2 Document Structure
A typical essay output includes (adjustable per assignment):

1. **Cover Page** — title, author, institution, course, date
2. **Table of Contents** — auto-generated, with page numbers
3. **Introduction** — context, thesis statement, structure preview
4. **Main Body** — organized in numbered sections and subsections
5. **Conclusion** — summary of arguments, final position, suggestions for further research
6. **References / Bibliography** — formatted per specified citation style
7. **(Optional) Appendices** — supplementary material, data tables, figures

### 8.3 Formatting Defaults
Unless overridden by assignment instructions:
- Font: Times New Roman, 12pt
- Line spacing: 1.5
- Margins: 2.5cm all sides
- Paragraph indentation: first line
- Page numbers: bottom center
- Headings: bold, hierarchical sizing
- Citation style: APA 7th edition (most common in Greek universities)

---

## 9. Configuration

The project uses a centralized configuration system (`pydantic-settings`) with three layers (highest wins):

1. **Environment variables** — prefix `ESSAY_WRITER_`, nested with `__`
2. **YAML config file** — `config/default.yaml` by default, override with `--config`
3. **Field defaults** — in the Pydantic models at `config/schemas.py`

Configuration covers:
- **Model selection** per agent role (orchestrator, intake, reader, reviewer).
- **Formatting defaults** — font, spacing, margins, citation style.
- **Search settings** — max sources per direction, language preferences.
- **Writing settings** — word count tolerance.
- **Paths** — output directory, skills directory.

When `AI_BASE_URL` is set (with `AI_API_KEY` and optionally `AI_MODEL`), all models route through an OpenAI-compatible custom endpoint.

---

## 10. Prompt & Instruction Templating

All agent system prompts use **Jinja2 templates** (`.j2` files) rather than static markdown. This provides variable injection, conditional logic, and loops for dynamic prompt construction.

4 templates: `orchestrator.j2`, `intake.j2`, `reader.j2`, `reviewer.j2`.

Templates are rendered at agent creation time. The rendered output becomes the `system_prompt` string in each agent's definition. Jinja is purely a build-time mechanism, invisible to the agents themselves.

**Skills** (`src/skills/*/SKILL.md`) provide detailed instructions via progressive disclosure — agents read the full skill via `read_file` when needed. 3 skills: `essay-writing`, `essay-review`, `docx-export`.

---

## 11. Virtual File System (VFS) — Data Exchange Layer

The VFS is used for persistent artifacts that need to survive across agent turns and be inspectable after runs. A `CompositeBackend` routes paths to different backends:

- **Default** → `StateBackend` — intermediate VFS artifacts (brief, plan, essay) live in LangGraph state, checkpointed automatically.
- `/input/` → `FilesystemBackend` — user-provided files staged in a temp directory (read-only access for agents).
- `/output/` → `FilesystemBackend` — the final `.docx` is written to disk.
- `/sources/` → `FilesystemBackend` — downloaded source PDFs are persisted to `.output/run_*/sources/` on disk.

### 11.1 VFS Directory Structure

```
/brief/assignment.md       — Assignment brief (written by intake)
/plan/plan.md              — Essay plan (written by orchestrator)
/essay/draft.md            — Complete essay draft (written by orchestrator)
/essay/final.md            — Polished essay (written by reviewer)
/input/                    — User-provided documents (read-only, temp dir)
/sources/                  — Downloaded source PDFs (persisted to disk)
/output/essay.docx         — Final formatted document (persisted to disk)
/skills/                   — Skill instructions (seeded by framework)
```

### 11.2 Design Principles

- **Simplicity**: Only essential artifacts go to VFS. Search results and reader notes stay in message history.
- **Inspectability**: VFS contents are dumped to disk with `--dump-vfs`, providing a full audit trail.
- **Resumability**: VFS state is checkpointed via LangGraph. Interrupted runs can be resumed from the last checkpoint.
- **Critical constraint**: `write_file` errors on existing files. Modifications must use `edit_file`.

---

## 12. Quality Assurance Checklist

Before the final document is delivered, the system should verify:

- [ ] All assignment requirements are addressed (topic, word count, structure, etc.)
- [ ] The essay reads naturally in Greek with appropriate academic register
- [ ] All claims are supported by cited sources
- [ ] Every in-text citation has a matching reference entry
- [ ] Every reference entry is actually cited in the text
- [ ] Citation format is consistent and correct throughout
- [ ] The document has a proper cover page, table of contents, and page numbers
- [ ] Word count falls within the specified range
- [ ] No placeholder text, TODOs, or agent artifacts remain in the output
- [ ] The .docx file opens correctly and renders as expected

---

## 13. Open Questions & Future Considerations

- **Source verification**: How far should we go in verifying that a discovered source actually exists and contains what we claim? Hallucinated references are a serious risk.
- **Template library**: Should we maintain a set of .docx templates for common Greek university formatting standards?
- **Incremental mode**: For revisions — can the user provide feedback on a generated essay and trigger a targeted rewrite of specific sections?
- **Cost management**: Deep agent pipelines with multiple subagents and large documents can be expensive. Should we implement cost estimation before execution?
- **Caching / reuse**: If multiple essays share the same sources or topic area, could extracted source material be cached and reused across runs?

