"""Shared HTTP utilities for tools."""

from __future__ import annotations

import os


def get_ssl_verify() -> str | bool:
    """Return the CA bundle path if set, otherwise default verification."""
    return (
        os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE") or True
    )
