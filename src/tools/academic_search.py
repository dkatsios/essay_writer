"""Semantic Scholar academic search."""

from __future__ import annotations

import json
import logging
import os
import threading
import time

import httpx

from src.tools._http import get_ssl_verify

logger = logging.getLogger(__name__)

_SEMANTIC_SCHOLAR_API = "https://api.semanticscholar.org/graph/v1/paper/search"
_MAX_RETRIES = 3
_INITIAL_BACKOFF = 2  # seconds
_MIN_REQUEST_INTERVAL = 1.0  # seconds between requests (unauthenticated limit: 1 req/s)

# Thread-safe throttle: parallel subagents share this to avoid bursting
_request_lock = threading.Lock()
_last_request_time = 0.0


def _throttle() -> None:
    """Ensure at least _MIN_REQUEST_INTERVAL seconds between API calls."""
    global _last_request_time
    with _request_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        if elapsed < _MIN_REQUEST_INTERVAL:
            time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
        _last_request_time = time.monotonic()


def _get_headers() -> dict[str, str]:
    """Build request headers, including API key if available."""
    headers: dict[str, str] = {}
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def search_semantic_scholar(
    query: str, max_results: int = 5
) -> tuple[list[dict], dict]:
    """Search Semantic Scholar and return (results, raw_api_response)."""
    params = {
        "query": query,
        "limit": max_results,
        "fields": "title,authors,year,abstract,externalIds,url,openAccessPdf,publicationTypes",
    }

    headers = _get_headers()
    for attempt in range(_MAX_RETRIES):
        _throttle()
        resp = httpx.get(
            _SEMANTIC_SCHOLAR_API,
            params=params,
            headers=headers,
            timeout=30,
            verify=get_ssl_verify(),
        )
        if resp.status_code == 429:
            wait = _INITIAL_BACKOFF * (2**attempt)
            logger.warning(
                "Semantic Scholar 429 — retrying in %ds (attempt %d/%d)",
                wait,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(wait)
            continue
        if resp.is_error:
            logger.error(
                "Semantic Scholar HTTP %d for query: %s",
                resp.status_code,
                query,
            )
            return [], {}
        break
    else:
        logger.error(
            "Semantic Scholar rate limit exceeded after %d retries for query: %s",
            _MAX_RETRIES,
            query,
        )
        return [], {}

    data = resp.json()

    results: list[dict] = []
    for paper in data.get("data", []):
        authors = [a.get("name", "") for a in paper.get("authors", [])]
        external_ids = paper.get("externalIds") or {}
        oa_pdf = paper.get("openAccessPdf") or {}
        pub_types = paper.get("publicationTypes") or []
        results.append(
            {
                "title": paper.get("title", ""),
                "authors": authors,
                "year": paper.get("year"),
                "abstract": paper.get("abstract", ""),
                "doi": external_ids.get("DOI", ""),
                "url": paper.get("url", ""),
                "pdf_url": oa_pdf.get("url", ""),
                "source_type": pub_types[0].lower() if pub_types else "",
            }
        )

    return results, data
