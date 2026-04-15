"""OpenAlex academic search.

Uses the OpenAlex API (https://docs.openalex.org/) for broad scholarly
coverage, especially strong for non-English sources.
No API key required — uses the polite pool with a mailto parameter.
"""

from __future__ import annotations

import logging
import os

import httpx

from src.tools._http import DEFAULT_MAILTO, http_get

logger = logging.getLogger(__name__)

_OPENALEX_API = "https://api.openalex.org/works"


def search_openalex(query: str, max_results: int = 5) -> tuple[list[dict], dict]:
    """Search OpenAlex and return (results, raw_api_response)."""
    mailto = os.environ.get("OPENALEX_MAILTO", DEFAULT_MAILTO)
    params = {
        "search": query,
        "filter": "has_abstract:true",
        "per_page": max_results,
        "mailto": mailto,
        "sort": "relevance_score:desc",
    }

    try:
        resp = http_get(
            _OPENALEX_API,
            params=params,
            max_retries=2,
            initial_backoff=1.0,
            request_name="OpenAlex",
        )
    except httpx.HTTPError as exc:
        logger.error("OpenAlex request failed for query %r: %s", query, exc)
        return [], {}

    data = resp.json()

    results: list[dict] = []
    for work in data.get("results", []):
        authors = [
            authorship.get("author", {}).get("display_name", "")
            for authorship in work.get("authorships", [])
        ]

        abstract = ""
        abstract_index = work.get("abstract_inverted_index")
        if abstract_index:
            word_positions: list[tuple[int, str]] = []
            for word, positions in abstract_index.items():
                for pos in positions:
                    word_positions.append((pos, word))
            word_positions.sort()
            abstract = " ".join(w for _, w in word_positions)

        doi = work.get("doi", "") or ""
        if doi.startswith("https://doi.org/"):
            doi = doi[len("https://doi.org/") :]

        oa = work.get("open_access") or {}
        pdf_url = oa.get("oa_url") or ""
        source_type = (work.get("type") or "").lower()

        results.append(
            {
                "title": work.get("title", ""),
                "authors": authors,
                "year": work.get("publication_year"),
                "abstract": abstract,
                "doi": doi,
                "url": work.get("id", ""),
                "pdf_url": pdf_url,
                "source_type": source_type,
                "citation_count": work.get("cited_by_count") or 0,
            }
        )

    return results, data
