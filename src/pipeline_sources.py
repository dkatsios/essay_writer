"""Source-processing steps for the deterministic essay pipeline."""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

import httpx

from src.rendering import render_prompt
from src.schemas import EssayPlan, SourceAssignmentPlan, SourceNote
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
    _read_text,
    _structured_call,
    _write_json,
)

logger = logging.getLogger(__name__)

_JUNK_ABSTRACT_PATTERNS = re.compile(
    r"\b(funding|acknowledgment|grant|supported by|no abstract)\b",
    re.IGNORECASE,
)
_MIN_ABSTRACT_WORDS = 20
_USER_SOURCE_PREFIX = "user_"
_SOURCE_READ_CONCURRENCY = 6
_MIN_RELEVANCE_SCORE = 2
_OPTIONAL_PDF_STOPWORDS = frozenset(
    "the a an and or for to of in on at by with from as is are was were be been being "
    "this that these those it its they them their we our you your he she his her not no "
    "but if than then so such also only both all any each more most other some very can "
    "will may might must should could would about into through over after before under "
    "between out up down new first one two how what when where which who whom why into".split()
)


class SourceShortfallAbort(RuntimeError):
    """Raised when the user declines to proceed after a source shortfall."""


def _body_word_count(content: str) -> int:
    return len(content.split())


def _has_substantive_body(content: str, min_words: int) -> bool:
    return _body_word_count(content.strip()) >= min_words


def _tokenize_for_overlap(text: str) -> set[str]:
    if not text:
        return set()
    words = re.findall(r"[\w'-]{3,}", text.lower(), flags=re.UNICODE)
    return {word for word in words if word not in _OPTIONAL_PDF_STOPWORDS}


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


def _load_source_body_sync(
    meta: dict,
    sources_dir: str,
    domain_tracker: _DomainFailureTracker | None,
    min_body_words: int,
) -> str:
    is_user_provided = meta.get("user_provided", False)
    url = meta.get("pdf_url") or meta.get("url", "")
    content = ""
    content_path = meta.get("content_path", "")
    if content_path and Path(content_path).exists():
        content = Path(content_path).read_text(encoding="utf-8")
        if len(content) > 50_000:
            content = content[:50_000] + "\n\n[... truncated ...]"
    if (
        not _has_substantive_body(content, min_body_words)
        and url
        and not is_user_provided
    ):
        if domain_tracker and domain_tracker.should_skip(url):
            logger.info("Skipping %s — domain throttled", url)
        else:
            try:
                fetched = fetch_url_content(url, sources_dir=sources_dir)
                if len(fetched) > 50_000:
                    fetched = fetched[:50_000] + "\n\n[... truncated ...]"
                if _body_word_count(fetched) >= _body_word_count(content):
                    content = fetched
            except httpx.HTTPStatusError as exc:
                logger.warning("Failed to fetch %s: %s", url, exc)
                if exc.response.status_code == 429 and domain_tracker:
                    domain_tracker.record_failure(url)
            except (httpx.RequestError, Exception) as exc:
                logger.warning("Failed to fetch %s: %s", url, exc)
    return content


