---
name: essay-review
description: Review and polish academic essays for coherence, language quality, citation correctness, and completeness
---

# Essay Review Skill

## When to Use
- After writing the complete draft (Step 6), before exporting

## Process

1. Read `/brief/assignment.md` — the assignment requirements.
2. Read `/essay/draft.md` — the essay to review.
3. Review the essay for:
   - **Structure**: Clear academic structure? Smooth transitions?
   - **Thesis**: Clear thesis supported throughout?
   - **Language**: Grammar, register, consistency in academic Greek
   - **Citations**: `[[source_id]]` markers present for all claims?
   - **Completeness**: All aspects of the assignment addressed?
4. Apply corrections using `edit_file` on `/essay/draft.md` — make targeted fixes, do NOT rewrite from scratch.
5. Use `count_words` to confirm the final word count is still within target.
6. Return a short status: "OK: review complete, {number} edits applied"

## HARD LIMIT
- Make at most **5 edits**, then STOP. Do not call `edit_file` more than 5 times total.
- After your edits, call `count_words` once and return immediately.
- Do NOT alternate between editing and counting words repeatedly. Edit first, count once at the end.

## Review Checklist

### Structure
- Does the essay follow the planned structure?
- Is there a clear thesis stated in the introduction?
- Does each section have a clear purpose?
- Are transitions between sections smooth and logical?
- Does the conclusion synthesize (not just summarize) the arguments?

### Language
- Check for grammatical errors in Greek
- Verify consistent academic register throughout
- Identify and fix awkward phrasing or unclear sentences
- Check for unnecessary repetition across sections

### Citations
- Every factual claim must have a `[[source_id]]` marker
- Direct quotes must include page numbers: `[[source_id|σ. 45]]`
- Do NOT replace `[[source_id]]` markers with literal citations — `build_docx` handles that
- Do NOT write a References/Βιβλιογραφία section

### Completeness
- Does the essay address all aspects of the assignment brief?
- Are all planned sections present and adequately developed?
- Is the overall word count within target?

## Important
- Do NOT remove or weaken citations — only fix formatting issues.
- Preserve the author's voice and argument structure — make targeted improvements, not rewrites.
- Keep `[[source_id]]` markers intact.
