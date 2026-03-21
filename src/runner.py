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
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage

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


def dump_vfs(result: dict, output_dir: Path) -> None:
    """Dump the VFS contents from an agent result to a local directory.

    Args:
        result: The final agent state dict (must contain a 'files' key).
        output_dir: Directory to write VFS files into (files go under vfs/ subdirectory).
    """
    vfs_files = result.get("files", {})
    if not vfs_files:
        logger.warning("No VFS files found in agent result.")
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


def run(
    input_path: str,
    *,
    prompt: str | None = None,
    config_path: str | None = None,
    thread_id: str | None = None,
    output_dir: Path | None = None,
) -> dict:
    """Run the essay writer agent with input from a file or directory.

    Args:
        input_path: Path to a file or directory containing assignment materials.
        prompt: Optional additional instructions to append.
        config_path: Path to the YAML configuration file.
        thread_id: Optional thread ID for conversation continuity.
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

    # Build the message content (plain text or multimodal)
    content = build_message_content(input_files, extra_prompt=prompt)

    # Create and run the agent
    agent = create_essay_agent(config, input_staging_dir=str(staging_dir))
    if thread_id is None:
        thread_id = uuid.uuid4().hex

    # Set up file logging if output directory is requested
    log_handler = None
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        log_handler = _setup_file_logging(output_dir)

    try:
        result = agent.invoke(
            {"messages": [HumanMessage(content=content)]},
            config={"configurable": {"thread_id": thread_id}},
        )
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
        if log_handler is not None:
            logging.getLogger().removeHandler(log_handler)
            log_handler.close()

    if output_dir is not None:
        dump_vfs(result, output_dir)

    return result


def run_prompt(
    prompt: str,
    *,
    config_path: str | None = None,
    thread_id: str | None = None,
    output_dir: Path | None = None,
) -> dict:
    """Run the essay writer agent with a plain text prompt (no files).

    Args:
        prompt: The essay assignment prompt.
        config_path: Path to the YAML configuration file.
        thread_id: Optional thread ID for conversation continuity.
        output_dir: If set, dump VFS and logs to this directory after the run.

    Returns:
        The final agent state dict.
    """
    config = load_config(config_path)
    agent = create_essay_agent(config)

    if thread_id is None:
        thread_id = uuid.uuid4().hex

    # Set up file logging if output directory is requested
    log_handler = None
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        log_handler = _setup_file_logging(output_dir)

    try:
        result = agent.invoke(
            {"messages": [HumanMessage(content=prompt)]},
            config={"configurable": {"thread_id": thread_id}},
        )
    finally:
        if log_handler is not None:
            logging.getLogger().removeHandler(log_handler)
            log_handler.close()

    if output_dir is not None:
        dump_vfs(result, output_dir)

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
        help="Path to a custom YAML config file (default: config/default.yaml).",
    )
    parser.add_argument(
        "--thread-id",
        default=None,
        help="Thread ID for conversation continuity.",
    )
    parser.add_argument(
        "--dump-vfs",
        nargs="?",
        const="auto",
        default=None,
        metavar="DIR",
        help=(
            "Dump VFS contents and logs to a directory after the run. "
            "If DIR is omitted, creates a timestamped directory under .output/."
        ),
    )
    args = parser.parse_args()

    # Resolve output directory for VFS dump and logs
    output_dir = None
    if args.dump_vfs is not None:
        if args.dump_vfs == "auto":
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            output_dir = Path(".output") / f"run_{timestamp}"
        else:
            output_dir = Path(args.dump_vfs)

    # Configure console logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

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
            thread_id=args.thread_id,
            output_dir=output_dir,
        )
    elif args.input_path is None:
        # Prompt-only mode (no files)
        result = run_prompt(
            args.prompt,
            config_path=args.config,
            thread_id=args.thread_id,
            output_dir=output_dir,
        )
    else:
        # File/directory input mode
        result = run(
            args.input_path,
            prompt=args.prompt,
            config_path=args.config,
            thread_id=args.thread_id,
            output_dir=output_dir,
        )

    # Print the final assistant message
    messages = result.get("messages", [])
    if messages:
        last_msg = messages[-1]
        print(last_msg.content)


if __name__ == "__main__":
    main()