async def _load_source_body_async(
    meta: dict,
    sources_dir: str,
    domain_tracker: _DomainFailureTracker | None,
    min_body_words: int,
) -> str:
    import asyncio

    is_user_provided = meta.get("user_provided", False)
    url = meta.get("pdf_url") or meta.get("url", "")
    content = ""
    content_path = meta.get("content_path", "")
    if content_path and Path(content_path).exists():
        content = await asyncio.to_thread(
            Path(content_path).read_text, encoding="utf-8"
        )
        if len(content) > 50_000:
            content = content[:50_000] + "\n\n[... truncated ...]"
    if (
        not _has_substantive_body(content, min_body_words)
        and url
        and not is_user_provided
    ):
        if domain_tracker and domain_tracker.should_skip(url):
            logger.info("Skipping %s — domain throttled", url)
        else:
            try:
                fetched = await asyncio.to_thread(fetch_url_content, url, sources_dir)
                if len(fetched) > 50_000:
                    fetched = fetched[:50_000] + "\n\n[... truncated ...]"
                if _body_word_count(fetched) >= _body_word_count(content):
                    content = fetched
            except httpx.HTTPStatusError as exc:
                logger.warning("Failed to fetch %s: %s", url, exc)
                if exc.response.status_code == 429 and domain_tracker:
                    domain_tracker.record_failure(url)
            except (httpx.RequestError, Exception) as exc:
                logger.warning("Failed to fetch %s: %s", url, exc)
    return content


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

        source_id = f"{_USER_SOURCE_PREFIX}{added:03d}"
        shutil.copy2(
            str(input_file.path), str(user_dir / f"{source_id}_{input_file.path.name}")
        )

        content_path = user_dir / f"{source_id}.txt"
        content_path.write_text(content, encoding="utf-8")

        registry[source_id] = {
            "authors": [],
            "year": "",
            "title": input_file.path.stem,
            "abstract": "",
            "doi": "",
            "url": "",
            "pdf_url": "",
            "source_type": "user_provided",
            "user_provided": True,
            "content_path": str(content_path),
        }
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


def do_research(ctx: PipelineContext, fetch_sources: int) -> None:
    _run_research_pass(
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
    domain_tracker: _DomainFailureTracker | None = None,
    essay_topic: str = "",
    *,
    min_body_words: int = 50,
) -> SourceNote:
    is_user_provided = meta.get("user_provided", False)
    url = meta.get("pdf_url") or meta.get("url", "")
    content = await _load_source_body_async(
        meta, sources_dir, domain_tracker, min_body_words
    )
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
            return _source_note_with_fulltext_flag(note, had_body)
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


def _select_best_sources(
    run_dir: Path, registry: dict, target_sources: int
) -> dict[str, dict]:
    notes_dir = run_dir / "sources" / "notes"
    accessible: list[tuple[str, int, int]] = []
    filtered_low_relevance = 0

    for source_id in registry:
        note_path = notes_dir / f"{source_id}.json"
        if not note_path.exists():
            continue
        try:
            note = SourceNote.model_validate_json(note_path.read_text(encoding="utf-8"))
            if not note.is_accessible:
                continue
            elif note.relevance_score < _MIN_RELEVANCE_SCORE:
                filtered_low_relevance += 1
                logger.info(
                    "Filtering source %s (relevance_score=%d)",
                    source_id,
                    note.relevance_score,
                )
            else:
                accessible.append(
                    (source_id, note.relevance_score, note.content_word_count)
                )
        except Exception:
            continue

    if filtered_low_relevance:
        logger.info(
            "Filtered %d low-relevance sources (score < %d)",
            filtered_low_relevance,
            _MIN_RELEVANCE_SCORE,
        )

    accessible.sort(
        key=lambda item: (
            1 if registry.get(item[0], {}).get("user_provided") else 0,
            item[1],
            1
            if any(
                author.strip()
                for author in registry.get(item[0], {}).get("authors", [])
            )
            else 0,
            int(registry.get(item[0], {}).get("citation_count", 0) or 0),
            item[2],
        ),
        reverse=True,
    )
    selected_ids = [source_id for source_id, _, _ in accessible[:target_sources]]
    return {
        source_id: registry[source_id]
        for source_id in selected_ids
        if source_id in registry
    }


def _source_read_candidates(
    registry: dict[str, dict],
    target_sources: int,
) -> list[tuple[str, dict]]:
    _ = target_sources
    user_sources = [
        (sid, meta) for sid, meta in registry.items() if meta.get("user_provided")
    ]
    api_sources = [
        (sid, meta)
        for sid, meta in registry.items()
        if not meta.get("user_provided") and (meta.get("url") or meta.get("pdf_url"))
    ]
    return user_sources + api_sources


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
        items.append(
            {
                "source_id": source_id,
                "title": (meta.get("title") or note.title or source_id).strip(),
                "doi": raw_doi,
                "doi_url": _doi_href(raw_doi),
            }
        )
    return items, [item[0] for item in chosen]


