---
name: section-writing
description: Write a single essay section given plan entry, sources, and prior sections context
---

# Section Writing Skill

## Process

1. Read `/plan/plan.md` to understand the overall essay structure.
2. Read the source notes provided in the task message.
3. If prior sections are mentioned, read them to maintain coherence and avoid repetition.
4. Write ONLY the assigned section in a **single `write_file` call** to the path specified in the task message.
5. Return: "OK: section written"

## HARD LIMITS
- **EXACTLY ONE `write_file` call.** That is your ONLY output action.
- Do NOT call `edit_file`. EVER.
- Do NOT read back the file after writing it.
- Do NOT call `grep`, `glob`, or `write_todos`.
- After `write_file`, return your status immediately. No more tool calls.

## Word Count — CRITICAL
- The task message specifies the word target for THIS section.
- Your output MUST be within ±10% of that target. Under 90% = FAILURE.
- Write LONG, detailed paragraphs. Develop every argument fully.
- If the target is 800 words, write at least 720 words.

## Writing Rules
- Follow the heading and numbering specified in the task message exactly.
- ALL content in Modern Greek (Δημοτική), formal academic register.
- Use `[[source_id]]` citation markers: `Σύμφωνα με [[smith2020]], ...`
- Page references: `[[source_id|σ. 45]]`
- Multiple sources: `[[smith2020]] [[jones2019]]` — separate markers, NEVER combined.
- Do NOT write a References/Βιβλιογραφία section.
- Do NOT write literal `(Author, Year)` citations.

## Continuity
- If prior sections are provided, ensure smooth transitions from the previous section.
- Do NOT repeat arguments or examples already covered in prior sections.
- Reference earlier points naturally: "Όπως αναφέρθηκε παραπάνω..." when appropriate.
