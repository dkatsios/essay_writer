"""DOCX text extraction tool."""

from __future__ import annotations

from typing import Annotated

from docx import Document
from langchain_core.tools import tool


@tool
def read_docx(
    file_path: Annotated[str, "Path to the .docx file to read."],
) -> str:
    """Extract text and structure from a .docx file. Preserves heading markers."""
    doc = Document(file_path)
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
