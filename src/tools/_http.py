"""Shared HTTP utilities for tools."""

from __future__ import annotations

import json
import os

DEFAULT_MAILTO = "essay-writer@example.com"


def get_ssl_verify() -> str | bool:
    """Return the CA bundle path if set, otherwise default verification."""
    return (
        os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE") or True
    )


def search_error_response(source: str, query: str, exc: Exception) -> str:
    """Return a JSON error string for a failed search request."""
    return json.dumps(
        {
            "error": "request_failed",
            "message": str(exc),
            "query": query,
            "source": source,
        },
        ensure_ascii=False,
    )
