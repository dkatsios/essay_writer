---
name: research
description: Extract research queries from the plan and call research_sources to build the source registry
---

# Research Skill

## When to Use
- After the essay plan is written (Step 3) to find academic sources

## Process
1. Read `/plan/plan.md` using `read_file`.
2. Extract the research queries from the "Research Queries" section.
3. Call `research_sources` **ONCE** with:
   - `queries_json`: a JSON array of the query strings (e.g., `["query 1", "query 2"]`)
   - `max_sources`: use the value specified in the task message (default 10 if not specified)
4. Return a short status: "OK: {N} sources registered"

## HARD LIMIT
- Call `research_sources` exactly **ONCE**. Do NOT call it again.
- Do NOT modify the queries — use them exactly as written in the plan.
- Do NOT write the registry yourself — the tool writes `/sources/registry.json` automatically.
