"""Deterministic Python pipeline for essay writing.

Two-phase execution:
  Phase 1 (fixed):  intake -> validate -> plan
  Phase 2 (dynamic): steps built from plan analysis (short vs long path)

LLM calls use:
- Instructor ``client.chat.completions.create(response_model=Schema)`` for JSON steps
- OpenAI SDK ``client.chat.completions.create(messages=...)`` for text steps

The pipeline handles all file I/O; LLMs never touch disk.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from src.agent import (
    AsyncModelClient,
    ModelClient,
    _retry_with_backoff,
    extract_text,
    extract_usage,
)
from src.rendering import render_prompt
from src.tools.author_names import surname_from_author_string
from src.tools.essay_sanitize import strip_leading_submission_metadata
from src.schemas import (
    AssignmentBrief,
    EssayPlan,
    SourceAssignmentPlan,
    SourceNote,
    ValidationQuestion,
    ValidationResult,
)
from src.tools.research_sources import run_research
from src.tools.web_fetcher import fetch_url_content

if TYPE_CHECKING:
    from config.schemas import EssayWriterConfig

logger = logging.getLogger(__name__)

_MAX_PRIOR_SECTION_CONTEXT = 2
_REVIEW_SECTION_NEIGHBORS = 1

# Words that signal an abstract is not useful content
_JUNK_ABSTRACT_PATTERNS = re.compile(
    r"\b(funding|acknowledgment|grant|supported by|no abstract)\b", re.IGNORECASE
)
_MIN_ABSTRACT_WORDS = 20

_USER_SOURCE_PREFIX = "user_"

# Lexical overlap for optional PDF prompt ranking (English + common academic Greek-ish noise)
_OPTIONAL_PDF_STOPWORDS = frozenset(
    "the a an and or for to of in on at by with from as is are was were be been being "
    "this that these those it its they them their we our you your he she his her not no "
    "but if than then so such also only both all any each more most other some very can "
    "will may might must should could would about into through over after before under "
    "between out up down new first one two how what when where which who whom why into".split()
)


def _body_word_count(content: str) -> int:
    return len(content.split())


def _has_substantive_body(content: str, min_words: int) -> bool:
    return _body_word_count(content.strip()) >= min_words


def _tokenize_for_overlap(text: str) -> set[str]:
    if not text:
        return set()
    words = re.findall(r"[\w'-]{3,}", text.lower(), flags=re.UNICODE)
    return {w for w in words if w not in _OPTIONAL_PDF_STOPWORDS}


def _optional_pdf_corpus_tokens(run_dir: Path) -> set[str]:
    """Cheap topic keywords from brief + plan for ranking optional PDF prompts."""
    tokens: set[str] = set()
    brief_path = run_dir / "brief" / "assignment.json"
    if brief_path.exists():
        try:
            brief = json.loads(brief_path.read_text(encoding="utf-8"))
            for key in ("topic", "title", "research_question", "course"):
                val = brief.get(key)
                if isinstance(val, str) and val:
                    tokens |= _tokenize_for_overlap(val)
        except Exception:
            pass
    plan_path = run_dir / "plan" / "plan.json"
    if plan_path.exists():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            for key in ("title", "thesis"):
                val = plan.get(key)
                if isinstance(val, str) and val:
                    tokens |= _tokenize_for_overlap(val)
            for sec in plan.get("sections") or []:
                if isinstance(sec, dict):
                    t = sec.get("title")
                    if isinstance(t, str) and t:
                        tokens |= _tokenize_for_overlap(t)
            for q in plan.get("research_queries") or []:
                if isinstance(q, str) and q:
                    tokens |= _tokenize_for_overlap(q)
        except Exception:
            pass
    return tokens


def _lexical_relevance_score(corpus: set[str], title: str, abstract: str) -> int:
    doc_tokens = _tokenize_for_overlap(title) | _tokenize_for_overlap(abstract)
    return len(doc_tokens & corpus)


def _doi_href(doi: str) -> str | None:
    d = (doi or "").strip()
    if not d:
        return None
    d = d.removeprefix("https://doi.org/").removeprefix("http://doi.org/")
    return f"https://doi.org/{d}"


def _source_note_with_fulltext_flag(
    note: SourceNote, had_substantive_body: bool
) -> SourceNote:
    return note.model_copy(update={"fetched_fulltext": had_substantive_body})


def _load_source_body_sync(
    meta: dict,
    sources_dir: str,
    domain_tracker: _DomainFailureTracker | None,
    min_body_words: int,
) -> str:
    """Load best available body text: local content_path first, then URL for API sources."""
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


def _is_useful_abstract(text: str) -> bool:
    """Return True if the abstract has enough substance for LLM extraction."""
    words = text.split()
    if len(words) < _MIN_ABSTRACT_WORDS:
        return False
    # If majority of content is junk keywords, skip
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


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class PipelineContext:
    """Shared state passed to every step."""

    worker: ModelClient
    async_worker: AsyncModelClient | None
    writer: ModelClient
    reviewer: ModelClient
    run_dir: Path
    config: EssayWriterConfig
    extra_prompt: str | None = None
    tracker: object | None = None  # TokenTracker (optional)
    user_sources_dir: Path | None = None
    on_optional_source_pdfs: Callable[[Path, list[dict]], None] | None = None


@dataclass
class Section:
    """A single section with computed intro/conclusion flags."""

    number: int
    title: str
    heading: str
    word_target: int
    key_points: str = ""
    content_outline: str = ""
    is_intro: bool = False
    is_conclusion: bool = False


@dataclass
class PipelineStep:
    """A named step in the pipeline."""

    name: str
    fn: Callable[[PipelineContext], None]


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


def _execute(steps: list[PipelineStep], ctx: PipelineContext) -> None:
    """Run a list of pipeline steps with timing and tracking."""
    for step in steps:
        print(f"\n{'=' * 50}", file=sys.stderr)
        print(f"  Step: {step.name}", file=sys.stderr)
        if ctx.tracker is not None:
            ctx.tracker.set_current_step(step.name)
        t0 = monotonic()
        try:
            step.fn(ctx)
            dur = monotonic() - t0
            print(f"  OK {step.name} ({dur:.1f}s)", file=sys.stderr)
        except Exception:
            dur = monotonic() - t0
            print(f"  FAIL {step.name} ({dur:.1f}s)", file=sys.stderr)
            if ctx.tracker is not None:
                ctx.tracker.record_duration(step.name, dur)
            raise
        if ctx.tracker is not None:
            ctx.tracker.record_duration(step.name, dur)


# ---------------------------------------------------------------------------
# LLM invocation helpers
# ---------------------------------------------------------------------------

_STRUCTURED_RETRIES = 2


def _record_usage(tracker: object | None, response) -> None:
    """Record token usage from any provider's API response on the tracker."""
    if tracker is None or response is None:
        return
    u = extract_usage(response)
    if u["input"] or u["output"] or u["thinking"]:
        tracker.record(u["model"], u["input"], u["output"], u["thinking"])


