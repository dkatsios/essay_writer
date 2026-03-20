# Essay Writer Agent — Project Design Document

## 1. Overview

Essay Writer is a deep agent system that produces high-quality academic essays for Greek university students at both undergraduate and graduate levels. The system accepts various input documents and instructions, orchestrates a multi-step pipeline of specialized subagents, and delivers a properly formatted `.docx` document ready for submission.

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
- Potentially URLs pointing to online resources

---

## 4. Pipeline — High-Level Workflow

The essay production follows a multi-phase pipeline. Each phase is handled by one or more specialized agents or tools. The main orchestrator agent coordinates the flow but does **not** hold all raw content in its context window. Agents exchange data through a **Virtual File System (VFS)** — a shared, structured storage layer that allows subagents to produce and consume artifacts without passing large payloads through the orchestrator.

### Phase 1: Intake & Understanding
- Parse and extract information from all input documents.
- Identify the essay topic, constraints, required structure, academic level, and citation style.
- Produce a structured **assignment brief** — a normalized internal representation of what needs to be written.
- The assignment brief is written to VFS at `brief/assignment.md`.

### Phase 2: Draft Planning
- **Before any research begins**, the Planner agent (via a dedicated planning skill) produces a **draft essay plan** based solely on the assignment brief and the agent's own knowledge.
- The draft plan includes:
  - Proposed title and thesis statement.
  - Section breakdown with headings, subheadings, and a brief description of the argument or content for each section.
  - **Word count targets per section** — explicit word budgets that must sum to the overall target word count from the assignment. The allocation reflects each section's relative importance and depth (e.g., a core analysis section gets more words than the introduction).
  - **Research directions** — for each section, what kinds of sources or evidence are needed (e.g., "Section 3 needs Greek case law on data protection", "Section 5 needs EU statistics on migration").
- This early plan serves a critical purpose: it gives the research phase **specific targets** rather than having research be open-ended.
- The draft plan is written to VFS at `plan/draft.md`.

### Phase 3: Targeted Research & Source Discovery
- Using the research directions from the draft plan, the orchestrator spawns **multiple research subagents in parallel**, each focused on finding sources for specific sections or topics.
- Research agents prioritize:
  - **Google Scholar** and equivalent academic search engines.
  - **Greek academic repositories** (e.g., Kallipos, university institutional repositories, National Documentation Centre / EKT).
  - **International journals and papers** — especially for topics where Greek-language sources are insufficient.
  - **Books and book chapters** — referenced by title, author, publisher, year.
- Research agents explicitly deprioritize or exclude:
  - Generic web articles and blog posts.
  - Wikipedia as a primary source (may be used only for initial orientation, never cited).

#### Source Metadata Collection
- When search tools return **structured data** (title, authors, abstract, DOI, etc.), this metadata is written directly to VFS at `sources/metadata/{source_id}.md`.
- When a source is available only as a raw PDF without structured metadata, a **lightweight cataloguing step** runs first: a cheaper model reads the document and produces a structured metadata entry (title, authors, abstract, introduction summary, publication info). This catalogued entry is written to VFS so the Planner can assess the source's relevance without reading the full document.
- The Planner reads **only the metadata entries** (abstracts, introduction summaries) to make source assignment decisions — never the full source documents.

### Phase 4: Plan Refinement & Source Assignment
- After research completes, the Planner agent **revisits the draft plan** in light of the discovered source metadata.
- The Planner reads source metadata from `sources/metadata/*.md` — specifically abstracts and introduction summaries — to understand what each source offers.
- Adjustments may include:
  - Strengthening sections where rich sources were found.
  - Restructuring or merging sections where sources are scarce.
  - Adding new angles surfaced by the research that weren't in the original plan.
  - Dropping speculative sections that can't be supported.
- The refined plan includes:
  - A **source-to-section mapping** — an explicit assignment of which sources are relevant to which sections. A single source may be assigned to multiple sections.
  - **Updated word count targets per section** — adjusted if sections were added, removed, or restructured. The per-section targets must still sum to the overall assignment word count.
- The refined plan is written to VFS at `plan/final.md`, and the mapping at `plan/source_mapping.md`.