def _cli_optional_pdf_hint(run_dir: Path, items: list[dict]) -> None:
    print(
        "\n"
        + "=" * 50
        + "\n  Some ranked sources have no full text (abstract-only or fetch failed).\n"
        + "  The web UI can prompt for optional PDFs; on CLI, use --sources with your files\n"
        + "  or add text under the run directory and re-run if you extend the tool.\n"
        + f"  Run directory: {run_dir}\n",
        file=sys.stderr,
    )
    for item in items:
        print(f"  • {item.get('title', item['source_id'])}"[:200], file=sys.stderr)
        if item.get("doi_url"):
            print(f"    {item['doi_url']}", file=sys.stderr)
        print(f"    id={item['source_id']}", file=sys.stderr)


def make_read_sources(
    target_sources: int,
    fetch_sources: int,
) -> Callable[[PipelineContext], None]:
    def _do_read_sources(ctx: PipelineContext) -> None:
        import asyncio

        registry_path = ctx.run_dir / "sources" / "registry.json"
        if not registry_path.exists():
            logger.warning("No registry.json found -- skipping source reading.")
            return

        min_body = ctx.config.search.optional_pdf_min_body_words
        top_n = ctx.config.search.optional_pdf_prompt_top_n
        sources_dir = str(ctx.run_dir / "sources")
        notes_dir = ctx.run_dir / "sources" / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)

        brief_path = ctx.run_dir / "brief" / "assignment.json"
        essay_topic = ""
        if brief_path.exists():
            try:
                brief = json.loads(brief_path.read_text(encoding="utf-8"))
                essay_topic = brief.get("topic", "")
            except Exception:
                pass

        async_worker = ctx.async_worker or ctx.worker.to_async()

        async def read_all(
            pairs: list[tuple[str, dict]],
        ) -> list[tuple[str, SourceNote | None]]:
            if ctx.tracker is not None:
                ctx.tracker.set_sub_total(len(pairs))
            domain_tracker = _DomainFailureTracker()
            semaphore = asyncio.Semaphore(_SOURCE_READ_CONCURRENCY)

            async def read_one(
                source_id: str, meta: dict
            ) -> tuple[str, SourceNote | None]:
                async with semaphore:
                    try:
                        note = await _async_read_one_source(
                            source_id,
                            meta,
                            async_worker,
                            sources_dir,
                            tracker=ctx.tracker,
                            domain_tracker=domain_tracker,
                            essay_topic=essay_topic,
                            min_body_words=min_body,
                        )
                        _write_json(notes_dir / f"{source_id}.json", note)
                        return source_id, note
                    except Exception:
                        logger.exception("Failed to read source %s", source_id)
                        return source_id, None
                    finally:
                        if ctx.tracker is not None:
                            ctx.tracker.increment_sub_done()

            return await asyncio.gather(
                *(read_one(source_id, meta) for source_id, meta in pairs)
            )

        def backfill_registry(
            registry: dict[str, dict],
            results: list[tuple[str, SourceNote | None]],
        ) -> dict[str, dict]:
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
                        author
                        for author in note.authors
                        if author and str(author).strip()
                    ]
                    if note.author_families and len(note.author_families) == len(
                        clean_authors
                    ):
                        entry["author_families"] = list(note.author_families)
                    else:
                        entry["author_families"] = [
                            surname_from_author_string(author)
                            for author in clean_authors
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

        def read_registry() -> dict[str, dict]:
            return json.loads(registry_path.read_text(encoding="utf-8"))

        def read_candidates(registry: dict[str, dict]) -> list[tuple[str, dict]]:
            tasks = _source_read_candidates(registry, target_sources)
            if not tasks:
                logger.info("No sources to read (no URLs and no user-provided files).")
            return tasks

        recovery_done = False
        registry = read_registry()
        tasks = read_candidates(registry)
        if not tasks:
            return

        logger.info("Reading %d ranked source candidates in parallel...", len(tasks))
        results = asyncio.run(read_all(tasks))
        registry = backfill_registry(registry, results)
        selected = _select_best_sources(ctx.run_dir, registry, target_sources)

        if len(selected) < target_sources:
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
                len(selected),
                target_sources,
                recovery_fetch_sources,
                recovery_fetch_per_api,
                ctx.config.search.recovery_prefer_fulltext,
            )
            _run_research_pass(
                ctx,
                recovery_fetch_sources,
                fetch_per_api=recovery_fetch_per_api,
                prefer_fulltext=ctx.config.search.recovery_prefer_fulltext,
            )
            recovery_done = True
            registry = read_registry()
            tasks = read_candidates(registry)
            if not tasks:
                return
            logger.info(
                "Reading %d ranked source candidates after recovery rerun...",
                len(tasks),
            )
            results = asyncio.run(read_all(tasks))
            registry = backfill_registry(registry, results)

        task_ids = {source_id for source_id, _ in tasks}
        corpus = _optional_pdf_corpus_tokens(ctx.run_dir)
        items, prompt_ids = _build_optional_pdf_prompt_payload(
            results, registry, task_ids, corpus, top_n
        )

        if items and top_n > 0:
            paths_before = {
                source_id: (registry.get(source_id) or {}).get("content_path")
                for source_id in prompt_ids
            }
            if ctx.on_optional_source_pdfs:
                ctx.on_optional_source_pdfs(ctx.run_dir, items)
            else:
                _cli_optional_pdf_hint(ctx.run_dir, items)

            registry = read_registry()
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
                reread_results = asyncio.run(read_all(reread_pairs))
                merged = dict(results)
                for source_id, note in reread_results:
                    merged[source_id] = note
                results = [(source_id, merged.get(source_id)) for source_id, _ in tasks]
                registry = backfill_registry(registry, reread_results)

        accessible_count = sum(
            1 for _, note in results if note is not None and note.is_accessible
        )
        inaccessible_count = len(tasks) - accessible_count

        selected = _select_best_sources(ctx.run_dir, registry, target_sources)
        (ctx.run_dir / "sources" / "selected.json").write_text(
            json.dumps(selected, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(
            "Selected %d usable sources from %d candidates (%d accessible, %d inaccessible)",
            len(selected),
            len(tasks),
            accessible_count,
            inaccessible_count,
        )

        if len(selected) < target_sources:
            print(
                f"  ⚠ Only {len(selected)} usable selected sources after filtering (target {target_sources}; {accessible_count} accessible candidates).",
                file=sys.stderr,
            )
            if ctx.on_source_shortfall is not None:
                proceed = ctx.on_source_shortfall(
                    ctx.run_dir,
                    {
                        "usable_sources": len(selected),
                        "target_sources": target_sources,
                        "accessible_candidates": accessible_count,
                        "total_candidates": len(tasks),
                        "recovery_attempted": recovery_done,
                    },
                )
                if not proceed:
                    raise SourceShortfallAbort(
                        "User declined to proceed after source shortfall"
                    )

    return _do_read_sources


def do_assign_sources(ctx: PipelineContext) -> None:
    plan_json = _read_text(ctx.run_dir / "plan" / "plan.json")
    source_notes = _load_selected_source_notes(ctx.run_dir)
    if not source_notes:
        logger.warning("No source notes available for assignment")
        return

    sections = _parse_sections(ctx.run_dir)
    min_per_section = max(2, len(source_notes) // (len(sections) or 1))
    prompt = render_prompt(
        "source_assignment.j2",
        plan_json=plan_json,
        source_notes=source_notes,
        min_per_section=min_per_section,
    )
    result = _structured_call(ctx.worker, prompt, SourceAssignmentPlan, ctx.tracker)

    assigned_ids: set[str] = set()
    for assignment in result.assignments:
        assigned_ids.update(assignment.source_ids)

    notes_by_id = {note.source_id: note for note in source_notes}
    missing_ids = [
        note.source_id for note in source_notes if note.source_id not in assigned_ids
    ]
    if missing_ids and sections and result.assignments:
        section_corpora: list[tuple[object, set[str]]] = []
        buckets: dict[int, list[object]] = {}
        for assignment in result.assignments:
            buckets.setdefault(assignment.section_number, []).append(assignment)
        for section in sections:
            matching = buckets.get(section.number)
            if not matching:
                continue
            section_corpora.append(
                (
                    matching.pop(0),
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
