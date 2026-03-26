---
name: validate
description: Evaluate whether the assignment brief has enough information to produce a quality essay
---

# Validate Skill

## When to Use
- After intake (Step 2) to check whether the brief supports a quality essay

## Process
1. Read the brief at `/brief/assignment.json` using `read_file`.
2. Evaluate whether the brief contains enough information for a planner and writer to produce a strong essay.
3. Write the result as JSON to `/brief/validation.json`.

## Evaluation Criteria

**High bar for questions.** Only flag gaps that would **significantly affect the direction or quality** of the essay. Do NOT ask about:
- Minor stylistic preferences
- Details the writer can reasonably infer or decide
- Information that is merely "nice to have"

Ask only when missing information would lead the essay down a wrong path or produce a substantially weaker result.

Examples of gaps worth flagging:
- Word count not specified (affects scope, depth, and structure)
- Topic is ambiguous (could mean two very different things)
- Academic level unclear when it would change tone/depth dramatically
- Contradictory requirements

Examples of gaps NOT worth flagging:
- Citation style not specified (writer can default to APA)
- Number of sources not specified (planner can decide)
- Formatting preferences not mentioned (use standard academic)

## Output Format (`/brief/validation.json`)

If the brief is **sufficient**, write:

```json
{
  "is_pass": true
}
```

If the brief has **significant gaps**, write:

```json
{
  "is_pass": false,
  "questions": [
    {
      "question": "Question text",
      "options": ["Option A", "Option B", "Option C"]
    },
    {
      "question": "Another question",
      "options": ["Option A", "Option B"]
    }
  ]
}
```

### Rules for questions:
- Each question MUST have 2–4 options in the `options` array
- Options should cover the most likely/reasonable answers
- Write questions and options in the **same language as the brief** (usually Greek)
- Keep questions concise — one line each
- Maximum 4 questions total
- Do NOT repeat information already in the brief
- The output MUST be valid JSON. Do not wrap in markdown code fences inside `write_file`.
