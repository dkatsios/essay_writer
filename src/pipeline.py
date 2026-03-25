"""Deterministic Python pipeline for essay writing.

Two-phase execution:
  Phase 1 (fixed):  intake -> plan
  Phase 2 (dynamic): steps built from plan analysis (short vs long path)

Each step is a callable wrapped in a PipelineStep dataclass.
The executor iterates steps with timing, tracking, and error handling.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

from langchain_core.messages import HumanMessage

if TYPE_CHECKING:
    from collections.abc import Callable

    from config.schemas import EssayWriterConfig
    from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class PipelineContext:
    """Shared state passed to every step."""

    worker: CompiledStateGraph
    writer: CompiledStateGraph
    run_dir: Path
    config: EssayWriterConfig
    extra_prompt: str | None = None
    callbacks: list | None = None
    tracker: object | None = None  # TokenTracker (optional)


@dataclass
class Section:
    """A single section parsed from plan.md."""

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
# Low-level agent invocation
# ---------------------------------------------------------------------------


def _invoke(
    agent: CompiledStateGraph,
    thread_id: str,
    message: str,
    callbacks: list | None = None,
) -> dict:
    """Invoke an agent with a single message."""
    config: dict = {"configurable": {"thread_id": thread_id}}
    if callbacks:
        config["callbacks"] = callbacks
    return agent.invoke(
        {"messages": [HumanMessage(content=message)]},
        config=config,
    )


# ---------------------------------------------------------------------------
# Plan parsing
# ---------------------------------------------------------------------------


def _get_target_words(run_dir: Path) -> int:
    """Sum word targets from plan.md."""
    plan = run_dir / "plan" / "plan.md"
    if not plan.exists():
        return 0
    total = 0
    for m in re.finditer(
        r"\*\*Word target\*\*:\s*(\d[\d,]*)", plan.read_text(encoding="utf-8")
    ):
        total += int(m.group(1).replace(",", ""))
    return total


def _parse_sections(run_dir: Path) -> list[Section]:
    """Parse section metadata from plan.md."""
    plan_path = run_dir / "plan" / "plan.md"
    if not plan_path.exists():
        return []

    text = plan_path.read_text(encoding="utf-8")
    sections: list[Section] = []

    # Split on section headers: ### N. Title
    section_pattern = re.compile(r"^###\s+(\d+)\.\s+(.+?)$", re.MULTILINE)
    matches = list(section_pattern.finditer(text))

    for i, match in enumerate(matches):
        number = int(match.group(1))
        title = match.group(2).strip()

        # Extract content between this header and the next
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end]

        # Stop at "## Research Queries" if present
        rq_match = re.search(r"^##\s+Research", block, re.MULTILINE)
        if rq_match:
            block = block[: rq_match.start()]

        # Extract word target
        wt_match = re.search(r"\*\*Word target\*\*:\s*(\d[\d,]*)", block)
        word_target = int(wt_match.group(1).replace(",", "")) if wt_match else 0

        # Extract heading
        heading_match = re.search(r"\*\*Heading\*\*:\s*(.+)", block)
        heading = (
            heading_match.group(1).strip() if heading_match else f"# {number}. {title}"
        )

        # Extract key points
        kp_match = re.search(r"\*\*Key points\*\*:\s*(.+)", block)
        key_points = kp_match.group(1).strip() if kp_match else ""

        # Extract content outline (may span multiple lines)
        co_match = re.search(
            r"\*\*Content outline\*\*:\s*(.*?)(?=\n-\s*\*\*|\n###|\Z)",
            block,
            re.DOTALL,
        )
        content_outline = co_match.group(1).strip() if co_match else ""

        is_intro = (
            number == 1
            or "introduction" in title.lower()
            or "\u03b5\u03b9\u03c3\u03b1\u03b3\u03c9\u03b3" in title.lower()
        )
        is_conclusion = (
            "conclusion" in title.lower()
            or "\u03c3\u03c5\u03bc\u03c0\u03ad\u03c1\u03b1\u03c3\u03bc" in title.lower()
        )

        sections.append(
            Section(
                number=number,
                title=title,
                heading=heading,
                word_target=word_target,
                key_points=key_points,
                content_outline=content_outline,
                is_intro=is_intro,
                is_conclusion=is_conclusion,
            )
        )

    return sections


def _compute_max_sources(target_words: int, config: EssayWriterConfig) -> int:
    """Compute max_sources based on target word count and config."""
    sc = config.search
    raw = math.ceil(target_words / 1000) * sc.sources_per_1k_words
    return max(sc.min_sources, min(raw, sc.max_sources))


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------


def _do_intake(ctx: PipelineContext) -> None:
    extra = (
        f"\nAdditional instructions from the user: {ctx.extra_prompt}"
        if ctx.extra_prompt
        else ""
    )
    _invoke(
        ctx.worker,
        "intake",
        f"Read /skills/worker/intake/SKILL.md. "
        f"Read extracted content from /input/extracted.md.{extra}",
        ctx.callbacks,
    )


def _do_plan(ctx: PipelineContext) -> None:
    _invoke(
        ctx.worker,
        "plan",
        "Read /skills/worker/essay-planning/SKILL.md. "
        "The brief is at /brief/assignment.md.",
        ctx.callbacks,
    )


def _make_research(max_sources: int) -> Callable[[PipelineContext], None]:
    def _do_research(ctx: PipelineContext) -> None:
        _invoke(
            ctx.worker,
            "research",
            f"Read /skills/worker/research/SKILL.md. The plan is at /plan/plan.md. "
            f"Use max_sources={max_sources}.",
            ctx.callbacks,
        )

    return _do_research


def _do_read_sources(ctx: PipelineContext) -> None:
    registry_path = ctx.run_dir / "sources" / "registry.json"
    if not registry_path.exists():
        logger.warning("No registry.json found -- skipping source reading.")
        return

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    tasks = [
        (sid, meta.get("url", ""), meta.get("topic", ""))
        for sid, meta in registry.items()
        if meta.get("url")
    ]
    if not tasks:
        logger.info("No sources with URLs to read.")
        return

    logger.info("Reading %d sources in parallel...", len(tasks))

    def read_one(args):
        source_id, url, topic = args
        _invoke(
            ctx.worker,
            f"read_{source_id}",
            f"Read /skills/worker/source-reading/SKILL.md. "
            f"Source: {source_id}, URL: {url}, Topic: {topic}.",
            ctx.callbacks,
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(read_one, t): t[0] for t in tasks}
        for future in as_completed(futures):
            sid = futures[future]
            try:
                future.result()
            except Exception:
                logger.exception("Failed to read source %s", sid)


# -- Short path: full-essay write & review --------------------------------


def _make_write_full(target_words: int) -> Callable[[PipelineContext], None]:
    def _do_write_full(ctx: PipelineContext) -> None:
        msg = "Read /skills/writer/essay-writing/SKILL.md."
        if target_words:
            msg += (
                f" The total word target is {target_words} words."
                f" You MUST write at least {target_words} words."
            )
        _invoke(ctx.writer, "write", msg, ctx.callbacks)

    return _do_write_full


def _make_review_full(target_words: int) -> Callable[[PipelineContext], None]:
    def _do_review_full(ctx: PipelineContext) -> None:
        msg = "Read /skills/writer/essay-review/SKILL.md."
        if target_words:
            msg += (
                f" The word target is {target_words} words."
                f" Do NOT produce fewer words than the draft."
            )
        _invoke(ctx.writer, "review", msg, ctx.callbacks)

    return _do_review_full


# -- Long path: section-by-section write & review -------------------------


def _writing_order(sections: list[Section]) -> list[Section]:
    """Body sections in plan order, then conclusion, then introduction."""
    body = [s for s in sections if not s.is_intro and not s.is_conclusion]
    conclusion = [s for s in sections if s.is_conclusion]
    intro = [s for s in sections if s.is_intro]
    return body + conclusion + intro


def _section_filename(section: Section) -> str:
    """Generate a filename for a section: 01_title.md"""
    safe = re.sub(r"[^\w\s-]", "", section.title)
    safe = re.sub(r"\s+", "_", safe.strip())[:40]
    return f"{section.number:02d}_{safe}.md"


def _make_write_sections(
    sections: list[Section],
    target_words: int,
) -> Callable[[PipelineContext], None]:
    """Write essay section by section (long path)."""

    def _do_write_sections(ctx: PipelineContext) -> None:
        sections_dir = ctx.run_dir / "essay" / "sections"
        sections_dir.mkdir(parents=True, exist_ok=True)

        order = _writing_order(sections)
        written_files: list[tuple[Section, str]] = []

        for section in order:
            fname = _section_filename(section)
            vfs_path = f"/essay/sections/{fname}"

            # Build context of already-written sections (in plan order)
            prior_context = ""
            if written_files:
                sorted_written = sorted(written_files, key=lambda x: x[0].number)
                parts = []
                for ws, wf in sorted_written:
                    wp = sections_dir / wf
                    if wp.exists():
                        parts.append(wp.read_text(encoding="utf-8"))
                if parts:
                    prior_context = (
                        "\n\n--- Previously written sections "
                        "(for context, in reading order) ---\n\n"
                        + "\n\n---\n\n".join(parts)
                    )

            msg = (
                f"Read /skills/writer/section-writing/SKILL.md.\n\n"
                f"## Your Task\n"
                f'Write section {section.number}: "{section.title}"\n'
                f"- **Heading to use**: {section.heading}\n"
                f"- **Word target**: {section.word_target} words\n"
                f"- **Key points**: {section.key_points}\n"
            )
            if section.content_outline:
                msg += f"- **Content outline**: {section.content_outline}\n"
            msg += (
                f"\nWrite to: {vfs_path}\n"
                f"You MUST write at least {section.word_target} words "
                f"for this section."
            )
            if prior_context:
                msg += prior_context

            tracker_step = f"write:{section.number}"
            if ctx.tracker is not None:
                ctx.tracker.current_step = tracker_step

            t0 = monotonic()
            _invoke(ctx.writer, f"write_s{section.number}", msg, ctx.callbacks)
            dur = monotonic() - t0

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

        draft_path = ctx.run_dir / "essay" / "draft.md"
        draft_path.write_text("\n\n".join(draft_parts), encoding="utf-8")
        logger.info("Combined %d sections into draft.md", len(draft_parts))

    return _do_write_sections


def _make_review_sections(
    sections: list[Section],
    target_words: int,
) -> Callable[[PipelineContext], None]:
    """Review essay section by section with progressive replacement."""

    def _do_review_sections(ctx: PipelineContext) -> None:
        sections_dir = ctx.run_dir / "essay" / "sections"
        plan_order = sorted(sections, key=lambda s: s.number)

        for section in plan_order:
            fname = _section_filename(section)
            section_path = sections_dir / fname

            if not section_path.exists():
                logger.warning("Section %d missing, skipping review", section.number)
                continue

            # Build full essay with target section delimited
            full_essay_parts = []
            for s in plan_order:
                fp = sections_dir / _section_filename(s)
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

            vfs_path = f"/essay/sections/{fname}"

            # Delete original so `write_file` (which rejects existing files)
            # can create the reviewed version at the same path.
            section_path.unlink()

            msg = (
                f"Read /skills/writer/section-review/SKILL.md.\n\n"
                f"## Your Task\n"
                f"Review and improve section {section.number}: "
                f'"{section.title}"\n'
                f"- **Word target**: {section.word_target} words\n"
                f"- **Write improved version to**: {vfs_path}\n\n"
                f"## Full Essay\n\n"
                f"The section to review is delimited with "
                f"`<!-- >>> SECTION TO REVIEW: START >>> -->` and "
                f"`<!-- <<< SECTION TO REVIEW: END <<< -->>`.\n"
                f"Rewrite ONLY that section. Do NOT touch other "
                f"sections.\n\n{full_essay}"
            )

            tracker_step = f"review:{section.number}"
            if ctx.tracker is not None:
                ctx.tracker.current_step = tracker_step

            t0 = monotonic()
            _invoke(
                ctx.writer,
                f"review_s{section.number}",
                msg,
                ctx.callbacks,
            )
            dur = monotonic() - t0

            if ctx.tracker is not None:
                ctx.tracker.record_duration(tracker_step, dur)

            print(
                f"    section {section.number} ({section.title}) -- {dur:.1f}s",
                file=sys.stderr,
            )

        # Concatenate all reviewed sections into reviewed.md
        reviewed_parts = []
        for s in plan_order:
            fp = sections_dir / _section_filename(s)
            if fp.exists():
                reviewed_parts.append(fp.read_text(encoding="utf-8"))

        reviewed_path = ctx.run_dir / "essay" / "reviewed.md"
        reviewed_path.write_text("\n\n".join(reviewed_parts), encoding="utf-8")
        logger.info(
            "Combined %d reviewed sections into reviewed.md",
            len(reviewed_parts),
        )

    return _do_review_sections


# -- Export (pure Python) --------------------------------------------------


def _do_export(ctx: PipelineContext) -> None:
    """Build docx from disk files (pure Python, no LLM)."""
    from src.tools.docx_builder import _build_document

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
    registry_path = ctx.run_dir / "sources" / "registry.json"
    if registry_path.exists():
        raw = registry_path.read_text(encoding="utf-8")
        try:
            sources = json.loads(raw)
        except json.JSONDecodeError:
            sources = json.loads(raw.encode().decode("unicode_escape"))

    doc_config = ctx.config.formatting.model_dump()
    brief_path = ctx.run_dir / "brief" / "assignment.md"
    if brief_path.exists():
        for line in brief_path.read_text(encoding="utf-8").split("\n"):
            if line.startswith("# "):
                doc_config.setdefault("title", line[2:].strip())
                break

    doc = _build_document(essay_text, doc_config, sources)

    output_path = Path(ctx.config.paths.output_dir) / "essay.docx"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    logger.info("essay.docx saved to %s", output_path)
    print(f"  essay.docx -> {output_path}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Pipeline builder & entry point
# ---------------------------------------------------------------------------


def _build_execution_steps(
    ctx: PipelineContext,
    target_words: int,
) -> list[PipelineStep]:
    """Build the dynamic portion of the pipeline after plan is available."""
    max_sources = _compute_max_sources(target_words, ctx.config)
    threshold = ctx.config.writing.long_essay_threshold

    steps: list[PipelineStep] = [
        PipelineStep("research", _make_research(max_sources)),
        PipelineStep("read_sources", _do_read_sources),
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
                PipelineStep(
                    "write",
                    _make_write_sections(sections, target_words),
                )
            )
            steps.append(
                PipelineStep(
                    "review",
                    _make_review_sections(sections, target_words),
                )
            )

    steps.append(PipelineStep("export", _do_export))
    return steps


def run_pipeline(
    worker: CompiledStateGraph,
    writer: CompiledStateGraph,
    run_dir: Path,
    config,
    *,
    extra_prompt: str | None = None,
    callbacks: list | None = None,
    token_tracker=None,
) -> None:
    """Execute the essay writing pipeline.

    Phase 1 (fixed):  intake -> plan
    Phase 2 (dynamic): research -> read_sources -> write -> review -> export
    """
    ctx = PipelineContext(
        worker=worker,
        writer=writer,
        run_dir=run_dir,
        config=config,
        extra_prompt=extra_prompt,
        callbacks=callbacks,
        tracker=token_tracker,
    )

    # Phase 1: always the same
    phase1 = [
        PipelineStep("intake", _do_intake),
        PipelineStep("plan", _do_plan),
    ]
    _execute(phase1, ctx)

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
    phase2 = _build_execution_steps(ctx, target_words)
    _execute(phase2, ctx)
