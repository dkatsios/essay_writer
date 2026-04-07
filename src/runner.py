"""CLI runner for the essay writer pipeline.

Usage (via uv):

    # Point at a directory of assignment files
    uv run python -m src.runner /path/to/assignment/

    # Point at a single file
    uv run python -m src.runner /path/to/brief.pdf

    # Files + additional instructions
    uv run python -m src.runner /path/to/files/ -p "Focus on economic aspects"

    # Prompt-only mode (no files)
    uv run python -m src.runner -p "Write a 3000-word essay on climate change"

    # Custom config
    uv run python -m src.runner /path/to/files/ --config my_config.yaml

    # Save run outputs to .output/run_<timestamp>/
    uv run python -m src.runner /path/to/files/ --dump-run
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from time import monotonic

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()

from config.schemas import load_config  # noqa: E402
from src.agent import create_model  # noqa: E402
from src.intake import build_extracted_text, scan  # noqa: E402
from src.pipeline import run_pipeline  # noqa: E402
from src.schemas import Clarification, ValidationQuestion  # noqa: E402


# ---------------------------------------------------------------------------
# Pricing per 1M tokens (google_genai models)
# ---------------------------------------------------------------------------
# Fallback pricing for unknown models
_DEFAULT_PRICING = {"input": 0.30, "output": 2.50, "thinking": 2.50}


def _pricing_file_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "gemini_pricing.json"


@lru_cache(maxsize=1)
def _load_pricing_table() -> dict[str, dict[str, float]]:
    """Load per-model pricing from the canonical JSON file."""
    try:
        raw = json.loads(_pricing_file_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load pricing table: %s", exc)
        return {}

    pricing: dict[str, dict[str, float]] = {}
    for model, values in raw.items():
        if model.startswith("_") or not isinstance(values, dict):
            continue
        input_price = float(values.get("input", _DEFAULT_PRICING["input"]))
        output_price = float(values.get("output", _DEFAULT_PRICING["output"]))
        pricing[model] = {
            "input": input_price,
            "output": output_price,
            "thinking": float(values.get("thinking", output_price)),
        }
    return pricing


def _model_short_name(full_name: str) -> str:
    """Extract model name from full spec like 'google_genai:gemini-2.5-flash'."""
    if ":" in full_name:
        full_name = full_name.split(":", 1)[1]
    # Remove version suffixes like -001
    for suffix in ("-001", "-002", "-latest"):
        full_name = full_name.removesuffix(suffix)
    return full_name


# ---------------------------------------------------------------------------
# Token tracker — captures per-step LLM token usage and costs
# ---------------------------------------------------------------------------


class TokenTracker:
    """Tracks LLM token usage per pipeline step."""

    def __init__(self) -> None:
        self.current_step: str = "unknown"
        # step -> {input_tokens, output_tokens, thinking_tokens, model, calls}
        self._steps: dict[str, dict] = {}
        self._lock = __import__("threading").Lock()
        # run_id -> step name snapshot (for thread-safe tracking)
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
        """Set the active pipeline step (thread-safe for UI polling)."""
        with self._lock:
            self.current_step = step

    def get_current_step(self) -> str:
        """Current pipeline step name for progress display."""
        with self._lock:
            return self.current_step

    def snapshot_step(self, run_id: str) -> None:
        """Capture current_step for a run_id (call from on_chat_model_start)."""
        with self._lock:
            self._run_steps[run_id] = self.current_step

    def pop_step(self, run_id: str) -> str:
        """Retrieve and remove the step snapshot for a run_id."""
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
        pricing_table = _load_pricing_table()

        for step, data in self._steps.items():
            model = data["model"] or "unknown"
            pricing = pricing_table.get(model, _DEFAULT_PRICING)
            in_cost = data["input_tokens"] * pricing["input"] / 1_000_000
            out_cost = data["output_tokens"] * pricing["output"] / 1_000_000
            think_cost = data["thinking_tokens"] * pricing["thinking"] / 1_000_000
            step_cost = in_cost + out_cost + think_cost

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
        """Write a concise markdown report to run_dir/report.md."""
        if not self._steps:
            return None

        # Gather totals
        total_cost = 0.0
        total_in = 0
        total_out = 0
        total_think = 0
        total_dur = 0.0
        rows: list[tuple[str, dict, float]] = []
        pricing_table = _load_pricing_table()

        for step, data in self._steps.items():
            model = data["model"] or "unknown"
            pricing = pricing_table.get(model, _DEFAULT_PRICING)
            step_cost = (
                data["input_tokens"] * pricing["input"]
                + data["output_tokens"] * pricing["output"]
                + data["thinking_tokens"] * pricing["thinking"]
            ) / 1_000_000
            rows.append((step, data, step_cost))
            total_cost += step_cost
            total_in += data["input_tokens"]
            total_out += data["output_tokens"]
            total_think += data["thinking_tokens"]
            total_dur += data["duration"]

        m, s = divmod(int(total_dur), 60)

        # Word counts from essay files
        draft_words = _count_words(run_dir / "essay" / "draft.md")
        reviewed_words = _count_words(run_dir / "essay" / "reviewed.md")
        target_words = _parse_target_words(run_dir / "plan" / "plan.json")
        sources_count = _count_sources(run_dir / "sources" / "registry.json")

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
            lines.append(f"| Sources | {sources_count} |")
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

        # Step breakdown table
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
    """Count words in a markdown file, 0 if missing."""
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8").split())


def _count_sources(path: Path) -> int:
    """Count entries in registry.json, 0 if missing."""
    if not path.exists():
        return 0
    import json

    try:
        return len(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValueError):
        return 0


def _parse_target_words(path: Path) -> int:
    """Read total word target from plan.json."""
    if not path.exists():
        return 0
    import json

    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
        return plan.get("total_word_target", 0)
    except (json.JSONDecodeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Step timer — prints elapsed time for each tool call
# ---------------------------------------------------------------------------


class _StepTimer:
    """Callback handler that prints elapsed time for tool calls."""

    def __init__(self) -> None:
        self._t0 = monotonic()
        self._tool_starts: dict[str, tuple[float, str]] = {}
        self._steps: list[tuple[str, float]] = []

    def _elapsed(self) -> str:
        secs = monotonic() - self._t0
        m, s = divmod(int(secs), 60)
        return f"{m:02d}:{s:02d}"

    def on_tool_start(self, tool_name: str, run_id: str) -> None:
        self._tool_starts[run_id] = (monotonic(), tool_name)
        print(f"  [{self._elapsed()}] ▶ {tool_name}", file=sys.stderr)

    def on_tool_end(self, tool_name: str, run_id: str) -> None:
        start_info = self._tool_starts.pop(run_id, None)
        if start_info:
            start_time, name = start_info
            dur = monotonic() - start_time
            self._steps.append((name, dur))
            print(f"  [{self._elapsed()}] ✓ {name} ({dur:.1f}s)", file=sys.stderr)
        else:
            print(f"  [{self._elapsed()}] ✓ {tool_name}", file=sys.stderr)

    def on_tool_error(self, tool_name: str, run_id: str) -> None:
        start_info = self._tool_starts.pop(run_id, None)
        if start_info:
            start_time, name = start_info
            dur = monotonic() - start_time
            self._steps.append((f"{name} [ERR]", dur))
            print(f"  [{self._elapsed()}] ✗ {name} ({dur:.1f}s)", file=sys.stderr)
        else:
            print(f"  [{self._elapsed()}] ✗ {tool_name}", file=sys.stderr)

    def summary(self, min_duration: float = 1.0) -> str:
        total = monotonic() - self._t0
        if not self._steps:
            return f"\nTotal wall-clock: {total:.1f}s"
        significant = [(n, d) for n, d in self._steps if d >= min_duration]
        skipped = len(self._steps) - len(significant)
        lines = [
            "",
            "── Step Timing ─────────────────────────────────",
            f"{'#':<4} {'Tool':<25} {'Duration':>10}",
            "─" * 48,
        ]
        for i, (name, dur) in enumerate(significant, 1):
            lines.append(f"{i:<4} {name:<25} {dur:>9.1f}s")
        lines.append("─" * 48)
        if skipped:
            lines.append(f"     ({skipped} steps under {min_duration}s omitted)")
        sum_tools = sum(d for _, d in self._steps)
        overhead = total - sum_tools
        lines.append(f"{'':4} {'Tool time':<25} {sum_tools:>9.1f}s")
        lines.append(f"{'':4} {'Pipeline overhead':<25} {overhead:>9.1f}s")
        m, s = divmod(int(total), 60)
        lines.append(f"{'':4} {'Total':<25} {m}m {s}s")
        return "\n".join(lines)


def _make_callbacks(timer: _StepTimer, tracker: TokenTracker) -> list:
    from langchain_core.callbacks import BaseCallbackHandler

    class _H(BaseCallbackHandler):
        def __init__(self):
            super().__init__()
            self._models: dict[str, str] = {}  # run_id -> model name

        def on_chat_model_start(self, serialized, messages, *, run_id, **kw):
            model = (kw.get("invocation_params") or {}).get("model", "")
            if not model:
                model = serialized.get("kwargs", {}).get("model", "")
            if not model:
                ids = serialized.get("id", [])
                model = ids[-1] if ids else ""
            self._models[str(run_id)] = model
            tracker.snapshot_step(str(run_id))

        def on_llm_end(self, response, *, run_id, **kw):
            model = self._models.pop(str(run_id), "")
            # Try llm_output first
            llm_out = response.llm_output or {}
            usage = llm_out.get("usage_metadata") or llm_out.get("token_usage") or {}
            # Fall back to generation_info on the first generation
            if not usage and response.generations:
                for gen_list in response.generations:
                    for gen in gen_list:
                        info = gen.generation_info or {}
                        usage = info.get("usage_metadata", {})
                        if usage:
                            break
                    if usage:
                        break
            # Also try message.usage_metadata (ChatGeneration stores AIMessage)
            if not usage and response.generations:
                for gen_list in response.generations:
                    for gen in gen_list:
                        msg = getattr(gen, "message", None)
                        if msg:
                            um = getattr(msg, "usage_metadata", None)
                            if um:
                                usage = (
                                    um
                                    if isinstance(um, dict)
                                    else {
                                        "input_tokens": getattr(um, "input_tokens", 0),
                                        "output_tokens": getattr(
                                            um, "output_tokens", 0
                                        ),
                                    }
                                )
                                # Check response_metadata for model
                                rm = getattr(msg, "response_metadata", {}) or {}
                                if not model and "model_name" in rm:
                                    model = rm["model_name"]
                                break
                    if usage:
                        break

            in_tok = (
                usage.get("input_tokens")
                or usage.get("prompt_tokens")
                or usage.get("prompt_token_count")
                or 0
            )
            raw_out = (
                usage.get("output_tokens")
                or usage.get("completion_tokens")
                or usage.get("candidates_token_count")
                or 0
            )
            # Thinking tokens are nested under output_token_details.reasoning
            details = usage.get("output_token_details") or {}
            think_tok = details.get("reasoning", 0) if isinstance(details, dict) else 0
            # output_tokens from API includes thinking; separate them for billing
            out_tok = max(raw_out - think_tok, 0)
            if in_tok or out_tok or think_tok:
                step = tracker.pop_step(str(run_id))
                tracker.record(model, in_tok, out_tok, think_tok, step=step)

        def on_tool_start(self, serialized, input_str, *, run_id, **kw):
            timer.on_tool_start(serialized.get("name", "unknown"), str(run_id))

        def on_tool_end(self, output, *, run_id, **kw):
            timer.on_tool_end(getattr(output, "name", None) or "tool", str(run_id))

        def on_tool_error(self, error, *, run_id, **kw):
            timer.on_tool_error("tool", str(run_id))

    return [_H()]


def _setup_file_logging(output_dir: Path) -> logging.FileHandler:
    log_path = output_dir / "run.log"
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S"
        )
    )
    logging.getLogger().addHandler(handler)
    return handler


# ---------------------------------------------------------------------------
# Validation callback
# ---------------------------------------------------------------------------


def _format_validation_questions(questions: list[ValidationQuestion]) -> str:
    """Format validation questions for CLI display."""
    lines: list[str] = []
    for i, question in enumerate(questions, 1):
        lines.append(f"{i}. {question.question}")
        for j, option in enumerate(question.options):
            lines.append(f"   {chr(ord('a') + j)}) {option}")
        lines.append("")
    return "\n".join(lines).strip()


def _parse_validation_answers(
    questions: list[ValidationQuestion],
    answers: str,
) -> list[Clarification]:
    """Parse a compact CLI answer string into per-question clarifications."""
    import re

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
            Clarification(
                question=question.question,
                answer=resolved_answer,
            )
        )

    return clarifications


def _handle_questions(questions: list[ValidationQuestion], run_dir: Path) -> None:
    """Print validator questions, collect answers via stdin, append to brief."""
    print(
        "\n"
        + "=" * 50
        + "\n  The assignment brief has gaps that may affect quality."
        + "\n  Please answer the following:\n",
        file=sys.stderr,
    )
    print(_format_validation_questions(questions), file=sys.stderr)
    print(
        "\n  Enter answers (e.g. '1. a, 2. c') or press Enter to skip:",
        file=sys.stderr,
    )
    answers = input("> ").strip()
    if not answers:
        return
    from src.schemas import AssignmentBrief

    brief_path = run_dir / "brief" / "assignment.json"
    brief = AssignmentBrief.model_validate_json(brief_path.read_text(encoding="utf-8"))
    if brief.clarifications is None:
        brief.clarifications = []
    brief.clarifications.extend(_parse_validation_answers(questions, answers))
    brief_path.write_text(
        brief.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Run entry points
# ---------------------------------------------------------------------------


def run(
    input_path: str,
    *,
    prompt: str | None = None,
    config_path: str | None = None,
    output_dir: Path | None = None,
    user_sources_dir: Path | None = None,
) -> None:
    """Run the essay pipeline with files (and optional prompt)."""
    config = load_config(config_path)

    # Scan and extract
    input_files = scan(input_path)
    for f in input_files:
        status = f.category if not f.warning else f"SKIPPED ({f.warning})"
        print(f"  [{status}] {f.path.name}", file=sys.stderr)
    extracted_text = build_extracted_text(input_files, extra_prompt=prompt)

    # Setup run directory
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = output_dir or Path(".output/scratch")
    run_dir.mkdir(parents=True, exist_ok=True)

    log_handler = _setup_file_logging(run_dir) if output_dir else None

    # Write extracted text for intake
    input_dir = run_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / "extracted.md").write_text(extracted_text, encoding="utf-8")

    # Create models
    worker = create_model(config.models.worker)
    writer = create_model(config.models.writer)
    reviewer = create_model(config.models.reviewer)

    timer = _StepTimer()
    tracker = TokenTracker()
    callbacks = _make_callbacks(timer, tracker)

    try:
        run_pipeline(
            worker,
            writer,
            reviewer,
            run_dir,
            config,
            extra_prompt=prompt,
            callbacks=callbacks,
            token_tracker=tracker,
            on_questions=_handle_questions
            if config.writing.interactive_validation
            else None,
            user_sources_dir=user_sources_dir,
        )
    finally:
        if log_handler:
            logging.getLogger().removeHandler(log_handler)
            log_handler.close()

    # Copy docx to run dir
    docx_src = Path(config.paths.output_dir) / "essay.docx"
    if output_dir and docx_src.exists():
        shutil.copy2(str(docx_src), str(output_dir / "essay.docx"))

    summary = timer.summary()
    cost = tracker.cost_summary()
    print(summary, file=sys.stderr)
    print(cost, file=sys.stderr)
    logger.info("Run summary:\n%s\n%s", summary, cost)

    tracker.write_report(run_dir)


def run_prompt(
    prompt: str,
    *,
    config_path: str | None = None,
    output_dir: Path | None = None,
    user_sources_dir: Path | None = None,
) -> None:
    """Run the essay pipeline with a plain text prompt (no files)."""
    config = load_config(config_path)

    run_dir = output_dir or Path(".output/scratch")
    run_dir.mkdir(parents=True, exist_ok=True)

    log_handler = _setup_file_logging(run_dir) if output_dir else None

    # Write prompt as extracted content
    input_dir = run_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / "extracted.md").write_text(
        f"# Assignment\n\n{prompt}\n", encoding="utf-8"
    )

    worker = create_model(config.models.worker)
    writer = create_model(config.models.writer)
    reviewer = create_model(config.models.reviewer)

    timer = _StepTimer()
    tracker = TokenTracker()
    callbacks = _make_callbacks(timer, tracker)

    try:
        run_pipeline(
            worker,
            writer,
            reviewer,
            run_dir,
            config,
            callbacks=callbacks,
            token_tracker=tracker,
            on_questions=_handle_questions
            if config.writing.interactive_validation
            else None,
            user_sources_dir=user_sources_dir,
        )
    finally:
        if log_handler:
            logging.getLogger().removeHandler(log_handler)
            log_handler.close()

    docx_src = Path(config.paths.output_dir) / "essay.docx"
    if output_dir and docx_src.exists():
        shutil.copy2(str(docx_src), str(output_dir / "essay.docx"))

    summary = timer.summary()
    cost = tracker.cost_summary()
    print(summary, file=sys.stderr)
    print(cost, file=sys.stderr)
    logger.info("Run summary:\n%s\n%s", summary, cost)

    tracker.write_report(run_dir)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Essay Writer — AI-powered academic essay generator",
    )
    parser.add_argument(
        "input_path",
        nargs="?",
        help="Path to a file or directory containing assignment materials.",
    )
    parser.add_argument(
        "--prompt",
        "-p",
        default=None,
        help="Additional instructions or standalone prompt.",
    )
    parser.add_argument(
        "--config", default=None, help="Path to a custom YAML config file."
    )
    parser.add_argument(
        "--sources",
        "-s",
        default=None,
        help="Path to a file or directory containing user-provided reference sources.",
    )
    parser.add_argument(
        "--dump-run",
        "--dump-vfs",
        dest="dump_run",
        action="store_true",
        default=False,
        help="Save run outputs to a timestamped directory under .output/.",
    )
    args = parser.parse_args()

    output_dir = None
    if args.dump_run:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_dir = Path(".output") / f"run_{timestamp}"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("langchain").setLevel(logging.WARNING)

    sources_dir = Path(args.sources) if args.sources else None

    if args.input_path is None and args.prompt is None:
        if sys.stdin.isatty():
            parser.print_help()
            sys.exit(1)
        prompt_text = sys.stdin.read().strip()
        if not prompt_text:
            print("Error: No input provided.", file=sys.stderr)
            sys.exit(1)
        run_prompt(
            prompt_text,
            config_path=args.config,
            output_dir=output_dir,
            user_sources_dir=sources_dir,
        )
    elif args.input_path is None:
        run_prompt(
            args.prompt,
            config_path=args.config,
            output_dir=output_dir,
            user_sources_dir=sources_dir,
        )
    else:
        run(
            args.input_path,
            prompt=args.prompt,
            config_path=args.config,
            output_dir=output_dir,
            user_sources_dir=sources_dir,
        )


if __name__ == "__main__":
    main()
