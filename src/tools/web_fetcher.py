"""URL content fetching tool."""

from __future__ import annotations

import os
import re
from typing import Annotated

import httpx
from langchain_core.tools import tool

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\n{3,}")


def _get_ssl_verify() -> str | bool:
    """Return the CA bundle path if set, otherwise default verification."""
    return (
        os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE") or True
    )


def _html_to_text(html: str) -> str:
    """Crude HTML tag stripping. Good enough for fetching article text."""
    text = _TAG_RE.sub("", html)
    return _WHITESPACE_RE.sub("\n\n", text).strip()


@tool
def fetch_url(
    url: Annotated[str, "The URL to fetch content from."],
) -> str:
    """Fetch content from a URL and return as plain text. Strips HTML tags."""
    resp = httpx.get(url, follow_redirects=True, timeout=30, verify=_get_ssl_verify())
    resp.raise_for_status()

    content_type = resp.headers.get("content-type", "")
    if "html" in content_type:
        return _html_to_text(resp.text)
    return resp.text
