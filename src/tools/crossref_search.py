"""Crossref academic search tool.

Uses the Crossref REST API (https://api.crossref.org/) for broad
scholarly metadata coverage, especially strong for journal articles
and DOI-based lookups across all disciplines.
No API key required — uses the polite pool with a mailto parameter.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Annotated

import httpx
from langchain_core.tools import tool

from src.tools._http import DEFAULT_MAILTO, get_ssl_verify, search_error_response

logger = logging.getLogger(__name__)

_CROSSREF_API = "https://api.crossref.org/works"


@tool
def crossref_search(
    query: Annotated[str, "The search query for finding academic papers."],
    max_results: Annotated[int, "Maximum number of results to return."] = 5,
) -> str:
    """Search Crossref for academic papers.

    Returns structured metadata: title, authors, year, abstract, DOI, URL.
    Broad journal article coverage across all disciplines.
    """
    mailto = os.environ.get("CROSSREF_MAILTO", DEFAULT_MAILTO)
    params = {
        "query": query,
        "rows": max_results,
        "mailto": mailto,
        "select": "title,author,published,abstract,DOI,URL",
    }

    try:
        resp = httpx.get(
            _CROSSREF_API, params=params, timeout=30, verify=get_ssl_verify()
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("Crossref request failed for query %r: %s", query, exc)
        return search_error_response("crossref", query, exc)

    data = resp.json()

    results: list[dict] = []
    for item in data.get("message", {}).get("items", []):
        # Extract author names
        authors = [
            f"{a.get('given', '')} {a.get('family', '')}".strip()
            for a in item.get("author", [])
        ]

        # Extract publication year from date-parts
        year = None
        published = item.get("published")
        if published:
            date_parts = published.get("date-parts", [[]])
            if date_parts and date_parts[0]:
                year = date_parts[0][0]

        # Crossref abstracts may contain JATS XML tags — strip them
        abstract = item.get("abstract", "") or ""
        if "<" in abstract:
            abstract = re.sub(r"<[^>]+>", "", abstract).strip()

        # Title is a list in Crossref
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
                "source": "crossref",
            }
        )

    return json.dumps(results, ensure_ascii=False, indent=2)
