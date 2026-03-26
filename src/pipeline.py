"""Deterministic Python pipeline for essay writing.

Two-phase execution:
  Phase 1 (fixed):  intake -> validate -> plan
  Phase 2 (dynamic): steps built from plan analysis (short vs long path)

LLM calls use:
- ``model.with_structured_output(Schema)`` for JSON steps (auto-retry)
- ``model.invoke(messages)`` for text steps (essays)

The pipeline handles all file I/O; LLMs never touch disk.
"""

from __future__ import annotations

import json
import logging
import math
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

from src.rendering import render_prompt
from src.schemas import (
    AssignmentBrief,
    EssayPlan,
    SourceNote,
    ValidationResult,
)
from src.tools.research_sources import run_research
from src.tools.web_fetcher import fetch_url_content

if TYPE_CHECKING:
    from collections.abc import Callable

    from config.schemas import EssayWriterConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class PipelineContext:
    """Shared state passed to every step."""

    worker: BaseChatModel
    writer: BaseChatModel
    reviewer: BaseChatModel
    run_dir: Path
    config: EssayWriterConfig
    extra_prompt: str | None = None
    callbacks: list | None = None
    tracker: object | None = None  # TokenTracker (optional)


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
            ctx.tracker.current_step = step.name
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


def _structured_call(
    model: BaseChatModel,
    prompt: str,
    schema: type[BaseModel],
    callbacks: list | None = None,
    retries: int = _STRUCTURED_RETRIES,
) -> BaseModel:
    """Call a model with structured output, retrying on validation errors.

    Uses ``model.with_structured_output(schema)`` which constrains the
    LLM to produce valid JSON matching the Pydantic model.  On validation
    failure, re-invokes with the error message for self-correction.
    """
    from src.agent import invoke_with_retry

    structured = model.with_structured_output(schema, method="json_schema")
    messages = [HumanMessage(content=prompt)]
    config = {"callbacks": callbacks} if callbacks else {}

    for attempt in range(retries + 1):
        try:
            result = invoke_with_retry(structured, messages, **config)
            if isinstance(result, BaseModel):
                return result
            # Some providers return dict instead of model
            return schema.model_validate(result)
        except (ValidationError, Exception) as exc:
            if attempt < retries and isinstance(exc, ValidationError):
                logger.warning(
                    "Structured output validation failed (attempt %d/%d): %s",
                    attempt + 1,
                    retries + 1,
                    exc,
                )
                messages = [
                    HumanMessage(content=prompt),
                    HumanMessage(
                        content=f"Your previous output had validation errors:\n{exc}\n"
                        "Please fix these errors and try again."
                    ),
                ]
                continue
            raise

    # Unreachable, but satisfies type checker
    raise RuntimeError("Structured call exhausted retries")


def _text_call(
    model: BaseChatModel,
    system_prompt: str,
    user_prompt: str,
    callbacks: list | None = None,
) -> str:
    """Call a model for free-form text output (essays, reviews)."""
    from src.agent import invoke_with_retry

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ]
    config = {"callbacks": callbacks} if callbacks else {}
    response = invoke_with_retry(model, messages, **config)
    return response.content


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


def _compute_max_sources(
    target_words: int, config: EssayWriterConfig
) -> tuple[int, int]:
    """Compute (target_sources, fetch_sources) based on word count and config."""
    sc = config.search
    raw = math.ceil(target_words / 1000) * sc.sources_per_1k_words
    target = max(sc.min_sources, min(raw, sc.max_sources))
    fetch = min(int(target * sc.overfetch_multiplier), sc.max_sources * 2)
    return target, fetch


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

    brief = _structured_call(ctx.worker, prompt, AssignmentBrief, ctx.callbacks)
    _write_json(ctx.run_dir / "brief" / "assignment.json", brief)


