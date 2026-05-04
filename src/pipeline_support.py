"""Shared helpers for the deterministic essay pipeline."""

from __future__ import annotations

import inspect
import json
import logging
import math
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

from pydantic import BaseModel

from src.agent import (
    AsyncModelClient,
    ModelClient,
    retry_with_backoff,
    extract_text,
    extract_usage,
)
from src.rendering import PromptPair
from src.schemas import AssignmentBrief, EssayPlan, SourceNote
from src.storage import AnyStorage, MemoryRunStorage, RunStorage

if TYPE_CHECKING:
    from config.settings import EssayWriterConfig
    from src.run_history_store import RunHistoryStore

logger = logging.getLogger(__name__)

_MAX_PRIOR_SECTION_CONTEXT = 2
_REVIEW_SECTION_NEIGHBORS = 1
_STRUCTURED_RETRIES = 2


@dataclass
class PipelineContext:
    """Shared state passed to every step."""

    worker: ModelClient | None
    async_worker: AsyncModelClient | None
    writer: ModelClient | None
    reviewer: ModelClient | None
    storage: AnyStorage
    config: EssayWriterConfig
    async_writer: AsyncModelClient | None = None
    async_reviewer: AsyncModelClient | None = None
    extra_prompt: str | None = None
    tracker: object | None = None
    on_optional_source_pdfs: (
        Callable[[AnyStorage, list[dict]], Awaitable[None]] | None
    ) = None
    on_source_shortfall: (
        Callable[[AnyStorage, dict], Awaitable[tuple[bool, list[str]]]] | None
    ) = None
    brief: AssignmentBrief | None = None
    job_id: str | None = None
    run_history_store: RunHistoryStore | None = None


@dataclass
class Section:
    """A single section with deferred-writing metadata from the plan."""

    position: int
    number: int
    title: str
    heading: str
    word_target: int
    key_points: str = ""
    content_outline: str = ""
    requires_full_context: bool = False
    deferred_order: int | None = None


@dataclass
class PipelineStep:
    """A named step in the pipeline."""

    name: str
    fn: Callable[[PipelineContext], Awaitable[None]]


