---
name: section-review
description: Review and polish a single essay section given full essay context
---

# Section Review Skill

## Process

1. The task message contains the full essay. The section to review is between `<!-- >>> SECTION TO REVIEW: START >>> -->` and `<!-- <<< SECTION TO REVIEW: END <<< -->-->` delimiters.
2. Read the surrounding sections for context and coherence.
3. Rewrite ONLY the delimited section. Do NOT touch other sections.
4. Write the improved version in a **single `write_file` call** to the path specified in the task message.
5. Return: "OK: section reviewed"

## HARD LIMITS
- ONE `write_file` call. That is your ONLY output action.
- Do NOT call `edit_file`. EVER.
- Do NOT call `grep`, `glob`, or `write_todos`.
- After `write_file`, return your status immediately. No more tool calls.

## Review Focus
- Fix grammar and awkward phrasing in Greek.
- Ensure smooth transitions to/from adjacent sections.
- Ensure every claim has a `[[source_id]]` marker.
- Keep `[[source_id]]` markers intact — do NOT replace with literal citations.
- Do NOT add a References/Βιβλιογραφία section.

## Word Count — CRITICAL
- The reviewed section MUST be at least as long as the original.
- Do NOT cut, summarize, or shorten the section.
- If the section is below its plan target, EXPAND it with more detail and analysis.
- Preserve all substantive content from the original.

## Coherence
- Read surrounding sections to ensure the reviewed section fits naturally.
- Smooth transitions in and out of the section.
- Avoid introducing contradictions with other sections.
