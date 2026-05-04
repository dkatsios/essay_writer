"""Shared runtime helpers for the web workflow and pipeline utilities."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from src.schemas import (
    Clarification,
    ValidationQuestion,
    expand_context_dependent_option,
)

logger = logging.getLogger(__name__)


def _read_json(storage, subpath: str) -> dict | list | None:
    if not storage.exists(subpath):
        return None
    try:
        return json.loads(storage.read_text(subpath))
    except (json.JSONDecodeError, ValueError):
        return None


def calc_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    thinking_tokens: int = 0,
) -> float:
    """Calculate cost in USD using genai-prices."""
    from genai_prices import Usage, calc_price

    try:
        result = calc_price(
            Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens + thinking_tokens,
            ),
            model_ref=model,
        )
        return float(result.total_price)
    except LookupError:
        logger.debug("No pricing data for model %r — cost will show as $0", model)
        return 0.0


def _model_short_name(full_name: str) -> str:
    """Extract a pricing-friendly bare model name from a provider spec."""
    if ":" in full_name:
        full_name = full_name.split(":", 1)[1]
    for prefix in ("vertex_ai.anthropic.", "vertex_ai.", "openai."):
        if full_name.startswith(prefix):
            full_name = full_name[len(prefix) :]
            break
    for suffix in ("-001", "-002", "-latest"):
        full_name = full_name.removesuffix(suffix)
    return full_name


def _step_cost(data: dict) -> float:
    """Return the cost for one tracked step, skipping empty non-LLM rows."""
    input_tokens = data["input_tokens"]
    output_tokens = data["output_tokens"]
    thinking_tokens = data["thinking_tokens"]
    if not data["model"] and not (input_tokens or output_tokens or thinking_tokens):
        return 0.0
    return calc_cost(
        data["model"] or "unknown",
        input_tokens,
        output_tokens,
        thinking_tokens,
    )


class TokenTracker:
    """Tracks LLM token usage per pipeline step."""

    def __init__(self) -> None:
        self.current_step: str = "unknown"
        self._steps: dict[str, dict] = {}
        self._lock = __import__("threading").Lock()
        self._run_steps: dict[str, str] = {}
        self.step_index: int = 0
        self.step_count: int = 0
        self.sub_done: int = 0
        self.sub_total: int = 0
        self._on_progress: Callable[[], None] | None = None

    def _ensure_step(self, step: str) -> dict:
        if step not in self._steps:
            self._steps[step] = {
                "input_tokens": 0,
                "output_tokens": 0,
                "thinking_tokens": 0,
                "model": "",
                "calls": 0,
                "duration": 0.0,
            }
        return self._steps[step]

    def record_duration(self, step: str, duration: float) -> None:
        data = self._ensure_step(step)
        data["duration"] = duration

    def set_current_step(self, step: str) -> None:
        with self._lock:
            self.current_step = step

    def get_current_step(self) -> str:
        with self._lock:
            return self.current_step

    def snapshot_step(self, run_id: str) -> None:
        with self._lock:
            self._run_steps[run_id] = self.current_step

    def pop_step(self, run_id: str) -> str:
        with self._lock:
            return self._run_steps.pop(run_id, self.current_step)

    def set_step_progress(self, index: int, count: int) -> None:
        """Set the pipeline-level step index and count."""
        with self._lock:
            self.step_index = index
            self.step_count = count

    def set_sub_total(self, total: int) -> None:
        """Set the sub-step total and reset sub_done to 0."""
        with self._lock:
            self.sub_total = total
            self.sub_done = 0

    def increment_sub_done(self) -> None:
        """Increment the sub-step counter and fire progress callback."""
        with self._lock:
            self.sub_done += 1
        cb = self._on_progress
        if cb is not None:
            cb()

    def set_on_progress(self, callback: Callable[[], None] | None) -> None:
        """Set callback invoked when sub-step progress changes."""
        self._on_progress = callback

    def record(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        thinking_tokens: int = 0,
        step: str | None = None,
    ) -> None:
        with self._lock:
            target = step or self.current_step
            data = self._ensure_step(target)
            data["input_tokens"] += input_tokens
            data["output_tokens"] += output_tokens
            data["thinking_tokens"] += thinking_tokens
            data["model"] = _model_short_name(model) if model else data["model"]
            data["calls"] += 1

    def cost_summary(self) -> str:
        if not self._steps:
            return "\n(No token data captured)"

        lines = [
            "",
            "── Cost Report ─────────────────────────────────────────────",
            f"{'Step':<16} {'Model':<20} {'Time':>7} {'In':>8} {'Out':>8} {'Think':>8} {'Cost':>8}",
            "─" * 80,
        ]
        total_cost = 0.0
        total_in = 0
        total_out = 0
        total_think = 0
        total_dur = 0.0

        for step, data in self._steps.items():
            model = data["model"] or "unknown"
            step_cost = _step_cost(data)

            dur = data["duration"]
            total_cost += step_cost
            total_in += data["input_tokens"]
            total_out += data["output_tokens"]
            total_think += data["thinking_tokens"]
            total_dur += dur

            dur_str = f"{dur:.0f}s" if dur else ""
            lines.append(
                f"{step:<16} {model:<20} {dur_str:>7} "
                f"{data['input_tokens']:>8,} {data['output_tokens']:>8,} "
                f"{data['thinking_tokens']:>8,} ${step_cost:>6.4f}"
            )

        m, s = divmod(int(total_dur), 60)
        lines.append("─" * 80)
        lines.append(
            f"{'TOTAL':<16} {'':<20} {f'{m}m{s:02d}s':>7} "
            f"{total_in:>8,} {total_out:>8,} {total_think:>8,} ${total_cost:>6.4f}"
        )
        return "\n".join(lines)

    def snapshot_step_metric(
        self,
        step: str,
        *,
        status: str = "completed",
        step_index: int | None = None,
        step_count: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            data = dict(self._ensure_step(step))
        step_cost = _step_cost(data)
        return {
            "status": status,
            "model": data["model"],
            "cost_usd": step_cost,
            "call_count": data["calls"],
            "input_tokens": data["input_tokens"],
            "output_tokens": data["output_tokens"],
            "thinking_tokens": data["thinking_tokens"],
            "duration_seconds": data["duration"],
            "step_index": step_index,
            "step_count": step_count,
        }

    def build_runtime_summary(
        self,
        storage,
        *,
        status: str,
        provider: str = "",
    ) -> dict[str, Any]:
        step_rows = [
            self.snapshot_step_metric(step_name) for step_name in list(self._steps)
        ]
        total_cost = sum(float(row["cost_usd"]) for row in step_rows)
        total_in = sum(int(row["input_tokens"]) for row in step_rows)
        total_out = sum(int(row["output_tokens"]) for row in step_rows)
        total_think = sum(int(row["thinking_tokens"]) for row in step_rows)
        total_dur = sum(float(row["duration_seconds"]) for row in step_rows)

        draft_words = _count_words(storage, "essay/draft.md")
        reviewed_words = _count_words(storage, "essay/reviewed.md")
        target_words = _parse_target_words(storage, "plan/plan.json")
        source_metrics = _summarize_source_report_metrics(storage)
        selected_count = _count_sources(storage, "sources/selected.json")
        essay_subpath = "essay/reviewed.md"
        if not storage.exists(essay_subpath):
            essay_subpath = "essay/draft.md"
        cited_count = _count_cited_sources(storage, essay_subpath)

        return {
            "status": status,
            "provider": provider,
            "total_cost_usd": total_cost,
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "total_thinking_tokens": total_think,
            "total_duration_seconds": total_dur,
            "step_count": len(step_rows),
            "registered_source_count": source_metrics["registered"],
            "scored_source_count": source_metrics["scored"],
            "above_threshold_source_count": source_metrics["above_threshold"],
            "selected_source_count": selected_count,
            "selected_full_text_count": source_metrics["selected_with_fulltext"],
            "selected_abstract_only_count": source_metrics["selected_abstract_only"],
            "cited_source_count": cited_count,
            "target_words": target_words,
            "draft_words": draft_words,
            "final_words": reviewed_words or draft_words,
        }

    def write_report(self, storage) -> bool:
        if not self._steps:
            return False

        rows = [
            (step_name, self.snapshot_step_metric(step_name))
            for step_name in list(self._steps)
        ]

        total_cost = sum(float(row[1]["cost_usd"]) for row in rows)
        total_in = sum(int(row[1]["input_tokens"]) for row in rows)
        total_out = sum(int(row[1]["output_tokens"]) for row in rows)
        total_think = sum(int(row[1]["thinking_tokens"]) for row in rows)
        total_dur = sum(float(row[1]["duration_seconds"]) for row in rows)

        m, s = divmod(int(total_dur), 60)

        draft_words = _count_words(storage, "essay/draft.md")
        reviewed_words = _count_words(storage, "essay/reviewed.md")
        target_words = _parse_target_words(storage, "plan/plan.json")
        source_metrics = _summarize_source_report_metrics(storage)
        selected_count = _count_sources(storage, "sources/selected.json")
        essay_subpath = "essay/reviewed.md"
        if not storage.exists(essay_subpath):
            essay_subpath = "essay/draft.md"
        cited_count = _count_cited_sources(storage, essay_subpath)

        lines = [
            "# Run Report",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Duration | {m}m {s}s |",
            f"| Total cost | ${total_cost:.4f} |",
            f"| Tokens (in / out / think) | {total_in:,} / {total_out:,} / {total_think:,} |",
        ]
        if source_metrics["registered"]:
            lines.append(f"| Sources registered | {source_metrics['registered']} |")
        if source_metrics["scored"]:
            lines.append(f"| Sources scored | {source_metrics['scored']} |")
        if source_metrics["above_threshold"]:
            threshold = source_metrics["min_relevance_score"]
            lines.append(
                f"| Sources above threshold | {source_metrics['above_threshold']} (score >= {threshold}) |"
            )
        if selected_count:
            lines.append(f"| Sources available for writing | {selected_count} |")
        if (
            source_metrics["selected_with_fulltext"]
            or source_metrics["selected_abstract_only"]
        ):
            lines.append(
                "| Selected source detail | "
                f"{source_metrics['selected_with_fulltext']} full text / "
                f"{source_metrics['selected_abstract_only']} abstract-only |"
            )
        if cited_count:
            lines.append(f"| Sources cited | {cited_count} |")
        if target_words:
            lines.append(f"| Target words | {target_words:,} |")
        if draft_words:
            lines.append(f"| Draft words | {draft_words:,} |")
        if reviewed_words:
            final_pct = (
                round(reviewed_words / target_words * 100) if target_words else 0
            )
            pct_str = f" ({final_pct}%)" if target_words else ""
            lines.append(f"| Final words | {reviewed_words:,}{pct_str} |")

        # Build child-to-parent aggregation for summary rows (e.g. write
        # aggregates write:1, write:2, …).  A row is a parent if it has no
        # model and at least one child step exists with the "parent:" prefix.
        step_names = {step for step, _ in rows}
        child_agg: dict[str, dict] = {}
        for step, data in rows:
            if ":" not in step:
                continue
            parent = step.split(":")[0]
            if parent not in step_names:
                continue
            agg = child_agg.setdefault(
                parent, {"in": 0, "out": 0, "think": 0, "cost": 0.0}
            )
            agg["in"] += data["input_tokens"]
            agg["out"] += data["output_tokens"]
            agg["think"] += data["thinking_tokens"]
            agg["cost"] += data["cost_usd"]

        lines += [
            "",
            "## Steps",
            "",
            "| Step | Model | Time | In | Out | Think | Cost |",
            "|------|-------|-----:|---:|----:|------:|-----:|",
        ]
        for step, data in rows:
            model = data["model"] or "—"
            dur = data["duration_seconds"]
            dur_str = f"{dur:.0f}s" if dur else "—"
            agg = child_agg.get(step)
            if agg and not data["model"]:
                # Summary row: show aggregated totals from child steps
                lines.append(
                    f"| **{step}** | {model} | **{dur_str}** "
                    f"| **{agg['in']:,}** | **{agg['out']:,}** "
                    f"| **{agg['think']:,}** | **${agg['cost']:.4f}** |"
                )
            else:
                lines.append(
                    f"| {step} | {model} | {dur_str} "
                    f"| {data['input_tokens']:,} | {data['output_tokens']:,} "
                    f"| {data['thinking_tokens']:,} | ${data['cost_usd']:.4f} |"
                )
        lines.append(
            f"| **Total** | | **{m}m {s}s** "
            f"| **{total_in:,}** | **{total_out:,}** "
            f"| **{total_think:,}** | **${total_cost:.4f}** |"
        )
        lines.append("")

        storage.write_text("report.md", "\n".join(lines))
        logger.info("Report written to storage")
        return True


def _count_words(storage, subpath: str) -> int:
    if not storage.exists(subpath):
        return 0
    return len(storage.read_text(subpath).split())


def _count_sources(storage, subpath: str) -> int:
    data = _read_json(storage, subpath)
    if isinstance(data, dict) or isinstance(data, list):
        return len(data)
    return 0


def _summarize_score_metrics(storage, subpath: str) -> dict[str, int]:
    data = _read_json(storage, subpath)
    if not isinstance(data, dict):
        return {
            "scored": 0,
            "above_threshold": 0,
            "min_relevance_score": 0,
        }

    raw_scores = data.get("scores")
    if not isinstance(raw_scores, dict):
        return {
            "scored": 0,
            "above_threshold": 0,
            "min_relevance_score": 0,
        }

    min_relevance_score = data.get("min_relevance_score", 0)
    if not isinstance(min_relevance_score, int):
        min_relevance_score = 0

    above_threshold = 0
    for source in raw_scores.values():
        if not isinstance(source, dict):
            continue
        relevance_score = source.get("relevance_score", 0)
        if isinstance(relevance_score, int) and relevance_score >= min_relevance_score:
            above_threshold += 1

    return {
        "scored": len(raw_scores),
        "above_threshold": above_threshold,
        "min_relevance_score": min_relevance_score,
    }


def _summarize_selected_note_metrics(storage) -> dict[str, int]:
    selected = _read_json(storage, "sources/selected.json")
    if not isinstance(selected, dict) or not selected:
        return {
            "selected_with_fulltext": 0,
            "selected_abstract_only": 0,
        }
    selected_with_fulltext = 0
    selected_abstract_only = 0

    for source_id in selected:
        note = _read_json(storage, f"sources/notes/{source_id}.json")
        if not isinstance(note, dict):
            continue
        if note.get("fetched_fulltext"):
            selected_with_fulltext += 1
        else:
            selected_abstract_only += 1

    return {
        "selected_with_fulltext": selected_with_fulltext,
        "selected_abstract_only": selected_abstract_only,
    }


def _summarize_source_report_metrics(storage) -> dict[str, int]:
    score_metrics = _summarize_score_metrics(storage, "sources/scores.json")
    selected_metrics = _summarize_selected_note_metrics(storage)
    return {
        "registered": _count_sources(storage, "sources/registry.json"),
        **score_metrics,
        **selected_metrics,
    }


def _count_cited_sources(storage, subpath: str) -> int:
    if not storage.exists(subpath):
        return 0
    text = storage.read_text(subpath)
    return len(set(re.findall(r"\[\[([^|\]]+?)(?:\|[^\]]*?)?\]\]", text)))


def _parse_target_words(storage, subpath: str) -> int:
    if not storage.exists(subpath):
        return 0
    try:
        plan = json.loads(storage.read_text(subpath))
        return plan.get("total_word_target", 0)
    except (json.JSONDecodeError, ValueError):
        return 0


def parse_validation_answers(
    questions: list[ValidationQuestion],
    answers: str,
) -> list[Clarification]:
    """Parse compact question answers into structured clarifications."""
    answer_map: dict[int, str] = {}
    for index_text, answer_text in re.findall(
        r"(?:^|,)\s*(\d+)\s*[.):-]?\s*([^,]+)", answers
    ):
        answer_map[int(index_text)] = answer_text.strip()

    if not answer_map and len(questions) == 1 and answers.strip():
        answer_map[1] = answers.strip()

    clarifications: list[Clarification] = []
    for index, question in enumerate(questions, 1):
        raw_answer = answer_map.get(index)
        if not raw_answer:
            continue

        resolved_answer = raw_answer
        selected_option_index: int | None = None
        label = raw_answer[:1].lower()
        option_index = ord(label) - ord("a")
        if len(raw_answer) == 1 and 0 <= option_index < len(question.options):
            resolved_answer = question.options[option_index]
            selected_option_index = option_index

        resolved_answer = expand_context_dependent_option(
            resolved_answer,
            question.options,
            selected_index=selected_option_index,
        )

        clarifications.append(
            Clarification(question=question.question, answer=resolved_answer)
        )

    return clarifications
