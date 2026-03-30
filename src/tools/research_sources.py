"""Unified research — searches all academic APIs and builds a source registry.

The pipeline provides targeted search queries; this module fans them out
across Semantic Scholar, OpenAlex, and Crossref in parallel, deduplicates,
and writes /sources/registry.json.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from src.tools.academic_search import search_semantic_scholar
from src.tools.crossref_search import search_crossref
from src.tools.openalex_search import search_openalex

logger = logging.getLogger(__name__)

_QUERY_CONCURRENCY = 3

# Year threshold — skip very old results
_MIN_YEAR = 2000

# Source types to exclude (dissertations, theses, etc.)
_EXCLUDED_TYPES = frozenset(
    {
        "dissertation",
        "thesis",
        "mastersthesis",
        "phdthesis",
        "posted-content",
    }
)

_GREEK_CHAR_RE = re.compile(r"[\u0370-\u03ff\u1f00-\u1fff]")


def _normalise_title(title: str) -> str:
    """Lowercase, strip punctuation/whitespace for dedup comparison."""
    return re.sub(r"[\W_]+", "", title.casefold(), flags=re.UNICODE)


def _make_source_id(authors: list[str], year: int | None) -> str:
    """Generate a source_id like 'smith2020' from the first author + year."""
    if authors:
        # Take last word of first author's name (surname heuristic)
        surname = authors[0].split()[-1] if authors[0] else "unknown"
        surname = re.sub(r"[^a-z]", "", surname.lower()) or "unknown"
    else:
        surname = "unknown"
    return f"{surname}{year or 'nd'}"


def _dedup_source_id(source_id: str, existing: set[str]) -> str:
    """Ensure uniqueness by appending a/b/c suffix if needed."""
    if source_id not in existing:
        return source_id
    for suffix in "abcdefghijklmnopqrstuvwxyz":
        candidate = f"{source_id}{suffix}"
        if candidate not in existing:
            return candidate
    return f"{source_id}_x"


def _search_one_query(
    query: str, max_per_api: int
) -> tuple[list[dict], dict[str, dict]]:
    """Run a single query against all three APIs and return (merged_results, raw_responses)."""
    results: list[dict] = []
    raw_responses: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(search_openalex, query, max_per_api): "openalex",
            pool.submit(search_crossref, query, max_per_api): "crossref",
            pool.submit(
                search_semantic_scholar, query, max_per_api
            ): "semantic_scholar",
        }
        for fut in as_completed(futures):
            api_name = futures[fut]
            try:
                hits, raw = fut.result()
                results.extend(hits)
                if raw:
                    raw_responses[api_name] = raw
            except Exception:
                logger.warning("Search API %s failed for query %r", api_name, query)

    return results, raw_responses


def _query_worker_count(query_count: int) -> int:
    """Return a bounded worker count for query-level concurrency."""
    if query_count <= 0:
        return 1
    return min(_QUERY_CONCURRENCY, query_count)


def _run_queries(queries: list[str], max_per_api: int) -> tuple[list[dict], list[dict]]:
    """Run multiple queries with bounded concurrency and stable merge order."""
    if not queries:
        return [], []

    collected: dict[int, tuple[str, list[dict], dict[str, dict]]] = {}
    with ThreadPoolExecutor(max_workers=_query_worker_count(len(queries))) as pool:
        futures = {
            pool.submit(_search_one_query, query, max_per_api): (index, query)
            for index, query in enumerate(queries)
        }
        for future in as_completed(futures):
            index, query = futures[future]
            try:
                results, raw_responses = future.result()
            except Exception:
                logger.warning("Query search failed for %r", query)
                results, raw_responses = [], {}
            collected[index] = (query, results, raw_responses)

    all_results: list[dict] = []
    all_raw: list[dict] = []
    for index, query in enumerate(queries):
        _, results, raw_responses = collected.get(index, (query, [], {}))
        all_results.extend(results)
        for api_name in sorted(raw_responses):
            all_raw.append(
                {
                    "query": query,
                    "api": api_name,
                    "response": raw_responses[api_name],
                }
            )

    return all_results, all_raw


def _accessibility_tier(hit: dict) -> int:
    """Score a result by accessibility: lower = better.

    0 = has OA PDF URL, 1 = has DOI URL, 2 = metadata-only URL.
    """
    if hit.get("pdf_url"):
        return 0
    if hit.get("doi"):
        return 1
    return 2


def _compute_max_per_api(
    max_sources: int,
    query_count: int,
    max_sources_per_direction: int,
) -> int:
    """Compute a bounded per-API result count."""
    raw = max(3, max_sources // max(query_count, 1))
    return min(raw, max_sources_per_direction)


def _language_rank(hit: dict, search_languages: list[str]) -> int:
    """Score a hit by language preference order. Lower is better."""
    text = " ".join(
        part for part in [hit.get("title", ""), hit.get("abstract", "")] if part
    )
    has_greek = bool(_GREEK_CHAR_RE.search(text))

    for index, language in enumerate(search_languages):
        if language == "el" and has_greek:
            return index
        if language == "en" and text and not has_greek:
            return index
    return len(search_languages)


def _build_registry(
    raw_results: list[dict],
    max_sources: int,
    *,
    prefer_greek_sources: bool = True,
    search_languages: list[str] | None = None,
) -> dict[str, dict]:
    """Deduplicate, filter, sort by accessibility, and build the registry."""
    seen_titles: set[str] = set()
    seen_dois: set[str] = set()
    candidates: list[dict] = []

    for hit in raw_results:
        title = hit.get("title", "") or ""
        if not title:
            continue

        # Year filter
        year = hit.get("year")
        if year is not None and year < _MIN_YEAR:
            continue

        # Dedup by DOI
        doi = hit.get("doi", "") or ""
        if doi:
            if doi in seen_dois:
                continue
            seen_dois.add(doi)

        # Dedup by normalised title
        norm = _normalise_title(title)
        if not norm or norm in seen_titles:
            continue
        seen_titles.add(norm)

        # Must have a URL for the reader to fetch
        url = hit.get("url", "") or ""
        pdf_url = hit.get("pdf_url", "") or ""
        if doi and not url:
            url = f"https://doi.org/{doi}"
        if not url and not pdf_url:
            continue

        # Filter out dissertations/theses
        source_type = (hit.get("source_type", "") or "").lower()
        normalised_type = source_type.replace("-", "").replace("_", "").replace(" ", "")
        if normalised_type in _EXCLUDED_TYPES:
            continue

        hit["_url"] = pdf_url or url
        hit["_pdf_url"] = pdf_url
        hit["_doi"] = doi
        hit["_source_type"] = source_type
        candidates.append(hit)

    language_order = list(search_languages or [])
    if prefer_greek_sources and "el" not in language_order:
        language_order.insert(0, "el")

    # Sort: accessibility first, then preferred language order.
    candidates.sort(
        key=lambda hit: (
            _accessibility_tier(hit),
            _language_rank(hit, language_order),
        )
    )

    used_ids: set[str] = set()
    registry: dict[str, dict] = {}

    for hit in candidates:
        authors = hit.get("authors", [])
        source_id = _make_source_id(authors, hit.get("year"))
        source_id = _dedup_source_id(source_id, used_ids)
        used_ids.add(source_id)

        registry[source_id] = {
            "authors": authors,
            "year": str(hit.get("year", "") or ""),
            "title": hit.get("title", ""),
            "abstract": hit.get("abstract", "") or "",
            "doi": hit["_doi"],
            "url": hit["_url"],
            "pdf_url": hit["_pdf_url"],
            "source_type": hit["_source_type"],
        }

        if len(registry) >= max_sources:
            break

    return registry


def run_research(
    queries: list[str],
    max_sources: int,
    sources_dir: str | None = None,
    *,
    max_sources_per_direction: int = 5,
    prefer_greek_sources: bool = True,
    search_languages: list[str] | None = None,
) -> dict[str, dict]:
    """Search academic databases and build a source registry.

    Fans out *queries* across Semantic Scholar, OpenAlex, and Crossref
    in parallel.  Deduplicates by DOI and title, writes the registry to
    *sources_dir*/registry.json, and returns it.
    """
    sources_path = Path(sources_dir) if sources_dir else None

    logger.info("run_research: %d queries, max_sources=%d", len(queries), max_sources)

    max_per_api = _compute_max_per_api(
        max_sources,
        len(queries),
        max_sources_per_direction,
    )

    all_results, all_raw = _run_queries(queries, max_per_api)

    registry = _build_registry(
        all_results,
        max_sources,
        prefer_greek_sources=prefer_greek_sources,
        search_languages=search_languages,
    )
    logger.info(
        "run_research: %d sources registered from %d raw results",
        len(registry),
        len(all_results),
    )

    if sources_path:
        sources_path.mkdir(parents=True, exist_ok=True)
        reg_path = sources_path / "registry.json"
        reg_path.write_text(
            json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("Registry written to %s", reg_path)

        raw_dir = sources_path / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        for i, entry in enumerate(all_raw):
            filename = f"{i:02d}_{entry['api']}.json"
            (raw_dir / filename).write_text(
                json.dumps(entry, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    return registry