def _structured_call(
    client: ModelClient,
    prompt: str,
    schema: type[BaseModel],
    tracker: object | None = None,
    retries: int = _STRUCTURED_RETRIES,
) -> BaseModel:
    """Call a model with structured output (Instructor handles validation retries)."""

    def _do_call():
        result = client.client.chat.completions.create(
            model=client.model,
            response_model=schema,
            max_retries=retries,
            messages=[{"role": "user", "content": prompt}],
        )
        return result

    result = _retry_with_backoff(_do_call)
    # Instructor returns the Pydantic model directly; usage is on _raw_response
    raw = getattr(result, "_raw_response", None)
    if raw:
        _record_usage(tracker, raw)
    return result


async def _async_structured_call(
    client: AsyncModelClient,
    prompt: str,
    schema: type[BaseModel],
    tracker: object | None = None,
    retries: int = _STRUCTURED_RETRIES,
) -> BaseModel:
    """Async version of _structured_call."""

    async def _do_call():
        result = await client.client.chat.completions.create(
            model=client.model,
            response_model=schema,
            max_retries=retries,
            messages=[{"role": "user", "content": prompt}],
        )
        return result

    result = await _retry_with_backoff(_do_call, is_async=True)
    raw = getattr(result, "_raw_response", None)
    if raw:
        _record_usage(tracker, raw)
    return result


def _text_call(
    client: ModelClient,
    system_prompt: str,
    user_prompt: str,
    tracker: object | None = None,
) -> str:
    """Call a model for free-form text output (essays, reviews)."""

    def _do_call():
        return client.client.chat.completions.create(
            model=client.model,
            response_model=None,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )

    response = _retry_with_backoff(_do_call)
    _record_usage(tracker, response)
    return extract_text(response)


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------


def _write_json(path: Path, data: BaseModel) -> None:
    """Write a Pydantic model as JSON to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        data.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _write_text(path: Path, text: str) -> None:
    """Write text to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read_text(path: Path) -> str:
    """Read text from disk."""
    return path.read_text(encoding="utf-8")


def _get_brief_language(run_dir: Path) -> str:
    """Read the essay language from the assignment brief."""
    brief_path = run_dir / "brief" / "assignment.json"
    if brief_path.exists():
        brief = AssignmentBrief.model_validate_json(
            brief_path.read_text(encoding="utf-8")
        )
        return brief.language
    return "Greek (Δημοτική)"


# ---------------------------------------------------------------------------
# Plan parsing
# ---------------------------------------------------------------------------


def _get_target_words(run_dir: Path) -> int:
    """Read total word target from plan.json."""
    plan_path = run_dir / "plan" / "plan.json"
    if not plan_path.exists():
        return 0
    plan = EssayPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    return plan.total_word_target


def _parse_sections(run_dir: Path) -> list[Section]:
    """Load sections from plan.json."""
    plan_path = run_dir / "plan" / "plan.json"
    if not plan_path.exists():
        return []

    plan = EssayPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    sections: list[Section] = []

    for ps in plan.sections:
        is_intro = (
            ps.number == 1
            or "introduction" in ps.title.lower()
            or "\u03b5\u03b9\u03c3\u03b1\u03b3\u03c9\u03b3" in ps.title.lower()
        )
        is_conclusion = (
            "conclusion" in ps.title.lower()
            or "\u03c3\u03c5\u03bc\u03c0\u03ad\u03c1\u03b1\u03c3\u03bc"
            in ps.title.lower()
        )
        sections.append(
            Section(
                number=ps.number,
                title=ps.title,
                heading=ps.heading,
                word_target=ps.word_target,
                key_points=ps.key_points,
                content_outline=ps.content_outline,
                is_intro=is_intro,
                is_conclusion=is_conclusion,
            )
        )

    return sections


def _suggested_sources(target_words: int, sources_per_1k: int = 5) -> int:
    """Compute a suggested source count using log-based scaling.

    Academic conventions show diminishing marginal sources as word count
    grows.  The formula uses log2 scaling to produce realistic targets:

        2k -> ~24,  5k -> ~39,  10k -> ~52,  20k -> ~66,  30k -> ~74
    """
    if target_words <= 0:
        return 0
    return round(sources_per_1k * 3 * math.log2(1 + target_words / 1000))


def _compute_max_sources(
    target_words: int,
    config: EssayWriterConfig,
    user_min_sources: int | None = None,
) -> tuple[int, int]:
    """Compute (target_sources, fetch_sources) based on word count and config.

    Uses log-based scaling for realistic source targets. If the brief or
    user supplied ``min_sources``, that value wins over the heuristic.

    With no explicit minimum, ``target_sources`` uses log-based scaling,
    floored by ``search.min_sources``. ``fetch_sources`` uses the overfetch
    multiplier so the registry has headroom before selection.
    """
    sc = config.search
    raw = _suggested_sources(target_words, sc.sources_per_1k_words)
    cfg_floor = sc.min_sources
    if user_min_sources is not None:
        target = max(cfg_floor, user_min_sources)
    else:
        target = max(cfg_floor, raw)
    fetch = max(target, int(target * sc.overfetch_multiplier))
    return target, fetch


def _corpus_tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower(), flags=re.UNICODE))


def _note_lexical_score(corpus_tokens: set[str], note: SourceNote) -> int:
    blob = f"{note.title} {note.summary}"[:8000]
    return len(corpus_tokens & _corpus_tokens(blob))


def _rank_notes_by_corpus(corpus: str, notes: list[SourceNote]) -> list[SourceNote]:
    ct = _corpus_tokens(corpus)
    return sorted(
        notes,
        key=lambda n: _note_lexical_score(ct, n),
        reverse=True,
    )


def _source_catalog_markdown(notes: list[SourceNote]) -> str:
    lines: list[str] = []
    for n in sorted(notes, key=lambda x: x.source_id):
        au = ", ".join(a.strip() for a in n.authors if a.strip()) or "n.a."
        lines.append(f"- `{n.source_id}` — {au} ({n.year or 'n.d.'}). {n.title}")
    return "\n".join(lines)


def _plan_corpus_from_json(plan_json: str) -> str:
    try:
        data = json.loads(plan_json)
    except json.JSONDecodeError:
        return ""
    parts: list[str] = [data.get("thesis") or "", data.get("title") or ""]
    for sec in data.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        parts.extend(
            [
                str(sec.get("title") or ""),
                str(sec.get("key_points") or ""),
                str(sec.get("content_outline") or ""),
            ]
        )
    return " ".join(parts)


