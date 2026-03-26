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
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()

from config.schemas import load_config  # noqa: E402
from src.agent import create_reviewer, create_worker, create_writer  # noqa: E402
from src.intake import build_message_content, scan, stage_files  # noqa: E402
from src.pipeline import run_pipeline  # noqa: E402


# ---------------------------------------------------------------------------
# Pricing per 1M tokens (google_genai models)
# ---------------------------------------------------------------------------
_PRICING: dict[str, dict[str, float]] = {
    "gemini-3.1-pro-preview": {"input": 2.00, "output": 12.00, "thinking": 12.00},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00, "thinking": 10.00},
    "gemini-3-flash-preview": {"input": 0.50, "output": 3.00, "thinking": 3.00},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "thinking": 2.50},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40, "thinking": 0.0},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40, "thinking": 0.0},
}
# Fallback pricing for unknown models
_DEFAULT_PRICING = {"input": 0.30, "output": 2.50, "thinking": 2.50}


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

    def record(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        thinking_tokens: int = 0,
    ) -> None:
        data = self._ensure_step(self.current_step)
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
            pricing = _PRICING.get(model, _DEFAULT_PRICING)
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

        for step, data in self._steps.items():
            model = data["model"] or "unknown"
            pricing = _PRICING.get(model, _DEFAULT_PRICING)
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
        target_words = _parse_target_words(run_dir / "plan" / "plan.md")
        sources_count = _count_sources(run_dir / "sources" / "registry.json")

        lines = [
            "# Run Report",
            "",
            f"| Metric | Value |",
            f"|--------|-------|",
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
    """Sum word targets from plan.md (lines like '- **Word target**: 350 words')."""
    if not path.exists():
        return 0
    import re

    total = 0
    for match in re.finditer(
        r"\*\*Word target\*\*:\s*(\d[\d,]*)", path.read_text(encoding="utf-8")
    ):
        total += int(match.group(1).replace(",", ""))
    return total


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
                tracker.record(model, in_tok, out_tok, think_tok)

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


def _handle_questions(questions: str, run_dir: Path) -> None:
    """Print validator questions, collect answers via stdin, append to brief."""
    print(
        "\n"
        + "=" * 50
        + "\n  The assignment brief has gaps that may affect quality."
        + "\n  Please answer the following:\n",
        file=sys.stderr,
    )
    print(questions, file=sys.stderr)
    print(
        "\n  Enter answers (e.g. '1. a, 2. c') or press Enter to skip:",
        file=sys.stderr,
    )
    answers = input("> ").strip()
    if not answers:
        return
    brief_path = run_dir / "brief" / "assignment.md"
    text = brief_path.read_text(encoding="utf-8")
    text += f"\n\n## Clarifications\n\n{questions}\n\n**User answers**: {answers}\n"
    brief_path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Run entry points
# ---------------------------------------------------------------------------


def run(
    input_path: str,
    *,
    prompt: str | None = None,
    config_path: str | None = None,
    output_dir: Path | None = None,
) -> None:
    """Run the essay pipeline with files (and optional prompt)."""
    config = load_config(config_path)

    # Scan and extract
    input_files = scan(input_path)
    for f in input_files:
        status = f.category if not f.warning else f"SKIPPED ({f.warning})"
        print(f"  [{status}] {f.path.name}", file=sys.stderr)

    staging_dir = stage_files(input_files)
    _, extracted_text = build_message_content(input_files, extra_prompt=prompt)
    (Path(staging_dir) / "extracted.md").write_text(extracted_text, encoding="utf-8")

    # Setup run directory
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = output_dir or Path(".output/scratch")
    run_dir.mkdir(parents=True, exist_ok=True)

    log_handler = _setup_file_logging(run_dir) if output_dir else None

    # Create agents
    worker = create_worker(config, run_dir, input_staging_dir=str(staging_dir))
    writer = create_writer(config, run_dir)
    reviewer = create_reviewer(config, run_dir)

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
            on_questions=_handle_questions,
        )
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
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

    worker = create_worker(config, run_dir, input_staging_dir=str(input_dir))
    writer = create_writer(config, run_dir)
    reviewer = create_reviewer(config, run_dir)

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
            on_questions=_handle_questions,
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
        "--dump-vfs",
        action="store_true",
        default=False,
        help="Save outputs to a timestamped directory under .output/.",
    )
    args = parser.parse_args()

    output_dir = None
    if args.dump_vfs:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_dir = Path(".output") / f"run_{timestamp}"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("deepagents.middleware.skills").setLevel(logging.ERROR)

    if args.input_path is None and args.prompt is None:
        if sys.stdin.isatty():
            parser.print_help()
            sys.exit(1)
        prompt_text = sys.stdin.read().strip()
        if not prompt_text:
            print("Error: No input provided.", file=sys.stderr)
            sys.exit(1)
        run_prompt(prompt_text, config_path=args.config, output_dir=output_dir)
    elif args.input_path is None:
        run_prompt(args.prompt, config_path=args.config, output_dir=output_dir)
    else:
        run(
            args.input_path,
            prompt=args.prompt,
            config_path=args.config,
            output_dir=output_dir,
        )


if __name__ == "__main__":
    main()
