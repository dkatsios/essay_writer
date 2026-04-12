"""Deterministic Python pipeline for essay writing."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from src.agent import _retry_with_backoff
from src.rendering import render_prompt
from src.schemas import AssignmentBrief, EssayPlan, ValidationQuestion, ValidationResult
from src.pipeline_sources import (
    _build_optional_pdf_prompt_payload,
    _lexical_relevance_score,
    _optional_pdf_corpus_tokens,
    _source_read_candidates,
    do_assign_sources,
    do_research,
    make_read_sources,
)
from src.pipeline_support import (
    PipelineContext,
    PipelineStep,
    Section,
    _async_structured_call as _support_async_structured_call,
    _build_prior_sections_context,
    _build_review_context,
    _compute_max_sources,
    _execute,
    _get_brief_language,
    _get_target_words,
    _load_selected_source_notes,
    _parse_sections,
    _read_text,
    _structured_call as _support_structured_call,
    _suggested_sources,
    _write_json,
)
from src.pipeline_writing import (
    do_export,
    make_review_full,
    make_review_sections,
    make_write_full,
    make_write_sections,
)

logger = logging.getLogger(__name__)


def _structured_call(client, prompt, schema, tracker=None, retries=2):
    def _do_call():
        return client.client.chat.completions.create(
            model=client.model,
            response_model=schema,
            max_retries=retries,
            messages=[{"role": "user", "content": prompt}],
        )

    result = _retry_with_backoff(_do_call)
    raw = getattr(result, "_raw_response", None)
    if raw and tracker is not None:
        usage = getattr(raw, "usage", None)
        if usage is not None:
            tracker.record(
                getattr(raw, "model", ""),
                getattr(usage, "prompt_tokens", 0) or 0,
                getattr(usage, "completion_tokens", 0) or 0,
                0,
            )
    return result


async def _async_structured_call(client, prompt, schema, tracker=None, retries=2):
    async def _do_call():
        return await client.client.chat.completions.create(
            model=client.model,
            response_model=schema,
            max_retries=retries,
            messages=[{"role": "user", "content": prompt}],
        )

    result = await _retry_with_backoff(_do_call, is_async=True)
    raw = getattr(result, "_raw_response", None)
    if raw and tracker is not None:
        usage = getattr(raw, "usage", None)
        if usage is not None:
            tracker.record(
                getattr(raw, "model", ""),
                getattr(usage, "prompt_tokens", 0) or 0,
                getattr(usage, "completion_tokens", 0) or 0,
                0,
            )
    return result


def _do_intake(ctx: PipelineContext) -> None:
    extracted_path = ctx.run_dir / "input" / "extracted.md"
    extracted_text = _read_text(extracted_path) if extracted_path.exists() else ""
    prompt = render_prompt(
        "intake.j2",
        extracted_text=extracted_text,
        extra_prompt=ctx.extra_prompt,
    )
    brief = _support_structured_call(ctx.worker, prompt, AssignmentBrief, ctx.tracker)
    _write_json(ctx.run_dir / "brief" / "assignment.json", brief)


def _do_validate(ctx: PipelineContext) -> None:
    brief_json = _read_text(ctx.run_dir / "brief" / "assignment.json")
    prompt = render_prompt(
        "validate.j2",
        brief_json=brief_json,
        language=_get_brief_language(ctx.run_dir),
    )
    result = _support_structured_call(ctx.worker, prompt, ValidationResult, ctx.tracker)
    _write_json(ctx.run_dir / "brief" / "validation.json", result)


def _do_plan(ctx: PipelineContext) -> None:
    brief_json = _read_text(ctx.run_dir / "brief" / "assignment.json")
    prompt = render_prompt(
        "plan.j2",
        brief_json=brief_json,
        language=_get_brief_language(ctx.run_dir),
    )
    plan = _support_structured_call(ctx.worker, prompt, EssayPlan, ctx.tracker)
    _write_json(ctx.run_dir / "plan" / "plan.json", plan)


def _read_validation(run_dir: Path) -> ValidationResult | None:
    path = run_dir / "brief" / "validation.json"
    if not path.exists():
        return None
    return ValidationResult.model_validate_json(path.read_text(encoding="utf-8"))


def _build_execution_steps(
    ctx: PipelineContext,
    target_words: int,
    fetch_sources: int,
    target_sources: int,
    citation_min_sources: int,
) -> list[PipelineStep]:
    threshold = ctx.config.writing.long_essay_threshold
    steps: list[PipelineStep] = [
        PipelineStep(
            "research", lambda current_ctx: do_research(current_ctx, fetch_sources)
        ),
        PipelineStep("read_sources", make_read_sources(target_sources)),
    ]

    if target_words <= threshold:
        steps.append(
            PipelineStep("write", make_write_full(target_words, citation_min_sources))
        )
        steps.append(
            PipelineStep("review", make_review_full(target_words, citation_min_sources))
        )
    else:
        sections = _parse_sections(ctx.run_dir)
        if not sections:
            logger.warning("Could not parse sections -- falling back to short path")
            steps.append(
                PipelineStep(
                    "write", make_write_full(target_words, citation_min_sources)
                )
            )
            steps.append(
                PipelineStep(
                    "review", make_review_full(target_words, citation_min_sources)
                )
            )
        else:
            steps.append(PipelineStep("assign_sources", do_assign_sources))
            steps.append(
                PipelineStep(
                    "write",
                    make_write_sections(sections, target_words, citation_min_sources),
                )
            )
            steps.append(
                PipelineStep("review", make_review_sections(sections, target_words))
            )

    steps.append(PipelineStep("export", do_export))
    return steps


def run_pipeline(
    worker,
    writer,
    reviewer,
    run_dir: Path,
    config,
    *,
    async_worker=None,
    extra_prompt: str | None = None,
    token_tracker=None,
    on_questions: Callable[[list[ValidationQuestion], Path], None] | None = None,
    on_optional_source_pdfs: Callable[[Path, list[dict]], None] | None = None,
    min_sources: int | None = None,
    user_sources_dir: Path | None = None,
) -> None:
    """Execute the essay writing pipeline."""
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

    for subdir in ("brief", "plan", "sources", "essay"):
        (run_dir / subdir).mkdir(parents=True, exist_ok=True)

    _execute([PipelineStep("intake", _do_intake)], ctx)

    if min_sources is not None:
        brief_path = run_dir / "brief" / "assignment.json"
        if brief_path.exists():
            brief = AssignmentBrief.model_validate_json(
                brief_path.read_text(encoding="utf-8")
            )
            brief.min_sources = min_sources
            brief_path.write_text(
                brief.model_dump_json(indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    _execute([PipelineStep("validate", _do_validate)], ctx)

    validation = _read_validation(run_dir)
    if validation and validation.questions and not validation.is_pass and on_questions:
        on_questions(validation.questions, run_dir)

    _execute([PipelineStep("plan", _do_plan)], ctx)

    target_words = _get_target_words(run_dir)
    threshold = config.writing.long_essay_threshold
    logger.info(
        "Target: %d words, threshold: %d -> %s path",
        target_words,
        threshold,
        "long" if target_words > threshold else "short",
    )

    user_min_sources = min_sources
    if user_min_sources is None:
        brief_path = run_dir / "brief" / "assignment.json"
        if brief_path.exists():
            brief = AssignmentBrief.model_validate_json(
                brief_path.read_text(encoding="utf-8")
            )
            user_min_sources = brief.min_sources

    target_sources, fetch_sources = _compute_max_sources(
        target_words,
        config,
        user_min_sources,
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
            brief.model_dump_json(indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    logger.info(
        "Sources: target=%d fetch=%d citation_minimum=%d",
        target_sources,
        fetch_sources,
        citation_min_sources,
    )

    _execute(
        _build_execution_steps(
            ctx,
            target_words,
            fetch_sources,
            target_sources,
            citation_min_sources,
        ),
        ctx,
    )