def _do_validate(ctx: PipelineContext) -> None:
    brief_json = _read_text(ctx.run_dir / "brief" / "assignment.json")
    prompt = render_prompt("validate.j2", brief_json=brief_json)

    result = _structured_call(ctx.worker, prompt, ValidationResult, ctx.callbacks)
    _write_json(ctx.run_dir / "brief" / "validation.json", result)


def _read_validation(run_dir: Path) -> str | None:
    """Read validation.json and return formatted questions if any."""
    path = run_dir / "brief" / "validation.json"
    if not path.exists():
        return None
    result = ValidationResult.model_validate_json(path.read_text(encoding="utf-8"))
    if result.is_pass or not result.questions:
        return None
    lines: list[str] = []
    for i, q in enumerate(result.questions, 1):
        lines.append(f"{i}. {q.question}")
        for j, opt in enumerate(q.options):
            label = chr(ord("a") + j)
            lines.append(f"   {label}) {opt}")
        lines.append("")
    return "\n".join(lines).strip()


def _do_plan(ctx: PipelineContext) -> None:
    brief_json = _read_text(ctx.run_dir / "brief" / "assignment.json")
    prompt = render_prompt("plan.j2", brief_json=brief_json)

    plan = _structured_call(ctx.worker, prompt, EssayPlan, ctx.callbacks)
    _write_json(ctx.run_dir / "plan" / "plan.json", plan)


def _do_research(ctx: PipelineContext, fetch_sources: int) -> None:
    """Run research — pure Python, no LLM."""
    plan_path = ctx.run_dir / "plan" / "plan.json"
    plan = EssayPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))

    run_research(
        queries=plan.research_queries,
        max_sources=fetch_sources,
        sources_dir=str(ctx.run_dir / "sources"),
    )


def _read_one_source(
    source_id: str,
    meta: dict,
    worker: BaseChatModel,
    sources_dir: str,
    callbacks: list | None,
) -> SourceNote:
    """Fetch and extract notes for a single source."""
    url = meta.get("pdf_url") or meta.get("url", "")
    content = ""

    if url:
        try:
            content = fetch_url_content(url, sources_dir=sources_dir)
            # Truncate very long content to avoid token limits
            if len(content) > 50_000:
                content = content[:50_000] + "\n\n[... truncated ...]"
        except (httpx.HTTPStatusError, httpx.RequestError, Exception) as exc:
            logger.warning("Failed to fetch %s: %s", url, exc)

    # If we have content or at least an abstract, use LLM to extract notes
    abstract = meta.get("abstract", "")
    if content or abstract:
        prompt = render_prompt(
            "source_reading.j2",
            source_id=source_id,
            title=meta.get("title", ""),
            authors=", ".join(meta.get("authors", [])),
            year=meta.get("year", ""),
            doi=meta.get("doi", ""),
            abstract=abstract,
            content=content,
        )
        try:
            return _structured_call(worker, prompt, SourceNote, callbacks)
        except Exception:
            logger.warning("LLM extraction failed for %s, using metadata", source_id)

    # Fallback: construct note from metadata alone
    if abstract:
        return SourceNote(
            source_id=source_id,
            is_accessible=True,
            title=meta.get("title", ""),
            authors=meta.get("authors", []),
            year=meta.get("year"),
            source_type=meta.get("source_type"),
            summary=abstract,
            url=url,
        )

    return SourceNote(
        source_id=source_id,
        is_accessible=False,
        title=meta.get("title", ""),
        authors=meta.get("authors", []),
        year=meta.get("year"),
        inaccessible_reason="No content or abstract available",
        url=url,
    )


