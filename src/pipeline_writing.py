"""Writing and export steps for the deterministic essay pipeline."""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import monotonic

from src.rendering import render_prompt
from src.schemas import AssignmentBrief, EssayPlan, SourceAssignmentPlan
from src.tools.essay_sanitize import strip_leading_submission_metadata
from src.pipeline_support import (
    PipelineContext,
    Section,
    _build_prior_sections_context,
    _build_review_context,
    _get_brief_language,
    _load_selected_source_notes,
    _parse_sections,
    _plan_corpus_from_json,
    _rank_notes_by_corpus,
    _read_text,
    _section_window,
    _source_catalog_markdown,
    _split_writer_source_context,
    _text_call,
    _write_text,
)

logger = logging.getLogger(__name__)

_REVIEW_CONCURRENCY = 4


def _load_source_assignments(run_dir: Path) -> dict[int, list[str]]:
    path = run_dir / "plan" / "source_assignments.json"
    if not path.exists():
        return {}
    try:
        plan = SourceAssignmentPlan.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        return {
            assignment.section_number: assignment.source_ids
            for assignment in plan.assignments
        }
    except Exception:
        logger.warning("Failed to load source assignments")
        return {}


def make_write_full(
    target_words: int,
    citation_min_sources: int,
) -> Callable[[PipelineContext], None]:
    def _do_write_full(ctx: PipelineContext) -> None:
        brief_json = _read_text(ctx.run_dir / "brief" / "assignment.json")
        plan_json = _read_text(ctx.run_dir / "plan" / "plan.json")
        source_notes = _load_selected_source_notes(ctx.run_dir)
        language = _get_brief_language(ctx.run_dir)
        detail_notes, catalog_md, total_notes = _split_writer_source_context(
            _plan_corpus_from_json(plan_json),
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


def make_review_full(
    target_words: int,
    citation_min_sources: int,
) -> Callable[[PipelineContext], None]:
    def _do_review_full(ctx: PipelineContext) -> None:
        brief_json = _read_text(ctx.run_dir / "brief" / "assignment.json")
        plan_json = _read_text(ctx.run_dir / "plan" / "plan.json")
        draft = _read_text(ctx.run_dir / "essay" / "draft.md")
        draft_words = len(draft.split())
        language = _get_brief_language(ctx.run_dir)
        source_notes = _load_selected_source_notes(ctx.run_dir)
        catalog_md = _source_catalog_markdown(source_notes)
        uncited_ids = [
            note.source_id
            for note in source_notes
            if note.source_id
            not in set(re.findall(r"\[\[([^|\]]+?)(?:\|[^\]]*?)?\]\]", draft))
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
            total_selected_sources=len(source_notes),
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


def _writing_order(sections: list[Section]) -> list[Section]:
    body = [
        section
        for section in sections
        if not section.is_intro and not section.is_conclusion
    ]
    conclusion = [section for section in sections if section.is_conclusion]
    intro = [section for section in sections if section.is_intro]
    return body + conclusion + intro


def _section_filename(section: Section) -> str:
    return f"{section.number:02d}.md"


def make_write_sections(
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
        written_sections: list[tuple[Section, str]] = []
        section_assignments = _load_source_assignments(ctx.run_dir)
        notes_by_id = {note.source_id: note for note in source_notes}

        for section in _writing_order(sections):
            prior_context = _build_prior_sections_context(written_sections)
            section_corpus = (
                f"{section.title} {section.key_points} {section.content_outline or ''}"
            )
            assigned_ids = section_assignments.get(section.number, [])

            if assigned_ids:
                assigned_notes = [
                    notes_by_id[source_id]
                    for source_id in assigned_ids
                    if source_id in notes_by_id
                ]
                remaining = [
                    note
                    for note in source_notes
                    if note.source_id not in set(assigned_ids)
                ]
                detail_notes = (
                    assigned_notes
                    + _rank_notes_by_corpus(section_corpus, remaining)[
                        : max(0, budget - len(assigned_notes))
                    ]
                )
                catalog_md = _source_catalog_markdown(source_notes)
                total_notes = len(source_notes)
            else:
                detail_notes, catalog_md, total_notes = _split_writer_source_context(
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

            start = monotonic()
            text = _text_call(
                ctx.writer,
                f"You are an expert academic writer producing essays in {language}.",
                prompt,
                ctx.tracker,
            )
            duration = monotonic() - start

            _write_text(sections_dir / _section_filename(section), text)
            if ctx.tracker is not None:
                ctx.tracker.record_duration(tracker_step, duration)
            print(
                f"    section {section.number} ({section.title}) -- {duration:.1f}s",
                file=sys.stderr,
            )
            written_sections.append((section, text))

        draft_parts = []
        for section in sorted(sections, key=lambda item: item.number):
            section_path = sections_dir / _section_filename(section)
            if section_path.exists():
                draft_parts.append(section_path.read_text(encoding="utf-8"))
            else:
                logger.warning(
                    "Section %d file missing: %s", section.number, section_path
                )

        _write_text(
            ctx.run_dir / "essay" / "draft.md",
            strip_leading_submission_metadata("\n\n".join(draft_parts)),
        )
        logger.info("Combined %d sections into draft.md", len(draft_parts))

    return _do_write_sections


def make_review_sections(
    sections: list[Section],
    target_words: int,
) -> Callable[[PipelineContext], None]:
    def _do_review_sections(ctx: PipelineContext) -> None:
        _ = target_words
        sections_dir = ctx.run_dir / "essay" / "sections"
        reviewed_dir = ctx.run_dir / "essay" / "reviewed"
        reviewed_dir.mkdir(parents=True, exist_ok=True)
        plan_order = sorted(sections, key=lambda item: item.number)
        language = _get_brief_language(ctx.run_dir)

        draft_texts: dict[int, str] = {}
        for section in plan_order:
            section_path = sections_dir / _section_filename(section)
            if section_path.exists():
                draft_texts[section.number] = section_path.read_text(encoding="utf-8")

        def _review_one(section: Section) -> tuple[Section, str, float]:
            if section.number not in draft_texts:
                logger.warning("Section %d missing, skipping review", section.number)
                return section, "", 0.0

            section_text = draft_texts[section.number]
            full_essay = _build_review_context(
                section,
                plan_order,
                {
                    sibling.number: draft_texts[sibling.number]
                    for sibling in _section_window(plan_order, section.number)
                    if sibling.number in draft_texts
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
            )

            tracker_step = f"review:{section.number}"
            if ctx.tracker is not None:
                ctx.tracker.set_current_step(tracker_step)

            start = monotonic()
            reviewed = _text_call(
                ctx.reviewer,
                f"You are an expert academic editor polishing essays in {language}.",
                prompt,
                ctx.tracker,
            )
            duration = monotonic() - start

            _write_text(reviewed_dir / _section_filename(section), reviewed)
            if ctx.tracker is not None:
                ctx.tracker.record_duration(tracker_step, duration)
            return section, reviewed, duration

        with ThreadPoolExecutor(max_workers=_REVIEW_CONCURRENCY) as pool:
            futures = {
                pool.submit(_review_one, section): section for section in plan_order
            }
            for future in as_completed(futures):
                section, _, duration = future.result()
                if duration > 0:
                    print(
                        f"    section {section.number} ({section.title}) -- {duration:.1f}s",
                        file=sys.stderr,
                    )

        reviewed_parts = []
        for section in plan_order:
            reviewed_path = reviewed_dir / _section_filename(section)
            if reviewed_path.exists():
                reviewed_parts.append(reviewed_path.read_text(encoding="utf-8"))
            elif section.number in draft_texts:
                reviewed_parts.append(draft_texts[section.number])

        _write_text(
            ctx.run_dir / "essay" / "reviewed.md",
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
        essay_path = ctx.run_dir / "essay" / name
        if essay_path.exists():
            essay_text = essay_path.read_text(encoding="utf-8")
            break
    if not essay_text:
        logger.error("No essay found -- cannot export.")
        return

    essay_text = strip_leading_submission_metadata(essay_text)

    sources: dict = {}
    for source_name in ("selected.json", "registry.json"):
        source_path = ctx.run_dir / "sources" / source_name
        if source_path.exists():
            sources = json.loads(source_path.read_text(encoding="utf-8"))
            break

    doc_config = ctx.config.formatting.model_dump()
    brief_path = ctx.run_dir / "brief" / "assignment.json"
    plan_path = ctx.run_dir / "plan" / "plan.json"

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

    # Write to run_dir first (per-job, safe under concurrency).
    run_docx = ctx.run_dir / "essay.docx"
    run_docx.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(run_docx))
    logger.info("essay.docx saved to %s", run_docx)
    print(f"  essay.docx -> {run_docx}", file=sys.stderr)

    # Also copy to the shared output_dir for CLI convenience.
    output_path = Path(ctx.config.paths.output_dir) / "essay.docx"
    if output_path.resolve() != run_docx.resolve():
        import shutil

        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(run_docx), str(output_path))
