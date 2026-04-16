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
from src.tools.author_names import surname_from_author_string

logger = logging.getLogger(__name__)

_CROSSREF_API = "https://api.crossref.org/works"


def _strip_inline_markup(text: str) -> str:
    """Remove JATS/HTML-style tags Crossref sometimes embeds in title and abstract."""
    if not text or "<" not in text:
        return text
    return re.sub(r"<[^>]+>", "", text).strip()


def search_crossref(
    query: str,
    max_results: int = 5,
    *,
    prefer_fulltext: bool = False,
) -> tuple[list[dict], dict]:
    """Search Crossref and return (results, raw_api_response)."""
    mailto = os.environ.get("CROSSREF_MAILTO", DEFAULT_MAILTO)
    filters = ["has-abstract:true"]
    if prefer_fulltext:
        filters.append("has-full-text:true")
    params = {
        "query": query,
        "filter": ",".join(filters),
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
        authors: list[str] = []
        author_families: list[str] = []
        for a in item.get("author", []):
            if a.get("name"):
                name = str(a.get("name", "")).strip()
                authors.append(name)
                author_families.append(surname_from_author_string(name) if name else "")
                continue
            given = (a.get("given") or "").strip()
            family = (a.get("family") or "").strip()
            display = f"{given} {family}".strip()
            authors.append(display)
            if family:
                author_families.append(family)
            elif display:
                author_families.append(surname_from_author_string(display))
            else:
                author_families.append("")

        year = None
        published = item.get("published")
        if published:
            date_parts = published.get("date-parts", [[]])
            if date_parts and date_parts[0]:
                year = date_parts[0][0]

        abstract = _strip_inline_markup(item.get("abstract", "") or "")

        title_list = item.get("title", [])
        title_raw = title_list[0] if title_list else ""
        title = _strip_inline_markup(title_raw)

        results.append(
            {
                "title": title,
                "authors": authors,
                "author_families": author_families,
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