def load_checkpoint(storage: AnyStorage) -> set[str]:
    """Load completed step names from the checkpoint file."""
    if not storage.exists("checkpoint.json"):
        return set()
    try:
        data = json.loads(storage.read_text("checkpoint.json"))
        return set(data.get("completed", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_checkpoint(storage: AnyStorage, step_name: str) -> None:
    """Append a completed step to the checkpoint file."""
    completed = list(load_checkpoint(storage))
    if step_name not in completed:
        completed.append(step_name)
    storage.write_text(
        "checkpoint.json",
        json.dumps({"completed": completed}, indent=2),
    )


def _persist_step_history(
    ctx: PipelineContext,
    *,
    step_name: str,
    status: str,
    step_index: int,
    step_count: int | None,
) -> None:
    if ctx.job_id is None or ctx.run_history_store is None or ctx.tracker is None:
        return
    snapshot_step_metric = getattr(ctx.tracker, "snapshot_step_metric", None)
    if callable(snapshot_step_metric):
        payload = snapshot_step_metric(
            step_name,
            status=status,
            step_index=step_index,
            step_count=step_count,
        )
        ctx.run_history_store.save_step_metric(ctx.job_id, step_name, **payload)
    if status == "completed":
        sync_artifacts_snapshot(ctx)


def sync_artifacts_snapshot(ctx: PipelineContext) -> None:
    if ctx.job_id is None or ctx.run_history_store is None:
        return
    ctx.run_history_store.sync_artifacts(ctx.job_id, ctx.storage)


async def execute(
    steps: list[PipelineStep],
    ctx: PipelineContext,
    *,
    checkpoint: set[str] | None = None,
    step_offset: int = 0,
    total_steps: int | None = None,
) -> None:
    """Run a list of pipeline steps with timing, tracking, and checkpointing."""
    running_idx = 0
    for step in steps:
        if checkpoint is not None and step.name in checkpoint:
            logger.info(
                "%s\nStep: %s (skipped — already completed)", "=" * 50, step.name
            )
            running_idx += 1
            continue

        logger.info("%s\nStep: %s", "=" * 50, step.name)
        if ctx.tracker is not None:
            ctx.tracker.set_current_step(step.name)
            if total_steps is not None:
                ctx.tracker.set_step_progress(step_offset + running_idx, total_steps)
            ctx.tracker.set_sub_total(0)
        start = monotonic()
        try:
            result = step.fn(ctx)
            if inspect.isawaitable(result):
                await result
            duration = monotonic() - start
            logger.info("OK %s (%.1fs)", step.name, duration)
        except Exception:
            duration = monotonic() - start
            logger.error("FAIL %s (%.1fs)", step.name, duration)
            if ctx.tracker is not None:
                ctx.tracker.record_duration(step.name, duration)
            _persist_step_history(
                ctx,
                step_name=step.name,
                status="failed",
                step_index=step_offset + running_idx,
                step_count=total_steps,
            )
            raise
        if ctx.tracker is not None:
            ctx.tracker.record_duration(step.name, duration)
        save_checkpoint(ctx.storage, step.name)
        _persist_step_history(
            ctx,
            step_name=step.name,
            status="completed",
            step_index=step_offset + running_idx,
            step_count=total_steps,
        )
        running_idx += 1


def build_messages(prompt: str | PromptPair) -> list[dict]:
    """Build a chat messages list from a plain string or PromptPair."""
    if isinstance(prompt, PromptPair):
        msgs: list[dict] = []
        if prompt.system:
            msgs.append({"role": "system", "content": prompt.system})
        msgs.append({"role": "user", "content": prompt.user})
        return msgs
    return [{"role": "user", "content": prompt}]


def record_usage(tracker: object | None, response) -> None:
    if tracker is None or response is None:
        return
    usage = extract_usage(response)
    if usage["input"] or usage["output"] or usage["thinking"]:
        tracker.record(
            usage["model"], usage["input"], usage["output"], usage["thinking"]
        )


def structured_call(
    client: ModelClient,
    prompt: str | PromptPair,
    schema: type[BaseModel],
    tracker: object | None = None,
    retries: int = _STRUCTURED_RETRIES,
) -> BaseModel:
    """Call a model with structured output."""
    messages = build_messages(prompt)

    def _do_call():
        return client.client.chat.completions.create(
            model=client.model,
            response_model=schema,
            max_retries=retries,
            messages=messages,
        )

    result = retry_with_backoff(_do_call)
    raw = getattr(result, "_raw_response", None)
    if raw:
        record_usage(tracker, raw)
    return result


async def async_structured_call(
    client: AsyncModelClient,
    prompt: str | PromptPair,
    schema: type[BaseModel],
    tracker: object | None = None,
    retries: int = _STRUCTURED_RETRIES,
) -> BaseModel:
    """Async version of structured_call."""
    messages = build_messages(prompt)

    async def _do_call():
        return await client.client.chat.completions.create(
            model=client.model,
            response_model=schema,
            max_retries=retries,
            messages=messages,
        )

    result = await retry_with_backoff(_do_call, is_async=True)
    raw = getattr(result, "_raw_response", None)
    if raw:
        record_usage(tracker, raw)
    return result


def text_call(
    client: ModelClient,
    prompt: PromptPair,
    tracker: object | None = None,
) -> str:
    """Call a model for free-form text output."""
    messages = build_messages(prompt)

    def _do_call():
        return client.client.chat.completions.create(
            model=client.model,
            response_model=None,
            messages=messages,
        )

    response = retry_with_backoff(_do_call)
    record_usage(tracker, response)
    return extract_text(response)


async def async_text_call(
    client: AsyncModelClient,
    prompt: PromptPair,
    tracker: object | None = None,
) -> str:
    """Async version of text_call."""
    messages = build_messages(prompt)

    async def _do_call():
        return await client.client.chat.completions.create(
            model=client.model,
            response_model=None,
            messages=messages,
        )

    response = await retry_with_backoff(_do_call, is_async=True)
    record_usage(tracker, response)
    return extract_text(response)


def write_json(storage: AnyStorage, subpath: str, data: BaseModel) -> None:
    storage.write_text(subpath, data.model_dump_json(indent=2, ensure_ascii=False))


def write_text(storage: AnyStorage, subpath: str, text: str) -> None:
    storage.write_text(subpath, text)


def read_text(storage: AnyStorage, subpath: str) -> str:
    return storage.read_text(subpath)


def get_brief_language(storage: AnyStorage) -> str:
    if storage.exists("brief/assignment.json"):
        brief = AssignmentBrief.model_validate_json(
            storage.read_text("brief/assignment.json")
        )
        return brief.language
    return "Greek (Δημοτική)"


def get_target_words(storage: AnyStorage) -> int:
    if not storage.exists("plan/plan.json"):
        return 0
    plan = EssayPlan.model_validate_json(storage.read_text("plan/plan.json"))
    return plan.total_word_target


def normalize_section_word_targets(sections: list[Section], total_target: int) -> None:
    """Scale section word targets so they sum to *total_target*, rounding to tens."""
    section_sum = sum(s.word_target for s in sections)
    if section_sum <= 0 or total_target <= 0 or section_sum == total_target:
        return
    ratio = total_target / section_sum
    for section in sections:
        section.word_target = max(10, round(section.word_target * ratio / 10) * 10)
    # Distribute any residual rounding error into the largest section.
    adjusted_sum = sum(s.word_target for s in sections)
    delta = total_target - adjusted_sum
    if delta and sections:
        largest = max(sections, key=lambda s: s.word_target)
        largest.word_target = max(10, largest.word_target + delta)
    logger.info(
        "Normalized section word targets: %d -> %d (total_target=%d)",
        section_sum,
        sum(s.word_target for s in sections),
        total_target,
    )


def parse_sections(storage: AnyStorage) -> list[Section]:
    if not storage.exists("plan/plan.json"):
        return []

    plan = EssayPlan.model_validate_json(storage.read_text("plan/plan.json"))
    sections: list[Section] = []
    duplicate_numbers = [
        number
        for number, count in Counter(
            section.number for section in plan.sections
        ).items()
        if count > 1
    ]
    if duplicate_numbers:
        logger.warning(
            "Plan contains duplicate section numbers %s; using plan order for internal section ids",
            sorted(duplicate_numbers),
        )

    for position, section in enumerate(plan.sections, start=1):
        sections.append(
            Section(
                position=position,
                number=section.number,
                title=section.title,
                heading=section.heading,
                word_target=section.word_target,
                key_points=section.key_points,
                content_outline=section.content_outline,
                requires_full_context=section.requires_full_context,
                deferred_order=section.deferred_order,
            )
        )

    normalize_section_word_targets(sections, plan.total_word_target)
    return sections


def suggested_sources(target_words: int, sources_per_1k: int = 5) -> int:
    if target_words <= 0:
        return 0
    return round(sources_per_1k * 3 * math.log2(1 + target_words / 1000))


def compute_max_sources(
    target_words: int,
    config: EssayWriterConfig,
    user_min_sources: int | None = None,
) -> tuple[int, int]:
    search = config.search
    raw_target = suggested_sources(target_words, search.sources_per_1k_words)
    config_floor = search.min_sources
    if user_min_sources is not None:
        target = max(config_floor, user_min_sources)
    else:
        target = max(config_floor, raw_target)
    fetch = max(target, int(target * search.overfetch_multiplier))
    return target, fetch


def corpus_tokens(text: str) -> set[str]:
    return {
        w for w in re.findall(r"\w+", text.lower(), flags=re.UNICODE) if len(w) >= 4
    }


def note_lexical_score(tokens: set[str], note: SourceNote) -> int:
    blob = f"{note.title} {note.summary}"[:8000]
    return len(tokens & corpus_tokens(blob))


def rank_notes_by_corpus(corpus: str, notes: list[SourceNote]) -> list[SourceNote]:
    tokens = corpus_tokens(corpus)
    return sorted(
        notes, key=lambda note: note_lexical_score(tokens, note), reverse=True
    )


def source_catalog_markdown(notes: list[SourceNote]) -> str:
    lines: list[str] = []
    for note in sorted(notes, key=lambda item: item.source_id):
        authors = (
            ", ".join(author.strip() for author in note.authors if author.strip())
            or "n.a."
        )
        lines.append(
            f"- `{note.source_id}` — {authors} ({note.year or 'n.d.'}). {note.title}"
        )
    return "\n".join(lines)


def plan_corpus_from_json(plan_json: str) -> str:
    try:
        data = json.loads(plan_json)
    except json.JSONDecodeError:
        return ""
    parts: list[str] = [data.get("thesis") or "", data.get("title") or ""]
    for section in data.get("sections") or []:
        if not isinstance(section, dict):
            continue
        parts.extend(
            [
                str(section.get("title") or ""),
                str(section.get("key_points") or ""),
                str(section.get("content_outline") or ""),
            ]
        )
    return " ".join(parts)


def split_writer_source_context(
    corpus: str,
    all_notes: list[SourceNote],
    full_detail_budget: int,
) -> tuple[list[SourceNote], str, int]:
    if not all_notes:
        return [], "", 0
    ranked = rank_notes_by_corpus(corpus, all_notes)
    budget = max(1, full_detail_budget)
    return ranked[:budget], source_catalog_markdown(all_notes), len(all_notes)


def load_source_notes(storage: AnyStorage) -> list[SourceNote]:
    note_files = storage.list_files("sources/notes/")
    if not note_files:
        return []
    notes: list[SourceNote] = []
    for subpath in sorted(note_files):
        if not subpath.endswith(".json"):
            continue
        try:
            note = SourceNote.model_validate_json(storage.read_text(subpath))
            if note.is_accessible:
                notes.append(note)
        except Exception:
            logger.warning("Failed to load source note: %s", subpath)
    return notes


def load_selected_source_notes(storage: AnyStorage) -> list[SourceNote]:
    all_notes = load_source_notes(storage)
    if not all_notes:
        return []

    if not storage.exists("sources/selected.json"):
        return all_notes

    try:
        selected = json.loads(storage.read_text("sources/selected.json"))
    except Exception:
        logger.warning("Failed to load selected sources; using all accessible notes")
        return all_notes

    if not isinstance(selected, dict):
        return all_notes

    if not selected:
        return []

    selected_ids = set(selected)
    selected_notes = [note for note in all_notes if note.source_id in selected_ids]
    if selected_notes:
        return selected_notes

    logger.warning(
        "Selected sources had no accessible notes; using all accessible notes"
    )
    return all_notes


def build_prior_sections_context(
    written_sections: list[tuple[Section, str]],
    max_sections: int = _MAX_PRIOR_SECTION_CONTEXT,
) -> str:
    if not written_sections:
        return ""
    recent_sections = sorted(
        written_sections[-max_sections:], key=lambda item: item[0].position
    )
    return "\n\n---\n\n".join(text for _, text in recent_sections if text)


def section_window(
    sections: list[Section],
    target_position: int,
    neighbor_count: int = _REVIEW_SECTION_NEIGHBORS,
) -> list[Section]:
    for index, section in enumerate(sections):
        if section.position == target_position:
            start = max(0, index - neighbor_count)
            end = min(len(sections), index + neighbor_count + 1)
            return sections[start:end]
    return []


def build_review_context(
    section: Section,
    sections: list[Section],
    section_texts: dict[int, str],
    neighbor_count: int = _REVIEW_SECTION_NEIGHBORS,
) -> str:
    parts: list[str] = []
    for current in section_window(sections, section.position, neighbor_count):
        text = section_texts.get(current.position, "")
        if not text:
            continue
        if current.position == section.position:
            text = (
                "<!-- >>> SECTION TO REVIEW: START >>> -->\n"
                f"{text}\n"
                "<!-- <<< SECTION TO REVIEW: END <<< -->"
            )
        parts.append(text)
    return "\n\n---\n\n".join(parts)
