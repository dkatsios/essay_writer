"""DOCX text extraction."""

from __future__ import annotations

from docx import Document


def extract_docx_text(doc: Document) -> str:
    """Convert a python-docx Document to markdown-like text with heading markers."""
    parts: list[str] = []
    for para in doc.paragraphs:
        style_name = para.style.name if para.style else ""
        text = para.text.strip()
        if not text:
            continue
        if style_name.startswith("Heading"):
            level = style_name.replace("Heading", "").strip()
            prefix = "#" * (int(level) if level.isdigit() else 1)
            parts.append(f"{prefix} {text}")
        else:
            parts.append(text)
    return "\n\n".join(parts)


def read_docx_text(file_path: str) -> str:
    """Extract text and structure from a .docx file."""
    return extract_docx_text(Document(file_path))
