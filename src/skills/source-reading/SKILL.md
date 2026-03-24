---
name: source-reading
description: Read a single academic source and extract relevant notes to /sources/notes/{source_id}.md
---

# Source Reading Skill

## When to Use
- When reading individual academic sources (Step 4)

## Process
1. Use `fetch_url`, `read_pdf`, or `read_docx` to access the source content.
2. Identify content relevant to the given topic focus.
3. Extract key quotes, data points, and arguments.
4. Write notes to `/sources/notes/{source_id}.md` using `write_file`.
5. Return a short status: "OK: {source_id} — {one-line summary}" on success, or "FAIL: {source_id} — {reason}" on failure.

## Notes Format

Write the following to `/sources/notes/{source_id}.md`:

```markdown
# {source_id}

## Source Summary
- **Title**: [title of the source]
- **Authors**: [author names]
- **Year**: [publication year]
- **Type**: [journal article / book / report / etc.]

## Relevant Extracts
- [Key finding or quote with page reference if available]
- [Another relevant point]
- ...
```

For inaccessible sources, write:

```markdown
# {source_id}

## Status: INACCESSIBLE
- **Reason**: [e.g., 404 Not Found / Paywall / Timeout / etc.]
- **URL**: [the URL that was attempted]
```

## Important
- The `source_id` will be provided in the task description — use it exactly as given for the VFS path.
- Focus ONLY on content relevant to the given topic.
- Keep extracts concise — aim for 200-500 words total per source.
- Include page numbers for direct quotes when available.
- If the source is inaccessible (paywall, 404, etc.), still write a notes file documenting the failure, then return "FAIL: {source_id} — {reason}".
- Do NOT fabricate content. Only include what is actually in the source.
