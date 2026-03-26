---
name: essay-review
description: Review and polish academic essays for coherence, language quality, and citation correctness
---

# Essay Review Skill

## Process

1. The assignment brief, essay plan, and draft are provided below in the prompt — do NOT read them from disk.
2. Review the draft following the criteria below.
3. Write the corrected essay to `/essay/reviewed.md` in a **single `write_file` call**.
4. Return: "OK: review complete"

## HARD LIMITS
- ONE `write_file` call to `/essay/reviewed.md`. That is your ONLY output action.
- Do NOT call `read_file`. The brief, plan, and draft are already in the prompt.
- Do NOT call `edit_file`. EVER.
- Do NOT call `grep`, `glob`, `ls`, or `write_todos`.
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