### Phase 5: Parallel Source Extraction
- The orchestrator spawns **one extractor subagent per source**, all running in parallel.
- Each extractor reads the full source document **once** and produces **separate VFS entries for each section** that the source is mapped to.
- For example, if source #6 is mapped to sections 2 and 4:
  - The extractor reads source #6 in full.
  - It writes `sections/section_02/sources/source_06.md` — containing only the arguments, quotes, data, and findings from source #6 that are relevant to section 2.
  - It writes `sections/section_04/sources/source_06.md` — containing only the content relevant to section 4.
- Each VFS entry is the **sole access point** for that source's contribution to that section. The section writer will never see the original source document — only this extract. Therefore, the entry must be **exhaustive and self-contained**, including:
  - Key arguments and findings relevant to the specific section.
  - Direct quotes with exact page numbers, ready for citation.
  - Data, statistics, tables, or case studies that could support the section's argument.
  - Counter-arguments or limitations from the source, if relevant.
  - Full bibliographic information formatted for the reference list (authors, title, year, journal/publisher, DOI/URL).
  - The citation key to use for in-text references (e.g., "Παπαδόπουλος, 2021").
- If an extractor determines that a source has **no useful content** for a particular section, it writes a minimal entry flagging this, so the orchestrator can decide whether to find a replacement or adjust the plan.
- **Key efficiency gain**: each source is parsed exactly once, regardless of how many sections use it. Without this pattern, N sections using the same source would mean N redundant parsing passes.

### Phase 6: Sequential Section Writing
- Sections are written **one at a time, in order** — not in parallel. This is a deliberate trade-off: it costs more tokens and takes longer, but produces significantly better coherence, natural transitions, and consistent argumentation across the essay.
- Each section writer subagent receives:
  - The **complete final plan** — so it understands the full essay structure and where its section fits.
  - **Prior sections context** — adapts based on essay size (see below).
  - The pre-extracted source material from VFS — it reads only `sections/section_XX/sources/*.md`, not the raw source documents.
  - Style and tone guidelines (academic level, citation format, language register).
- The section writer is explicitly instructed to hit the **word count target** assigned to its section by the planner.
- Each section writer produces its output and writes it to VFS at `sections/section_XX/draft.md`.

#### Word Count Validation & Retry
- After each section is written, the orchestrator runs a **word count check** against the section's target.
- A configurable **tolerance band** (e.g., ±10%) determines whether the section passes. Minor deviations within tolerance are accepted without intervention.
- If the section falls **outside the tolerance**:
  - The orchestrator spawns a **targeted rewrite** — not a from-scratch rewrite, but an adjustment pass. The rewrite prompt includes the current draft, the exact word count, the target, and a specific instruction (e.g., "condense from 1200 to ~800 words — preserve all cited sources and key arguments" or "expand from 400 to ~700 words — develop the analysis of X further using the available source material").
  - The rewrite agent receives the same source extracts as the original writer so it can expand intelligently if needed.
  - A maximum number of retry attempts is configurable (default: 2) to prevent infinite loops. If the section still doesn't meet the target after retries, the orchestrator accepts the best attempt and logs a warning.
- The orchestrator appends each validated section to the running essay body before spawning the next writer.

#### Adaptive Context via Jinja Templates
The section writer prompt is a Jinja template that adapts the amount of prior context based on the total essay word count target (typically known from the assignment instructions):

- **Short essays** (e.g., up to ~3000 words): The writer receives the **full text of all previously written sections**. The total context remains manageable, and full access maximizes coherence.
- **Long essays** (e.g., 5000+ words): Each section writer, in addition to writing its section, also produces a **concise summary** of what it wrote (key arguments, terminology introduced, conclusions reached). This summary is written to VFS at `sections/section_XX/summary.md`. Subsequent writers receive the **summaries of prior sections** rather than their full text, keeping the context lean while preserving continuity.

This is controlled by a Jinja `{% if %}` block in the writer prompt template — the same template handles both cases, switching behavior based on a word count threshold from the configuration. The threshold itself is configurable, not hardcoded.

- **Note**: Parallel section writing may be revisited in a future version as an optimization, with a stronger post-assembly coherence pass to compensate.

