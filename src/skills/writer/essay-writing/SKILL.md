---
name: essay-writing
description: Write complete academic essays in Greek with proper source integration, citations, and word count control
---

# Essay Writing Skill

## When to Use
- When writing the essay draft (Step 5)

## Process

1. Read `/brief/assignment.md` for the assignment requirements.
2. Read `/plan/plan.md` for the section structure and word targets.
3. Read source notes from `/sources/notes/` — use `ls /sources/notes/` then `read_file` for each file.
4. Map sources to sections (which source supports which argument).
5. Write the COMPLETE essay (all sections, introduction through conclusion) in a **single `write_file` call** to `/essay/draft.md`.
6. Use `count_words` on `/essay/draft.md` to verify the total is within ±10% of the target.
7. Return a short status: "OK: essay written, {word_count} words"

## Writing Guidelines

- Follow the section structure from the plan
- Open each section with a clear topic sentence
- Develop arguments with evidence from sources
- Use transitions between sections
- Do NOT write a References/Βιβλιογραφία section — `build_docx` generates it automatically

## Citation Markers

- Use `[[source_id]]` markers where a citation belongs: "Σύμφωνα με τους ερευνητές [[smith2020]], η ηγεσία..."
- For page references: `[[source_id|σ. 45]]` or `[[source_id|σσ. 45-50]]`
- The `build_docx` tool will replace these with formatted citations and generate the bibliography/endnotes automatically
- Do NOT write literal `(Author, Year)` citations — those are generated deterministically
- Direct quotes must include a page reference: `[[papadopoulos2023|σ. 15]]`
- Multiple sources get SEPARATE markers: `[[smith2020]] [[jones2019]]` — NEVER `[[smith2020], [jones2019]]`

## Word Count Control

- Use the `count_words` tool on your draft after writing
- Target: within ±10% of the assigned total word count
- If over: tighten prose, remove redundancy, merge similar points
- If under: develop arguments further, add more source integration, expand analysis

## Academic Greek Style Guide

### Register
- Use formal academic language (Δημοτική)
- Avoid colloquialisms, slang, or overly informal expressions
- Use passive voice where appropriate in academic context
- Employ hedging language: "φαίνεται ότι", "είναι πιθανό", "τα δεδομένα υποδεικνύουν"

### Structure
- One idea per paragraph
- Clear topic sentences
- Logical connectors: "Επιπλέον", "Ωστόσο", "Εν κατακλείδι", "Αντιθέτως", "Συνεπώς"
- Build arguments progressively — from evidence to analysis to synthesis

### Source Markers
- Basic: `[[hersey2011]]` → becomes `(Hersey & Blanchard, 2011)` in APA mode
- With page: `[[hersey2011|σ. 42]]` → `(Hersey & Blanchard, 2011, σ. 42)`
- Narrative intro: "Ο Hersey [[hersey2011]] υποστηρίζει ότι..."
- Multiple sources: "...[[smith2020]] [[jones2019]]" — each in SEPARATE `[[]]`, NEVER `[[smith2020], [jones2019]]`
- Use `source_id` format: `authorlastname + year` (e.g., `papadopoulos2023`, `bass2006`)
- For multiple authors use first author: `graen1995` for Graen & Uhl-Bien (1995)
- Ensure every claim has at least one `[[source_id]]` marker

### Common Pitfalls
- Don't start paragraphs with citations
- Don't string multiple quotes without analysis
- Don't use first person unless the assignment explicitly allows it
- Don't make unsupported claims — every assertion needs a citation or clear logical derivation
