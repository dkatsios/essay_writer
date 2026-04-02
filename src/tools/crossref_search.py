"""Crossref academic search.

Uses the Crossref REST API (https://api.crossref.org/) for broad
scholarly metadata coverage, especially strong for journal articles
and DOI-based lookups across all disciplines.
No API key required — uses the polite pool with a mailto parameter.
"""

from __future__ import annotations

import logging
import os
import re

import httpx

from src.tools._http import DEFAULT_MAILTO, http_get

logger = logging.getLogger(__name__)

_CROSSREF_API = "https://api.crossref.org/works"


def search_crossref(query: str, max_results: int = 5) -> tuple[list[dict], dict]:
    """Search Crossref and return (results, raw_api_response)."""
    mailto = os.environ.get("CROSSREF_MAILTO", DEFAULT_MAILTO)
    params = {
        "query": query,
        "rows": max_results,
        "mailto": mailto,
        "select": "title,author,published,abstract,DOI,URL,type,is-referenced-by-count",
    }

    try:
        resp = http_get(
            _CROSSREF_API,
            params=params,
            max_retries=2,
            initial_backoff=1.0,
            request_name="Crossref",
        )
    except httpx.HTTPError as exc:
        logger.error("Crossref request failed for query %r: %s", query, exc)
        return [], {}

    data = resp.json()

    results: list[dict] = []
    for item in data.get("message", {}).get("items", []):
        authors = [
            f"{a.get('given', '')} {a.get('family', '')}".strip()
            for a in item.get("author", [])
        ]

        year = None
        published = item.get("published")
        if published:
            date_parts = published.get("date-parts", [[]])
            if date_parts and date_parts[0]:
                year = date_parts[0][0]

        abstract = item.get("abstract", "") or ""
        if "<" in abstract:
            abstract = re.sub(r"<[^>]+>", "", abstract).strip()

        title_list = item.get("title", [])
        title = title_list[0] if title_list else ""

        results.append(
            {
                "title": title,
                "authors": authors,
                "year": year,
                "abstract": abstract,
                "doi": item.get("DOI", ""),
                "url": item.get("URL", ""),
                "pdf_url": "",
                "source_type": (item.get("type") or "").lower(),
                "citation_count": item.get("is-referenced-by-count") or 0,
            }
        )

    return results, data