def _select_best_sources(
    run_dir: Path, registry: dict, target_sources: int
) -> dict[str, dict]:
    """Select the best target_sources from read notes."""
    notes_dir = run_dir / "sources" / "notes"
    accessible: list[tuple[str, int]] = []
    inaccessible: list[str] = []

    for sid in registry:
        note_path = notes_dir / f"{sid}.json"
        if not note_path.exists():
            inaccessible.append(sid)
            continue
        try:
            note = SourceNote.model_validate_json(note_path.read_text(encoding="utf-8"))
            if note.is_accessible:
                accessible.append((sid, note.content_word_count))
            else:
                inaccessible.append(sid)
        except Exception:
            inaccessible.append(sid)

    accessible.sort(key=lambda x: x[1], reverse=True)
    selected_ids = [sid for sid, _ in accessible[:target_sources]]

    remaining = target_sources - len(selected_ids)
    if remaining > 0:
        selected_ids.extend(inaccessible[:remaining])

    return {sid: registry[sid] for sid in selected_ids if sid in registry}


def _make_read_sources(target_sources: int) -> Callable[[PipelineContext], None]:
    def _do_read_sources(ctx: PipelineContext) -> None:
        registry_path = ctx.run_dir / "sources" / "registry.json"
        if not registry_path.exists():
            logger.warning("No registry.json found -- skipping source reading.")
            return

        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        tasks = [
            (sid, meta)
            for sid, meta in registry.items()
            if meta.get("url") or meta.get("pdf_url")
        ]
        if not tasks:
            logger.info("No sources with URLs to read.")
            return

        logger.info("Reading %d sources in parallel...", len(tasks))
        sources_dir = str(ctx.run_dir / "sources")
        notes_dir = ctx.run_dir / "sources" / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)

        def read_one(args: tuple[str, dict]) -> tuple[str, SourceNote]:
            sid, meta = args
            note = _read_one_source(sid, meta, ctx.worker, sources_dir, ctx.callbacks)
            _write_json(notes_dir / f"{sid}.json", note)
            return sid, note

        accessible_count = 0
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(read_one, t): t[0] for t in tasks}
            for future in as_completed(futures):
                sid = futures[future]
                try:
                    _, note = future.result()
                    if note.is_accessible:
                        accessible_count += 1
                except Exception:
                    logger.exception("Failed to read source %s", sid)

        inaccessible_count = len(tasks) - accessible_count

        # Select best N sources
        selected = _select_best_sources(ctx.run_dir, registry, target_sources)
        selected_path = ctx.run_dir / "sources" / "selected.json"
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


# -- Short path: full-essay write & review --------------------------------


def _make_write_full(target_words: int) -> Callable[[PipelineContext], None]:
    def _do_write_full(ctx: PipelineContext) -> None:
        brief_json = _read_text(ctx.run_dir / "brief" / "assignment.json")
        plan_json = _read_text(ctx.run_dir / "plan" / "plan.json")
        source_notes = _load_source_notes(ctx.run_dir)

        prompt = render_prompt(
            "essay_writing.j2",
            brief_json=brief_json,
            plan_json=plan_json,
            source_notes=source_notes,
            target_words=target_words,
        )

        essay = _text_call(
            ctx.writer,
            "You are an expert academic writer producing essays in Modern Greek (Δημοτική).",
            prompt,
            ctx.callbacks,
        )
        _write_text(ctx.run_dir / "essay" / "draft.md", essay)

    return _do_write_full


