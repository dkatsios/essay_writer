"""PDF text extraction tool."""

from __future__ import annotations

from typing import Annotated

import pymupdf
from langchain_core.tools import tool


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


@tool
def read_pdf(
    file_path: Annotated[str, "Path to the PDF file to read."],
    pages: Annotated[
        str | None,
        "Optional page range, e.g. '1-5' or '3,7-10'. If omitted, reads all pages.",
    ] = None,
) -> str:
    """Extract text from a PDF file. Returns text with page markers."""
    doc = pymupdf.open(file_path)
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
