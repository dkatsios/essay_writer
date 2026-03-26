---
name: essay-planning
description: Create a structured essay plan with sections, word allocation, and research queries
---

# Essay Planning Skill

## When to Use
- When creating the essay plan from the assignment brief (Step 2)

## Process
1. Read `/brief/assignment.json` for the assignment requirements.
2. Identify the core question or topic the essay must address.
3. Formulate a clear thesis statement (1-2 sentences).
4. Break the essay into sections (typically 4-6 including introduction and conclusion).
5. Allocate word counts per section summing to the overall target.
6. Write 6-8 targeted search queries for finding academic sources (in both Greek and English).
7. Write the plan as JSON to `/plan/plan.json`.
8. Return a short status: "OK: plan written, {section_count} sections, {total_words} target words, {query_count} queries"

## Plan Structure (`/plan/plan.json`)

Write a JSON object with this exact schema:

```json
{
  "thesis": "1-2 sentence thesis statement in Greek",
  "total_word_target": 3000,
  "sections": [
    {
      "number": 1,
      "title": "Εισαγωγή (Introduction)",
      "heading": "# 1. Εισαγωγή",
      "word_target": 350,
      "key_points": "What to cover in this section",
      "content_outline": "2-4 bullet points describing the content in detail"
    },
    {
      "number": 2,
      "title": "Section Title in Greek",
      "heading": "# 2. Section Title",
      "word_target": 800,
      "key_points": "What to cover",
      "content_outline": "Detailed content description"
    }
  ],
  "research_queries": [
    "query 1 in Greek or English",
    "query 2",
    "query 3"
  ]
}
```

### Field rules:
- `thesis` (required): Thesis statement in Greek.
- `total_word_target` (required): Sum of all section `word_target` values.
- `sections` (required): Array of section objects. Each must have `number`, `title`, `heading`, `word_target`, `key_points`, `content_outline`.
- `research_queries` (required): Array of 6-8 search query strings.

### Content Outline Detail

Each section's `content_outline` should be detailed enough for independent writing:
- What arguments/sub-topics to develop
- Which types of evidence/data to present
- How the section relates to the thesis
- Any specific examples or case studies to include

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
- The output MUST be valid JSON. Do not wrap in markdown code fences inside `write_file`.
