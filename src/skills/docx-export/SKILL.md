---
name: docx-export
description: Export the final essay to a formatted .docx file with cover page, TOC, and academic formatting
---

# DOCX Export Skill

## When to Use
- When the orchestrator exports the essay to .docx (Step 7)

## Process

1. Read the assignment brief from `/brief/assignment.md` to extract cover page metadata (title, author, institution, course, professor, date).
2. Prepare the document configuration JSON with:
   - **title**: extracted from the essay or assignment brief
   - **author**: from the assignment brief (if provided)
   - **institution**: from the assignment brief (if provided)
   - **course**: from the assignment brief (if provided)
   - **professor**: from the assignment brief (if provided)
   - **date**: from the assignment brief or current date
   - **font**, **font_size**, **line_spacing**, **margins_cm**: from formatting config
   - **citation_style**: from formatting config (`apa7` or `footnotes`)
   - **page_numbers**: position setting
   - **paragraph_indent**: whether to indent first lines
   - **text_alignment**: text alignment (justified, left, etc.)
3. Call the `build_docx` tool with:
   - `output_path`: `/output/essay.docx`
   - `config_json`: the JSON configuration string

The tool reads `/essay/draft.md` and `/sources/registry.json` automatically — do NOT read them yourself or pass them as arguments.

## Document Structure

The generated .docx will contain:
1. **Cover page**: Title (bold, 18pt, centered), followed by author, institution, course, professor, date (14pt, centered)
2. **Table of contents**: Native Word TOC field — auto-updates when the document is opened in Word
3. **Essay body**: Formatted with the specified font, size, spacing, and margins
4. **Citations**: `[[source_id]]` markers are replaced with formatted citations based on `citation_style`:
   - `apa7`: inline `(Author, Year)` + Βιβλιογραφία section
   - `footnotes`: superscript numbers + Σημειώσεις (endnotes) section
5. **Page numbers**: In the footer, centered

## Heading Mapping
- `# Heading` → Heading 1 (main sections)
- `## Heading` → Heading 2 (subsections)
- `### Heading` → Heading 3 (sub-subsections)
- Plain text → Normal style (body paragraphs)

## Important
- Ensure the essay text uses markdown heading markers (`#`, `##`, etc.) for proper formatting.
- Ensure the essay uses `[[source_id]]` markers for citations — do NOT write literal APA citations.
- The TOC is a native Word field that auto-updates on open. No manual update needed.
- Greek characters are fully supported — no special handling needed.
