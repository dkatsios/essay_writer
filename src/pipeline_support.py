"""Shared helpers for the deterministic essay pipeline."""

from __future__ import annotations

import json
import logging
import math
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

from pydantic import BaseModel

from src.agent import (
    AsyncModelClient,
    ModelClient,
    _retry_with_backoff,
    extract_text,
    extract_usage,
)
from src.schemas import AssignmentBrief, EssayPlan, SourceNote

if TYPE_CHECKING:
    from config.schemas import EssayWriterConfig

logger = logging.getLogger(__name__)

_MAX_PRIOR_SECTION_CONTEXT = 2
_REVIEW_SECTION_NEIGHBORS = 1
_STRUCTURED_RETRIES = 2


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
    tracker: object | None = None
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


def _load_checkpoint(run_dir: Path) -> set[str]:
    """Load completed step names from the checkpoint file."""
    path = run_dir / "checkpoint.json"
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("completed", []))
    except (json.JSONDecodeError, OSError):
        return set()


def _save_checkpoint(run_dir: Path, step_name: str) -> None:
    """Append a completed step to the checkpoint file."""
    path = run_dir / "checkpoint.json"
    completed = list(_load_checkpoint(run_dir))
    if step_name not in completed:
        completed.append(step_name)
    path.write_text(
        json.dumps({"completed": completed}, indent=2),
        encoding="utf-8",
    )


def _execute(
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
            print(f"\n{'=' * 50}", file=sys.stderr)
            print(f"  Step: {step.name} (skipped — already completed)", file=sys.stderr)
            running_idx += 1
            continue

        print(f"\n{'=' * 50}", file=sys.stderr)
        print(f"  Step: {step.name}", file=sys.stderr)
        if ctx.tracker is not None:
            ctx.tracker.set_current_step(step.name)
            if total_steps is not None:
                ctx.tracker.set_step_progress(
                    step_offset + running_idx, total_steps
                )
            ctx.tracker.set_sub_total(0)
        start = monotonic()
        try:
            step.fn(ctx)
            duration = monotonic() - start
            print(f"  OK {step.name} ({duration:.1f}s)", file=sys.stderr)
        except Exception:
            duration = monotonic() - start
            print(f"  FAIL {step.name} ({duration:.1f}s)", file=sys.stderr)
            if ctx.tracker is not None:
                ctx.tracker.record_duration(step.name, duration)
            raise
        if ctx.tracker is not None:
            ctx.tracker.record_duration(step.name, duration)
        _save_checkpoint(ctx.run_dir, step.name)
        running_idx += 1


def _record_usage(tracker: object | None, response) -> None:
    if tracker is None or response is None:
        return
    usage = extract_usage(response)
    if usage["input"] or usage["output"] or usage["thinking"]:
        tracker.record(
            usage["model"], usage["input"], usage["output"], usage["thinking"]
        )


def _structured_call(
    client: ModelClient,
    prompt: str,
    schema: type[BaseModel],
    tracker: object | None = None,
    retries: int = _STRUCTURED_RETRIES,
) -> BaseModel:
    """Call a model with structured output."""

    def _do_call():
        return client.client.chat.completions.create(
            model=client.model,
            response_model=schema,
            max_retries=retries,
            messages=[{"role": "user", "content": prompt}],
        )

    result = _retry_with_backoff(_do_call)
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
        return await client.client.chat.completions.create(
            model=client.model,
            response_model=schema,
            max_retries=retries,
            messages=[{"role": "user", "content": prompt}],
        )

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
    """Call a model for free-form text output."""

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


def _write_json(path: Path, data: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        data.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _get_brief_language(run_dir: Path) -> str:
    brief_path = run_dir / "brief" / "assignment.json"
    if brief_path.exists():
        brief = AssignmentBrief.model_validate_json(
            brief_path.read_text(encoding="utf-8")
        )
        return brief.language
    return "Greek (Δημοτική)"


def _get_target_words(run_dir: Path) -> int:
    plan_path = run_dir / "plan" / "plan.json"
    if not plan_path.exists():
        return 0
    plan = EssayPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    return plan.total_word_target


def _parse_sections(run_dir: Path) -> list[Section]:
    plan_path = run_dir / "plan" / "plan.json"
    if not plan_path.exists():
        return []

    plan = EssayPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    sections: list[Section] = []
    for section in plan.sections:
        title = section.title.lower()
        sections.append(
            Section(
                number=section.number,
                title=section.title,
                heading=section.heading,
                word_target=section.word_target,
                key_points=section.key_points,
                content_outline=section.content_outline,
                is_intro=(
                    section.number == 1 or "introduction" in title or "εισαγωγ" in title
                ),
                is_conclusion=("conclusion" in title or "συμπέρασμ" in title),
            )
        )
    return sections


def _suggested_sources(target_words: int, sources_per_1k: int = 5) -> int:
    if target_words <= 0:
        return 0
    return round(sources_per_1k * 3 * math.log2(1 + target_words / 1000))


def _compute_max_sources(
    target_words: int,
    config: EssayWriterConfig,
    user_min_sources: int | None = None,
) -> tuple[int, int]:
    search = config.search
    raw_target = _suggested_sources(target_words, search.sources_per_1k_words)
    config_floor = search.min_sources
    if user_min_sources is not None:
        target = max(config_floor, user_min_sources)
    else:
        target = max(config_floor, raw_target)
    fetch = max(target, int(target * search.overfetch_multiplier))
    return target, fetch


def _corpus_tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower(), flags=re.UNICODE))


def _note_lexical_score(corpus_tokens: set[str], note: SourceNote) -> int:
    blob = f"{note.title} {note.summary}"[:8000]
    return len(corpus_tokens & _corpus_tokens(blob))


def _rank_notes_by_corpus(corpus: str, notes: list[SourceNote]) -> list[SourceNote]:
    corpus_tokens = _corpus_tokens(corpus)
    return sorted(
        notes, key=lambda note: _note_lexical_score(corpus_tokens, note), reverse=True
    )


def _source_catalog_markdown(notes: list[SourceNote]) -> str:
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


def _plan_corpus_from_json(plan_json: str) -> str:
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


def _split_writer_source_context(
    corpus: str,
    all_notes: list[SourceNote],
    full_detail_budget: int,
) -> tuple[list[SourceNote], str, int]:
    if not all_notes:
        return [], "", 0
    ranked = _rank_notes_by_corpus(corpus, all_notes)
    budget = max(1, full_detail_budget)
    return ranked[:budget], _source_catalog_markdown(all_notes), len(all_notes)


def _load_source_notes(run_dir: Path) -> list[SourceNote]:
    notes_dir = run_dir / "sources" / "notes"
    if not notes_dir.exists():
        return []
    notes: list[SourceNote] = []
    for note_file in sorted(notes_dir.iterdir()):
        if note_file.suffix != ".json":
            continue
        try:
            note = SourceNote.model_validate_json(note_file.read_text(encoding="utf-8"))
            if note.is_accessible:
                notes.append(note)
        except Exception:
            logger.warning("Failed to load source note: %s", note_file.name)
    return notes


def _load_selected_source_notes(run_dir: Path) -> list[SourceNote]:
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
