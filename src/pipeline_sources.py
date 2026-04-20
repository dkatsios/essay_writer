"""Source-processing steps for the deterministic essay pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

import httpx

from src.rendering import render_prompt
from src.schemas import (
    EssayPlan,
    RegistryEntry,
    SourceAssignmentPlan,
    SourceNote,
    SourceScoreBatch,
)
from src.tools.author_names import surname_from_author_string
from src.tools.research_sources import run_research
from src.tools.web_fetcher import fetch_url_content
from src.pipeline_support import (
    PipelineContext,
    _async_structured_call,
    _corpus_tokens,
    _load_selected_source_notes,
    _note_lexical_score,
    _parse_sections,
    _write_json,
)

logger = logging.getLogger(__name__)

_JUNK_ABSTRACT_PATTERNS = re.compile(
    r"\b(funding|acknowledgment|grant|supported by|no abstract)\b",
    re.IGNORECASE,
)
_MIN_ABSTRACT_WORDS = 20
_USER_SOURCE_PREFIX = "user_"
_USER_ID_HASH_LENGTH = 8
_SOURCE_READ_CONCURRENCY = 6
_MIN_RELEVANCE_SCORE = 2
_MIN_TOKEN_LENGTH = 4


class SourceShortfallAbort(RuntimeError):
    """Raised when the user declines to proceed after a source shortfall."""


def _body_word_count(content: str) -> int:
    return len(content.split())


def _has_substantive_body(content: str, min_words: int) -> bool:
    return _body_word_count(content.strip()) >= min_words


def _tokenize_for_overlap(text: str) -> set[str]:
    if not text:
        return set()
    return {
        w
        for w in re.findall(r"[\w'-]{3,}", text.lower(), flags=re.UNICODE)
        if len(w) >= _MIN_TOKEN_LENGTH
    }


def _optional_pdf_corpus_tokens(run_dir: Path) -> set[str]:
    tokens: set[str] = set()
    brief_path = run_dir / "brief" / "assignment.json"
    if brief_path.exists():
        try:
            brief = json.loads(brief_path.read_text(encoding="utf-8"))
            for key in ("topic", "title", "research_question", "course"):
                value = brief.get(key)
                if isinstance(value, str) and value:
                    tokens |= _tokenize_for_overlap(value)
        except Exception:
            pass
    plan_path = run_dir / "plan" / "plan.json"
    if plan_path.exists():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            for key in ("title", "thesis"):
                value = plan.get(key)
                if isinstance(value, str) and value:
                    tokens |= _tokenize_for_overlap(value)
            for section in plan.get("sections") or []:
                if isinstance(section, dict):
                    title = section.get("title")
                    if isinstance(title, str) and title:
                        tokens |= _tokenize_for_overlap(title)
            for query in plan.get("research_queries") or []:
                if isinstance(query, str) and query:
                    tokens |= _tokenize_for_overlap(query)
        except Exception:
            pass
    return tokens


def _lexical_relevance_score(corpus: set[str], title: str, abstract: str) -> int:
    doc_tokens = _tokenize_for_overlap(title) | _tokenize_for_overlap(abstract)
    return len(doc_tokens & corpus)


def _doi_href(doi: str) -> str | None:
    doi_value = (doi or "").strip()
    if not doi_value:
        return None
    doi_value = doi_value.removeprefix("https://doi.org/").removeprefix(
        "http://doi.org/"
    )
    return f"https://doi.org/{doi_value}"


def _article_href(doi: str, url: str, pdf_url: str) -> str | None:
    doi_url = _doi_href(doi)
    if doi_url:
        return doi_url

    article_url = (url or "").strip()
    direct_pdf_url = (pdf_url or "").strip()
    if article_url and article_url != direct_pdf_url:
        host = urlparse(article_url).netloc.lower()
        if host != "openalex.org":
            return article_url

    if not direct_pdf_url:
        return article_url or None

    parsed = urlparse(direct_pdf_url)
    host = parsed.netloc.lower()
    path = parsed.path
    if host.endswith("onlinelibrary.wiley.com"):
        for prefix in ("/doi/pdfdirect/", "/doi/pdf/", "/doi/epdf/"):
            if path.startswith(prefix):
                return (
                    f"{parsed.scheme}://{parsed.netloc}/doi/{path.removeprefix(prefix)}"
                )
    if host.endswith("tandfonline.com"):
        for prefix in ("/doi/pdf/", "/doi/epdf/"):
            if path.startswith(prefix):
                return f"{parsed.scheme}://{parsed.netloc}/doi/full/{path.removeprefix(prefix)}"
    if host.endswith("journals.sagepub.com") and path.startswith("/doi/pdf/"):
        return f"{parsed.scheme}://{parsed.netloc}/doi/{path.removeprefix('/doi/pdf/')}"

    return article_url or None


def _source_note_with_fulltext_flag(
    note: SourceNote, had_substantive_body: bool
) -> SourceNote:
    return note.model_copy(update={"fetched_fulltext": had_substantive_body})


def _is_useful_abstract(text: str) -> bool:
    words = text.split()
    if len(words) < _MIN_ABSTRACT_WORDS:
        return False
    if _JUNK_ABSTRACT_PATTERNS.search(text) and len(words) < 40:
        return False
    return True


class _DomainFailureTracker:
    """Track domains that return 429 and skip after threshold."""

    def __init__(self, max_failures: int = 2) -> None:
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()
        self._max = max_failures

    def record_failure(self, url: str) -> None:
        domain = urlparse(url).netloc
        with self._lock:
            self._counts[domain] = self._counts.get(domain, 0) + 1

    def should_skip(self, url: str) -> bool:
        domain = urlparse(url).netloc
        with self._lock:
            return self._counts.get(domain, 0) >= self._max


def _inject_user_sources(user_sources_dir: Path, run_dir: Path) -> None:
    from src.intake import scan
    import shutil

    files = scan(str(user_sources_dir))
    if not files:
        return

    registry_path = run_dir / "sources" / "registry.json"
    registry: dict[str, dict] = {}
    if registry_path.exists():
        registry = json.loads(registry_path.read_text(encoding="utf-8"))

    user_dir = run_dir / "sources" / "user"
    user_dir.mkdir(parents=True, exist_ok=True)

    added = 0
    for input_file in files:
        if input_file.warning:
            logger.warning("Skipping unsupported user source: %s", input_file.path.name)
            continue

        content = (input_file.text or "").strip()
        if not content:
            logger.warning(
                "Skipping user source with no extractable text (e.g. scanned PDF or image-only file): %s",
                input_file.path.name,
            )
            continue

        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[
            :_USER_ID_HASH_LENGTH
        ]
        source_id = f"{_USER_SOURCE_PREFIX}{content_hash}"
        if source_id in registry:
            logger.debug("User source already in registry: %s", source_id)
            continue
        shutil.copy2(
            str(input_file.path), str(user_dir / f"{source_id}_{input_file.path.name}")
        )

        content_path = user_dir / f"{source_id}.txt"
        content_path.write_text(content, encoding="utf-8")

        registry[source_id] = RegistryEntry(
            title=input_file.path.stem,
            source_type="user_provided",
            user_provided=True,
            content_path=str(content_path),
        ).model_dump()
        added += 1

    if added:
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(
            json.dumps(registry, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Injected %d user-provided sources into registry", added)


def _run_research_pass(
    ctx: PipelineContext,
    fetch_sources: int,
    *,
    fetch_per_api: int,
    prefer_fulltext: bool,
) -> None:
    plan_path = ctx.run_dir / "plan" / "plan.json"
    plan = EssayPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    run_research(
        queries=plan.research_queries,
        max_sources=fetch_sources,
        sources_dir=str(ctx.run_dir / "sources"),
        fetch_per_api=fetch_per_api,
        prefer_fulltext=prefer_fulltext,
    )
    if ctx.user_sources_dir and ctx.user_sources_dir.exists():
        _inject_user_sources(ctx.user_sources_dir, ctx.run_dir)


async def do_research(ctx: PipelineContext, fetch_sources: int) -> None:
    # _run_research_pass → run_research() uses its own ThreadPoolExecutor
    # for concurrent HTTP requests; asyncio.to_thread offloads the whole
    # blocking call so it doesn't stall the event loop.
    await asyncio.to_thread(
        _run_research_pass,
        ctx,
        fetch_sources,
        fetch_per_api=ctx.config.search.fetch_per_api,
        prefer_fulltext=False,
    )


async def _async_read_one_source(
    source_id: str,
    meta: dict,
    worker,
    sources_dir: str,
    tracker: object | None = None,
    essay_topic: str = "",
    *,
    min_body_words: int = 50,
    prefetched_content: str = "",
) -> SourceNote:
    is_user_provided = meta.get("user_provided", False)
    url = meta.get("pdf_url") or meta.get("url", "")
    content = prefetched_content
    had_body = _has_substantive_body(content, min_body_words)

    abstract = meta.get("abstract", "")
    if content or _is_useful_abstract(abstract):
        prompt = render_prompt(
            "source_reading.j2",
            source_id=source_id,
            title=meta.get("title", ""),
            authors=", ".join(meta.get("authors", [])),
            year=meta.get("year", ""),
            doi=meta.get("doi", ""),
            abstract=abstract,
            content=content,
            user_provided=is_user_provided,
            essay_topic=essay_topic,
        )
        try:
            note = await _async_structured_call(worker, prompt, SourceNote, tracker)
            note = _source_note_with_fulltext_flag(note, had_body)
            # API sources that passed scoring are always accessible
            if not is_user_provided:
                note = note.model_copy(update={"is_accessible": True})
            return note
        except Exception:
            logger.warning("LLM extraction failed for %s, using metadata", source_id)

    if _is_useful_abstract(abstract):
        return SourceNote(
            source_id=source_id,
            is_accessible=True,
            fetched_fulltext=False,
            title=meta.get("title", ""),
            authors=meta.get("authors", []),
            year=meta.get("year"),
            source_type=meta.get("source_type"),
            summary=abstract,
            url=url,
        )

    # Only reachable for user sources with no content and no abstract
    reason = (
        "Abstract too short or not useful"
        if abstract
        else "No content or abstract available"
    )
    logger.info("Skipping LLM read for %s: %s", source_id, reason)
    return SourceNote(
        source_id=source_id,
        is_accessible=False,
        fetched_fulltext=False,
        title=meta.get("title", ""),
        authors=meta.get("authors", []),
        year=meta.get("year"),
        inaccessible_reason=reason,
        url=url,
    )


async def _async_fetch_pdf_content(
    source_id: str,
    meta: dict,
    sources_dir: str,
    domain_tracker: _DomainFailureTracker | None = None,
    min_body_words: int = 50,
) -> tuple[str, str]:
    """Fetch PDF content for a single source (network I/O only, no LLM).

    For user-provided sources, loads content from ``content_path``.
    For API sources, fetches only when ``pdf_url`` exists; non-PDF URLs are
    skipped entirely.

    Returns ``(source_id, content_text)``.
    """
    import asyncio

    is_user_provided = meta.get("user_provided", False)

    # User-provided: load from content_path
    if is_user_provided:
        content_path = meta.get("content_path", "")
        if content_path and Path(content_path).exists():
            content = await asyncio.to_thread(
                Path(content_path).read_text, encoding="utf-8"
            )
            if len(content) > 50_000:
                content = content[:50_000] + "\n\n[... truncated ...]"
            return source_id, content
        return source_id, ""

    # API source: only fetch pdf_url
    pdf_url = meta.get("pdf_url", "")
    if not pdf_url:
        return source_id, ""

    if domain_tracker and domain_tracker.should_skip(pdf_url):
        logger.info("Skipping %s — domain throttled", pdf_url)
        return source_id, ""

    try:
        fetched = await asyncio.to_thread(fetch_url_content, pdf_url, sources_dir)
        if len(fetched) > 50_000:
            fetched = fetched[:50_000] + "\n\n[... truncated ...]"
        return source_id, fetched
    except httpx.HTTPStatusError as exc:
        logger.warning("Failed to fetch PDF %s: %s", pdf_url, exc)
        if exc.response.status_code == 429 and domain_tracker:
            domain_tracker.record_failure(pdf_url)
    except (httpx.RequestError, Exception) as exc:
        logger.warning("Failed to fetch PDF %s: %s", pdf_url, exc)
    return source_id, ""


def _filter_scorable_sources(
    registry: dict[str, dict],
) -> list[dict]:
    """Keep candidates that have a useful abstract.

    Returns a list of dicts ready for the batch-scoring template:
    ``{source_id, title, authors, year, doi, abstract}``.
    Candidates without a useful abstract are dropped regardless of PDF body.
    """
    scorable: list[dict] = []
    dropped = 0
    for source_id, meta in registry.items():
        abstract = meta.get("abstract", "") or ""
        if not _is_useful_abstract(abstract):
            dropped += 1
            continue

        authors = meta.get("authors", [])
        scorable.append(
            {
                "source_id": source_id,
                "title": meta.get("title", ""),
                "authors": ", ".join(authors)
                if isinstance(authors, list)
                else str(authors),
                "year": meta.get("year", ""),
                "doi": meta.get("doi", ""),
                "abstract": abstract,
            }
        )

    if dropped:
        logger.info("Dropped %d sources with no usable abstract", dropped)
    return scorable


async def _async_batch_score_sources(
    scorable: list[dict],
    essay_topic: str,
    thesis: str,
    async_worker,
    tracker: object | None = None,
    batch_size: int = 50,
    sections: list[dict] | None = None,
) -> dict[str, int]:
    """Score sources in batches via the LLM.

    Returns ``{source_id: relevance_score}`` for every source in *scorable*.
    On batch failure, retries once then falls back to individual scoring.
    """
    if not scorable:
        return {}

    batches = [
        scorable[i : i + batch_size] for i in range(0, len(scorable), batch_size)
    ]
    num_batches = len(batches)
    if tracker is not None:
        tracker.set_sub_total(num_batches)

    async def _score_batch(batch: list[dict]) -> dict[str, int]:
        """Score a single batch, returning {source_id: score}."""
        prompt = render_prompt(
            "source_scoring.j2",
            essay_topic=essay_topic,
            thesis=thesis,
            sources=batch,
            sections=sections or [],
        )
        result = await _async_structured_call(
            async_worker, prompt, SourceScoreBatch, tracker
        )
        return {item.source_id: item.relevance_score for item in result.scores}

    scores: dict[str, int] = {}
    for batch in batches:
        try:
            scores.update(await _score_batch(batch))
        except Exception:
            logger.warning(
                "Batch scoring failed for %d sources; retrying once…",
                len(batch),
            )
            try:
                scores.update(await _score_batch(batch))
            except Exception:
                if len(batch) <= 1:
                    logger.warning(
                        "Single-source scoring failed for %s; assigning score 0",
                        batch[0]["source_id"] if batch else "?",
                    )
                    for source in batch:
                        scores[source["source_id"]] = 0
                else:
                    logger.warning(
                        "Batch retry failed; falling back to individual scoring for %d sources",
                        len(batch),
                    )
                    for source in batch:
                        try:
                            scores.update(await _score_batch([source]))
                        except Exception:
                            logger.warning(
                                "Individual scoring failed for %s; assigning score 0",
                                source["source_id"],
                            )
                            scores[source["source_id"]] = 0
        finally:
            if tracker is not None:
                tracker.increment_sub_done()

    # Fill in any sources the LLM missed with score 0
    for source in scorable:
        scores.setdefault(source["source_id"], 0)

    return scores


def _select_top_sources(
    scores: dict[str, int],
    registry: dict[str, dict],
    target_sources: int,
    fetch_results: dict[str, str],
    min_body_words: int = 50,
) -> list[str]:
    """Rank scored sources and return the top *target_sources* IDs.

    Filters out sources with relevance_score < ``_MIN_RELEVANCE_SCORE``.
    """
    filtered_low = 0
    candidates: list[tuple[str, int]] = []
    for source_id, score in scores.items():
        if score < _MIN_RELEVANCE_SCORE:
            filtered_low += 1
            logger.info("Filtering source %s (relevance_score=%d)", source_id, score)
            continue
        candidates.append((source_id, score))

    if filtered_low:
        logger.info(
            "Filtered %d low-relevance sources (score < %d)",
            filtered_low,
            _MIN_RELEVANCE_SCORE,
        )

    def _sort_key(item: tuple[str, int]) -> tuple:
        source_id, score = item
        meta = registry.get(source_id, {})
        return (
            1 if meta.get("user_provided") else 0,
            score,
            1 if any(author.strip() for author in meta.get("authors", [])) else 0,
            int(meta.get("citation_count", 0) or 0),
            1
            if _has_substantive_body(fetch_results.get(source_id, ""), min_body_words)
            else 0,
        )

    candidates.sort(key=_sort_key, reverse=True)
    return [source_id for source_id, _ in candidates[:target_sources]]


def _build_optional_pdf_prompt_payload(
    results: list[tuple[str, SourceNote | None]],
    registry: dict[str, dict],
    task_sids: set[str],
    corpus: set[str],
    top_n: int,
) -> tuple[list[dict], list[str]]:
    eligible: list[tuple[str, dict, SourceNote, int, int]] = []
    for source_id, note in results:
        if source_id not in task_sids or note is None:
            continue
        meta = registry.get(source_id) or {}
        if meta.get("user_provided") or note.fetched_fulltext:
            continue
        title = (meta.get("title") or note.title or "").strip()
        abstract = (meta.get("abstract", "") or "").strip()
        citations = int(meta.get("citation_count", 0) or 0)
        lexical_score = _lexical_relevance_score(corpus, title, abstract)
        eligible.append((source_id, meta, note, lexical_score, citations))
    eligible.sort(key=lambda item: (-item[3], -item[4], item[0]))
    chosen = eligible[:top_n]

    items: list[dict] = []
    for source_id, meta, note, _, _ in chosen:
        raw_doi = (meta.get("doi", "") or note.doi or "").strip()
        raw_pdf_url = (
            meta.get("pdf_url", "") or meta.get("url", "") or note.url or ""
        ).strip()
        raw_article_url = _article_href(
            raw_doi,
            str(meta.get("url", "") or note.url or ""),
            raw_pdf_url,
        )
        items.append(
            {
                "source_id": source_id,
                "title": (meta.get("title") or note.title or source_id).strip(),
                "doi": raw_doi,
                "doi_url": _doi_href(raw_doi),
                "pdf_url": raw_pdf_url or None,
                "article_url": raw_article_url,
            }
        )
    return items, [item[0] for item in chosen]


def _log_optional_pdf_hint(run_dir: Path, items: list[dict]) -> None:
    logger.info(
        "%s\nSome ranked sources have no full text (abstract-only or fetch failed).\n"
        "Optional PDF uploads are available when the active entrypoint provides\n"
        "an upload callback. No callback is configured for this run.\n"
        "Run directory: %s",
        "=" * 50,
        run_dir,
    )
    for item in items:
        logger.info("  • %s", f"{item.get('title', item['source_id'])}"[:200])
        if item.get("doi_url"):
            logger.info("    %s", item["doi_url"])
        if item.get("article_url"):
            logger.info("    article=%s", item["article_url"])
        if item.get("pdf_url"):
            logger.info("    pdf=%s", item["pdf_url"])
        logger.info("    id=%s", item["source_id"])


# ---------------------------------------------------------------------------
# Extracted phases — each takes explicit inputs, returns explicit outputs.
# ---------------------------------------------------------------------------


async def _fetch_all_pdfs(
    pairs: list[tuple[str, dict]],
    sources_dir: str,
    *,
    tracker: object | None = None,
    min_body_words: int = 50,
) -> dict[str, str]:
    """Fetch PDF content for all candidates. Returns {source_id: content}."""
    import asyncio

    if tracker is not None:
        tracker.set_sub_total(len(pairs))
    domain_tracker = _DomainFailureTracker()
    semaphore = asyncio.Semaphore(_SOURCE_READ_CONCURRENCY)

    async def _fetch_one(source_id: str, meta: dict) -> tuple[str, str]:
        async with semaphore:
            try:
                return await _async_fetch_pdf_content(
                    source_id,
                    meta,
                    sources_dir,
                    domain_tracker=domain_tracker,
                    min_body_words=min_body_words,
                )
            except Exception:
                logger.exception("Failed to fetch PDF for %s", source_id)
                return source_id, ""
            finally:
                if tracker is not None:
                    tracker.increment_sub_done()

    results = await asyncio.gather(*(_fetch_one(sid, meta) for sid, meta in pairs))
    return dict(results)


async def _extract_all(
    pairs: list[tuple[str, dict]],
    fetch_results: dict[str, str],
    async_worker,
    sources_dir: str,
    notes_dir: Path,
    *,
    essay_topic: str = "",
    tracker: object | None = None,
    min_body_words: int = 50,
) -> list[tuple[str, SourceNote | None]]:
    """Run full LLM extraction on selected sources with pre-fetched content."""
    import asyncio

    if tracker is not None:
        tracker.set_sub_total(len(pairs))
    semaphore = asyncio.Semaphore(_SOURCE_READ_CONCURRENCY)

    async def _extract_one(source_id: str, meta: dict) -> tuple[str, SourceNote | None]:
        async with semaphore:
            try:
                note = await _async_read_one_source(
                    source_id,
                    meta,
                    async_worker,
                    sources_dir,
                    tracker=tracker,
                    essay_topic=essay_topic,
                    min_body_words=min_body_words,
                    prefetched_content=fetch_results.get(source_id, ""),
                )
                _write_json(notes_dir / f"{source_id}.json", note)
                return source_id, note
            except Exception:
                logger.exception("Failed to read source %s", source_id)
                return source_id, None
            finally:
                if tracker is not None:
                    tracker.increment_sub_done()

    return await asyncio.gather(*(_extract_one(sid, meta) for sid, meta in pairs))


def _backfill_registry(
    registry: dict[str, dict],
    results: list[tuple[str, SourceNote | None]],
    registry_path: Path,
) -> dict[str, dict]:
    """Update registry metadata for user-provided sources from LLM extraction."""
    registry_updated = False
    for source_id, note in results:
        if (
            not note
            or not registry.get(source_id, {}).get("user_provided")
            or not note.is_accessible
        ):
            continue
        entry = registry[source_id]
        if note.title:
            entry["title"] = note.title
        if note.authors:
            entry["authors"] = note.authors
            clean_authors = [
                author for author in note.authors if author and str(author).strip()
            ]
            if note.author_families and len(note.author_families) == len(clean_authors):
                entry["author_families"] = list(note.author_families)
            else:
                entry["author_families"] = [
                    surname_from_author_string(author) for author in clean_authors
                ]
        if note.year:
            entry["year"] = note.year
        if note.doi:
            entry["doi"] = note.doi
        registry_updated = True

    if registry_updated:
        registry_path.write_text(
            json.dumps(registry, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Backfilled registry metadata for user-provided sources")
    return registry


def _read_registry(registry_path: Path) -> dict[str, dict]:
    """Load the source registry from disk."""
    return json.loads(registry_path.read_text(encoding="utf-8"))


def make_read_sources(
    target_sources: int,
    fetch_sources: int,
) -> Callable:
    async def _do_read_sources(ctx: PipelineContext) -> None:
        registry_path = ctx.run_dir / "sources" / "registry.json"
        if not registry_path.exists():
            logger.warning("No registry.json found -- skipping source reading.")
            return

        await _async_read_sources_orchestration(
            ctx, registry_path, target_sources, fetch_sources
        )

    return _do_read_sources


async def _async_read_sources_orchestration(
    ctx: PipelineContext,
    registry_path: Path,
    target_sources: int,
    fetch_sources: int,
) -> None:
    """Core async orchestration for source reading.

    All async phases use ``await`` directly; the sync recovery research
    pass is offloaded via ``asyncio.to_thread``.
    """

    min_body = ctx.config.search.optional_pdf_min_body_words
    batch_size = ctx.config.search.batch_score_size
    top_n = ctx.config.search.optional_pdf_prompt_top_n
    sources_dir = str(ctx.run_dir / "sources")
    notes_dir = ctx.run_dir / "sources" / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)

    brief_path = ctx.run_dir / "brief" / "assignment.json"
    essay_topic = ""
    thesis = ""
    if brief_path.exists():
        try:
            brief = json.loads(brief_path.read_text(encoding="utf-8"))
            essay_topic = brief.get("topic", "")
        except Exception:
            pass
    plan_path = ctx.run_dir / "plan" / "plan.json"
    plan_sections: list[dict] = []
    if plan_path.exists():
        try:
            plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
            thesis = plan_data.get("thesis", "")
            plan_sections = [
                {"title": s.get("title", ""), "key_points": s.get("key_points", "")}
                for s in plan_data.get("sections", [])
            ]
        except Exception:
            pass

    async_worker = ctx.async_worker

    # -- Orchestration -------------------------------------------------

    recovery_done = False
    registry = _read_registry(registry_path)

    # Separate user sources from API sources
    user_pairs = [
        (sid, meta) for sid, meta in registry.items() if meta.get("user_provided")
    ]
    api_pairs = [
        (sid, meta) for sid, meta in registry.items() if not meta.get("user_provided")
    ]

    # Phase 2: filter API sources by abstract BEFORE fetching PDFs
    if ctx.tracker is not None:
        ctx.tracker.set_current_step("read_sources:score")
    scorable = _filter_scorable_sources({sid: meta for sid, meta in api_pairs})
    scorable_ids = {s["source_id"] for s in scorable}
    logger.info(
        "Filtered to %d scorable API sources (of %d total API)",
        len(scorable),
        len(api_pairs),
    )

    # Phase 1: fetch PDFs only for scorable API sources + user sources
    fetch_pairs = [
        (sid, meta) for sid, meta in api_pairs if sid in scorable_ids
    ] + user_pairs
    if not fetch_pairs and not scorable:
        logger.info("No sources to read (no scorable API and no user-provided).")
        return
    if ctx.tracker is not None:
        ctx.tracker.set_current_step("read_sources:fetch")
    logger.info("Fetching PDF content for %d source candidates...", len(fetch_pairs))
    fetch_results: dict[str, str] = await _fetch_all_pdfs(
        fetch_pairs, sources_dir, tracker=ctx.tracker, min_body_words=min_body
    )

    # Phase 3: batch-score scorable API sources
    if ctx.tracker is not None:
        ctx.tracker.set_current_step("read_sources:score")
    logger.info(
        "Batch-scoring %d scorable sources (batch_size=%d)...",
        len(scorable),
        batch_size,
    )
    scores: dict[str, int] = await _async_batch_score_sources(
        scorable,
        essay_topic,
        thesis,
        async_worker,
        tracker=ctx.tracker,
        batch_size=batch_size,
        sections=plan_sections,
    )

    # Phase 4: select top T from API sources
    above_threshold = sum(1 for s in scores.values() if s >= _MIN_RELEVANCE_SCORE)
    selected_ids = _select_top_sources(
        scores, registry, target_sources, fetch_results, min_body
    )
    logger.info(
        "Selected %d API sources from %d scored candidates (%d above threshold)",
        len(selected_ids),
        len(scorable),
        above_threshold,
    )

    # Recovery pass if below target (API sources only)
    if len(selected_ids) < target_sources:
        recovery_fetch_sources = max(
            fetch_sources + 1,
            int(fetch_sources * ctx.config.search.recovery_overfetch_multiplier),
        )
        recovery_fetch_per_api = max(
            ctx.config.search.fetch_per_api + 1,
            int(
                ctx.config.search.fetch_per_api
                * ctx.config.search.recovery_fetch_per_api_multiplier
            ),
        )
        logger.info(
            "Selected usable sources below target (%d/%d) — rerunning research with max_sources=%d fetch_per_api=%d prefer_fulltext=%s",
            len(selected_ids),
            target_sources,
            recovery_fetch_sources,
            recovery_fetch_per_api,
            ctx.config.search.recovery_prefer_fulltext,
        )
        await asyncio.to_thread(
            _run_research_pass,
            ctx,
            recovery_fetch_sources,
            fetch_per_api=recovery_fetch_per_api,
            prefer_fulltext=ctx.config.search.recovery_prefer_fulltext,
        )
        recovery_done = True
        registry = _read_registry(registry_path)

        # Build DOI/title sets from already-scored sources for dedup
        scored_dois: set[str] = set()
        scored_titles: set[str] = set()
        for sid in scores:
            meta = registry.get(sid, {})
            doi = (meta.get("doi") or "").strip().lower()
            if doi:
                scored_dois.add(doi)
            title = (meta.get("title") or "").strip()
            if title:
                norm = re.sub(r"[\W_]+", "", title.casefold(), flags=re.UNICODE)
                if norm:
                    scored_titles.add(norm)

        def _is_duplicate(meta: dict) -> bool:
            doi = (meta.get("doi") or "").strip().lower()
            if doi and doi in scored_dois:
                return True
            title = (meta.get("title") or "").strip()
            if title:
                norm = re.sub(r"[\W_]+", "", title.casefold(), flags=re.UNICODE)
                if norm and norm in scored_titles:
                    return True
            return False

        # Filter + fetch new API candidates only (skip ID and content dupes)
        new_api = {
            sid: meta
            for sid, meta in registry.items()
            if not meta.get("user_provided")
            and sid not in scores
            and not _is_duplicate(meta)
        }
        if new_api:
            new_scorable = _filter_scorable_sources(new_api)
            if new_scorable:
                new_scorable_ids = {s["source_id"] for s in new_scorable}
                new_fetch_pairs = [
                    (sid, meta)
                    for sid, meta in new_api.items()
                    if sid in new_scorable_ids and sid not in fetch_results
                ]
                if new_fetch_pairs:
                    if ctx.tracker is not None:
                        ctx.tracker.set_current_step("read_sources:fetch")
                    logger.info(
                        "Fetching PDF content for %d new candidates after recovery...",
                        len(new_fetch_pairs),
                    )
                    new_fetches = await _fetch_all_pdfs(
                        new_fetch_pairs,
                        sources_dir,
                        tracker=ctx.tracker,
                        min_body_words=min_body,
                    )
                    fetch_results.update(new_fetches)

                if ctx.tracker is not None:
                    ctx.tracker.set_current_step("read_sources:score")
                logger.info(
                    "Batch-scoring %d new sources after recovery...",
                    len(new_scorable),
                )
                new_scores = await _async_batch_score_sources(
                    new_scorable,
                    essay_topic,
                    thesis,
                    async_worker,
                    tracker=ctx.tracker,
                    batch_size=batch_size,
                    sections=plan_sections,
                )
                scores.update(new_scores)

        # Recompute above_threshold after merging recovery scores
        above_threshold = sum(1 for s in scores.values() if s >= _MIN_RELEVANCE_SCORE)
        selected_ids = _select_top_sources(
            scores, registry, target_sources, fetch_results, min_body
        )

    # Phase 5: full extraction on selected API sources
    if ctx.tracker is not None:
        ctx.tracker.set_current_step("read_sources:extract")
    selected_pairs = [(sid, registry[sid]) for sid in selected_ids if sid in registry]

    # Extract user sources (bypass scoring entirely)
    all_extract_pairs = user_pairs + selected_pairs
    if all_extract_pairs:
        logger.info(
            "Running full LLM extraction on %d sources (%d user, %d API)...",
            len(all_extract_pairs),
            len(user_pairs),
            len(selected_pairs),
        )
        results = await _extract_all(
            all_extract_pairs,
            fetch_results,
            async_worker,
            sources_dir,
            notes_dir,
            essay_topic=essay_topic,
            tracker=ctx.tracker,
            min_body_words=min_body,
        )
        registry = _backfill_registry(registry, results, registry_path)

        # Partition by origin using registry metadata (not positional slicing)
        user_accessible_ids = [
            sid
            for sid, note in results
            if note is not None
            and note.is_accessible
            and registry.get(sid, {}).get("user_provided")
        ]
        api_extracted_ids = [
            sid
            for sid, note in results
            if note is not None
            and note.is_accessible
            and not registry.get(sid, {}).get("user_provided")
        ]
        selected_ids = user_accessible_ids + api_extracted_ids

    # Optional PDF prompt for selected sources without fulltext
    task_ids = set(selected_ids)
    corpus = _optional_pdf_corpus_tokens(ctx.run_dir)
    results_for_optional = [
        (
            sid,
            SourceNote.model_validate_json(
                (notes_dir / f"{sid}.json").read_text(encoding="utf-8")
            ),
        )
        for sid in selected_ids
        if (notes_dir / f"{sid}.json").exists()
    ]
    items, prompt_ids = _build_optional_pdf_prompt_payload(
        results_for_optional, registry, task_ids, corpus, top_n
    )

    if items and top_n > 0:
        paths_before = {
            source_id: (registry.get(source_id) or {}).get("content_path")
            for source_id in prompt_ids
        }
        if ctx.on_optional_source_pdfs:
            await ctx.on_optional_source_pdfs(ctx.run_dir, items)
        else:
            _log_optional_pdf_hint(ctx.run_dir, items)

        registry = _read_registry(registry_path)
        reread_ids = [
            source_id
            for source_id in prompt_ids
            if (registry.get(source_id) or {}).get("content_path")
            != paths_before.get(source_id)
        ]
        if reread_ids:
            logger.info(
                "Re-reading %d source(s) after optional PDF upload…",
                len(reread_ids),
            )
            reread_pairs = [
                (source_id, registry[source_id]) for source_id in reread_ids
            ]
            # Re-fetch content for uploaded sources
            reread_fetch = await _fetch_all_pdfs(
                reread_pairs,
                sources_dir,
                tracker=ctx.tracker,
                min_body_words=min_body,
            )
            fetch_results.update(reread_fetch)
            reread_results = await _extract_all(
                reread_pairs,
                fetch_results,
                async_worker,
                sources_dir,
                notes_dir,
                essay_topic=essay_topic,
                tracker=ctx.tracker,
                min_body_words=min_body,
            )
            registry = _backfill_registry(registry, reread_results, registry_path)

            # Update selected_ids: add any newly accessible sources
            for sid, note in reread_results:
                if note is not None and note.is_accessible and sid not in selected_ids:
                    selected_ids.append(sid)

    # Build selected registry subset and save
    selected_registry = {sid: registry[sid] for sid in selected_ids if sid in registry}
    (ctx.run_dir / "sources" / "selected.json").write_text(
        json.dumps(selected_registry, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(
        "Selected %d usable sources (target %d)",
        len(selected_registry),
        target_sources,
    )

    if len(selected_registry) < target_sources:
        logger.warning(
            "Only %d usable selected sources after filtering (target %d).",
            len(selected_registry),
            target_sources,
        )
        if ctx.on_source_shortfall is not None:
            proceed = await ctx.on_source_shortfall(
                ctx.run_dir,
                {
                    "usable_sources": len(selected_registry),
                    "target_sources": target_sources,
                    "scorable_candidates": len(scorable),
                    "above_threshold": above_threshold,
                    "total_candidates": len(api_pairs) + len(user_pairs),
                    "recovery_attempted": recovery_done,
                },
            )
            if not proceed:
                raise SourceShortfallAbort(
                    "User declined to proceed after source shortfall"
                )


async def do_assign_sources(ctx: PipelineContext) -> None:
    source_notes = _load_selected_source_notes(ctx.run_dir)
    if not source_notes:
        logger.warning("No source notes available for assignment")
        return

    sections = _parse_sections(ctx.run_dir)
    min_per_section = max(2, len(source_notes) // (len(sections) or 1))
    prompt = render_prompt(
        "source_assignment.j2",
        sections=sections,
        source_notes=source_notes,
        min_per_section=min_per_section,
    )
    result = await _async_structured_call(
        ctx.async_worker, prompt, SourceAssignmentPlan, ctx.tracker
    )

    assigned_ids: set[str] = set()
    for assignment in result.assignments:
        assigned_ids.update(assignment.source_ids)

    notes_by_id = {note.source_id: note for note in source_notes}
    missing_ids = [
        note.source_id for note in source_notes if note.source_id not in assigned_ids
    ]
    if missing_ids and sections and result.assignments:
        section_corpora: list[tuple[object, set[str]]] = []
        assignment_by_position: dict[int, object] = {}
        for assignment in result.assignments:
            assignment_by_position.setdefault(assignment.section_position, assignment)
        for section in sections:
            matching = assignment_by_position.get(section.position)
            if not matching:
                continue
            section_corpora.append(
                (
                    matching,
                    _corpus_tokens(
                        f"{section.title} {section.key_points} {section.content_outline or ''}"
                    ),
                )
            )

        if not section_corpora:
            logger.warning("Could not align source assignments to planned sections")
        else:
            for source_id in missing_ids:
                note = notes_by_id[source_id]
                best_section, _ = max(
                    section_corpora,
                    key=lambda assignment: _note_lexical_score(
                        assignment[1],
                        note,
                    ),
                )
                best_section.source_ids.append(source_id)
            logger.info(
                "Patched %d unassigned sources into best-fit sections", len(missing_ids)
            )

    _write_json(ctx.run_dir / "plan" / "source_assignments.json", result)