def _split_writer_source_context(
    corpus: str,
    all_notes: list[SourceNote],
    full_detail_budget: int,
) -> tuple[list[SourceNote], str, int]:
    """Return (detail_notes, catalog_markdown, total_count) for writer prompts."""
    if not all_notes:
        return [], "", 0
    ranked = _rank_notes_by_corpus(corpus, all_notes)
    budget = max(1, full_detail_budget)
    detail = ranked[:budget]
    return detail, _source_catalog_markdown(all_notes), len(all_notes)


def _load_source_notes(run_dir: Path) -> list[SourceNote]:
    """Load all accessible source notes from disk."""
    notes_dir = run_dir / "sources" / "notes"
    if not notes_dir.exists():
        return []
    notes = []
    for f in sorted(notes_dir.iterdir()):
        if f.suffix == ".json":
            try:
                note = SourceNote.model_validate_json(f.read_text(encoding="utf-8"))
                if note.is_accessible:
                    notes.append(note)
            except Exception:
                logger.warning("Failed to load source note: %s", f.name)
    return notes


def _load_selected_source_notes(run_dir: Path) -> list[SourceNote]:
    """Load selected accessible notes, falling back to all accessible notes."""
    all_notes = _load_source_notes(run_dir)
    if not all_notes:
        return []

    selected_path = run_dir / "sources" / "selected.json"
    if not selected_path.exists():
        return all_notes

    try:
        selected = json.loads(selected_path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Failed to load selected sources; using all accessible notes")
        return all_notes

    if not isinstance(selected, dict) or not selected:
        return all_notes

    selected_ids = set(selected)
    selected_notes = [note for note in all_notes if note.source_id in selected_ids]
    if selected_notes:
        return selected_notes

    logger.warning(
        "Selected sources had no accessible notes; using all accessible notes"
    )
    return all_notes


def _build_prior_sections_context(
    written_sections: list[tuple[Section, str]],
    max_sections: int = _MAX_PRIOR_SECTION_CONTEXT,
) -> str:
    """Build bounded prior-section context for section writing."""
    if not written_sections:
        return ""

    recent_sections = sorted(
        written_sections[-max_sections:], key=lambda item: item[0].number
    )
    return "\n\n---\n\n".join(text for _, text in recent_sections if text)


def _section_window(
    sections: list[Section],
    target_number: int,
    neighbor_count: int = _REVIEW_SECTION_NEIGHBORS,
) -> list[Section]:
    """Return the target section plus a bounded number of neighbors."""
    for index, section in enumerate(sections):
        if section.number == target_number:
            start = max(0, index - neighbor_count)
            end = min(len(sections), index + neighbor_count + 1)
            return sections[start:end]
    return []


def _build_review_context(
    section: Section,
    sections: list[Section],
    section_texts: dict[int, str],
    neighbor_count: int = _REVIEW_SECTION_NEIGHBORS,
) -> str:
    """Build bounded review context around the target section."""
    parts: list[str] = []
    for current in _section_window(sections, section.number, neighbor_count):
        text = section_texts.get(current.number, "")
        if not text:
            continue
        if current.number == section.number:
            text = (
                "<!-- >>> SECTION TO REVIEW: START >>> -->\n"
                f"{text}\n"
                "<!-- <<< SECTION TO REVIEW: END <<< -->"
            )
        parts.append(text)
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------


def _do_intake(ctx: PipelineContext) -> None:
    extracted_path = ctx.run_dir / "input" / "extracted.md"
    extracted_text = _read_text(extracted_path) if extracted_path.exists() else ""

    prompt = render_prompt(
        "intake.j2",
        extracted_text=extracted_text,
        extra_prompt=ctx.extra_prompt,
    )

    brief = _structured_call(ctx.worker, prompt, AssignmentBrief, ctx.tracker)
    _write_json(ctx.run_dir / "brief" / "assignment.json", brief)


def _do_validate(ctx: PipelineContext) -> None:
    brief_json = _read_text(ctx.run_dir / "brief" / "assignment.json")
    language = _get_brief_language(ctx.run_dir)
    prompt = render_prompt("validate.j2", brief_json=brief_json, language=language)

    result = _structured_call(ctx.worker, prompt, ValidationResult, ctx.tracker)
    _write_json(ctx.run_dir / "brief" / "validation.json", result)


def _read_validation(run_dir: Path) -> ValidationResult | None:
    """Read validation.json and return the structured validation result."""
    path = run_dir / "brief" / "validation.json"
    if not path.exists():
        return None
    return ValidationResult.model_validate_json(path.read_text(encoding="utf-8"))


def _format_validation_questions(result: ValidationResult) -> str | None:
    """Format validation questions for interactive CLI display."""
    if result.is_pass or not result.questions:
        return None
    lines: list[str] = []
    for i, q in enumerate(result.questions, 1):
        lines.append(f"{i}. {q.question}")
        n = len(q.options)
        sugg = q.suggested_option_index if n else 0
        if n:
            sugg = max(0, min(sugg, n - 1))
        for j, opt in enumerate(q.options):
            label = chr(ord("a") + j)
            hint = "  ← suggested default" if j == sugg else ""
            lines.append(f"   {label}) {opt}{hint}")
        lines.append("")
    return "\n".join(lines).strip()


def _do_plan(ctx: PipelineContext) -> None:
    brief_json = _read_text(ctx.run_dir / "brief" / "assignment.json")
    language = _get_brief_language(ctx.run_dir)
    prompt = render_prompt("plan.j2", brief_json=brief_json, language=language)

    plan = _structured_call(ctx.worker, prompt, EssayPlan, ctx.tracker)

    _write_json(ctx.run_dir / "plan" / "plan.json", plan)


def _inject_user_sources(user_sources_dir: Path, run_dir: Path) -> None:
    """Add user-provided source files to the registry with placeholder metadata."""
    from src.intake import scan

    files = scan(str(user_sources_dir))
    if not files:
        return

    registry_path = run_dir / "sources" / "registry.json"
    registry: dict[str, dict] = {}
    if registry_path.exists():
        registry = json.loads(registry_path.read_text(encoding="utf-8"))

    user_dir = run_dir / "sources" / "user"
    user_dir.mkdir(parents=True, exist_ok=True)

    import shutil

    added = 0
    for f in files:
        if f.warning:
            logger.warning("Skipping unsupported user source: %s", f.path.name)
            continue

        content = (f.text or "").strip()
        if not content:
            logger.warning(
                "Skipping user source with no extractable text (e.g. scanned PDF or image-only file): %s",
                f.path.name,
            )
            continue

        source_id = f"{_USER_SOURCE_PREFIX}{added:03d}"

        # Copy file into run directory for reproducibility (unique name per id
        # so duplicate basenames from different paths do not collide).
        dest = user_dir / f"{source_id}_{f.path.name}"
        shutil.copy2(str(f.path), str(dest))

        content_path = user_dir / f"{source_id}.txt"
        content_path.write_text(content, encoding="utf-8")

        registry[source_id] = {
            "authors": [],
            "year": "",
            "title": f.path.stem,
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
            json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("Injected %d user-provided sources into registry", added)


def _do_research(ctx: PipelineContext, fetch_sources: int) -> None:
    """Run research — pure Python, no LLM."""
    plan_path = ctx.run_dir / "plan" / "plan.json"
    plan = EssayPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))

    run_research(
        queries=plan.research_queries,
        max_sources=fetch_sources,
        sources_dir=str(ctx.run_dir / "sources"),
        fetch_per_api=ctx.config.search.fetch_per_api,
    )

    # Inject user-provided sources into the registry
    if ctx.user_sources_dir and ctx.user_sources_dir.exists():
        _inject_user_sources(ctx.user_sources_dir, ctx.run_dir)


