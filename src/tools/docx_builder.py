"""DOCX document assembly tool.

Converts structured essay text (markdown-like) into a formatted .docx file
with cover page, table of contents, headings, body text, and references.
"""

from __future__ import annotations

import json
import re
from typing import Annotated

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt
from langchain_core.tools import tool


def _set_document_defaults(doc: Document, config: dict) -> None:
    """Apply default formatting to the document."""
    style = doc.styles["Normal"]
    font = style.font
    font.name = config.get("font", "Times New Roman")
    font.size = Pt(config.get("font_size", 12))

    pf = style.paragraph_format
    pf.space_after = Pt(0)

    line_spacing = config.get("line_spacing", 1.5)
    pf.line_spacing = line_spacing

    if config.get("paragraph_indent", True):
        pf.first_line_indent = Cm(1.27)

    for section in doc.sections:
        margin = Cm(config.get("margins_cm", 2.5))
        section.top_margin = margin
        section.bottom_margin = margin
        section.left_margin = margin
        section.right_margin = margin


def _add_cover_page(doc: Document, config: dict) -> None:
    """Add a cover page with title, author, institution, course, date."""
    # Add some blank space
    for _ in range(6):
        doc.add_paragraph()

    title = config.get("title", "")
    if title:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(title)
        run.bold = True
        run.font.size = Pt(18)

    doc.add_paragraph()

    for field in ["author", "institution", "course", "professor", "date"]:
        value = config.get(field, "")
        if value:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.add_run(value).font.size = Pt(14)

    doc.add_page_break()


def _add_toc(doc: Document) -> None:
    """Add a table of contents field. Word will populate it on open."""
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("ΠΙΝΑΚΑΣ ΠΕΡΙΕΧΟΜΕΝΩΝ")
    run.bold = True
    run.font.size = Pt(14)

    p = doc.add_paragraph()
    fld_char_begin = OxmlElement("w:fldChar")
    fld_char_begin.set(qn("w:fldCharType"), "begin")

    instr_text = OxmlElement("w:instrText")
    instr_text.set(qn("xml:space"), "preserve")
    instr_text.text = r' TOC \o "1-3" \h \z \u '

    fld_char_separate = OxmlElement("w:fldChar")
    fld_char_separate.set(qn("w:fldCharType"), "separate")

    fld_char_end = OxmlElement("w:fldChar")
    fld_char_end.set(qn("w:fldCharType"), "end")

    run = p.add_run()
    run._element.append(fld_char_begin)
    run2 = p.add_run()
    run2._element.append(instr_text)
    run3 = p.add_run()
    run3._element.append(fld_char_separate)
    run4 = p.add_run("(Update table of contents to populate)")
    run5 = p.add_run()
    run5._element.append(fld_char_end)

    doc.add_page_break()


def _add_page_numbers(doc: Document, position: str = "bottom_center") -> None:
    """Add page numbers to the document footer."""
    for section in doc.sections:
        footer = section.footer
        footer.is_linked_to_previous = False
        p = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER

        fld_char_begin = OxmlElement("w:fldChar")
        fld_char_begin.set(qn("w:fldCharType"), "begin")
        instr_text = OxmlElement("w:instrText")
        instr_text.text = "PAGE"
        fld_char_end = OxmlElement("w:fldChar")
        fld_char_end.set(qn("w:fldCharType"), "end")

        run = p.add_run()
        run._element.append(fld_char_begin)
        run2 = p.add_run()
        run2._element.append(instr_text)
        run3 = p.add_run()
        run3._element.append(fld_char_end)


_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+)$")
_BULLET_RE = re.compile(r"^[\*\-]\s+(.+)$")
_NUMBERED_RE = re.compile(r"^\d+\.\s+(.+)$")
_INLINE_RE = re.compile(r"(\*\*\*(.+?)\*\*\*|\*\*(.+?)\*\*|\*(.+?)\*)")


def _add_formatted_runs(paragraph, text: str) -> None:
    """Parse markdown inline formatting and add runs with bold/italic."""
    pos = 0
    for m in _INLINE_RE.finditer(text):
        # Add plain text before this match
        if m.start() > pos:
            paragraph.add_run(text[pos : m.start()])
        if m.group(2):  # ***bold+italic***
            run = paragraph.add_run(m.group(2))
            run.bold = True
            run.italic = True
        elif m.group(3):  # **bold**
            run = paragraph.add_run(m.group(3))
            run.bold = True
        elif m.group(4):  # *italic*
            run = paragraph.add_run(m.group(4))
            run.italic = True
        pos = m.end()
    # Remaining plain text
    if pos < len(text):
        paragraph.add_run(text[pos:])


def _parse_and_add_content(doc: Document, essay_text: str) -> None:
    """Parse markdown-like essay text and add to document with proper styles."""
    lines = essay_text.split("\n")
    current_paragraph_lines: list[str] = []

    def flush_paragraph() -> None:
        if current_paragraph_lines:
            text = " ".join(current_paragraph_lines)
            p = doc.add_paragraph()
            _add_formatted_runs(p, text)
            current_paragraph_lines.clear()

    for line in lines:
        stripped = line.strip()
        heading_match = _HEADING_RE.match(stripped)
        if heading_match:
            flush_paragraph()
            level = len(heading_match.group(1))
            text = heading_match.group(2).strip()
            doc.add_heading(text, level=min(level, 4))
        elif stripped == "":
            flush_paragraph()
        elif _BULLET_RE.match(stripped):
            flush_paragraph()
            content = _BULLET_RE.match(stripped).group(1)
            p = doc.add_paragraph(style="List Bullet")
            _add_formatted_runs(p, content)
        elif _NUMBERED_RE.match(stripped):
            flush_paragraph()
            content = _NUMBERED_RE.match(stripped).group(1)
            p = doc.add_paragraph(style="List Number")
            _add_formatted_runs(p, content)
        else:
            current_paragraph_lines.append(stripped)

    flush_paragraph()


def _build_document(essay_text: str, config: dict) -> Document:
    """Build a complete .docx document from essay text and config."""
    doc = Document()
    _set_document_defaults(doc, config)
    _add_cover_page(doc, config)
    _add_toc(doc)
    _parse_and_add_content(doc, essay_text)
    _add_page_numbers(doc, config.get("page_numbers", "bottom_center"))
    return doc


def make_build_docx(output_dir: str):
    """Create a build_docx tool bound to a real output directory.

    The LLM passes VFS paths like ``/output/essay.docx``.  This factory
    resolves them to real filesystem paths under *output_dir* so that
    ``doc.save()`` writes to disk correctly.
    """
    from pathlib import Path

    output_dir_path = Path(output_dir)

    @tool
    def build_docx(
        essay_text: Annotated[str, "The full essay text in markdown-like format."],
        output_path: Annotated[str, "Output file path for the .docx file."],
        config_json: Annotated[
            str,
            "JSON string with document config: title, author, institution, "
            "course, professor, date, font, font_size, line_spacing, "
            "margins_cm, citation_style, page_numbers, paragraph_indent.",
        ],
    ) -> str:
        """Build a formatted .docx document from essay text.

        Creates a document with cover page, table of contents, formatted body,
        and page numbers. Handles Greek characters natively.
        """
        config = json.loads(config_json)
        doc = _build_document(essay_text, config)

        # Resolve VFS path → real filesystem path
        clean = output_path.lstrip("/")
        if clean.startswith("output/"):
            clean = clean[len("output/") :]
        real_path = output_dir_path / clean
        real_path.parent.mkdir(parents=True, exist_ok=True)

        doc.save(str(real_path))
        return f"Document saved to {output_path}"

    return build_docx
