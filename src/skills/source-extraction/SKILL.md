---
name: source-extraction
description: Read academic sources and produce consolidated research notes with quotes, page numbers, and citation keys
---

# Source Extraction Skill

## When to Use
- When producing consolidated research notes from all available sources (Step 4)

## Process

1. Read the essay plan from `/plan/plan.md` to understand the essay structure and what content each section needs.
2. Read all source metadata from `/sources/metadata/*.md` to identify available sources.
3. Access full source content:
   - For user-provided sources (`provided: true`): use `read_pdf` or `read_docx` on the file path in `/input/`
   - For searched sources with URLs: use `fetch_url` to access full text for the most relevant sources
   - For sources only available as metadata: use the abstract and any available information
4. Write consolidated research notes to `/sources/research_notes.md`.

## Output Format

Write a single file to `/sources/research_notes.md` with all sources:

```markdown
# Σημειώσεις Έρευνας

## Source: [Full Source Title]

### Bibliographic Information
- **Authors**: [Full names, e.g., "Παπαδόπουλος, Γ. & Ιωάννου, Μ."]
- **Title**: [Full title in original language]
- **Year**: [2023]
- **Journal/Publisher**: [Full name]
- **DOI**: [if available]
- **URL**: [if available]
- **Citation Key**: [e.g., "Παπαδόπουλος & Ιωάννου, 2023"]

### Key Arguments
- [Argument with context] (p. XX)

### Direct Quotes
> "[Exact quote preserving original language and punctuation]" (p. XX)

### Data and Evidence
- [Statistic or finding] (p. XX)

### Relevance
[Which essay sections this source supports and how]

---

## Source: [Next Source Title]
...
```

## Extraction Guidelines

1. **Completeness**: The essay writer depends entirely on your notes. Extract everything relevant — key arguments, direct quotes, data, and full bibliographic details.
2. **Preserve quotes exactly**: Copy quotes verbatim in their original language. Do not translate, paraphrase, or "fix" them.
3. **Include page numbers**: Always include page numbers for quotes and key arguments when available.
4. **Prioritize**: Focus effort on sources most relevant to the essay plan. User-provided sources must always be fully extracted.
5. **Handle errors gracefully**: If a source is unreadable, note this briefly and move on.

## Citation Key Format (APA7)

- One author: "Παπαδόπουλος, 2023"
- Two authors: "Παπαδόπουλος & Ιωάννου, 2023"
- Three or more: "Παπαδόπουλος κ.ά., 2023" (Greek) or "Smith et al., 2023" (English)
