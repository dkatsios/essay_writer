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
import shutil
import sys
import uuid

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage

load_dotenv()

from config.schemas import load_config
from src.agent import create_essay_agent
from src.intake import build_message_content, scan, stage_files


def run(
    input_path: str,
    *,
    prompt: str | None = None,
    config_path: str | None = None,
    thread_id: str | None = None,
) -> dict:
    """Run the essay writer agent with input from a file or directory.

    Args:
        input_path: Path to a file or directory containing assignment materials.
        prompt: Optional additional instructions to append.
        config_path: Path to the YAML configuration file.
        thread_id: Optional thread ID for conversation continuity.

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

    try:
        result = agent.invoke(
            {"messages": [HumanMessage(content=content)]},
            config={"configurable": {"thread_id": thread_id}},
        )
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    return result


def run_prompt(
    prompt: str,
    *,
    config_path: str | None = None,
    thread_id: str | None = None,
) -> dict:
    """Run the essay writer agent with a plain text prompt (no files).

    Args:
        prompt: The essay assignment prompt.
        config_path: Path to the YAML configuration file.
        thread_id: Optional thread ID for conversation continuity.

    Returns:
        The final agent state dict.
    """
    config = load_config(config_path)
    agent = create_essay_agent(config)

    if thread_id is None:
        thread_id = uuid.uuid4().hex

    result = agent.invoke(
        {"messages": [HumanMessage(content=prompt)]},
        config={"configurable": {"thread_id": thread_id}},
    )

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
        "--prompt", "-p",
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
    args = parser.parse_args()

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
        )
    elif args.input_path is None:
        # Prompt-only mode (no files)
        result = run_prompt(
            args.prompt,
            config_path=args.config,
            thread_id=args.thread_id,
        )
    else:
        # File/directory input mode
        result = run(
            args.input_path,
            prompt=args.prompt,
            config_path=args.config,
            thread_id=args.thread_id,
        )

    # Print the final assistant message
    messages = result.get("messages", [])
    if messages:
        last_msg = messages[-1]
        print(last_msg.content)


if __name__ == "__main__":
    main()