### Phase 7: Review & Refinement
- The orchestrator assembles all section drafts from VFS into a complete essay at `essay/assembled.md`.
- A **Reviewer agent** evaluates the full essay for:
  - **Coherence** — logical flow between sections, no contradictions, smooth transitions.
  - **Language quality** — grammar, syntax, spelling, and tone consistency in Greek.
  - **Citation audit** — every in-text citation has a matching reference entry and vice versa. Citation format is consistent.
  - **Completeness** — all assignment requirements are met (word count, required topics, structural requirements).
  - **Plagiarism awareness** — text is original and properly paraphrased.
- The reviewer writes specific feedback to VFS at `review/feedback.md`.
- **Backward refinement**: Since sections are written sequentially, later sections may introduce arguments or terminology that warrant adjustments to earlier sections. The reviewer (or a dedicated polishing agent) can update earlier sections to improve coherence — for example, adding foreshadowing in the introduction for a point that emerged during writing, or harmonizing terminology across the whole essay.
- The polished essay is written to `essay/reviewed.md`.

### Phase 8: Document Export
- The Document Builder agent assembles the final essay into a `.docx` file with full formatting:
  - **Cover page** (title, student name if provided, course, professor, date).
  - **Table of contents** (auto-generated with page numbers).
  - **Headings and subheadings** with proper hierarchy and styles.
  - **Body text** with consistent font, size, line spacing, and margins.
  - **In-text citations** formatted per the required style.
  - **References / Bibliography section** — fully formatted.
  - **Page numbers**.
  - **Headers/footers** if required.
- The document builder must handle Greek characters and polytonic text correctly.

---

## 5. Agent Architecture

