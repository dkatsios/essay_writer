---
name: intake
description: Read assignment materials and produce a structured brief at /brief/assignment.json
---

# Intake Skill

## When to Use
- At the start of the pipeline (Step 1) to process assignment materials

## Process
1. Read the extracted content from `/input/extracted.md` using `read_file`. This file contains text extracted from the user's assignment files (PDFs, DOCX, PPTX, text).
2. If any files are scanned documents (images), use `read_pdf` on the original files in `/input/` to access their content.
3. Analyze the content to identify: topic, scope, word count target, academic level, course details, professor name, student name, any specific instructions.
4. Write the structured brief as JSON to `/brief/assignment.json` using `write_file`.

## Output Format (`/brief/assignment.json`)

Write a JSON object with this exact schema:

```json
{
  "topic": "Main topic/title of the essay",
  "word_count": "3000",
  "academic_level": "undergraduate",
  "language": "Greek (Δημοτική)",
  "course": "Course name or null",
  "professor": "Professor name or null",
  "student": "Student name/email or null",
  "institution": "University name or null",
  "description": "Full description of what the essay should cover, key points to address, any specific structure requested",
  "special_instructions": "Any additional requirements or null"
}
```

### Field rules:
- `topic` (required): The main essay topic/title.
- `word_count`: Target word count as a string, or `null` if not specified.
- `academic_level`: One of `"undergraduate"`, `"postgraduate"`, or `null`.
- `language`: Default `"Greek (Δημοτική)"`.
- `course`, `professor`, `student`, `institution`: Include if found, else `null`.
- `description` (required): Comprehensive description of the assignment.
- `special_instructions`: Extra requirements, or `null` if none.

## Important
- Extract ALL relevant information from the provided documents — do not leave anything out.
- If information is in Greek, keep it in Greek.
- If a field cannot be determined from the documents, set it to `null`.
- Do NOT fabricate information. Only include what is explicitly stated in the documents.
- The output MUST be valid JSON. Do not wrap in markdown code fences inside `write_file`.
- Return a short status when done: "OK: brief written to /brief/assignment.json"
