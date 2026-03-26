"""URL content fetching tool."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import httpx
from langchain_core.tools import tool

from src.tools._http import get_ssl_verify

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


_MAX_FETCH_FAILURES = 3  # after this many total failures, hard-stop


def make_fetch_url(sources_dir: str | None = None):
    """Create a fetch_url tool, optionally saving PDFs to sources_dir."""
    sources_path = Path(sources_dir) if sources_dir else None
    failed_urls: dict[str, int] = {}  # url → failure count

    @tool
    def fetch_url(
        url: Annotated[str, "The URL to fetch content from."],
    ) -> str:
        """Fetch content from a URL and return as plain text.

        Strips HTML tags from web pages. For PDFs, extracts text and saves
        the original PDF to the sources directory.
        """
        total_failures = sum(failed_urls.values())
        if total_failures >= _MAX_FETCH_FAILURES:
            return (
                f"STOP: This source is inaccessible. "
                f"{total_failures} fetch attempts have already failed. "
                f"Previously failed URLs: {list(failed_urls.keys())}. "
                f"Do NOT attempt any more URLs — write an INACCESSIBLE note and move on."
            )

        try:
            resp = httpx.get(
                url, follow_redirects=True, timeout=30, verify=get_ssl_verify()
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            failed_urls[url] = failed_urls.get(url, 0) + 1
            total_failures = sum(failed_urls.values())
            msg = f"Error fetching {url}: HTTP {exc.response.status_code} (attempt {failed_urls[url]} for this URL, {total_failures} total failures)"
            if total_failures >= _MAX_FETCH_FAILURES:
                msg += (
                    f"\nSTOP: {total_failures} failures reached. "
                    f"Previously failed: {list(failed_urls.keys())}. "
                    f"Do NOT retry — write an INACCESSIBLE note and move on."
                )
            return msg
        except httpx.RequestError as exc:
            failed_urls[url] = failed_urls.get(url, 0) + 1
            total_failures = sum(failed_urls.values())
            msg = f"Error fetching {url}: {type(exc).__name__} (attempt {failed_urls[url]}, {total_failures} total failures)"
            if total_failures >= _MAX_FETCH_FAILURES:
                msg += f"\nSTOP: {total_failures} failures reached. Do NOT retry — write an INACCESSIBLE note."
            return msg

        content_type = resp.headers.get("content-type", "")

        if "pdf" in content_type or url.lower().endswith(".pdf"):
            # Save PDF to sources dir
            if sources_path is not None:
                sources_path.mkdir(parents=True, exist_ok=True)
                filename = _slugify_url(url) + ".pdf"
                pdf_path = sources_path / filename
                pdf_path.write_bytes(resp.content)
            try:
                return _extract_pdf_text(resp.content)
            except Exception:
                return f"Downloaded PDF from {url} but could not extract text."

        if "html" in content_type:
            text = _html_to_text(resp.text)
        else:
            text = resp.text

        # Save fetched text content to sources dir — only if substantive
        if sources_path is not None and text:
            word_count = len(text.split())
            if word_count >= 200:
                sources_path.mkdir(parents=True, exist_ok=True)
                filename = _slugify_url(url) + ".txt"
                content_path = sources_path / filename
                content_path.write_text(text[:50_000], encoding="utf-8")

        return text

    return fetch_url
