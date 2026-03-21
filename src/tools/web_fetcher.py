"""URL content fetching tool."""

from __future__ import annotations

import re
from typing import Annotated

import httpx
from langchain_core.tools import tool

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\n{3,}")


def _html_to_text(html: str) -> str:
    """Crude HTML tag stripping. Good enough for fetching article text."""
    text = _TAG_RE.sub("", html)
    return _WHITESPACE_RE.sub("\n\n", text).strip()


@tool
def fetch_url(
    url: Annotated[str, "The URL to fetch content from."],
) -> str:
    """Fetch content from a URL and return as plain text. Strips HTML tags."""
    resp = httpx.get(url, follow_redirects=True, timeout=30)
    resp.raise_for_status()

    content_type = resp.headers.get("content-type", "")
    if "html" in content_type:
        return _html_to_text(resp.text)
    return resp.text