def _read_one_source(
    source_id: str,
    meta: dict,
    worker: ModelClient,
    sources_dir: str,
    tracker: object | None = None,
    domain_tracker: _DomainFailureTracker | None = None,
    essay_topic: str = "",
    *,
    min_body_words: int = 50,
) -> SourceNote:
    """Fetch and extract notes for a single source."""
    is_user_provided = meta.get("user_provided", False)
    url = meta.get("pdf_url") or meta.get("url", "")
    content = _load_source_body_sync(meta, sources_dir, domain_tracker, min_body_words)
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
            note = _structured_call(worker, prompt, SourceNote, tracker)
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


async def _async_read_one_source(
    source_id: str,
    meta: dict,
    worker: AsyncModelClient,
    sources_dir: str,
    tracker: object | None = None,
    domain_tracker: _DomainFailureTracker | None = None,
    essay_topic: str = "",
    *,
    min_body_words: int = 50,
) -> SourceNote:
    """Async: fetch content in a thread, then extract notes via ainvoke."""
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


_MIN_RELEVANCE_SCORE = 2
"""Sources scored below this by the reader LLM are filtered out before selection."""


def _select_best_sources(
    run_dir: Path, registry: dict, target_sources: int
) -> dict[str, dict]:
    """Select the best target_sources from read notes."""
    notes_dir = run_dir / "sources" / "notes"
    accessible: list[tuple[str, int, int]] = []
    inaccessible: list[str] = []
    filtered_low_relevance = 0

    for sid in registry:
        note_path = notes_dir / f"{sid}.json"
        if not note_path.exists():
            inaccessible.append(sid)
            continue
        try:
            note = SourceNote.model_validate_json(note_path.read_text(encoding="utf-8"))
            if not note.is_accessible:
                inaccessible.append(sid)
            elif note.relevance_score < _MIN_RELEVANCE_SCORE:
                filtered_low_relevance += 1
                logger.info(
                    "Filtering source %s (relevance_score=%d)",
                    sid,
                    note.relevance_score,
                )
            else:
                accessible.append((sid, note.relevance_score, note.content_word_count))
        except Exception:
            inaccessible.append(sid)

    if filtered_low_relevance:
        logger.info(
            "Filtered %d low-relevance sources (score < %d)",
            filtered_low_relevance,
            _MIN_RELEVANCE_SCORE,
        )

    # Sort: user-provided first, then relevance score, valid authors,
    # citation count, and content word count as tiebreaker
    accessible.sort(
        key=lambda x: (
            1 if registry.get(x[0], {}).get("user_provided") else 0,
            x[1],  # relevance_score
            1
            if any(a.strip() for a in registry.get(x[0], {}).get("authors", []))
            else 0,
            int(registry.get(x[0], {}).get("citation_count", 0) or 0),
            x[2],  # content_word_count
        ),
        reverse=True,
    )
    selected_ids = [sid for sid, _, _ in accessible[:target_sources]]

    remaining = target_sources - len(selected_ids)
    if remaining > 0:
        selected_ids.extend(inaccessible[:remaining])

    return {sid: registry[sid] for sid in selected_ids if sid in registry}


def _source_read_candidates(
    registry: dict[str, dict],
    target_sources: int,
) -> list[tuple[str, dict]]:
    """Return every registry row worth reading.

    User-provided sources are first. All API-backed registry entries are
    included so selection can consider the full research pool (large essays
    may target dozens of sources). *target_sources* is unused but kept for
    call-site stability.
    """
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


_SOURCE_READ_CONCURRENCY = 6


def _build_optional_pdf_prompt_payload(
    results: list[tuple[str, SourceNote | None]],
    registry: dict[str, dict],
    task_sids: set[str],
    corpus: set[str],
    top_n: int,
) -> tuple[list[dict], list[str]]:
    """Rank API sources without substantive fetched body; return UI payload and ids."""
    eligible: list[tuple[str, dict, SourceNote, int, int]] = []
    for sid, note in results:
        if sid not in task_sids or note is None:
            continue
        meta = registry.get(sid) or {}
        if meta.get("user_provided"):
            continue
        if note.fetched_fulltext:
            continue
        title = (meta.get("title") or note.title or "").strip()
        abstract = (meta.get("abstract", "") or "").strip()
        cit = int(meta.get("citation_count", 0) or 0)
        lex = _lexical_relevance_score(corpus, title, abstract)
        eligible.append((sid, meta, note, lex, cit))
    eligible.sort(key=lambda x: (-x[3], -x[4], x[0]))
    chosen = eligible[:top_n]
    items: list[dict] = []
    for sid, meta, note, _, _ in chosen:
        raw_doi = (meta.get("doi", "") or note.doi or "").strip()
        items.append(
            {
                "source_id": sid,
                "title": (meta.get("title") or note.title or sid).strip(),
                "doi": raw_doi,
                "doi_url": _doi_href(raw_doi),
            }
        )
    prompt_sids = [t[0] for t in chosen]
    return items, prompt_sids


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
    for row in items:
        line = f"  • {row.get('title', row['source_id'])}"
        print(line[:200], file=sys.stderr)
        if row.get("doi_url"):
            print(f"    {row['doi_url']}", file=sys.stderr)
        print(f"    id={row['source_id']}", file=sys.stderr)


