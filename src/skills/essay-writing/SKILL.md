---
name: essay-writing
description: Write complete academic essays in Greek with proper source integration, citations, and word count control
---

# Essay Writing Skill

## When to Use
- When writing the essay draft (Step 5)

## Writing Process

1. **Read your materials**:
   - Read the essay plan from `/plan/plan.md` for section structure and word targets
   - Read source notes from `/sources/notes/` (use `ls` then `read_file` for each)
   - Read the assignment brief from `/brief/assignment.md` for requirements

2. **Plan internally**:
   - Map sources to sections (which source supports which argument)
   - Identify 2-4 key points per section
   - Plan transitions between sections

3. **Write the essay**:
   - Follow the section structure from the plan
   - Open each section with a clear topic sentence
   - Develop arguments with evidence from sources
   - Use transitions between sections
   - Include a properly formatted References section at the end

4. **Reference sources with markers**:
   - Use `[[source_id]]` markers where a citation belongs: "Σύμφωνα με τους ερευνητές [[smith2020]], η ηγεσία..."
   - For page references: `[[source_id|σ. 45]]` or `[[source_id|σσ. 45-50]]`
   - The `build_docx` tool will replace these with formatted citations and generate the bibliography/endnotes automatically
   - Do NOT write literal `(Author, Year)` citations or a References/Βιβλιογραφία section — those are generated deterministically
   - Direct quotes must include a page reference: `[[papadopoulos2023|σ. 15]]`

5. **Verify word count**:
   - Use the `count_words` tool on your draft
   - Target: within ±10% of the assigned total word count
   - If over: tighten prose, remove redundancy, merge similar points
   - If under: develop arguments further, add more source integration, expand analysis

6. **Write to VFS**:
   - Write the complete essay to `/essay/draft.md`

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
- Multiple sources: "...[[smith2020]] [[jones2019]]"
- Use `source_id` format: `authorlastname + year` (e.g., `papadopoulos2023`, `bass2006`)
- For multiple authors use first author: `graen1995` for Graen & Uhl-Bien (1995)
- Ensure every claim has at least one `[[source_id]]` marker

### Common Pitfalls
- Don't start paragraphs with citations
- Don't string multiple quotes without analysis
- Don't use first person unless the assignment explicitly allows it
- Don't make unsupported claims — every assertion needs a citation or clear logical derivation
