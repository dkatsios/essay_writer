---
name: essay-writing
description: Write complete academic essays in Greek with proper source integration and citations
---

# Essay Writing Skill

## Process

1. Read `/brief/assignment.md` for the assignment requirements.
2. Read `/plan/plan.md` for the section structure and word targets.
3. Read source notes — `ls /sources/notes/` then `read_file` for each.
4. Write the COMPLETE essay in a **single `write_file` call** to `/essay/draft.md`.
5. Return: "OK: essay written"

## HARD LIMITS
- ONE `write_file` call. That is your ONLY output action.
- Do NOT call `edit_file`. EVER.
- Do NOT read back `/essay/draft.md` after writing it.
- Do NOT call `grep`, `glob`, or `write_todos`.
- After `write_file`, return your status immediately. No more tool calls.

## Writing Rules
- Follow the section structure from the plan.
- ALL content in Modern Greek (Δημοτική), formal academic register.
- Use `[[source_id]]` citation markers: `Σύμφωνα με [[smith2020]], ...`
- Page references: `[[source_id|σ. 45]]`
- Multiple sources: `[[smith2020]] [[jones2019]]` — separate markers, NEVER combined.
- Do NOT write a References/Βιβλιογραφία section — `build_docx` generates it.
- Do NOT write literal `(Author, Year)` citations.
- Target word count: within ±10% of the plan's total.
