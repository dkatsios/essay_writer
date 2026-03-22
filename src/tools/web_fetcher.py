"""URL content fetching tool."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Annotated

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


@tool
def fetch_url(
    url: Annotated[str, "The URL to fetch content from."],
) -> str:
    """Fetch content from a URL and return as plain text. Strips HTML tags."""
    try:
        resp = httpx.get(
            url, follow_redirects=True, timeout=30, verify=get_ssl_verify()
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return f"Error fetching {url}: HTTP {exc.response.status_code}"
    except httpx.RequestError as exc:
        return f"Error fetching {url}: {type(exc).__name__}"

    content_type = resp.headers.get("content-type", "")
    if "html" in content_type:
        return _html_to_text(resp.text)
    return resp.text
