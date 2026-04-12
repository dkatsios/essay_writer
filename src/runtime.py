"""Shared runtime helpers for CLI and web entry points."""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

from src.schemas import Clarification, ValidationQuestion

logger = logging.getLogger(__name__)


def _calc_cost(
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


class TokenTracker:
    """Tracks LLM token usage per pipeline step."""

    def __init__(self) -> None:
        self.current_step: str = "unknown"
        self._steps: dict[str, dict] = {}
        self._lock = __import__("threading").Lock()
        self._run_steps: dict[str, str] = {}

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
            step_cost = _calc_cost(
                model,
                data["input_tokens"],
                data["output_tokens"],
                data["thinking_tokens"],
            )

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

    def write_report(self, run_dir: Path) -> Path | None:
        if not self._steps:
            return None

        total_cost = 0.0
        total_in = 0
        total_out = 0
        total_think = 0
        total_dur = 0.0
        rows: list[tuple[str, dict, float]] = []

        for step, data in self._steps.items():
            model = data["model"] or "unknown"
            step_cost = _calc_cost(
                model,
                data["input_tokens"],
                data["output_tokens"],
                data["thinking_tokens"],
            )
            rows.append((step, data, step_cost))
            total_cost += step_cost
            total_in += data["input_tokens"]
            total_out += data["output_tokens"]
            total_think += data["thinking_tokens"]
            total_dur += data["duration"]

        m, s = divmod(int(total_dur), 60)

        draft_words = _count_words(run_dir / "essay" / "draft.md")
        reviewed_words = _count_words(run_dir / "essay" / "reviewed.md")
        target_words = _parse_target_words(run_dir / "plan" / "plan.json")
        sources_count = _count_sources(run_dir / "sources" / "registry.json")
        selected_count = _count_sources(run_dir / "sources" / "selected.json")
        essay_path = run_dir / "essay" / "reviewed.md"
        if not essay_path.exists():
            essay_path = run_dir / "essay" / "draft.md"
        cited_count = _count_cited_sources(essay_path)

        lines = [
            "# Run Report",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Duration | {m}m {s}s |",
            f"| Total cost | ${total_cost:.4f} |",
            f"| Tokens (in / out / think) | {total_in:,} / {total_out:,} / {total_think:,} |",
        ]
        if sources_count:
            lines.append(f"| Sources fetched | {sources_count} |")
        if selected_count:
            lines.append(f"| Sources selected | {selected_count} |")
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

        lines += [
            "",
            "## Steps",
            "",
            "| Step | Model | Time | In | Out | Think | Cost |",
            "|------|-------|-----:|---:|----:|------:|-----:|",
        ]
        for step, data, step_cost in rows:
            model = data["model"] or "—"
            dur = data["duration"]
            dur_str = f"{dur:.0f}s" if dur else "—"
            lines.append(
                f"| {step} | {model} | {dur_str} "
                f"| {data['input_tokens']:,} | {data['output_tokens']:,} "
                f"| {data['thinking_tokens']:,} | ${step_cost:.4f} |"
            )
        lines.append(
            f"| **Total** | | **{m}m {s}s** "
            f"| **{total_in:,}** | **{total_out:,}** "
            f"| **{total_think:,}** | **${total_cost:.4f}** |"
        )
        lines.append("")

        report_path = run_dir / "report.md"
        report_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"  Report: {report_path}", file=sys.stderr)
        return report_path


def _count_words(path: Path) -> int:
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8").split())


def _count_sources(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return len(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValueError):
        return 0


def _count_cited_sources(path: Path) -> int:
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8")
    return len(set(re.findall(r"\[\[([^|\]]+?)(?:\|[^\]]*?)?\]\]", text)))


def _parse_target_words(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
        return plan.get("total_word_target", 0)
    except (json.JSONDecodeError, ValueError):
        return 0


def format_validation_questions(questions: list[ValidationQuestion]) -> str:
    """Format validation questions for CLI display."""
    lines: list[str] = []
    for i, question in enumerate(questions, 1):
        lines.append(f"{i}. {question.question}")
        n = len(question.options)
        sugg = question.suggested_option_index if n else 0
        if n:
            sugg = max(0, min(sugg, n - 1))
        for j, option in enumerate(question.options):
            label = chr(ord("a") + j)
            hint = "  ← suggested default" if j == sugg else ""
            lines.append(f"   {label}) {option}{hint}")
        lines.append("")
    return "\n".join(lines).strip()


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
        label = raw_answer[:1].lower()
        option_index = ord(label) - ord("a")
        if len(raw_answer) == 1 and 0 <= option_index < len(question.options):
            resolved_answer = question.options[option_index]

        clarifications.append(
            Clarification(question=question.question, answer=resolved_answer)
        )

    return clarifications
