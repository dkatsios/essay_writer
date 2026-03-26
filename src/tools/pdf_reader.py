"""PDF text extraction."""

from __future__ import annotations

from pathlib import Path

import pymupdf


def _parse_page_range(pages: str, total: int) -> list[int]:
    """Parse a page range string like '1-5,8,10-12' into a list of 0-based indices."""
    result: list[int] = []
    for part in pages.split(","):
        part = part.strip()
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start = max(int(start_s) - 1, 0)
            end = min(int(end_s), total)
            result.extend(range(start, end))
        else:
            idx = int(part) - 1
            if 0 <= idx < total:
                result.append(idx)
    return sorted(set(result))


def read_pdf_text(file_path: str, pages: str | None = None) -> str:
    """Extract text from a PDF file. Returns text with page markers."""
    p = Path(file_path)
    if not p.is_file():
        raise FileNotFoundError(f"PDF not found: {file_path}")
    doc = pymupdf.open(str(p))
    total = len(doc)

    if pages:
        indices = _parse_page_range(pages, total)
    else:
        indices = list(range(total))

    parts: list[str] = []
    for i in indices:
        page = doc[i]
        text = page.get_text()
        parts.append(f"--- Page {i + 1} of {total} ---\n{text}")

    doc.close()
    return "\n\n".join(parts)
