"""Deterministic Python pipeline for essay writing."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from src.rendering import render_prompt
from src.schemas import AssignmentBrief, EssayPlan, ValidationQuestion, ValidationResult
from src.pipeline_sources import (
    do_assign_sources,
    do_research,
    make_read_sources,
)
from src.pipeline_support import (
    PipelineContext,
    PipelineStep,
    async_structured_call,
    compute_max_sources,
    execute,
    get_brief_language,
    get_target_words,
    load_checkpoint,
    parse_sections,
    read_text,
    write_json,
)
from src.pipeline_writing import (
    do_export,
    make_reconcile_sections,
    make_review_full,
    make_review_sections,
    make_write_full,
    make_write_sections,
)

logger = logging.getLogger(__name__)


async def _do_intake(ctx: PipelineContext) -> None:
    extracted_path = ctx.run_dir / "input" / "extracted.md"
    extracted_text = read_text(extracted_path) if extracted_path.exists() else ""
    prompt = render_prompt(
        "intake.j2",
        extracted_text=extracted_text,
        extra_prompt=ctx.extra_prompt,
    )
    brief = await async_structured_call(
        ctx.async_worker, prompt, AssignmentBrief, ctx.tracker
    )
    write_json(ctx.run_dir / "brief" / "assignment.json", brief)


async def _do_validate(ctx: PipelineContext) -> None:
    brief_json = read_text(ctx.run_dir / "brief" / "assignment.json")
    prompt = render_prompt(
        "validate.j2",
        brief_json=brief_json,
        language=get_brief_language(ctx.run_dir),
    )
    result = await async_structured_call(
        ctx.async_worker, prompt, ValidationResult, ctx.tracker
    )
    write_json(ctx.run_dir / "brief" / "validation.json", result)


async def _do_plan(ctx: PipelineContext) -> None:
    brief_json = read_text(ctx.run_dir / "brief" / "assignment.json")
    prompt = render_prompt(
        "plan.j2",
        brief_json=brief_json,
        language=get_brief_language(ctx.run_dir),
    )
    plan = await async_structured_call(
        ctx.async_worker, prompt, EssayPlan, ctx.tracker
    )
    write_json(ctx.run_dir / "plan" / "plan.json", plan)


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
        PipelineStep("read_sources", make_read_sources(target_sources, fetch_sources)),
    ]

    if target_words <= threshold:
        steps.append(
            PipelineStep("write", make_write_full(target_words, citation_min_sources))
        )
        steps.append(
            PipelineStep("review", make_review_full(target_words, citation_min_sources))
        )
    else:
        sections = parse_sections(ctx.run_dir)
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
                PipelineStep(
                    "reconcile_sections",
                    make_reconcile_sections(sections, target_words),
                )
            )
            steps.append(
                PipelineStep("review", make_review_sections(sections, target_words))
            )

    steps.append(PipelineStep("export", do_export))
    return steps


async def run_pipeline(
    worker,
    writer,
    reviewer,
    run_dir: Path,
    config,
    *,
    async_worker=None,
    async_writer=None,
    async_reviewer=None,
    extra_prompt: str | None = None,
    token_tracker=None,
    on_questions: Callable[[list[ValidationQuestion], Path], Awaitable[None]]
    | None = None,
    on_optional_source_pdfs: Callable[[Path, list[dict]], Awaitable[None]]
    | None = None,
    on_source_shortfall: Callable[[Path, dict], Awaitable[tuple[bool, list[str]]]]
    | None = None,
    min_sources: int | None = None,
    user_sources_dir: Path | None = None,
    resume: bool = False,
) -> None:
    """Execute the essay writing pipeline.

    All callbacks (``on_questions``, ``on_optional_source_pdfs``,
    ``on_source_shortfall``) must be async callables.  They are
    ``await``-ed directly by the pipeline steps.
    """
    # Ensure async clients exist for all roles.
    if async_worker is None:
        if worker is None:
            raise ValueError("Either async_worker or worker must be provided")
        async_worker = worker.to_async()
    if async_writer is None:
        if writer is None:
            raise ValueError("Either async_writer or writer must be provided")
        async_writer = writer.to_async()
    if async_reviewer is None:
        if reviewer is None:
            raise ValueError("Either async_reviewer or reviewer must be provided")
        async_reviewer = reviewer.to_async()

    checkpoint = load_checkpoint(run_dir) if resume else set()
    if checkpoint:
        logger.info("Resuming — completed steps: %s", ", ".join(sorted(checkpoint)))

    ctx = PipelineContext(
        worker=worker,
        async_worker=async_worker,
        writer=writer,
        reviewer=reviewer,
        run_dir=run_dir,
        config=config,
        async_writer=async_writer,
        async_reviewer=async_reviewer,
        extra_prompt=extra_prompt,
        tracker=token_tracker,
        user_sources_dir=user_sources_dir,
        on_optional_source_pdfs=on_optional_source_pdfs,
        on_source_shortfall=on_source_shortfall,
    )

    for subdir in ("brief", "plan", "sources", "essay"):
        (run_dir / subdir).mkdir(parents=True, exist_ok=True)

    # Preliminary step count for progress (intake + validate + plan).
    preliminary_total = 3

    await execute(
        [PipelineStep("intake", _do_intake)],
        ctx,
        checkpoint=checkpoint,
        step_offset=0,
        total_steps=preliminary_total,
    )

    # Apply user-supplied min_sources to the brief before validation.
    brief_path = run_dir / "brief" / "assignment.json"
    if min_sources is not None and brief_path.exists():
        brief = AssignmentBrief.model_validate_json(
            brief_path.read_text(encoding="utf-8")
        )
        brief.min_sources = min_sources
        brief_path.write_text(
            brief.model_dump_json(indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    await execute(
        [PipelineStep("validate", _do_validate)],
        ctx,
        checkpoint=checkpoint,
        step_offset=1,
        total_steps=preliminary_total,
    )

    # Skip Q&A callback if plan already exists (implies Q&A was handled).
    if "plan" not in checkpoint:
        validation = _read_validation(run_dir)
        if (
            validation
            and validation.questions
            and not validation.is_pass
            and on_questions
        ):
            await on_questions(validation.questions, run_dir)

    await execute(
        [PipelineStep("plan", _do_plan)],
        ctx,
        checkpoint=checkpoint,
        step_offset=2,
        total_steps=preliminary_total,
    )

    target_words = get_target_words(run_dir)
    threshold = config.writing.long_essay_threshold
    logger.info(
        "Target: %d words, threshold: %d -> %s path",
        target_words,
        threshold,
        "long" if target_words > threshold else "short",
    )

    # Compute source counts once, read the brief once, write once.
    brief = AssignmentBrief.model_validate_json(
        brief_path.read_text(encoding="utf-8")
    )
    user_min_sources = min_sources
    if user_min_sources is None:
        user_min_sources = brief.min_sources

    target_sources, fetch_sources = compute_max_sources(
        target_words,
        config,
        user_min_sources,
    )
    citation_min_sources = max(
        target_sources,
        user_min_sources if user_min_sources is not None else 0,
    )

    brief.min_sources = citation_min_sources
    brief_path.write_text(
        brief.model_dump_json(indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    ctx.brief = brief

    logger.info(
        "Sources: target=%d citation_minimum=%d",
        target_sources,
        citation_min_sources,
    )

    execution_steps = _build_execution_steps(
        ctx,
        target_words,
        fetch_sources,
        target_sources,
        citation_min_sources,
    )
    total_steps = 3 + len(execution_steps)

    await execute(
        execution_steps,
        ctx,
        checkpoint=checkpoint,
        step_offset=3,
        total_steps=total_steps,
    )
