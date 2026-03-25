---
name: intake
description: Read assignment materials and produce a structured brief at /brief/assignment.md
---

# Intake Skill

## When to Use
- At the start of the pipeline (Step 1) to process assignment materials

## Process
1. Read the extracted content from `/input/extracted.md` using `read_file`. This file contains text extracted from the user's assignment files (PDFs, DOCX, PPTX, text).
2. If any files are scanned documents (images), use `read_pdf` on the original files in `/input/` to access their content.
3. Analyze the content to identify: topic, scope, word count target, academic level, course details, professor name, student name, any specific instructions.
4. Write the structured brief to `/brief/assignment.md`.

## Output Format (`/brief/assignment.md`)

```markdown
# Assignment Brief

## Topic
[Main topic/title of the essay]

## Requirements
- **Word count**: [target word count, or "not specified"]
- **Academic level**: [undergraduate / postgraduate / not specified]
- **Language**: Greek (Δημοτική)

## Course Details
- **Course**: [course name, if found]
- **Professor**: [professor name, if found]
- **Student**: [student name/email, if found]
- **Institution**: [university name, if found]

## Assignment Description
[Full description of what the essay should cover, key points to address, any specific structure requested]

## Special Instructions
[Any additional requirements: specific sources to use, formatting preferences, theoretical frameworks to apply, etc.]
```

## Important
- Extract ALL relevant information from the provided documents — do not leave anything out.
- If information is in Greek, keep it in Greek.
- If a field cannot be determined from the documents, write "not specified".
- Do NOT fabricate information. Only include what is explicitly stated in the documents.
- Return a short status when done: "OK: brief written to /brief/assignment.md"
