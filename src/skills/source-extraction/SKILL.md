---
name: source-extraction
description: Extract section-specific content from academic sources with quotes, page numbers, and citation keys
---

# Source Extraction Skill

## When to Use
- When extracting content from a source document for specific essay sections (Phase 5)

## Process

1. Read the source mapping from `/plan/source_mapping.md` to determine which sections need content from this source.
2. Read the full source document:
   - For PDFs: use `read_pdf` (read all pages for complete extraction)
   - For DOCX: use `read_docx`
   - For web content: use `fetch_url`
   - For VFS metadata files: use `read_file`
3. Read the final plan from `/plan/final.md` to understand each section's arguments.
4. For each section this source maps to, produce a comprehensive extract.

## Output Format

Write each extract to `/sections/section_XX/sources/source_YY.md`:

```markdown
# Source Extract: [Full Source Title]
## For Section: [Section Title]

### Bibliographic Information
- **Authors**: [Full names, e.g., "Παπαδόπουλος, Γ. & Ιωάννου, Μ."]
- **Title**: [Full title in original language]
- **Year**: [2023]
- **Journal/Publisher**: [Full name]
- **Volume/Issue**: [if applicable]
- **Pages**: [if applicable]
- **DOI**: [if available]
- **URL**: [if available]
- **Citation Key**: [e.g., "Παπαδόπουλος & Ιωάννου, 2023"]

### Key Arguments
- [Argument with context] (p. XX)
- [Another argument] (pp. XX-YY)

### Direct Quotes
> "[Exact quote preserving original language and punctuation]" (p. XX)

> "[Another relevant quote]" (p. YY)

### Data and Evidence
- [Statistic or finding] (p. XX)
- [Case study reference] (p. YY)

### Counter-arguments or Limitations
- [Limitations acknowledged by the authors] (p. XX)

### Relevance to Section
[How this content supports or challenges the section's planned argument]
```

## Extraction Guidelines

1. **Be exhaustive**: This is the ONLY time the source will be read. Writers depend entirely on your extracts. Missing content means missing citations.
2. **Preserve quotes exactly**: Copy quotes verbatim in their original language. Do not translate, paraphrase, or "fix" them.
3. **Always include page numbers**: Every piece of content must have a page reference.
4. **Full bibliography every time**: Even if the same source appears in multiple section extracts, include complete bibliographic information in each.
5. **Separate sections clearly**: Write a separate VFS file for each section, even if the content overlaps.
6. **Handle errors gracefully**: If the source is unreadable, write an entry with `## Status: UNREADABLE` and explain the error. If a source has no relevant content for a section, write `## Status: EMPTY`.

## Citation Key Format (APA7)

- One author: "Παπαδόπουλος, 2023"
- Two authors: "Παπαδόπουλος & Ιωάννου, 2023"
- Three or more: "Παπαδόπουλος κ.ά., 2023" (Greek) or "Smith et al., 2023" (English)