def _make_review_full(target_words: int) -> Callable[[PipelineContext], None]:
    def _do_review_full(ctx: PipelineContext) -> None:
        brief_json = _read_text(ctx.run_dir / "brief" / "assignment.json")
        plan_json = _read_text(ctx.run_dir / "plan" / "plan.json")
        draft = _read_text(ctx.run_dir / "essay" / "draft.md")

        prompt = render_prompt(
            "essay_review.j2",
            brief_json=brief_json,
            plan_json=plan_json,
            draft_text=draft,
            target_words=target_words,
        )

        reviewed = _text_call(
            ctx.reviewer,
            "You are an expert academic editor polishing essays in Modern Greek (Δημοτική).",
            prompt,
            ctx.callbacks,
        )
        _write_text(ctx.run_dir / "essay" / "reviewed.md", reviewed)

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
) -> Callable[[PipelineContext], None]:
    def _do_write_sections(ctx: PipelineContext) -> None:
        sections_dir = ctx.run_dir / "essay" / "sections"
        sections_dir.mkdir(parents=True, exist_ok=True)

        plan_json = _read_text(ctx.run_dir / "plan" / "plan.json")
        source_notes = _load_source_notes(ctx.run_dir)
        order = _writing_order(sections)
        written_files: list[tuple[Section, str]] = []

        for section in order:
            fname = _section_filename(section)

            # Build context of already-written sections
            prior_context = ""
            if written_files:
                sorted_written = sorted(written_files, key=lambda x: x[0].number)
                parts = []
                for ws, wf in sorted_written:
                    wp = sections_dir / wf
                    if wp.exists():
                        parts.append(wp.read_text(encoding="utf-8"))
                if parts:
                    prior_context = "\n\n---\n\n".join(parts)

            prompt = render_prompt(
                "section_writing.j2",
                plan_json=plan_json,
                source_notes=source_notes,
                section=section,
                prior_sections=prior_context,
            )

            tracker_step = f"write:{section.number}"
            if ctx.tracker is not None:
                ctx.tracker.current_step = tracker_step

            t0 = monotonic()
            text = _text_call(
                ctx.writer,
                "You are an expert academic writer producing essays in Modern Greek (Δημοτική).",
                prompt,
                ctx.callbacks,
            )
            dur = monotonic() - t0

            _write_text(sections_dir / fname, text)

            if ctx.tracker is not None:
                ctx.tracker.record_duration(tracker_step, dur)

            print(
                f"    section {section.number} ({section.title}) -- {dur:.1f}s",
                file=sys.stderr,
            )
            written_files.append((section, fname))

        # Concatenate all sections in plan order into draft.md
        plan_order = sorted(sections, key=lambda s: s.number)
        draft_parts = []
        for s in plan_order:
            fp = sections_dir / _section_filename(s)
            if fp.exists():
                draft_parts.append(fp.read_text(encoding="utf-8"))
            else:
                logger.warning("Section %d file missing: %s", s.number, fp)

        _write_text(ctx.run_dir / "essay" / "draft.md", "\n\n".join(draft_parts))
        logger.info("Combined %d sections into draft.md", len(draft_parts))

    return _do_write_sections


