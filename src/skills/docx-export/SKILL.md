---
name: docx-export
description: Export the final essay to a formatted .docx file with cover page, TOC, and academic formatting
---

# DOCX Export Skill

## When to Use
- When the orchestrator exports the final essay to .docx (Step 7)

## Process

1. Read the final essay from `/essay/final.md`.
2. Read the assignment brief from `/brief/assignment.md` to extract cover page metadata.
3. Prepare the document configuration JSON with:
   - **title**: extracted from the essay or assignment brief
   - **author**: from the assignment brief (if provided)
   - **institution**: from the assignment brief (if provided)
   - **course**: from the assignment brief (if provided)
   - **professor**: from the assignment brief (if provided)
   - **date**: from the assignment brief or current date
   - **font**, **font_size**, **line_spacing**, **margins_cm**: from formatting config
   - **citation_style**: from formatting config
   - **page_numbers**: position setting
   - **paragraph_indent**: whether to indent first lines
4. Call the `build_docx` tool with:
   - `essay_text`: the full essay text (with markdown heading markers)
   - `output_path`: `/output/essay.docx`
   - `config_json`: the JSON configuration string

## Document Structure

The generated .docx will contain:
1. **Cover page**: Title (bold, 18pt, centered), followed by author, institution, course, professor, date (14pt, centered)
2. **Table of contents**: Auto-generated from headings (requires manual update in Word)
3. **Essay body**: Formatted with the specified font, size, spacing, and margins
4. **References section**: Part of the essay body, formatted as a heading with entries below
5. **Page numbers**: In the footer, centered

## Heading Mapping
- `# Heading` → Heading 1 (main sections)
- `## Heading` → Heading 2 (subsections)
- `### Heading` → Heading 3 (sub-subsections)
- Plain text → Normal style (body paragraphs)

## Important
- Ensure the essay text uses markdown heading markers (`#`, `##`, etc.) for proper formatting.
- The TOC is a Word field — it shows a placeholder until the user updates it in Word (Ctrl+A, F9).
- Greek characters are fully supported — no special handling needed.
