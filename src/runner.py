"""CLI and programmatic runner for the essay writer agent.

Usage (via uv):

    # Point at a directory of assignment files
    uv run python -m src.runner /path/to/assignment/

    # Point at a single file
    uv run python -m src.runner /path/to/brief.pdf

    # Files + additional instructions
    uv run python -m src.runner /path/to/files/ -p "Focus on economic aspects"

    # Prompt-only mode (no files)
    uv run python -m src.runner -p "Write a 3000-word essay on climate change"

    # Custom config file
    uv run python -m src.runner /path/to/files/ --config my_config.yaml

Supported input file types: .md, .txt, .rst, .csv, .pdf, .docx, .pptx,
.png, .jpg, .jpeg, .gif, .bmp, .tiff, .webp

Configuration priority (highest wins):
    1. Environment variables (prefix ESSAY_WRITER_, nested with __)
    2. Custom YAML config file (via --config)
    3. Field defaults in config/schemas.py
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)

load_dotenv()

from config.schemas import load_config  # noqa: E402
from src.agent import create_essay_agent  # noqa: E402
from src.intake import build_message_content, scan, stage_files  # noqa: E402


# ---------------------------------------------------------------------------
# Simple step timer — prints elapsed time for each tool / subagent call
# ---------------------------------------------------------------------------


class _StepTimer:
    """Callback handler that prints elapsed time for tool and subagent calls."""

    def __init__(self) -> None:
        self._t0 = monotonic()
        self._tool_starts: dict[str, tuple[float, str]] = {}  # run_id -> (time, name)
        self._steps: list[tuple[str, float]] = []  # (tool_name, duration)

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
            start_time, original_name = start_info
            dur = monotonic() - start_time
            self._steps.append((original_name, dur))
            print(
                f"  [{self._elapsed()}] ✓ {original_name} ({dur:.1f}s)",
                file=sys.stderr,
            )
        else:
            print(f"  [{self._elapsed()}] ✓ {tool_name}", file=sys.stderr)

    def on_tool_error(self, tool_name: str, run_id: str) -> None:
        start_info = self._tool_starts.pop(run_id, None)
        if start_info:
            start_time, original_name = start_info
            dur = monotonic() - start_time
            self._steps.append((f"{original_name} [ERR]", dur))
            print(
                f"  [{self._elapsed()}] ✗ {original_name} ({dur:.1f}s)",
                file=sys.stderr,
            )
        else:
            print(f"  [{self._elapsed()}] ✗ {tool_name}", file=sys.stderr)

    def summary(self) -> str:
        total = monotonic() - self._t0
        if not self._steps:
            return f"\nTotal wall-clock: {total:.1f}s"
        lines = [
            "",
            "── Step Timing ─────────────────────────────────",
            f"{'#':<4} {'Tool':<25} {'Duration':>10}",
            "─" * 48,
        ]
        for i, (name, dur) in enumerate(self._steps, 1):
            lines.append(f"{i:<4} {name:<25} {dur:>9.1f}s")

        lines.append("─" * 48)
        sum_tools = sum(d for _, d in self._steps)
        overhead = total - sum_tools
        lines.append(f"{'':4} {'Tool time':<25} {sum_tools:>9.1f}s")
        lines.append(f"{'':4} {'Orchestrator thinking':<25} {overhead:>9.1f}s")
        m, s = divmod(int(total), 60)
        lines.append(f"{'':4} {'Total':<25} {m}m {s}s")
        return "\n".join(lines)


def _make_callbacks(timer: _StepTimer) -> list:
    """Build a LangChain callback handler list from a StepTimer."""
    from langchain_core.callbacks import BaseCallbackHandler

    class _Handler(BaseCallbackHandler):
        def on_tool_start(self, serialized, input_str, *, run_id, **kw):
            name = serialized.get("name", "unknown")
            timer.on_tool_start(name, str(run_id))

        def on_tool_end(self, output, *, run_id, **kw):
            name = getattr(output, "name", None) or "tool"
            timer.on_tool_end(name, str(run_id))

        def on_tool_error(self, error, *, run_id, **kw):
            timer.on_tool_error("tool", str(run_id))

    return [_Handler()]


def _setup_file_logging(output_dir: Path) -> logging.FileHandler:
    """Attach a DEBUG-level file handler to the root logger."""
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


def dump_vfs(
    agent: CompiledStateGraph,
    thread_id: str,
    output_dir: Path,
) -> None:
    """Dump the VFS contents from an agent's graph state to a local directory.

    Args:
        agent: The compiled agent graph (used to retrieve full state).
        thread_id: The thread ID for the run.
        output_dir: Directory to write VFS files into (files go under vfs/ subdirectory).
    """
    state = agent.get_state({"configurable": {"thread_id": thread_id}})
    vfs_files = state.values.get("files", {})
    if not vfs_files:
        logger.warning("No VFS files found in agent state.")
        return

    vfs_dir = output_dir / "vfs"
    vfs_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for vfs_path, file_data in vfs_files.items():
        # Skip skill files — those are seeded inputs, not agent outputs
        if vfs_path.startswith("/skills/"):
            continue
        file_content = "\n".join(file_data["content"])
        dest = vfs_dir / vfs_path.lstrip("/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(file_content, encoding="utf-8")
        written += 1
    logger.info("VFS dumped to %s (%d files)", vfs_dir, written)


_THREAD_ID = "essay_run"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Resilient invoke — retry on malformed_function_call from proxy
# ---------------------------------------------------------------------------

_MAX_RETRIES = 5


def _invoke_with_retry(
    agent: CompiledStateGraph,
    initial_input,
    thread_id: str,
    callbacks: list,
    run_tags: list[str] | None = None,
) -> dict:
    """Invoke the agent, retrying on malformed_function_call finish reasons.

    The PwC proxy sometimes returns ``finish_reason: malformed_function_call``
    with ``completion_tokens: 0``, causing the agent to stop prematurely.
    When this happens, we re-invoke via ``agent.invoke(None, ...)`` which
    continues from the last checkpoint.
    """
    invoke_config = {
        "configurable": {"thread_id": thread_id},
        "callbacks": callbacks,
        "recursion_limit": 150,
    }
    if run_tags:
        invoke_config["tags"] = run_tags

    result = agent.invoke(initial_input, config=invoke_config)

    for attempt in range(1, _MAX_RETRIES + 1):
        msgs = result.get("messages", [])
        if not msgs:
            break
        last = msgs[-1]
        resp_meta = getattr(last, "response_metadata", {})
        finish = resp_meta.get("finish_reason", "")
        usage = getattr(last, "usage_metadata", None) or {}
        output_tokens = (
            usage.get("output_tokens", -1) if isinstance(usage, dict) else -1
        )
        needs_retry = finish.lower() == "malformed_function_call" or (
            finish == "STOP" and output_tokens == 0
        )
        if not needs_retry:
            break

        print(
            f"\n⚠ {finish} (output_tokens={output_tokens}) — retrying ({attempt}/{_MAX_RETRIES})…",
            file=sys.stderr,
        )
        logger.warning(
            "%s (output_tokens=%s, attempt %d/%d, %d msgs). Retrying.",
            finish,
            output_tokens,
            attempt,
            _MAX_RETRIES,
            len(msgs),
        )
        # Continue from checkpoint with a nudge message
        result = agent.invoke(
            {
                "messages": [
                    HumanMessage(content="Continue. Complete your current step.")
                ]
            },
            config=invoke_config,
        )

    return result


# ---------------------------------------------------------------------------
# Deterministic fallback — ensure review + export always happens
# ---------------------------------------------------------------------------

_NUDGE_MSG = (
    "STOP. Step 7 (EXPORT) was not completed. "
    "You MUST call build_docx NOW. "
    "Call build_docx with output_path='/output/essay.docx' and config_json. "
    "The tool reads the essay and sources automatically. "
    "This is MANDATORY — do it immediately."
)


def _docx_mtime(config) -> float | None:
    """Return the mtime of essay.docx if it exists, else None."""
    p = Path(config.paths.output_dir) / "essay.docx"
    return p.stat().st_mtime if p.exists() else None


def _copy_docx_to_run_dir(config, output_dir: Path) -> None:
    """Copy essay.docx into the timestamped run directory."""
    src = Path(config.paths.output_dir) / "essay.docx"
    if src.exists():
        dest = output_dir / "essay.docx"
        shutil.copy2(str(src), str(dest))
        logger.info("Copied essay.docx to %s", dest)


def _print_summary(timer: _StepTimer) -> None:
    """Print step timing summary to stderr and log."""
    summary = timer.summary()
    print(summary, file=sys.stderr)
    logger.info("Run summary:\n%s", summary)


def _ensure_docx_output(
    agent: CompiledStateGraph,
    thread_id: str,
    config,
    sources_dir: str | None,
    callbacks: list,
    docx_mtime_before: float | None = None,
    run_tags: list[str] | None = None,
) -> None:
    """Check if essay.docx was produced; if not, nudge then direct-build.

    Args:
        docx_mtime_before: mtime of essay.docx before the run started.
            Used to detect whether this run (vs. a prior run) created the file.
    """
    docx_path = Path(config.paths.output_dir) / "essay.docx"
    if docx_path.exists():
        current_mtime = docx_path.stat().st_mtime
        if docx_mtime_before is None or current_mtime > docx_mtime_before:
            return  # This run produced the file

    state = agent.get_state({"configurable": {"thread_id": thread_id}})
    vfs = state.values.get("files", {})

    # Essay now lives on disk via FilesystemBackend
    essay_dir = str(Path(sources_dir).parent / "essay") if sources_dir else None
    essay_path = Path(essay_dir) / "draft.md" if essay_dir else None
    if not (essay_path and essay_path.exists()):
        # Fall back to VFS state check (prompt-only mode)
        if "/essay/draft.md" not in vfs:
            logger.warning("No draft found — cannot produce docx fallback.")
            return

    # Tier 1: nudge the agent to call build_docx
    print("\n⚠ essay.docx missing — nudging agent…", file=sys.stderr)
    nudge_config = {
        "configurable": {"thread_id": thread_id},
        "callbacks": callbacks,
    }
    if run_tags:
        nudge_config["tags"] = run_tags
    try:
        agent.invoke(
            {"messages": [HumanMessage(content=_NUDGE_MSG)]},
            config=nudge_config,
        )
    except Exception:
        logger.exception("Nudge invocation failed")

    if docx_path.exists():
        return

    # Tier 2: build docx directly from the draft (skip review)
    # Re-read state in case the nudge updated VFS
    state = agent.get_state({"configurable": {"thread_id": thread_id}})
    vfs = state.values.get("files", {})
    _direct_build_docx(vfs, config, sources_dir)


def _direct_build_docx(
    vfs: dict,
    config,
    sources_dir: str | None,
) -> None:
    """Last-resort fallback: call _build_document directly from Python."""
    from src.tools.docx_builder import _build_document

    # Read essay from disk (FilesystemBackend route) first, fall back to VFS state
    essay_text = None
    essay_dir = str(Path(sources_dir).parent / "essay") if sources_dir else None
    if essay_dir:
        essay_path = Path(essay_dir) / "draft.md"
        if essay_path.exists():
            essay_text = essay_path.read_text(encoding="utf-8")
    if not essay_text:
        final_data = vfs.get("/essay/draft.md")
        if not final_data:
            logger.error("No essay text found for direct build.")
            return
        essay_text = "\n".join(final_data.get("content", []))

    # Read source registry from disk
    sources: dict = {}
    if sources_dir:
        registry_path = Path(sources_dir) / "registry.json"
        if registry_path.exists():
            raw = registry_path.read_text(encoding="utf-8")
            try:
                sources = json.loads(raw)
            except json.JSONDecodeError:
                # LLMs sometimes write double-escaped JSON (\n, \")
                sources = json.loads(raw.encode().decode("unicode_escape"))

    # Build config dict from settings + cover page info from brief
    fmt = config.formatting
    doc_config = fmt.model_dump()

    brief_data = vfs.get("/brief/assignment.md")
    if brief_data:
        brief_text = "\n".join(brief_data.get("content", []))
        for line in brief_text.split("\n"):
            if line.startswith("# "):
                doc_config.setdefault("title", line[2:].strip())
                break

    print("⚠ Building docx directly from draft (review skipped)…", file=sys.stderr)
    doc = _build_document(essay_text, doc_config, sources)

    output_path = Path(config.paths.output_dir) / "essay.docx"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    logger.info("Fallback: essay.docx saved to %s", output_path)
    print(f"✓ essay.docx saved to {output_path}", file=sys.stderr)


def run(
    input_path: str,
    *,
    prompt: str | None = None,
    config_path: str | None = None,
    output_dir: Path | None = None,
) -> dict:
    """Run the essay writer agent with input from a file or directory.

    Args:
        input_path: Path to a file or directory containing assignment materials.
        prompt: Optional additional instructions to append.
        config_path: Path to the YAML configuration file.
        output_dir: If set, dump VFS and logs to this directory after the run.

    Returns:
        The final agent state dict.
    """
    config = load_config(config_path)

    # Scan and extract content from input files
    input_files = scan(input_path)

    # Print what was found
    for f in input_files:
        status = f.category if not f.warning else f"SKIPPED ({f.warning})"
        print(f"  [{status}] {f.path.name}", file=sys.stderr)

    # Stage files into a temp directory for the agent's /input/ backend
    staging_dir = stage_files(input_files)

    # Build orchestrator summary (text-only) and extracted content for worker
    orchestrator_summary, extracted_text = build_message_content(
        input_files, extra_prompt=prompt
    )

    # Write extracted content to staging dir so worker reads it from /input/
    (Path(staging_dir) / "extracted.md").write_text(extracted_text, encoding="utf-8")

    # Sources dir persists downloaded PDFs alongside run artifacts
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
    sources_dir = str(output_dir / "sources") if output_dir else None

    log_handler = _setup_file_logging(output_dir) if output_dir else None

    # Create and run the agent
    agent = create_essay_agent(
        config,
        input_staging_dir=str(staging_dir),
        sources_dir=sources_dir,
    )

    timer = _StepTimer()
    callbacks = _make_callbacks(timer)
    mtime_before = _docx_mtime(config)
    run_tag = output_dir.name if output_dir else None
    run_tags = [run_tag] if run_tag else None

    try:
        result = _invoke_with_retry(
            agent,
            {"messages": [HumanMessage(content=orchestrator_summary)]},
            _THREAD_ID,
            callbacks,
            run_tags=run_tags,
        )
        _ensure_docx_output(
            agent,
            _THREAD_ID,
            config,
            sources_dir,
            callbacks,
            mtime_before,
            run_tags=run_tags,
        )
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
        if log_handler:
            logging.getLogger().removeHandler(log_handler)
            log_handler.close()

    if output_dir is not None:
        dump_vfs(agent, _THREAD_ID, output_dir)
        _copy_docx_to_run_dir(config, output_dir)

    _print_summary(timer)

    if output_dir and run_tag:
        from src.analysis import generate_run_report

        generate_run_report(output_dir, run_tag)

    return result


def run_prompt(
    prompt: str,
    *,
    config_path: str | None = None,
    output_dir: Path | None = None,
) -> dict:
    """Run the essay writer agent with a plain text prompt (no files).

    Args:
        prompt: The essay assignment prompt.
        config_path: Path to the YAML configuration file.
        output_dir: If set, dump VFS and logs to this directory after the run.

    Returns:
        The final agent state dict.
    """
    config = load_config(config_path)

    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
    sources_dir = str(output_dir / "sources") if output_dir else None

    log_handler = _setup_file_logging(output_dir) if output_dir else None

    agent = create_essay_agent(
        config,
        sources_dir=sources_dir,
    )

    timer = _StepTimer()
    callbacks = _make_callbacks(timer)
    mtime_before = _docx_mtime(config)
    run_tag = output_dir.name if output_dir else None
    run_tags = [run_tag] if run_tag else None

    try:
        result = _invoke_with_retry(
            agent,
            {"messages": [HumanMessage(content=prompt)]},
            _THREAD_ID,
            callbacks,
            run_tags=run_tags,
        )
        _ensure_docx_output(
            agent,
            _THREAD_ID,
            config,
            sources_dir,
            callbacks,
            mtime_before,
            run_tags=run_tags,
        )
    finally:
        if log_handler:
            logging.getLogger().removeHandler(log_handler)
            log_handler.close()

    if output_dir is not None:
        dump_vfs(agent, _THREAD_ID, output_dir)
        _copy_docx_to_run_dir(config, output_dir)

    _print_summary(timer)

    if output_dir and run_tag:
        from src.analysis import generate_run_report

        generate_run_report(output_dir, run_tag)

    return result


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Essay Writer — AI-powered academic essay generator",
        epilog=(
            "Examples:\n"
            "  %(prog)s /path/to/assignment/\n"
            "  %(prog)s /path/to/brief.pdf\n"
            '  %(prog)s /path/to/files/ --prompt "Focus on economic aspects"\n'
            '  %(prog)s --prompt "Write a 3000-word essay on climate change"\n'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
        help="Additional instructions or a standalone text prompt (if no input path).",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to a custom YAML config file.",
    )
    parser.add_argument(
        "--dump-vfs",
        action="store_true",
        default=False,
        help=("Dump VFS contents and logs to a timestamped directory under .output/."),
    )
    args = parser.parse_args()

    # Resolve output directory for VFS dump and logs
    output_dir = None
    if args.dump_vfs:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_dir = Path(".output") / f"run_{timestamp}"

    # Configure console logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Suppress noisy loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("deepagents.middleware.skills").setLevel(logging.ERROR)

    if args.input_path is None and args.prompt is None:
        if sys.stdin.isatty():
            parser.print_help()
            sys.exit(1)
        # Read prompt from stdin
        prompt_text = sys.stdin.read().strip()
        if not prompt_text:
            print("Error: No input provided.", file=sys.stderr)
            sys.exit(1)
        result = run_prompt(
            prompt_text,
            config_path=args.config,
            output_dir=output_dir,
        )
    elif args.input_path is None:
        # Prompt-only mode (no files)
        result = run_prompt(
            args.prompt,
            config_path=args.config,
            output_dir=output_dir,
        )
    else:
        # File/directory input mode
        result = run(
            args.input_path,
            prompt=args.prompt,
            config_path=args.config,
            output_dir=output_dir,
        )

    # Print the final assistant message
    messages = result.get("messages", [])
    if messages:
        last_msg = messages[-1]
        print(last_msg.content)


if __name__ == "__main__":
    main()