### 5.1 Main Orchestrator Agent
The central coordinator. It:
- Receives and interprets user input.
- Delegates work to specialized subagents.
- Maintains the high-level state of the pipeline (which phase we're in, what's done, what's pending).
- Makes decisions about the flow (e.g., whether to trigger more research, adjust the plan, or request section rewrites).
- Manages the VFS structure and reads subagent outputs to make coordination decisions.
- Does **not** hold raw source documents in its context — only the assignment brief, the plan, source metadata, and the evolving essay.

### 5.2 Subagents

| Subagent | Responsibility | Parallelism |
|---|---|---|
| **Document Parser** | Reads input documents (PDFs, DOCX) and extracts structured information into the assignment brief. | One per input document |
| **Planner** | Produces the draft plan (before research) and refines it (after research). Uses a dedicated planning skill. Maps sources to sections based on metadata only. | Single instance, runs twice |
| **Research Agent** | Searches academic databases for credible sources targeting specific topics from the draft plan. Writes source metadata to VFS. | Multiple in parallel, one per research direction |
| **Source Cataloguer** | For sources without structured metadata: reads the PDF and produces a lightweight metadata entry (title, authors, abstract, introduction summary, publication info). Uses a cheaper model. | One per uncatalogued source, in parallel |
| **Source Extractor** | Reads a single source document in full. Produces exhaustive, self-contained VFS entries for each section that uses this source. This is the sole access point — the section writer will never see the original document. | One per source, all in parallel |
| **Section Writer** | Writes a single essay section. Receives the full plan, all previously written sections, and the pre-extracted source material for its section. | Sequential — one at a time, in order |
| **Reviewer / Polisher** | Reviews the assembled essay for coherence, language, citations, and completeness. Can refine earlier sections based on how the essay developed. | Single instance |
| **Document Builder** | Converts the final essay into a formatted .docx file. | Single instance |

### 5.3 Tools

Tools are capabilities that agents can invoke:

| Tool | Purpose |
|---|---|
| **Academic Search** | Queries Google Scholar, Semantic Scholar, or similar APIs for papers and articles. |
| **Greek Repository Search** | Targets Greek academic repositories (EKT, Kallipos, institutional repos). |
| **PDF Reader** | Extracts text content from PDF files. |
| **DOCX Reader** | Extracts text and structure from Word documents. |
| **DOCX Builder** | Constructs the final .docx output with full formatting, TOC, citations, etc. |
| **Citation Formatter** | Formats references according to the specified citation style (APA, Harvard, etc.). |
| **Word Counter** | Tracks word count per section and overall. |
| **Web Fetcher** | Retrieves content from URLs when needed. |
| **VFS Read/Write** | Read and write artifacts to the shared virtual file system. |

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

The project uses a **centralized configuration system** to manage all settings — model selection, formatting defaults, citation styles, pipeline behavior, and more. This avoids scattering configuration across code, prompts, and environment variables.

Options under consideration for the technical design phase:
- **Pydantic-based config** — strongly typed, validated at startup, good IDE support.
- **YAML files with Dynaconf** — layered configuration with environment overrides, good for separating defaults from user overrides.
- A hybrid approach is also possible (YAML files loaded into Pydantic models).

Configuration will cover at minimum:
- Model selection per agent/subagent role (e.g., cheaper model for cataloguing, stronger model for writing).
- Default formatting (font, spacing, margins, citation style).
- Pipeline behavior (e.g., whether to pause for human review after planning).
- Search settings (which academic sources to query, language preferences).
- VFS root path and output directory.

---

## 10. Prompt & Instruction Templating

All agent prompts, skills, and instructions use **Jinja2 templates** (`.j2` files) rather than static markdown. This provides:

- **Variable injection** — dynamically insert the assignment brief, section plan, academic level, citation style, or any other context into prompts at render time.
- **Conditional logic** — `{% if academic_level == 'graduate' %}` blocks allow the same template to produce different instructions for different academic levels, citation styles, or essay types without maintaining separate prompt files.
- **Loops** — iterate over sections, sources, or other dynamic lists within a single template.
- **Template inheritance** — shared base templates for common prompt patterns (e.g., a base "writer" template extended by the section writer and the reviewer).

Templates are rendered at runtime by the orchestrator before being passed to subagents. The rendered output is plain text / markdown — Jinja is purely a build-time mechanism, invisible to the agents themselves.

---

## 11. Virtual File System (VFS) — Data Exchange Layer

The VFS is the backbone of inter-agent communication. Instead of passing large artifacts through agent messages or the orchestrator's context, all intermediate and final outputs are written to and read from a shared VFS.

### 11.1 VFS Directory Structure

```
vfs/
├── brief/
│   └── assignment.md              # Normalized assignment brief
├── plan/
│   ├── draft.md                   # Initial plan (before research)
│   ├── final.md                   # Refined plan (after research)
│   └── source_mapping.md          # Source-to-section assignment table
├── sources/
│   └── metadata/
│       ├── source_01.md           # Metadata: title, authors, year, DOI, abstract
│       ├── source_02.md
│       └── ...
├── sections/
│   ├── section_01/
│   │   ├── sources/
│   │   │   ├── source_02.md       # Extracted content from source 2 for section 1
│   │   │   └── source_05.md       # Extracted content from source 5 for section 1
│   │   ├── draft.md               # Written draft of section 1
│   │   └── summary.md             # Concise summary (produced for long essays)
│   ├── section_02/
│   │   ├── sources/
│   │   │   ├── source_01.md
│   │   │   ├── source_06.md
│   │   │   └── source_08.md
│   │   ├── draft.md
│   │   └── summary.md
│   └── ...
├── essay/
│   ├── assembled.md               # Full essay after assembly
│   └── reviewed.md                # Essay after review pass
├── review/
│   └── feedback.md                # Reviewer's feedback and suggestions
└── output/
    └── essay.docx                 # Final exported document
```

### 11.2 Benefits of VFS-Centric Architecture

- **Context isolation**: Each subagent reads only the VFS paths relevant to its task. A section writer never sees raw source PDFs — only the pre-extracted summaries in its `sections/section_XX/sources/` directory.
- **Single-parse efficiency**: Each source is read and processed by exactly one extractor, regardless of how many sections reference it. The extractor fans out its findings into per-section VFS entries.
- **Inspectability**: The VFS provides a complete audit trail of the pipeline. Every intermediate artifact is available for debugging, review, or manual intervention.
- **Parallelism**: Subagents writing to non-overlapping VFS paths can run fully in parallel with no coordination overhead.
- **Resumability**: If a pipeline step fails, the VFS retains all prior work. The orchestrator can retry or adjust from the last successful state.

### 11.3 Context Window Management

The VFS structure directly addresses the context window problem:

- **Orchestrator context** contains: assignment brief, plan, source metadata (titles/abstracts only), and coordination state. It never holds raw documents or full essay text except during final assembly.
- **Extractor context** contains: one source document + the section descriptions it needs to extract for. After processing, it writes to VFS and terminates.
- **Writer context** contains: the full plan + prior sections context + pre-extracted source summaries for its section from VFS. For short essays, prior context is the full text of completed sections. For long essays, prior context is concise per-section summaries — controlled by a Jinja template conditional based on the word count target.
- **Reviewer context** contains: the full assembled essay + the assignment brief. This is the largest context in the pipeline but is bounded by the essay length itself.

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

## 13. Notes for Consideration

The following points have been identified during design and need to be resolved before or during the technical design phase.

### 13.1 User-Provided Sources
The user may provide source materials directly (e.g., "the professor gave us these 3 PDFs to base the essay on"). These sources differ from discovered sources in that they **skip the research phase** entirely but still need to flow through the rest of the pipeline:
- They need **cataloguing** — structured metadata must be extracted (title, authors, abstract, etc.) and written to VFS at `sources/metadata/`, just like discovered sources.
- They need to be included in the **plan refinement and source-to-section mapping** — the planner should treat them as first-class sources.
- They need **extraction** — one extractor per source, producing per-section VFS entries, same as any other source.
- The pipeline should clearly distinguish between user-provided and discovered sources in the metadata (e.g., a `provided: true` flag) so the system knows not to question their credibility or try to find alternatives.

### 13.2 Introduction & Conclusion Writing Order
In academic writing, the introduction previews the essay's structure and argument, and the conclusion wraps it all up. Writing them in strict sequential order creates a problem:
- If the **introduction is written first**, it must predict what the body sections will say — and may become inaccurate as the essay develops.
- If the **conclusion is written last**, it benefits from seeing all prior sections, which is natural.

Options to consider:
- Write the introduction as a **lightweight placeholder** early in the sequence, then revise it during the polish/review phase once the full body exists.
- Write the introduction **last** (or second-to-last, before the conclusion), reversing the natural document order for the writing sequence while maintaining the correct order in the final output.
- Rely entirely on the **reviewer/polisher** to fix the introduction after all sections are complete.

This decision affects the sequential writing loop design and should be settled during technical design.

### 13.3 Human-in-the-Loop Checkpoints
The pipeline can run fully autonomously or pause at key points for user approval. Candidate pause points:
- **After Phase 2 (Draft Planning)** — the user reviews the proposed essay structure before any research tokens are spent.
- **After Phase 4 (Plan Refinement)** — the user sees the final plan with source assignments before extraction and writing begin. This is the last cheap checkpoint — everything after this is token-intensive.
- **After Phase 7 (Review)** — the user reviews the polished essay before it's exported to .docx.

Whether these checkpoints are active should be controlled via configuration (see Section 9). The default behavior (fully autonomous vs. interactive) is a design decision.

### 13.4 Pipeline Failure & Fallback Handling
Several failure modes need defined behavior:
- **Research finds no sources for a section** — should the planner be re-invoked to restructure, should the section be written from general knowledge with a warning, or should the pipeline halt and ask the user?
- **A source PDF is corrupted or unreadable** — the extractor should flag this in VFS so the orchestrator can decide whether to find a replacement or proceed without it.
- **All extractors flag "no useful content" for a section** — similar to no sources found, but after extraction. The section may need to be rethought or dropped.
- **Word count retries exhausted** — already handled (accept best attempt + warning), but the orchestrator should track cumulative word count drift to flag if the overall essay is significantly off target.
- **A subagent fails entirely** (timeout, API error, etc.) — the VFS-based architecture supports resumability, but the orchestrator needs retry logic with backoff and a maximum failure threshold before halting.

These failure modes should be handled gracefully rather than crashing the pipeline. The general principle: **degrade and inform, don't halt silently**.

---

## 14. Open Questions & Future Considerations

- **Source verification**: How far should we go in verifying that a discovered source actually exists and contains what we claim? Hallucinated references are a serious risk.
- **Template library**: Should we maintain a set of .docx templates for common Greek university formatting standards?
- **Incremental mode**: For revisions — can the user provide feedback on a generated essay and trigger a targeted rewrite of specific sections?
- **Multi-essay support**: Could the system handle a batch of related essays (e.g., weekly assignments for a course)?
- **Cost management**: Deep agent pipelines with multiple subagents and large documents can be expensive. Should we implement cost estimation before execution?
- **Parallel section writing**: The current sequential approach prioritizes coherence. A future version could explore parallel writing with a stronger post-assembly coherence pass, trading quality risk for speed.
- **Caching / reuse**: If multiple essays share the same sources or topic area, could extracted source material be cached and reused across runs?
