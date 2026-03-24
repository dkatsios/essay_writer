"""OpenAlex academic search tool.

Uses the OpenAlex API (https://docs.openalex.org/) for broad scholarly
coverage, especially strong for non-English sources.
No API key required — uses the polite pool with a mailto parameter.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Annotated

import httpx
from langchain_core.tools import tool

from src.tools._http import DEFAULT_MAILTO, get_ssl_verify, search_error_response

logger = logging.getLogger(__name__)

_OPENALEX_API = "https://api.openalex.org/works"


def search_openalex(query: str, max_results: int = 5) -> list[dict]:
    """Search OpenAlex and return a list of result dicts."""
    mailto = os.environ.get("OPENALEX_MAILTO", DEFAULT_MAILTO)
    params = {
        "search": query,
        "per_page": max_results,
        "mailto": mailto,
    }

    try:
        resp = httpx.get(
            _OPENALEX_API, params=params, timeout=30, verify=get_ssl_verify()
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("OpenAlex request failed for query %r: %s", query, exc)
        return []

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

        results.append(
            {
                "title": work.get("title", ""),
                "authors": authors,
                "year": work.get("publication_year"),
                "abstract": abstract,
                "doi": doi,
                "url": work.get("id", ""),
            }
        )

    return results


@tool
def openalex_search(
    query: Annotated[str, "The search query for finding academic papers."],
    max_results: Annotated[int, "Maximum number of results to return."] = 5,
) -> str:
    """Search OpenAlex for academic papers.

    Returns structured metadata: title, authors, year, abstract, DOI, URL.
    Good coverage of non-English and European sources.
    """
    results = search_openalex(query, max_results)
    return json.dumps(results, ensure_ascii=False, indent=2)
