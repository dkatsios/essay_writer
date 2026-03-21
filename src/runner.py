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
    2. YAML config file (config/default.yaml or --config override)
    3. Field defaults in config/schemas.py
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)

load_dotenv()

from config.schemas import load_config  # noqa: E402
from src.agent import create_essay_agent  # noqa: E402
from src.intake import build_message_content, scan, stage_files  # noqa: E402


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


def _thread_id_from_dir(output_dir: Path) -> str:
    """Extract the thread_id (timestamp) from a run directory name.

    Expects directory names like 'run_20260321_145536'.
    """
    match = re.search(r"run_(\d{8}_\d{6})", output_dir.name)
    if not match:
        raise ValueError(
            f"Cannot extract thread_id from directory name: {output_dir.name}. "
            "Expected format: run_YYYYMMDD_HHMMSS"
        )
    return match.group(1)


def _create_checkpointer(checkpoint_db: Path):
    """Create a SqliteSaver and eagerly initialize the DB on disk."""
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = sqlite3.connect(str(checkpoint_db), check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    checkpointer.setup()
    return checkpointer


class _ExecutionContext:
    """Shared checkpoint / logging state for a single run."""

    __slots__ = ("thread_id", "checkpointer", "log_handler")

    def __init__(
        self,
        output_dir: Path | None,
        *,
        require_checkpoint_db: bool = False,
    ):
        self.checkpointer = None
        self.thread_id = "default"
        self.log_handler: logging.FileHandler | None = None

        if output_dir is None:
            return

        output_dir.mkdir(parents=True, exist_ok=True)
        self.thread_id = _thread_id_from_dir(output_dir)

        checkpoint_db = output_dir / "checkpoints.db"
        if require_checkpoint_db and not checkpoint_db.exists():
            raise FileNotFoundError(f"No checkpoint DB found at {checkpoint_db}")
        self.checkpointer = _create_checkpointer(checkpoint_db)
        self.log_handler = _setup_file_logging(output_dir)

    def teardown(self) -> None:
        """Remove the file log handler (if any) from the root logger."""
        if self.log_handler is not None:
            logging.getLogger().removeHandler(self.log_handler)
            self.log_handler.close()


def resume(
    output_dir: Path,
    *,
    config_path: str | None = None,
) -> dict:
    """Resume a previous run from its checkpoint DB.

    Args:
        output_dir: The .output/run_<ts>/ directory from a previous run.
            Must contain checkpoints.db. The thread_id is derived from the
            directory name (the timestamp portion).
        config_path: Path to the YAML configuration file.

    Returns:
        The final agent state dict.
    """
    ctx = _ExecutionContext(output_dir, require_checkpoint_db=True)
    config = load_config(config_path)

    agent = create_essay_agent(config, checkpointer=ctx.checkpointer)
    logger.info("Resuming run from %s (thread: %s)", output_dir, ctx.thread_id)

    try:
        result = agent.invoke(
            None,
            config={"configurable": {"thread_id": ctx.thread_id}},
        )
    finally:
        ctx.teardown()

    dump_vfs(agent, ctx.thread_id, output_dir)
    return result


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
            The directory name provides the thread_id for checkpointing.

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

    # Build the message content (plain text or multimodal)
    content = build_message_content(input_files, extra_prompt=prompt)

    # Set up checkpointing and logging BEFORE the slow agent creation
    # so that an early SIGINT still leaves resumable artifacts.
    ctx = _ExecutionContext(output_dir)

    # Create and run the agent
    agent = create_essay_agent(
        config, input_staging_dir=str(staging_dir), checkpointer=ctx.checkpointer
    )

    try:
        result = agent.invoke(
            {"messages": [HumanMessage(content=content)]},
            config={"configurable": {"thread_id": ctx.thread_id}},
        )
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
        ctx.teardown()

    if output_dir is not None:
        dump_vfs(agent, ctx.thread_id, output_dir)

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
            The directory name provides the thread_id for checkpointing.

    Returns:
        The final agent state dict.
    """
    config = load_config(config_path)

    # Set up checkpointing and logging BEFORE the slow agent creation
    ctx = _ExecutionContext(output_dir)

    agent = create_essay_agent(config, checkpointer=ctx.checkpointer)

    try:
        result = agent.invoke(
            {"messages": [HumanMessage(content=prompt)]},
            config={"configurable": {"thread_id": ctx.thread_id}},
        )
    finally:
        ctx.teardown()

    if output_dir is not None:
        dump_vfs(agent, ctx.thread_id, output_dir)

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
            "  %(prog)s --resume .output/run_20260321_145536/\n"
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
        help="Path to a custom YAML config file (default: config/default.yaml).",
    )
    parser.add_argument(
        "--dump-vfs",
        action="store_true",
        default=False,
        help=("Dump VFS contents and logs to a timestamped directory under .output/."),
    )
    parser.add_argument(
        "--resume",
        default=None,
        metavar="DIR",
        help=(
            "Resume a previous run from its checkpoint directory "
            "(e.g., .output/run_20260321_145536/)."
        ),
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

    # Resume mode — continue from a previous checkpoint
    if args.resume is not None:
        resume_dir = Path(args.resume)
        result = resume(resume_dir, config_path=args.config)
    elif args.input_path is None and args.prompt is None:
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
