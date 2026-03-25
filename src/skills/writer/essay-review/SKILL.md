---
name: essay-review
description: Review and polish academic essays for coherence, language quality, and citation correctness
---

# Essay Review Skill

## Process

1. Read `/brief/assignment.md` — the assignment requirements.
2. Read `/essay/draft.md` — the essay to review.
3. Review for structure, thesis clarity, language quality, citation markers, and completeness.
4. Write the corrected essay to `/essay/reviewed.md` in a **single `write_file` call**.
5. Return: "OK: review complete"

## HARD LIMITS
- ONE `write_file` call to `/essay/reviewed.md`. That is your ONLY output action.
- Do NOT call `edit_file`. EVER.
- Do NOT call `grep`, `glob`, or `write_todos`.
- After `write_file`, return your status immediately. No more tool calls.

## Review Focus
- Fix grammar and awkward phrasing in Greek.
- Ensure every claim has a `[[source_id]]` marker.
- Keep `[[source_id]]` markers intact — do NOT replace with literal citations.
- Do NOT add a References/Βιβλιογραφία section.
- Preserve the argument structure — improve, don't rewrite.
