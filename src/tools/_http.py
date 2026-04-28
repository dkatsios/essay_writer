"""Shared HTTP utilities for tools."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass

import httpx
from curl_cffi import requests as curl_requests

DEFAULT_MAILTO = "essay-writer@example.com"
DEFAULT_TIMEOUT = 30.0

logger = logging.getLogger(__name__)

_CLIENT_LOCK = threading.Lock()
_HTTP_CLIENT: httpx.Client | None = None


def _default_headers() -> dict[str, str]:
    return {"User-Agent": "essay-writer/0.1"}


def get_http_client() -> httpx.Client:
    """Return a shared HTTP client with connection pooling."""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        with _CLIENT_LOCK:
            if _HTTP_CLIENT is None:
                _HTTP_CLIENT = httpx.Client(
                    headers=_default_headers(),
                    limits=httpx.Limits(
                        max_connections=20,
                        max_keepalive_connections=10,
                    ),
                    verify=get_ssl_verify(),
                )
    return _HTTP_CLIENT


def http_get(
    url: str,
    *,
    params: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    follow_redirects: bool = False,
    max_retries: int = 0,
    initial_backoff: float = 1.0,
    retry_statuses: tuple[int, ...] = (429, 500, 502, 503, 504),
    request_name: str | None = None,
    log_retries: bool = True,
) -> httpx.Response:
    """Issue a GET request with shared transport and optional retries."""
    client = get_http_client()
    label = request_name or url
    delay = initial_backoff
    last_response: httpx.Response | None = None

    for attempt in range(max_retries + 1):
        try:
            response = client.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
                follow_redirects=follow_redirects,
            )
        except httpx.RequestError:
            if attempt < max_retries:
                if log_retries:
                    logger.warning(
                        "%s request failed (attempt %d/%d); retrying in %.1fs",
                        label,
                        attempt + 1,
                        max_retries + 1,
                        delay,
                    )
                time.sleep(delay)
                delay *= 2
                continue
            raise

        last_response = response
        if response.status_code in retry_statuses and attempt < max_retries:
            if log_retries:
                logger.warning(
                    "%s returned HTTP %d (attempt %d/%d); retrying in %.1fs",
                    label,
                    response.status_code,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                )
            time.sleep(delay)
            delay *= 2
            continue

        response.raise_for_status()
        return response

    if last_response is not None:
        last_response.raise_for_status()
    raise RuntimeError("http_get exhausted retries without a response")


# ---------------------------------------------------------------------------
# PDF fetching — curl_cffi with Chrome TLS fingerprint impersonation
# ---------------------------------------------------------------------------


@dataclass
class PdfResponse:
    """Minimal response wrapper for PDF fetch results."""

    status_code: int
    content: bytes
    headers: dict[str, str]


def pdf_get(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = 2,
    initial_backoff: float = 1.0,
    retry_statuses: tuple[int, ...] = (429, 500, 502, 503, 504),
) -> PdfResponse:
    """Fetch a URL using curl_cffi with Chrome TLS impersonation.

    Designed for PDF downloads from academic publishers that block plain
    HTTP clients via TLS fingerprinting. Falls back to the same retry
    logic as ``http_get``.

    Raises ``curl_cffi.requests.errors.RequestsError`` on transport errors
    or ``httpx.HTTPStatusError`` (synthesized) on non-retryable HTTP errors.
    """
    delay = initial_backoff
    last_status: int | None = None

    for attempt in range(max_retries + 1):
        try:
            resp = curl_requests.get(
                url,
                impersonate="chrome",
                timeout=timeout,
                allow_redirects=True,
                verify=get_ssl_verify(),
            )
        except Exception:
            if attempt < max_retries:
                logger.warning(
                    "pdf_get %s failed (attempt %d/%d); retrying in %.1fs",
                    url,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                )
                time.sleep(delay)
                delay *= 2
                continue
            raise

        last_status = resp.status_code
        if resp.status_code in retry_statuses and attempt < max_retries:
            logger.warning(
                "pdf_get %s returned HTTP %d (attempt %d/%d); retrying in %.1fs",
                url,
                resp.status_code,
                attempt + 1,
                max_retries + 1,
                delay,
            )
            time.sleep(delay)
            delay *= 2
            continue

        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code} for {url}",
                request=httpx.Request("GET", url),
                response=httpx.Response(resp.status_code),
            )

        return PdfResponse(
            status_code=resp.status_code,
            content=resp.content,
            headers=dict(resp.headers),
        )

    raise httpx.HTTPStatusError(
        f"HTTP {last_status} for {url} after {max_retries + 1} attempts",
        request=httpx.Request("GET", url),
        response=httpx.Response(last_status or 0),
    )


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
