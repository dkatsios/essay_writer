"""Writing and export steps for the deterministic essay pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from time import monotonic

from src.rendering import render_prompt
from src.schemas import (
    EssayPlan,
    EssayReconciliationPlan,
    SectionReconciliationNotes,
    SourceAssignmentPlan,
)
from src.tools.essay_sanitize import strip_leading_submission_metadata
from src.pipeline_support import (
    AnyStorage,
    PipelineContext,
    Section,
    async_structured_call,
    async_text_call,
    build_prior_sections_context,
    build_review_context,
    load_selected_source_notes,
    plan_corpus_from_json,
    rank_notes_by_corpus,
    read_text,
    section_window,
    source_catalog_markdown,
    split_writer_source_context,
    write_json,
    write_text,
)

logger = logging.getLogger(__name__)

_WRITE_CONCURRENCY = 8
_REVIEW_CONCURRENCY = 4


def _effective_min_sources(citation_min_sources: int, source_notes: list) -> int:
    return min(citation_min_sources, len(source_notes))


def _load_source_assignments(
    storage: AnyStorage, sections: list[Section]
) -> dict[int, list[str]]:
    if not storage.exists("plan/source_assignments.json"):
        return {}
    try:
        plan = SourceAssignmentPlan.model_validate_json(
            storage.read_text("plan/source_assignments.json")
        )
        position_set = {section.position for section in sections}
        aligned: dict[int, list[str]] = {}
        for assignment in plan.assignments:
            if assignment.section_position in position_set:
                aligned[assignment.section_position] = assignment.source_ids
        return aligned
    except Exception:
        logger.warning("Failed to load source assignments")
        return {}


def make_write_full(
    target_words: int,
    citation_min_sources: int,
) -> Callable:
    async def _do_write_full(ctx: PipelineContext) -> None:
        brief_json = ctx.brief.model_dump_json(indent=2, ensure_ascii=False)
        plan_json = read_text(ctx.storage, "plan/plan.json")
        source_notes = load_selected_source_notes(ctx.storage)
        language = ctx.brief.language
        min_sources = _effective_min_sources(citation_min_sources, source_notes)
        detail_notes, catalog_md, total_notes = split_writer_source_context(
            plan_corpus_from_json(plan_json),
            source_notes,
            ctx.config.search.section_source_full_detail_max,
        )

        prompt = render_prompt(
            "essay_writing.j2",
            brief_json=brief_json,
            plan_json=plan_json,
            source_notes=detail_notes,
            source_catalog=catalog_md,
            total_selected_sources=total_notes,
            target_words=target_words,
            tolerance_percent=round(ctx.config.writing.word_count_tolerance * 100),
            min_words=round(
                target_words * (1 - ctx.config.writing.word_count_tolerance)
            ),
            language=language,
            min_sources=min_sources,
        )

        essay = await async_text_call(
            ctx.async_writer,
            prompt,
            ctx.tracker,
        )
        write_text(
            ctx.storage,
            "essay/draft.md",
            strip_leading_submission_metadata(essay),
        )

    return _do_write_full


def make_review_full(
    target_words: int,
    citation_min_sources: int,
) -> Callable:
    async def _do_review_full(ctx: PipelineContext) -> None:
        brief_json = ctx.brief.model_dump_json(indent=2, ensure_ascii=False)
        plan_json = read_text(ctx.storage, "plan/plan.json")
        draft = read_text(ctx.storage, "essay/draft.md")
        draft_words = len(draft.split())
        language = ctx.brief.language
        source_notes = load_selected_source_notes(ctx.storage)
        min_sources = _effective_min_sources(citation_min_sources, source_notes)

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
            min_sources=min_sources,
        )

        reviewed = await async_text_call(
            ctx.async_reviewer,
            prompt,
            ctx.tracker,
        )
        write_text(
            ctx.storage,
            "essay/reviewed.md",
            strip_leading_submission_metadata(reviewed),
        )

    return _do_review_full


def _section_filename(section: Section) -> str:
    return f"{section.position:02d}.md"


def _truncate_at_next_section(
    text: str, section: Section, sections: list[Section]
) -> str:
    """Strip content belonging to a later section if the reviewer overstepped."""
    idx = next(i for i, s in enumerate(sections) if s.position == section.position)
    for later in sections[idx + 1 :]:
        for marker in (later.heading, f"## {later.title}", f"### {later.title}"):
            if marker and marker in text:
                truncated = text[: text.index(marker)].rstrip()
                logger.warning(
                    "Reviewer for section %d overstepped into section %d; truncated",
                    section.position,
                    later.position,
                )
                return truncated
    return text


def partition_sections_for_writing(
    sections: list[Section],
) -> tuple[list[Section], list[Section]]:
    parallel_sections = [
        section for section in sections if not section.requires_full_context
    ]
    deferred_sections = [
        section for section in sections if section.requires_full_context
    ]

    def _deferred_sort_key(section: Section) -> tuple[int, int]:
        order = section.deferred_order if section.deferred_order is not None else 0
        return (order, section.position)

    return parallel_sections, sorted(deferred_sections, key=_deferred_sort_key)


def _load_section_drafts(
    storage: AnyStorage, sections: list[Section]
) -> dict[int, str]:
    drafts: dict[int, str] = {}
    for section in sections:
        subpath = f"essay/sections/{_section_filename(section)}"
        if storage.exists(subpath):
            drafts[section.position] = storage.read_text(subpath)
    return drafts


def _build_full_draft_context(
    sections: list[Section],
    written_sections: list[tuple[Section, str]],
) -> str:
    if not written_sections:
        return ""

    written_by_position = {
        section.position: text for section, text in written_sections if text
    }
    ordered_written = [
        (section, written_by_position[section.position])
        for section in sections
        if section.position in written_by_position
    ]
    return build_prior_sections_context(
        ordered_written,
        max_sections=len(ordered_written),
    )


def _load_reconciliation_notes(
    storage: AnyStorage,
) -> dict[int, SectionReconciliationNotes]:
    if not storage.exists("essay/reconciliation.json"):
        return {}

    try:
        plan = EssayReconciliationPlan.model_validate_json(
            storage.read_text("essay/reconciliation.json")
        )
    except Exception:
        logger.warning("Failed to load reconciliation notes")
        return {}

    return {notes.section_position: notes for notes in plan.sections}


def _normalize_reconciliation_plan(
    sections: list[Section],
    plan: EssayReconciliationPlan,
) -> EssayReconciliationPlan:
    notes_by_position = {
        notes.section_position: notes
        for notes in plan.sections
        if any(section.position == notes.section_position for section in sections)
    }
    normalized_sections = [
        notes_by_position.get(
            section.position,
            SectionReconciliationNotes(
                section_position=section.position,
                title=section.title,
                instructions=[],
            ),
        )
        for section in sections
    ]
    return EssayReconciliationPlan(
        global_notes=plan.global_notes,
        sections=normalized_sections,
    )


async def _write_section_draft(
    ctx: PipelineContext,
    section: Section,
    *,
    plan_json: str,
    source_notes: list,
    notes_by_id: dict[str, object],
    section_assignments: dict[int, list[str]],
    budget: int,
    language: str,
    min_sources: int,
    essay_context: str,
    tracker_step: str | None = None,
) -> tuple[Section, str, float]:
    section_corpus = (
        f"{section.title} {section.key_points} {section.content_outline or ''}"
    )
    assigned_ids = section_assignments.get(section.position, [])

    if assigned_ids:
        assigned_notes = [
            notes_by_id[source_id]
            for source_id in assigned_ids
            if source_id in notes_by_id
        ]
        remaining = [
            note for note in source_notes if note.source_id not in set(assigned_ids)
        ]
        detail_notes = (
            assigned_notes
            + rank_notes_by_corpus(section_corpus, remaining)[
                : max(0, budget - len(assigned_notes))
            ]
        )
        catalog_md = source_catalog_markdown(source_notes)
        total_notes = len(source_notes)
    else:
        detail_notes, catalog_md, total_notes = split_writer_source_context(
            section_corpus,
            source_notes,
            budget,
        )

    prompt = render_prompt(
        "section_writing.j2",
        plan_json=plan_json,
        source_notes=detail_notes,
        source_catalog=catalog_md,
        total_selected_sources=total_notes,
        section=section,
        assigned_source_ids=assigned_ids,
        tolerance_percent=round(ctx.config.writing.word_count_tolerance * 100),
        min_words=round(
            section.word_target * (1 - ctx.config.writing.word_count_tolerance)
        ),
        language=language,
        min_sources=min_sources,
        has_full_context=bool(essay_context),
        essay_context=essay_context,
    )

    tracker_step = tracker_step or f"write:{section.position}"
    if ctx.tracker is not None:
        ctx.tracker.set_current_step(tracker_step)

    start = monotonic()
    text = await async_text_call(
        ctx.async_writer,
        prompt,
        ctx.tracker,
    )
    duration = monotonic() - start

    write_text(ctx.storage, f"essay/sections/{_section_filename(section)}", text)
    if ctx.tracker is not None:
        ctx.tracker.record_duration(tracker_step, duration)
    return section, text, duration


def make_write_sections(
    sections: list[Section],
    target_words: int,
    citation_min_sources: int,
) -> Callable:
    async def _do_write_sections(ctx: PipelineContext) -> None:
        plan_json = read_text(ctx.storage, "plan/plan.json")
        source_notes = load_selected_source_notes(ctx.storage)
        language = ctx.brief.language
        budget = ctx.config.search.section_source_full_detail_max
        min_sources = _effective_min_sources(citation_min_sources, source_notes)
        written_sections: list[tuple[Section, str]] = []
        section_assignments = _load_source_assignments(ctx.storage, sections)
        notes_by_id = {note.source_id: note for note in source_notes}

        parallel_sections, deferred_sections = partition_sections_for_writing(sections)
        ordered = parallel_sections + deferred_sections
        if ctx.tracker is not None:
            ctx.tracker.set_sub_total(len(ordered))

        if parallel_sections:
            sem = asyncio.Semaphore(_WRITE_CONCURRENCY)

            async def _bounded_write(sec: Section) -> tuple[Section, str, float]:
                async with sem:
                    result = await _write_section_draft(
                        ctx,
                        sec,
                        plan_json=plan_json,
                        source_notes=source_notes,
                        notes_by_id=notes_by_id,
                        section_assignments=section_assignments,
                        budget=budget,
                        language=language,
                        min_sources=min_sources,
                        essay_context="",
                        tracker_step="write",
                    )
                    if ctx.tracker is not None:
                        ctx.tracker.increment_sub_done()
                    return result

            results = await asyncio.gather(
                *[_bounded_write(section) for section in parallel_sections]
            )
            for section, text, duration in results:
                logger.info(
                    "section %s (%s) -- %.1fs",
                    section.number,
                    section.title,
                    duration,
                )
                written_sections.append((section, text))

        for section in deferred_sections:
            essay_context = _build_full_draft_context(sections, written_sections)
            section, text, duration = await _write_section_draft(
                ctx,
                section,
                plan_json=plan_json,
                source_notes=source_notes,
                notes_by_id=notes_by_id,
                section_assignments=section_assignments,
                budget=budget,
                language=language,
                min_sources=min_sources,
                essay_context=essay_context,
            )
            logger.info(
                "section %s (%s) -- %.1fs",
                section.number,
                section.title,
                duration,
            )
            written_sections.append((section, text))
            if ctx.tracker is not None:
                ctx.tracker.increment_sub_done()

        draft_parts = []
        for section in sections:
            subpath = f"essay/sections/{_section_filename(section)}"
            if ctx.storage.exists(subpath):
                draft_parts.append(ctx.storage.read_text(subpath))
            else:
                logger.warning(
                    "Section %d at position %d file missing: %s",
                    section.number,
                    section.position,
                    subpath,
                )

        write_text(
            ctx.storage,
            "essay/draft.md",
            strip_leading_submission_metadata("\n\n".join(draft_parts)),
        )
        logger.info("Combined %d sections into draft.md", len(draft_parts))

    return _do_write_sections


def make_reconcile_sections(
    sections: list[Section],
    target_words: int,
) -> Callable:
    async def _do_reconcile_sections(ctx: PipelineContext) -> None:
        plan_json = read_text(ctx.storage, "plan/plan.json")
        language = ctx.brief.language
        draft_texts = _load_section_drafts(ctx.storage, sections)

        drafted_sections = [
            {
                "position": section.position,
                "number": section.number,
                "title": section.title,
                "heading": section.heading,
                "word_target": section.word_target,
                "requires_full_context": section.requires_full_context,
                "text": draft_texts.get(section.position, ""),
            }
            for section in sections
            if draft_texts.get(section.position)
        ]
        if not drafted_sections:
            write_json(
                ctx.storage,
                "essay/reconciliation.json",
                EssayReconciliationPlan(global_notes=[], sections=[]),
            )
            return

        prompt = render_prompt(
            "section_reconciliation.j2",
            plan_json=plan_json,
            drafted_sections=drafted_sections,
            language=language,
        )
        plan = await async_structured_call(
            ctx.async_worker,
            prompt,
            EssayReconciliationPlan,
            ctx.tracker,
        )
        normalized = _normalize_reconciliation_plan(sections, plan)
        write_json(ctx.storage, "essay/reconciliation.json", normalized)

    return _do_reconcile_sections


def make_review_sections(
    sections: list[Section],
    target_words: int,
) -> Callable:
    async def _do_review_sections(ctx: PipelineContext) -> None:
        _ = target_words
        plan_order = list(sections)
        language = ctx.brief.language
        reconciliation_notes = _load_reconciliation_notes(ctx.storage)

        draft_texts = _load_section_drafts(ctx.storage, plan_order)

        if ctx.tracker is not None:
            ctx.tracker.set_sub_total(len(plan_order))

        async def _review_one(section: Section) -> tuple[Section, str, float]:
            if section.position not in draft_texts:
                logger.warning("Section %d missing, skipping review", section.number)
                return section, "", 0.0

            section_text = draft_texts[section.position]
            notes = reconciliation_notes.get(section.position)
            full_essay = build_review_context(
                section,
                plan_order,
                {
                    sibling.position: draft_texts[sibling.position]
                    for sibling in section_window(plan_order, section.position)
                    if sibling.position in draft_texts
                },
            )

            prompt = render_prompt(
                "section_review.j2",
                section=section,
                full_essay=full_essay,
                section_words=len(section_text.split()),
                tolerance_ratio=ctx.config.writing.word_count_tolerance,
                tolerance_percent=round(ctx.config.writing.word_count_tolerance * 100),
                tolerance_ratio_over=ctx.config.writing.word_count_tolerance_over,
                tolerance_percent_over=round(
                    ctx.config.writing.word_count_tolerance_over * 100
                ),
                language=language,
                reconciliation_instructions=(
                    notes.instructions if notes is not None else []
                ),
            )

            tracker_step = f"review:{section.position}"
            if ctx.tracker is not None:
                ctx.tracker.set_current_step("review")

            start = monotonic()
            reviewed = await async_text_call(
                ctx.async_reviewer,
                prompt,
                ctx.tracker,
            )
            duration = monotonic() - start

            reviewed = _truncate_at_next_section(reviewed, section, plan_order)
            write_text(
                ctx.storage, f"essay/reviewed/{_section_filename(section)}", reviewed
            )
            if ctx.tracker is not None:
                ctx.tracker.record_duration(tracker_step, duration)
            return section, reviewed, duration

        sem = asyncio.Semaphore(_REVIEW_CONCURRENCY)

        async def _bounded_review(sec: Section) -> tuple[Section, str, float]:
            async with sem:
                result = await _review_one(sec)
                if ctx.tracker is not None:
                    ctx.tracker.increment_sub_done()
                return result

        results = await asyncio.gather(
            *[_bounded_review(section) for section in plan_order]
        )
        for section, _, duration in results:
            if duration > 0:
                logger.info(
                    "section %s (%s) -- %.1fs",
                    section.number,
                    section.title,
                    duration,
                )

        reviewed_parts = []
        for section in plan_order:
            reviewed_subpath = f"essay/reviewed/{_section_filename(section)}"
            if ctx.storage.exists(reviewed_subpath):
                reviewed_parts.append(ctx.storage.read_text(reviewed_subpath))
            elif section.position in draft_texts:
                reviewed_parts.append(draft_texts[section.position])

        write_text(
            ctx.storage,
            "essay/reviewed.md",
            strip_leading_submission_metadata("\n\n".join(reviewed_parts)),
        )
        logger.info(
            "Combined %d reviewed sections into reviewed.md", len(reviewed_parts)
        )

    return _do_review_sections


def do_export(ctx: PipelineContext) -> None:
    from src.tools.docx_builder import build_document

    essay_text = None
    for name in ("reviewed.md", "draft.md"):
        subpath = f"essay/{name}"
        if ctx.storage.exists(subpath):
            essay_text = ctx.storage.read_text(subpath)
            break
    if not essay_text:
        logger.error("No essay found -- cannot export.")
        return

    essay_text = strip_leading_submission_metadata(essay_text)

    sources: dict = {}
    for source_name in ("selected.json", "registry.json"):
        subpath = f"sources/{source_name}"
        if ctx.storage.exists(subpath):
            sources = json.loads(ctx.storage.read_text(subpath))
            break

    doc_config = ctx.config.formatting.model_dump()

    if ctx.storage.exists("plan/plan.json"):
        plan = EssayPlan.model_validate_json(ctx.storage.read_text("plan/plan.json"))
        if plan.title:
            doc_config.setdefault("title", plan.title)

    brief = ctx.brief
    if brief is not None:
        doc_config.setdefault("title", brief.topic)
        if brief.student:
            doc_config.setdefault("author", brief.student)
        if brief.institution:
            doc_config.setdefault("institution", brief.institution)
        if brief.course:
            doc_config.setdefault("course", brief.course)
        if brief.professor:
            doc_config.setdefault("professor", brief.professor)
        if "date" not in doc_config:
            from datetime import date as _date

            months = {
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
            month_names = months.get(brief.language, months["English"])
            doc_config["date"] = f"{month_names[today.month]} {today.year}"

    document = build_document(essay_text, doc_config, sources)

    # Save docx to storage via in-memory buffer.
    from io import BytesIO

    buf = BytesIO()
    document.save(buf)
    ctx.storage.write_bytes("essay.docx", buf.getvalue())
    logger.info("essay.docx saved to storage")
