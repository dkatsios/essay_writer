---
name: essay-planning
description: Create a structured essay plan with sections, word allocation, and research queries
---

# Essay Planning Skill

## When to Use
- When creating the essay plan from the assignment brief (Step 2)

## Process
1. Read `/brief/assignment.md` for the assignment requirements.
2. Identify the core question or topic the essay must address.
3. Formulate a clear thesis statement (1-2 sentences).
4. Break the essay into sections (typically 4-6 including introduction and conclusion).
5. Allocate word counts per section summing to the overall target.
6. Write 6-8 targeted search queries for finding academic sources (in both Greek and English).
7. Write the plan to `/plan/plan.md`.
8. Return a short status: "OK: plan written, {section_count} sections, {total_words} target words, {query_count} queries"

## Plan Structure (`/plan/plan.md`)

```markdown
# Essay Plan

## Thesis
[1-2 sentence thesis statement in Greek]

## Sections

### 1. Εισαγωγή (Introduction)
- **Word target**: [N words]
- **Key points**: [what to cover]

### 2. [Section Title in Greek]
- **Word target**: [N words]
- **Key points**: [what to cover]

...

### N. Συμπέρασμα (Conclusion)
- **Word target**: [N words]
- **Key points**: [what to cover]

## Research Queries
- [query 1 in Greek or English]
- [query 2]
- ...
```

## Word Allocation Guidelines

| Essay Length | Introduction | Per Body Section | Conclusion |
|---|---|---|---|
| < 2000 words | 10-12% | Divide remaining equally | 10-12% |
| 2000-4000 words | 10-15% | Divide remaining by importance | 10-15% |
| > 4000 words | 8-10% | Divide remaining by importance | 8-10% |

## Important
- All section titles and thesis should be in Modern Greek (Δημοτική).
- Research queries should include BOTH Greek and English terms for broader coverage.
- Word count targets must sum to the overall target from the brief.
- If the brief does not specify a word count, default to 3000 words.
- Be specific with research queries — avoid vague directions like "general background".
