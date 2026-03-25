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
from src.agent import create_worker, create_writer  # noqa: E402
from src.intake import build_message_content, scan, stage_files  # noqa: E402
from src.pipeline import run_pipeline  # noqa: E402


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


def _make_callbacks(timer: _StepTimer) -> list:
    from langchain_core.callbacks import BaseCallbackHandler

    class _H(BaseCallbackHandler):
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
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S")
    )
    logging.getLogger().addHandler(handler)
    return handler


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

    timer = _StepTimer()
    callbacks = _make_callbacks(timer)

    try:
        run_pipeline(
            worker, writer, run_dir, config,
            extra_prompt=prompt,
            callbacks=callbacks,
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
    print(summary, file=sys.stderr)
    logger.info("Run summary:\n%s", summary)

    if output_dir:
        from src.analysis import generate_run_report
        generate_run_report(output_dir, output_dir.name)


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

    timer = _StepTimer()
    callbacks = _make_callbacks(timer)

    try:
        run_pipeline(
            worker, writer, run_dir, config,
            callbacks=callbacks,
        )
    finally:
        if log_handler:
            logging.getLogger().removeHandler(log_handler)
            log_handler.close()

    docx_src = Path(config.paths.output_dir) / "essay.docx"
    if output_dir and docx_src.exists():
        shutil.copy2(str(docx_src), str(output_dir / "essay.docx"))

    summary = timer.summary()
    print(summary, file=sys.stderr)
    logger.info("Run summary:\n%s", summary)

    if output_dir:
        from src.analysis import generate_run_report
        generate_run_report(output_dir, output_dir.name)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Essay Writer — AI-powered academic essay generator",
    )
    parser.add_argument(
        "input_path", nargs="?",
        help="Path to a file or directory containing assignment materials.",
    )
    parser.add_argument("--prompt", "-p", default=None, help="Additional instructions or standalone prompt.")
    parser.add_argument("--config", default=None, help="Path to a custom YAML config file.")
    parser.add_argument("--dump-vfs", action="store_true", default=False,
        help="Save outputs to a timestamped directory under .output/.")
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
        run(args.input_path, prompt=args.prompt, config_path=args.config, output_dir=output_dir)


if __name__ == "__main__":
    main()
