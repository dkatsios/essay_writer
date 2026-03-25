---
name: essay-review
description: Review and polish academic essays for coherence, language quality, and citation correctness
---

# Essay Review Skill

## Process

1. Read `/brief/assignment.md` — the assignment requirements.
2. Read `/plan/plan.md` — for the section word targets.
3. Read `/essay/draft.md` — the essay to review.
4. Write the corrected essay to `/essay/reviewed.md` in a **single `write_file` call**.
5. Return: "OK: review complete"

## HARD LIMITS
- ONE `write_file` call to `/essay/reviewed.md`. That is your ONLY output action.
- Do NOT call `edit_file`. EVER.
- Do NOT call `grep`, `glob`, or `write_todos`.
- After `write_file`, return your status immediately. No more tool calls.
- **Do NOT attempt to rewrite or create additional files.** One call, done.

## Review Focus
- Fix grammar and awkward phrasing in Greek.
- Ensure every claim has a `[[source_id]]` marker.
- Keep `[[source_id]]` markers intact — do NOT replace with literal citations.
- Do NOT add a References/Βιβλιογραφία section.
- Preserve the argument structure — improve, don't rewrite from scratch.

## Word Count — CRITICAL
- The reviewed essay MUST be at least as long as the draft.
- Do NOT cut, summarize, or shorten sections.
- If a section is below its plan target, EXPAND it with more detail and analysis.
- Target: within ±10% of the plan’s total word count.