def _make_read_sources(
    target_sources: int,
) -> Callable[[PipelineContext], None]:
    def _do_read_sources(c: PipelineContext) -> None:
        import asyncio

        registry_path = c.run_dir / "sources" / "registry.json"
        if not registry_path.exists():
            logger.warning("No registry.json found -- skipping source reading.")
            return

        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        tasks = _source_read_candidates(registry, target_sources)
        if not tasks:
            logger.info("No sources to read (no URLs and no user-provided files).")
            return

        min_body = c.config.search.optional_pdf_min_body_words
        top_n = c.config.search.optional_pdf_prompt_top_n

        logger.info("Reading %d ranked source candidates in parallel...", len(tasks))
        sources_dir = str(c.run_dir / "sources")
        notes_dir = c.run_dir / "sources" / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)

        brief_path = c.run_dir / "brief" / "assignment.json"
        essay_topic = ""
        if brief_path.exists():
            try:
                brief = json.loads(brief_path.read_text(encoding="utf-8"))
                essay_topic = brief.get("topic", "")
            except Exception:
                pass

        domain_tracker = _DomainFailureTracker()
        semaphore = asyncio.Semaphore(_SOURCE_READ_CONCURRENCY)
        async_worker = c.async_worker or c.worker.to_async()

        async def read_one(
            sid: str,
            meta: dict,
            dt: _DomainFailureTracker,
        ) -> tuple[str, SourceNote | None]:
            async with semaphore:
                try:
                    note = await _async_read_one_source(
                        sid,
                        meta,
                        async_worker,
                        sources_dir,
                        tracker=c.tracker,
                        domain_tracker=dt,
                        essay_topic=essay_topic,
                        min_body_words=min_body,
                    )
                    _write_json(notes_dir / f"{sid}.json", note)
                    return sid, note
                except Exception:
                    logger.exception("Failed to read source %s", sid)
                    return sid, None

        async def read_all(
            pairs: list[tuple[str, dict]], dt: _DomainFailureTracker
        ) -> list[tuple[str, SourceNote | None]]:
            return await asyncio.gather(
                *(read_one(sid, meta, dt) for sid, meta in pairs)
            )

        results = asyncio.run(read_all(tasks, domain_tracker))

        registry_updated = False
        for sid, note in results:
            if (
                note
                and registry.get(sid, {}).get("user_provided")
                and note.is_accessible
            ):
                entry = registry[sid]
                if note.title:
                    entry["title"] = note.title
                if note.authors:
                    entry["authors"] = note.authors
                    clean_authors = [a for a in note.authors if a and str(a).strip()]
                    if note.author_families and len(note.author_families) == len(
                        clean_authors
                    ):
                        entry["author_families"] = list(note.author_families)
                    else:
                        entry["author_families"] = [
                            surname_from_author_string(a) for a in clean_authors
                        ]
                if note.year:
                    entry["year"] = note.year
                if note.doi:
                    entry["doi"] = note.doi
                registry_updated = True

        if registry_updated:
            registry_path.write_text(
                json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logger.info("Backfilled registry metadata for user-provided sources")

        task_sids = {sid for sid, _ in tasks}
        corpus = _optional_pdf_corpus_tokens(c.run_dir)
        items, prompt_sids = _build_optional_pdf_prompt_payload(
            results, registry, task_sids, corpus, top_n
        )

        if items and top_n > 0:
            paths_before = {
                sid: (registry.get(sid) or {}).get("content_path")
                for sid in prompt_sids
            }
            cb = c.on_optional_source_pdfs
            if cb:
                cb(c.run_dir, items)
            else:
                _cli_optional_pdf_hint(c.run_dir, items)
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            reread_ids = [
                sid
                for sid in prompt_sids
                if (registry.get(sid) or {}).get("content_path")
                != paths_before.get(sid)
            ]
            if reread_ids:
                logger.info(
                    "Re-reading %d source(s) after optional PDF upload…",
                    len(reread_ids),
                )
                reread_pairs = [(sid, registry[sid]) for sid in reread_ids]
                domain_tracker2 = _DomainFailureTracker()
                reread_results = asyncio.run(read_all(reread_pairs, domain_tracker2))
                by_sid = dict(results)
                for sid, note in reread_results:
                    by_sid[sid] = note
                results = [(sid, by_sid[sid]) for sid, _ in tasks]

        accessible_count = sum(
            1 for _, note in results if note is not None and note.is_accessible
        )
        inaccessible_count = len(tasks) - accessible_count

        selected = _select_best_sources(c.run_dir, registry, target_sources)
        selected_path = c.run_dir / "sources" / "selected.json"
        selected_path.write_text(
            json.dumps(selected, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(
            "Selected %d/%d sources (%d accessible, %d inaccessible)",
            len(selected),
            len(tasks),
            accessible_count,
            inaccessible_count,
        )

        if inaccessible_count:
            print(
                f"  ⚠ {inaccessible_count}/{len(tasks)} sources inaccessible "
                f"({accessible_count} usable). Selected {len(selected)} best sources.",
                file=sys.stderr,
            )

    return _do_read_sources


# -- Source assignment (long-essay path only) ------------------------------


def _do_assign_sources(ctx: PipelineContext) -> None:
    """Assign selected sources to sections using the worker model."""
    plan_json = _read_text(ctx.run_dir / "plan" / "plan.json")
    source_notes = _load_selected_source_notes(ctx.run_dir)
    if not source_notes:
        logger.warning("No source notes available for assignment")
        return

    sections = _parse_sections(ctx.run_dir)
    num_sections = len(sections) or 1
    min_per_section = max(2, len(source_notes) // num_sections)

    prompt = render_prompt(
        "source_assignment.j2",
        plan_json=plan_json,
        source_notes=source_notes,
        min_per_section=min_per_section,
    )

    result = _structured_call(ctx.worker, prompt, SourceAssignmentPlan, ctx.tracker)

    # Patch: ensure every selected source appears in at least one assignment.
    # The LLM may skip some; assign stragglers to the best-fit section by
    # lexical overlap between the source note and section topic.
    assigned_ids: set[str] = set()
    for a in result.assignments:
        assigned_ids.update(a.source_ids)

    notes_by_id = {n.source_id: n for n in source_notes}
    missing_ids = [n.source_id for n in source_notes if n.source_id not in assigned_ids]

    if missing_ids and sections:
        section_corpora = {
            s.number: _corpus_tokens(
                f"{s.title} {s.key_points} {s.content_outline or ''}"
            )
            for s in sections
        }
        for sid in missing_ids:
            note = notes_by_id[sid]
            best_section = max(
                result.assignments,
                key=lambda a: _note_lexical_score(
                    section_corpora.get(a.section_number, set()), note
                ),
            )
            best_section.source_ids.append(sid)
        logger.info(
            "Patched %d unassigned sources into best-fit sections", len(missing_ids)
        )

    _write_json(ctx.run_dir / "plan" / "source_assignments.json", result)


def _load_source_assignments(run_dir: Path) -> dict[int, list[str]]:
    """Load source-to-section assignments, returning empty dict if unavailable."""
    path = run_dir / "plan" / "source_assignments.json"
    if not path.exists():
        return {}
    try:
        plan = SourceAssignmentPlan.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        return {a.section_number: a.source_ids for a in plan.assignments}
    except Exception:
        logger.warning("Failed to load source assignments")
        return {}


# -- Short path: full-essay write & review --------------------------------


def _make_write_full(
    target_words: int, citation_min_sources: int
) -> Callable[[PipelineContext], None]:
    def _do_write_full(ctx: PipelineContext) -> None:
        brief_json = _read_text(ctx.run_dir / "brief" / "assignment.json")
        plan_json = _read_text(ctx.run_dir / "plan" / "plan.json")
        source_notes = _load_selected_source_notes(ctx.run_dir)
        language = _get_brief_language(ctx.run_dir)
        budget = ctx.config.search.section_source_full_detail_max
        corpus = _plan_corpus_from_json(plan_json)
        detail_notes, catalog_md, total_n = _split_writer_source_context(
            corpus, source_notes, budget
        )

        prompt = render_prompt(
            "essay_writing.j2",
            brief_json=brief_json,
            plan_json=plan_json,
            source_notes=detail_notes,
            source_catalog=catalog_md,
            total_selected_sources=total_n,
            target_words=target_words,
            tolerance_percent=round(ctx.config.writing.word_count_tolerance * 100),
            min_words=round(
                target_words * (1 - ctx.config.writing.word_count_tolerance)
            ),
            language=language,
            min_sources=citation_min_sources,
        )

        essay = _text_call(
            ctx.writer,
            f"You are an expert academic writer producing essays in {language}.",
            prompt,
            ctx.tracker,
        )
        _write_text(
            ctx.run_dir / "essay" / "draft.md",
            strip_leading_submission_metadata(essay),
        )

    return _do_write_full


def _make_review_full(
    target_words: int, citation_min_sources: int
) -> Callable[[PipelineContext], None]:
    def _do_review_full(ctx: PipelineContext) -> None:
        brief_json = _read_text(ctx.run_dir / "brief" / "assignment.json")
        plan_json = _read_text(ctx.run_dir / "plan" / "plan.json")
        draft = _read_text(ctx.run_dir / "essay" / "draft.md")
        draft_words = len(draft.split())
        language = _get_brief_language(ctx.run_dir)
        source_notes = _load_selected_source_notes(ctx.run_dir)
        catalog_md = _source_catalog_markdown(source_notes)
        total_selected = len(source_notes)
        cited_ids = set(re.findall(r"\[\[([^|\]]+?)(?:\|[^\]]*?)?\]\]", draft))
        uncited_ids = [
            n.source_id for n in source_notes if n.source_id not in cited_ids
        ]

        prompt = render_prompt(
            "essay_review.j2",
            brief_json=brief_json,
            plan_json=plan_json,
            draft_text=draft,
            target_words=target_words,
            draft_words=draft_words,
            tolerance_ratio=ctx.config.writing.word_count_tolerance,
            tolerance_percent=round(ctx.config.writing.word_count_tolerance * 100),
            tolerance_ratio_over=ctx.config.writing.word_count_tolerance_over,
            tolerance_percent_over=round(
                ctx.config.writing.word_count_tolerance_over * 100
            ),
            language=language,
            min_sources=citation_min_sources,
            source_catalog=catalog_md,
            total_selected_sources=total_selected,
            uncited_ids=uncited_ids,
        )

        reviewed = _text_call(
            ctx.reviewer,
            f"You are an expert academic editor polishing essays in {language}.",
            prompt,
            ctx.tracker,
        )
        _write_text(
            ctx.run_dir / "essay" / "reviewed.md",
            strip_leading_submission_metadata(reviewed),
        )

    return _do_review_full


# -- Long path: section-by-section write & review -------------------------


def _writing_order(sections: list[Section]) -> list[Section]:
    """Body sections in plan order, then conclusion, then introduction."""
    body = [s for s in sections if not s.is_intro and not s.is_conclusion]
    conclusion = [s for s in sections if s.is_conclusion]
    intro = [s for s in sections if s.is_intro]
    return body + conclusion + intro


def _section_filename(section: Section) -> str:
    return f"{section.number:02d}.md"


def _make_write_sections(
    sections: list[Section],
    target_words: int,
    citation_min_sources: int,
) -> Callable[[PipelineContext], None]:
    def _do_write_sections(ctx: PipelineContext) -> None:
        sections_dir = ctx.run_dir / "essay" / "sections"
        sections_dir.mkdir(parents=True, exist_ok=True)

        plan_json = _read_text(ctx.run_dir / "plan" / "plan.json")
        source_notes = _load_selected_source_notes(ctx.run_dir)
        language = _get_brief_language(ctx.run_dir)
        budget = ctx.config.search.section_source_full_detail_max
        order = _writing_order(sections)
        written_sections: list[tuple[Section, str]] = []

        # Load source-to-section assignments (long-path only)
        section_assignments = _load_source_assignments(ctx.run_dir)
        notes_by_id = {n.source_id: n for n in source_notes}

        for section in order:
            fname = _section_filename(section)
            prior_context = _build_prior_sections_context(written_sections)
            section_corpus = (
                f"{section.title} {section.key_points} {section.content_outline or ''}"
            )

            assigned_ids = section_assignments.get(section.number, [])
            if assigned_ids:
                # Boost assigned sources to the top of the detail window
                assigned_notes = [
                    notes_by_id[sid] for sid in assigned_ids if sid in notes_by_id
                ]
                remaining = [
                    n for n in source_notes if n.source_id not in set(assigned_ids)
                ]
                ranked_remaining = _rank_notes_by_corpus(section_corpus, remaining)
                slots_left = max(0, budget - len(assigned_notes))
                detail_notes = assigned_notes + ranked_remaining[:slots_left]
                catalog_md = _source_catalog_markdown(source_notes)
                total_n = len(source_notes)
            else:
                detail_notes, catalog_md, total_n = _split_writer_source_context(
                    section_corpus, source_notes, budget
                )

            prompt = render_prompt(
                "section_writing.j2",
                plan_json=plan_json,
                source_notes=detail_notes,
                source_catalog=catalog_md,
                total_selected_sources=total_n,
                section=section,
                prior_sections=prior_context,
                assigned_source_ids=assigned_ids,
                tolerance_percent=round(ctx.config.writing.word_count_tolerance * 100),
                min_words=round(
                    section.word_target * (1 - ctx.config.writing.word_count_tolerance)
                ),
                language=language,
                min_sources=citation_min_sources,
            )

            tracker_step = f"write:{section.number}"
            if ctx.tracker is not None:
                ctx.tracker.set_current_step(tracker_step)

            t0 = monotonic()
            text = _text_call(
                ctx.writer,
                f"You are an expert academic writer producing essays in {language}.",
                prompt,
                ctx.tracker,
            )
            dur = monotonic() - t0

            _write_text(sections_dir / fname, text)

            if ctx.tracker is not None:
                ctx.tracker.record_duration(tracker_step, dur)

            print(
                f"    section {section.number} ({section.title}) -- {dur:.1f}s",
                file=sys.stderr,
            )
            written_sections.append((section, text))

        # Concatenate all sections in plan order into draft.md
        plan_order = sorted(sections, key=lambda s: s.number)
        draft_parts = []
        for s in plan_order:
            fp = sections_dir / _section_filename(s)
            if fp.exists():
                draft_parts.append(fp.read_text(encoding="utf-8"))
            else:
                logger.warning("Section %d file missing: %s", s.number, fp)

        combined_draft = "\n\n".join(draft_parts)
        _write_text(
            ctx.run_dir / "essay" / "draft.md",
            strip_leading_submission_metadata(combined_draft),
        )
        logger.info("Combined %d sections into draft.md", len(draft_parts))

    return _do_write_sections


_REVIEW_CONCURRENCY = 4


def _make_review_sections(
    sections: list[Section],
    target_words: int,
) -> Callable[[PipelineContext], None]:
    def _do_review_sections(ctx: PipelineContext) -> None:
        sections_dir = ctx.run_dir / "essay" / "sections"
        reviewed_dir = ctx.run_dir / "essay" / "reviewed"
        reviewed_dir.mkdir(parents=True, exist_ok=True)
        plan_order = sorted(sections, key=lambda s: s.number)
        language = _get_brief_language(ctx.run_dir)

        # Read all draft texts upfront (immutable context for all reviews)
        draft_texts: dict[int, str] = {}
        for s in plan_order:
            fp = sections_dir / _section_filename(s)
            if fp.exists():
                draft_texts[s.number] = fp.read_text(encoding="utf-8")

        def _review_one(section: Section) -> tuple[Section, str, float]:
            if section.number not in draft_texts:
                logger.warning("Section %d missing, skipping review", section.number)
                return section, "", 0.0

            section_text = draft_texts[section.number]
            section_words = len(section_text.split())

            neighbor_texts = {
                s.number: draft_texts[s.number]
                for s in _section_window(plan_order, section.number)
                if s.number in draft_texts
            }
            full_essay = _build_review_context(section, plan_order, neighbor_texts)

            prompt = render_prompt(
                "section_review.j2",
                section=section,
                full_essay=full_essay,
                section_words=section_words,
                tolerance_ratio=ctx.config.writing.word_count_tolerance,
                tolerance_percent=round(ctx.config.writing.word_count_tolerance * 100),
                tolerance_ratio_over=ctx.config.writing.word_count_tolerance_over,
                tolerance_percent_over=round(
                    ctx.config.writing.word_count_tolerance_over * 100
                ),
                language=language,
            )

            tracker_step = f"review:{section.number}"
            if ctx.tracker is not None:
                ctx.tracker.set_current_step(tracker_step)

            t0 = monotonic()
            reviewed = _text_call(
                ctx.reviewer,
                f"You are an expert academic editor polishing essays in {language}.",
                prompt,
                ctx.tracker,
            )
            dur = monotonic() - t0

            _write_text(reviewed_dir / _section_filename(section), reviewed)

            if ctx.tracker is not None:
                ctx.tracker.record_duration(tracker_step, dur)

            return section, reviewed, dur

        # Run reviews in parallel — sections use draft context only
        with ThreadPoolExecutor(max_workers=_REVIEW_CONCURRENCY) as pool:
            futures = {pool.submit(_review_one, s): s for s in plan_order}
            for future in as_completed(futures):
                section, _, dur = future.result()
                if dur > 0:
                    print(
                        f"    section {section.number} ({section.title}) -- {dur:.1f}s",
                        file=sys.stderr,
                    )

        # Concatenate reviewed sections in plan order
        reviewed_parts = []
        for s in plan_order:
            fp = reviewed_dir / _section_filename(s)
            if fp.exists():
                reviewed_parts.append(fp.read_text(encoding="utf-8"))
            elif s.number in draft_texts:
                reviewed_parts.append(draft_texts[s.number])

        combined_reviewed = "\n\n".join(reviewed_parts)
        _write_text(
            ctx.run_dir / "essay" / "reviewed.md",
            strip_leading_submission_metadata(combined_reviewed),
        )
        logger.info(
            "Combined %d reviewed sections into reviewed.md", len(reviewed_parts)
        )

    return _do_review_sections


# -- Export (pure Python) --------------------------------------------------


def _do_export(ctx: PipelineContext) -> None:
    """Build docx from disk files (pure Python, no LLM)."""
    from src.tools.docx_builder import build_document

    essay_text = None
    for name in ("reviewed.md", "draft.md"):
        p = ctx.run_dir / "essay" / name
        if p.exists():
            essay_text = p.read_text(encoding="utf-8")
            break
    if not essay_text:
        logger.error("No essay found -- cannot export.")
        return

    essay_text = strip_leading_submission_metadata(essay_text)

    sources: dict = {}
    for fname in ("selected.json", "registry.json"):
        src_path = ctx.run_dir / "sources" / fname
        if src_path.exists():
            sources = json.loads(src_path.read_text(encoding="utf-8"))
            break

    doc_config = ctx.config.formatting.model_dump()
    brief_path = ctx.run_dir / "brief" / "assignment.json"
    plan_path = ctx.run_dir / "plan" / "plan.json"

    # Prefer plan title (specific) over brief topic (may be generic)
    if plan_path.exists():
        plan = EssayPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
        if plan.title:
            doc_config.setdefault("title", plan.title)

    if brief_path.exists():
        brief = AssignmentBrief.model_validate_json(
            brief_path.read_text(encoding="utf-8")
        )
        doc_config.setdefault("title", brief.topic)
        if brief.student:
            doc_config.setdefault("author", brief.student)
        if brief.institution:
            doc_config.setdefault("institution", brief.institution)
        if brief.course:
            doc_config.setdefault("course", brief.course)
        if brief.professor:
            doc_config.setdefault("professor", brief.professor)
        # Default date in the essay's language
        if "date" not in doc_config:
            from datetime import date as _date

            _MONTHS = {
                "Greek (Δημοτική)": [
                    "",
                    "Ιανουάριος",
                    "Φεβρουάριος",
                    "Μάρτιος",
                    "Απρίλιος",
                    "Μάιος",
                    "Ιούνιος",
                    "Ιούλιος",
                    "Αύγουστος",
                    "Σεπτέμβριος",
                    "Οκτώβριος",
                    "Νοέμβριος",
                    "Δεκέμβριος",
                ],
                "English": [
                    "",
                    "January",
                    "February",
                    "March",
                    "April",
                    "May",
                    "June",
                    "July",
                    "August",
                    "September",
                    "October",
                    "November",
                    "December",
                ],
            }
            today = _date.today()
            lang = brief.language if brief_path.exists() else "English"
            months = _MONTHS.get(lang, _MONTHS["English"])
            doc_config["date"] = f"{months[today.month]} {today.year}"

    doc = build_document(essay_text, doc_config, sources)

    output_path = Path(ctx.config.paths.output_dir) / "essay.docx"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    logger.info("essay.docx saved to %s", output_path)
    print(f"  essay.docx -> {output_path}", file=sys.stderr)

    run_docx = ctx.run_dir / "essay.docx"
    if run_docx.resolve() != output_path.resolve():
        import shutil

        shutil.copy2(str(output_path), str(run_docx))


# ---------------------------------------------------------------------------
# Pipeline builder & entry point
# ---------------------------------------------------------------------------


def _build_execution_steps(
    ctx: PipelineContext,
    target_words: int,
    fetch_sources: int,
    target_sources: int,
    citation_min_sources: int,
) -> list[PipelineStep]:
    """Build the dynamic portion of the pipeline after plan is available."""
    threshold = ctx.config.writing.long_essay_threshold

    steps: list[PipelineStep] = [
        PipelineStep("research", lambda c: _do_research(c, fetch_sources)),
        PipelineStep("read_sources", _make_read_sources(target_sources)),
    ]

    if target_words <= threshold:
        steps.append(
            PipelineStep("write", _make_write_full(target_words, citation_min_sources))
        )
        steps.append(
            PipelineStep(
                "review", _make_review_full(target_words, citation_min_sources)
            )
        )
    else:
        sections = _parse_sections(ctx.run_dir)
        if not sections:
            logger.warning("Could not parse sections -- falling back to short path")
            steps.append(
                PipelineStep(
                    "write", _make_write_full(target_words, citation_min_sources)
                )
            )
            steps.append(
                PipelineStep(
                    "review", _make_review_full(target_words, citation_min_sources)
                )
            )
        else:
            steps.append(
                PipelineStep("assign_sources", _do_assign_sources),
            )
            steps.append(
                PipelineStep(
                    "write",
                    _make_write_sections(sections, target_words, citation_min_sources),
                )
            )
            steps.append(
                PipelineStep("review", _make_review_sections(sections, target_words))
            )

    steps.append(PipelineStep("export", _do_export))
    return steps


def run_pipeline(
    worker: ModelClient,
    writer: ModelClient,
    reviewer: ModelClient,
    run_dir: Path,
    config: EssayWriterConfig,
    *,
    async_worker: AsyncModelClient | None = None,
    extra_prompt: str | None = None,
    token_tracker=None,
    on_questions: Callable[[list[ValidationQuestion], Path], None] | None = None,
    on_optional_source_pdfs: Callable[[Path, list[dict]], None] | None = None,
    min_sources: int | None = None,
    user_sources_dir: Path | None = None,
) -> None:
    """Execute the essay writing pipeline.

    Phase 1 (fixed):  intake -> validate -> plan
    Phase 2 (dynamic): research -> read_sources -> write -> review -> export
    """
    ctx = PipelineContext(
        worker=worker,
        async_worker=async_worker,
        writer=writer,
        reviewer=reviewer,
        run_dir=run_dir,
        config=config,
        extra_prompt=extra_prompt,
        tracker=token_tracker,
        user_sources_dir=user_sources_dir,
        on_optional_source_pdfs=on_optional_source_pdfs,
    )

    # Ensure output subdirectories exist
    for subdir in ("brief", "plan", "sources", "essay"):
        (run_dir / subdir).mkdir(parents=True, exist_ok=True)

    # Phase 1a: intake + validate
    _execute([PipelineStep("intake", _do_intake)], ctx)

    # Override brief.min_sources with the explicit form value if provided
    if min_sources is not None:
        brief_path = run_dir / "brief" / "assignment.json"
        if brief_path.exists():
            brief = AssignmentBrief.model_validate_json(
                brief_path.read_text(encoding="utf-8")
            )
            brief.min_sources = min_sources
            brief_path.write_text(
                brief.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8"
            )

    _execute([PipelineStep("validate", _do_validate)], ctx)

    # Check validation result
    validation = _read_validation(run_dir)
    if validation and validation.questions and not validation.is_pass and on_questions:
        on_questions(validation.questions, run_dir)

    # Phase 1b: plan
    _execute([PipelineStep("plan", _do_plan)], ctx)

    # Analyze plan to decide strategy
    target_words = _get_target_words(run_dir)
    threshold = config.writing.long_essay_threshold
    logger.info(
        "Target: %d words, threshold: %d -> %s path",
        target_words,
        threshold,
        "long" if target_words > threshold else "short",
    )

    # Phase 2: built from plan analysis
    # Use explicit min_sources if provided; fall back to brief extraction
    user_min_sources = min_sources
    if user_min_sources is None:
        brief_path = run_dir / "brief" / "assignment.json"
        if brief_path.exists():
            brief = AssignmentBrief.model_validate_json(
                brief_path.read_text(encoding="utf-8")
            )
            user_min_sources = brief.min_sources
    target_sources, fetch_sources = _compute_max_sources(
        target_words, config, user_min_sources
    )
    citation_min_sources = max(
        target_sources,
        user_min_sources if user_min_sources is not None else 0,
    )
    brief_path = run_dir / "brief" / "assignment.json"
    if brief_path.exists():
        brief = AssignmentBrief.model_validate_json(
            brief_path.read_text(encoding="utf-8")
        )
        brief.min_sources = citation_min_sources
        brief_path.write_text(
            brief.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8"
        )
    logger.info(
        "Sources: target=%d fetch=%d citation_minimum=%d",
        target_sources,
        fetch_sources,
        citation_min_sources,
    )
    phase2 = _build_execution_steps(
        ctx, target_words, fetch_sources, target_sources, citation_min_sources
    )
    _execute(phase2, ctx)
