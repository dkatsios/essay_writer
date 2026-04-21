"""URL content fetching."""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from src.tools._http import http_get

logger = logging.getLogger(__name__)

_WHITESPACE_RE = re.compile(r"\n{3,}")


class _TextExtractor(HTMLParser):
    """Minimal HTML-to-text converter using the stdlib parser."""

    def __init__(self) -> None:
        super().__init__()
        self._pieces: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = False
        if tag in ("p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"):
            self._pieces.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._pieces.append(data)

    def get_text(self) -> str:
        return _WHITESPACE_RE.sub("\n\n", "".join(self._pieces)).strip()


def _html_to_text(html: str) -> str:
    """Convert HTML to plain text using stdlib HTMLParser."""
    parser = _TextExtractor()
    parser.feed(html)
    return parser.get_text()


def _slugify_url(url: str) -> str:
    """Turn a URL into a safe filename stem (max 80 chars)."""
    parsed = urlparse(url)
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", parsed.netloc + parsed.path)
    return slug.strip("_")[:80]


def _extract_pdf_text(data: bytes) -> str:
    """Extract text from PDF bytes using pymupdf."""
    import pymupdf

    doc = pymupdf.open(stream=data, filetype="pdf")
    parts = []
    total = len(doc)
    for i in range(total):
        text = doc[i].get_text()
        parts.append(f"--- Page {i + 1} of {total} ---\n{text}")
    doc.close()
    return "\n\n".join(parts)


def extract_pdf_bytes_to_text(data: bytes) -> str:
    """Extract plain text from a PDF given as raw bytes (e.g. user upload)."""
    return _extract_pdf_text(data)


def fetch_url_content(url: str, sources_dir: str | None = None) -> str:
    """Fetch content from a URL and return as plain text.

    Strips HTML tags from web pages. For PDFs, extracts text and saves
    the original PDF to the sources directory.

    Raises ``httpx.HTTPStatusError`` or ``httpx.RequestError`` on failure.
    """
    sources_path = Path(sources_dir) if sources_dir else None

    resp = http_get(
        url,
        follow_redirects=True,
        max_retries=2,
        initial_backoff=1.0,
        request_name="web fetch",
        log_retries=False,
    )

    content_type = resp.headers.get("content-type", "")

    if "pdf" in content_type or url.lower().endswith(".pdf"):
        if sources_path is not None:
            sources_path.mkdir(parents=True, exist_ok=True)
            filename = _slugify_url(url) + ".pdf"
            pdf_path = sources_path / filename
            pdf_path.write_bytes(resp.content)
        return _extract_pdf_text(resp.content)

    if "html" in content_type:
        text = _html_to_text(resp.text)
    else:
        text = resp.text

    if sources_path is not None and text:
        word_count = len(text.split())
        if word_count >= 200:
            sources_path.mkdir(parents=True, exist_ok=True)
            filename = _slugify_url(url) + ".txt"
            content_path = sources_path / filename
            content_path.write_text(text[:50_000], encoding="utf-8")

    return text
