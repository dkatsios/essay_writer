"""Academic search tool for finding scholarly sources.

This is a placeholder implementation. The actual search backend
(SerpAPI, Semantic Scholar, scholarly, etc.) will be chosen during
integration testing based on reliability and cost.
"""

from __future__ import annotations

import json
from typing import Annotated

import httpx
from langchain_core.tools import tool

_SEMANTIC_SCHOLAR_API = "https://api.semanticscholar.org/graph/v1/paper/search"


@tool
def academic_search(
    query: Annotated[str, "The search query for finding academic papers."],
    max_results: Annotated[int, "Maximum number of results to return."] = 5,
) -> str:
    """Search Semantic Scholar for academic papers.

    Returns structured metadata: title, authors, year, abstract, DOI, URL.
    """
    params = {
        "query": query,
        "limit": max_results,
        "fields": "title,authors,year,abstract,externalIds,url",
    }
    resp = httpx.get(_SEMANTIC_SCHOLAR_API, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    results: list[dict] = []
    for paper in data.get("data", []):
        authors = [a.get("name", "") for a in paper.get("authors", [])]
        external_ids = paper.get("externalIds") or {}
        results.append({
            "title": paper.get("title", ""),
            "authors": authors,
            "year": paper.get("year"),
            "abstract": paper.get("abstract", ""),
            "doi": external_ids.get("DOI", ""),
            "url": paper.get("url", ""),
        })

    return json.dumps(results, ensure_ascii=False, indent=2)
