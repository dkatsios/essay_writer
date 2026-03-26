---
name: source-reading
description: Read a single academic source and extract relevant notes to /sources/notes/{source_id}.json
---

# Source Reading Skill

## When to Use
- When reading individual academic sources (Step 4)

## Process
1. Use `fetch_url`, `read_pdf`, or `read_docx` to access the source content.
2. If the URL returns a 403/paywall/error, you still have the **title, authors, year, abstract, and DOI** from the task message. Use that metadata to write a useful note.
3. Identify content relevant to the given topic focus.
4. Extract key quotes, data points, and arguments.
5. Write notes as JSON to `/sources/notes/{source_id}.json` using `write_file`.
6. Return a short status: "OK: {source_id} — {one-line summary}" on success, or "OK: {source_id} — metadata only (inaccessible)" if you used only metadata.

## Notes Format

Write a JSON object to `/sources/notes/{source_id}.json` with this schema:

### When the source is accessible (full text or abstract available):

```json
{
  "source_id": "smith2020",
  "is_accessible": true,
  "title": "Title of the source",
  "authors": ["Author One", "Author Two"],
  "year": "2020",
  "source_type": "journal-article",
  "summary": "Brief summary of the source and its relevance to the topic",
  "relevant_extracts": [
    "Key finding or quote with page reference if available",
    "Another relevant point",
    "Data point or statistic"
  ]
}
```

### When the source is inaccessible but you have metadata/abstract:

```json
{
  "source_id": "smith2020",
  "is_accessible": true,
  "title": "Title",
  "authors": ["Author One"],
  "year": "2020",
  "source_type": "journal-article",
  "summary": "Based on abstract: key points relevant to topic",
  "relevant_extracts": [
    "Key point from abstract relevant to the topic",
    "Another relevant point from metadata"
  ]
}
```

Note: Set `is_accessible` to `true` even when using metadata/abstract — as long as you can extract useful information, the note is considered accessible.

### When NOTHING useful can be extracted (no abstract, no metadata):

```json
{
  "source_id": "smith2020",
  "is_accessible": false,
  "title": "Title if known",
  "authors": [],
  "inaccessible_reason": "404 Not Found",
  "url": "https://example.com/paper"
}
```

## Important
- The `source_id` will be provided in the task description — use it exactly as given.
- Focus ONLY on content relevant to the given topic.
- Keep extracts concise — aim for 200-500 words total across `summary` + `relevant_extracts`.
- A note from metadata/abstract is much more valuable than an inaccessible stub. Always prefer writing something useful and set `is_accessible` to `true`.
- **HARD LIMIT**: If `fetch_url` returns an error (404, timeout, etc.), do NOT retry the same URL or try URL variations. Write the note immediately. Maximum 2 fetch attempts per source.
- Include page numbers for direct quotes when available.
- Do NOT fabricate content. Only include what is actually in the source.
- The output MUST be valid JSON. Do not wrap in markdown code fences inside `write_file`.
