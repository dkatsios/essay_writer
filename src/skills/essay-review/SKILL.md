---
name: essay-review
description: Review and polish academic essays for coherence, language quality, citation correctness, and completeness
---

# Essay Review Skill

## When to Use
- After writing the complete draft in Step 5, before exporting in Step 7.

## Review Process

### Step 1: Read All Materials
- Read `/essay/draft.md` — the essay draft
- Read `/brief/assignment.md` — the original assignment brief

### Step 2: Structural Review
- Does the essay follow the planned structure?
- Is there a clear thesis stated in the introduction?
- Does each section have a clear purpose and contribute to the overall argument?
- Are transitions between sections smooth and logical?
- Does the conclusion synthesize (not just summarize) the arguments?
- Does the introduction accurately preview the essay's content?

### Step 3: Language Review
- Check for grammatical errors in Greek
- Verify consistent academic register throughout
- Identify and fix awkward phrasing or unclear sentences
- Ensure terminology is used consistently
- Check for unnecessary repetition across sections

### Step 4: Citation Audit
- Every factual claim must have a `[[source_id]]` marker
- Verify markers reference valid source IDs from `/sources/registry.json`
- Direct quotes must include page numbers: `[[source_id|σ. 45]]`
- Do NOT replace `[[source_id]]` markers with literal citations — `build_docx` handles that
- Greek citation conventions: use "σ." for page, "σσ." for page range

### Step 5: Completeness Check
- Does the essay address all aspects of the assignment brief?
- Are all planned sections present and adequately developed?
- Is the overall word count within target?
- Are there any gaps in the argumentation?

### Step 6: References Section
- Do NOT write a References/Βιβλιογραφία section in the essay — it is generated automatically by `build_docx` from `/sources/registry.json`
- If the draft contains a hand-written references section, remove it

## Applying Fixes

Use `edit_file` on `/essay/draft.md` to apply targeted corrections. Do NOT rewrite the entire draft — make surgical fixes for each issue found.