def _make_review_sections(
    sections: list[Section],
    target_words: int,
) -> Callable[[PipelineContext], None]:
    def _do_review_sections(ctx: PipelineContext) -> None:
        sections_dir = ctx.run_dir / "essay" / "sections"
        reviewed_dir = ctx.run_dir / "essay" / "reviewed"
        reviewed_dir.mkdir(parents=True, exist_ok=True)
        plan_order = sorted(sections, key=lambda s: s.number)

        def _best_path(s: Section) -> Path:
            rp = reviewed_dir / _section_filename(s)
            sp = sections_dir / _section_filename(s)
            return rp if rp.exists() else sp

        for section in plan_order:
            section_path = _best_path(section)
            if not section_path.exists():
                logger.warning("Section %d missing, skipping review", section.number)
                continue

            # Build full essay with target section delimited
            full_essay_parts = []
            for s in plan_order:
                fp = _best_path(s)
                if not fp.exists():
                    continue
                text = fp.read_text(encoding="utf-8")
                if s.number == section.number:
                    text = (
                        "<!-- >>> SECTION TO REVIEW: START >>> -->\n"
                        f"{text}\n"
                        "<!-- <<< SECTION TO REVIEW: END <<< -->"
                    )
                full_essay_parts.append(text)
            full_essay = "\n\n---\n\n".join(full_essay_parts)

            prompt = render_prompt(
                "section_review.j2",
                section=section,
                full_essay=full_essay,
            )

            tracker_step = f"review:{section.number}"
            if ctx.tracker is not None:
                ctx.tracker.current_step = tracker_step

            t0 = monotonic()
            reviewed = _text_call(
                ctx.reviewer,
                "You are an expert academic editor polishing essays in Modern Greek (Δημοτική).",
                prompt,
                ctx.callbacks,
            )
            dur = monotonic() - t0

            _write_text(reviewed_dir / _section_filename(section), reviewed)

            if ctx.tracker is not None:
                ctx.tracker.record_duration(tracker_step, dur)

            print(
                f"    section {section.number} ({section.title}) -- {dur:.1f}s",
                file=sys.stderr,
            )

        # Concatenate reviewed sections
        reviewed_parts = []
        for s in plan_order:
            fp = _best_path(s)
            if fp.exists():
                reviewed_parts.append(fp.read_text(encoding="utf-8"))

        _write_text(ctx.run_dir / "essay" / "reviewed.md", "\n\n".join(reviewed_parts))
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

    sources: dict = {}
    for fname in ("selected.json", "registry.json"):
        src_path = ctx.run_dir / "sources" / fname
        if src_path.exists():
            sources = json.loads(src_path.read_text(encoding="utf-8"))
            break

    doc_config = ctx.config.formatting.model_dump()
    brief_path = ctx.run_dir / "brief" / "assignment.json"
    if brief_path.exists():
        brief = AssignmentBrief.model_validate_json(
            brief_path.read_text(encoding="utf-8")
        )
        doc_config.setdefault("title", brief.topic)

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
) -> list[PipelineStep]:
    """Build the dynamic portion of the pipeline after plan is available."""
    threshold = ctx.config.writing.long_essay_threshold

    steps: list[PipelineStep] = [
        PipelineStep("research", lambda c: _do_research(c, fetch_sources)),
        PipelineStep("read_sources", _make_read_sources(target_sources)),
    ]

    if target_words <= threshold:
        steps.append(PipelineStep("write", _make_write_full(target_words)))
        steps.append(PipelineStep("review", _make_review_full(target_words)))
    else:
        sections = _parse_sections(ctx.run_dir)
        if not sections:
            logger.warning("Could not parse sections -- falling back to short path")
            steps.append(PipelineStep("write", _make_write_full(target_words)))
            steps.append(PipelineStep("review", _make_review_full(target_words)))
        else:
            steps.append(
                PipelineStep("write", _make_write_sections(sections, target_words))
            )
            steps.append(
                PipelineStep("review", _make_review_sections(sections, target_words))
            )

    steps.append(PipelineStep("export", _do_export))
    return steps


def run_pipeline(
    worker: BaseChatModel,
    writer: BaseChatModel,
    reviewer: BaseChatModel,
    run_dir: Path,
    config: EssayWriterConfig,
    *,
    extra_prompt: str | None = None,
    callbacks: list | None = None,
    token_tracker=None,
    on_questions: Callable[[str, Path], None] | None = None,
) -> None:
    """Execute the essay writing pipeline.

    Phase 1 (fixed):  intake -> validate -> plan
    Phase 2 (dynamic): research -> read_sources -> write -> review -> export
    """
    ctx = PipelineContext(
        worker=worker,
        writer=writer,
        reviewer=reviewer,
        run_dir=run_dir,
        config=config,
        extra_prompt=extra_prompt,
        callbacks=callbacks,
        tracker=token_tracker,
    )

    # Ensure output subdirectories exist
    for subdir in ("brief", "plan", "sources", "essay"):
        (run_dir / subdir).mkdir(parents=True, exist_ok=True)

    # Phase 1a: intake + validate
    _execute([PipelineStep("intake", _do_intake)], ctx)
    _execute([PipelineStep("validate", _do_validate)], ctx)

    # Check validation result
    questions = _read_validation(run_dir)
    if questions and on_questions:
        on_questions(questions, run_dir)

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
    target_sources, fetch_sources = _compute_max_sources(target_words, config)
    phase2 = _build_execution_steps(ctx, target_words, fetch_sources, target_sources)
    _execute(phase2, ctx)
