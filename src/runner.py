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

    # Save run outputs to .output/run_<timestamp>/ when using --dump-run
    uv run python -m src.runner /path/to/files/ --dump-run

    # Resume a previous run from its output directory
    uv run python -m src.runner --resume .output/run_<timestamp>/
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()

from config.schemas import load_config  # noqa: E402
from src.agent import create_async_client, create_client  # noqa: E402
from src.intake import build_extracted_text, scan  # noqa: E402
from src.pipeline import run_pipeline  # noqa: E402
from src.pipeline_sources import SourceShortfallAbort  # noqa: E402
from src.runtime import (  # noqa: E402
    TokenTracker,
    format_validation_questions,
    parse_validation_answers,
)
from src.scratch_dir import SCRATCH_RUN_DIR  # noqa: E402
from src.schemas import Clarification, ValidationQuestion  # noqa: E402
from src.run_logging import (  # noqa: E402
    clear_run_id,
    set_run_id,
    setup_run_logging,
    teardown_run_logging,
)


# ---------------------------------------------------------------------------
# Validation callback
# ---------------------------------------------------------------------------


def _format_validation_questions(questions: list[ValidationQuestion]) -> str:
    return format_validation_questions(questions)


def _parse_validation_answers(
    questions: list[ValidationQuestion],
    answers: str,
) -> list[Clarification]:
    return parse_validation_answers(questions, answers)


async def _handle_questions(questions: list[ValidationQuestion], run_dir: Path) -> None:
    """Print validator questions, collect answers via stdin, append to brief."""
    logger.info(
        "%s\nThe assignment brief has gaps that may affect quality.\nPlease answer the following:",
        "=" * 50,
    )
    logger.info("%s", _format_validation_questions(questions))
    logger.info(
        "\n  Enter answers (e.g. '1. a, 2. c'). Lines marked ← suggested default; "
        "press Enter to skip all:",
    )
    answers = (await asyncio.to_thread(input, "> ")).strip()
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


async def _handle_source_shortfall(run_dir: Path, summary: dict) -> bool:
    usable = int(summary.get("usable_sources", 0) or 0)
    target = int(summary.get("target_sources", 0) or 0)
    scorable = int(summary.get("scorable_candidates", 0) or 0)
    above = int(summary.get("above_threshold", 0) or 0)
    total = int(summary.get("total_candidates", 0) or 0)
    recovered = bool(summary.get("recovery_attempted"))

    logger.warning(
        "%s\nSource shortfall detected.\nTarget usable sources: %d\nScorable candidates: %d / %d\nScored above threshold: %d / %d\nAvailable selected usable sources: %d",
        "=" * 50,
        target,
        scorable,
        total,
        above,
        scorable,
        usable,
    )
    if recovered:
        logger.warning(
            "  A recovery search pass already ran with broader/full-text-biased parameters.",
        )
    logger.warning(
        "  Continue with the available usable sources instead of the original target? [y/N]",
    )
    answer = (await asyncio.to_thread(input, "> ")).strip().lower()
    return answer in {"y", "yes"}


# ---------------------------------------------------------------------------
# Run entry points
# ---------------------------------------------------------------------------


async def _execute_pipeline(
    extracted_text: str,
    *,
    prompt: str | None = None,
    config_path: str | None = None,
    output_dir: Path | None = None,
    user_sources_dir: Path | None = None,
) -> None:
    """Shared pipeline execution for both file-based and prompt-only modes."""
    config = load_config(config_path)

    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = output_dir or SCRATCH_RUN_DIR
    run_dir.mkdir(parents=True, exist_ok=True)

    run_id = str(threading.current_thread().ident)
    set_run_id(run_id)
    log_handler = setup_run_logging(run_dir, run_id)

    input_dir = run_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / "extracted.md").write_text(extracted_text, encoding="utf-8")

    worker = create_client(config.models.worker)
    writer = create_client(config.models.writer)
    reviewer = create_client(config.models.reviewer)
    async_worker = create_async_client(config.models.worker)
    async_writer = create_async_client(config.models.writer)
    async_reviewer = create_async_client(config.models.reviewer)

    tracker = TokenTracker()

    try:
        await run_pipeline(
            worker,
            writer,
            reviewer,
            run_dir,
            config,
            extra_prompt=prompt,
            token_tracker=tracker,
            on_questions=_handle_questions
            if config.writing.interactive_validation
            else None,
            on_source_shortfall=_handle_source_shortfall,
            user_sources_dir=user_sources_dir,
            async_worker=async_worker,
            async_writer=async_writer,
            async_reviewer=async_reviewer,
        )
    except SourceShortfallAbort as exc:
        logger.warning("Aborted: %s", exc)
        return
    finally:
        cost = tracker.cost_summary()
        logger.info("%s", cost)
        tracker.write_report(run_dir)
        clear_run_id()
        teardown_run_logging(log_handler)


async def run(
    input_path: str,
    *,
    prompt: str | None = None,
    config_path: str | None = None,
    output_dir: Path | None = None,
    user_sources_dir: Path | None = None,
) -> None:
    """Run the essay pipeline with files (and optional prompt)."""
    input_files = scan(input_path)
    for f in input_files:
        status = f.category if not f.warning else f"SKIPPED ({f.warning})"
        logger.info("[%s] %s", status, f.path.name)
    extracted_text = build_extracted_text(input_files, extra_prompt=prompt)

    await _execute_pipeline(
        extracted_text,
        prompt=prompt,
        config_path=config_path,
        output_dir=output_dir,
        user_sources_dir=user_sources_dir,
    )


async def run_prompt(
    prompt: str,
    *,
    config_path: str | None = None,
    output_dir: Path | None = None,
    user_sources_dir: Path | None = None,
) -> None:
    """Run the essay pipeline with a plain text prompt (no files)."""
    await _execute_pipeline(
        f"# Assignment\n\n{prompt}\n",
        prompt=prompt,
        config_path=config_path,
        output_dir=output_dir,
        user_sources_dir=user_sources_dir,
    )


async def resume_run(
    run_dir_path: str,
    *,
    config_path: str | None = None,
) -> None:
    """Resume a previous pipeline run from its output directory."""
    run_dir = Path(run_dir_path)
    if not run_dir.is_dir():
        logger.error("%s is not a directory.", run_dir)
        sys.exit(1)

    extracted_path = run_dir / "input" / "extracted.md"
    if not extracted_path.exists():
        print(
            f"Error: {extracted_path} not found — not a valid run directory.",
            file=sys.stderr,
        )
        sys.exit(1)

    config = load_config(config_path)
    run_id = str(threading.current_thread().ident)
    set_run_id(run_id)
    log_handler = setup_run_logging(run_dir, run_id)

    worker = create_client(config.models.worker)
    writer = create_client(config.models.writer)
    reviewer = create_client(config.models.reviewer)
    async_worker = create_async_client(config.models.worker)
    async_writer = create_async_client(config.models.writer)
    async_reviewer = create_async_client(config.models.reviewer)

    tracker = TokenTracker()

    try:
        await run_pipeline(
            worker,
            writer,
            reviewer,
            run_dir,
            config,
            extra_prompt=None,
            token_tracker=tracker,
            on_questions=_handle_questions
            if config.writing.interactive_validation
            else None,
            on_source_shortfall=_handle_source_shortfall,
            resume=True,
            async_worker=async_worker,
            async_writer=async_writer,
            async_reviewer=async_reviewer,
        )
    except SourceShortfallAbort as exc:
        logger.warning("Aborted: %s", exc)
        return
    finally:
        cost = tracker.cost_summary()
        logger.info("%s", cost)
        tracker.write_report(run_dir)
        clear_run_id()
        teardown_run_logging(log_handler)


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
        dest="dump_run",
        action="store_true",
        default=False,
        help="Save run outputs to a timestamped directory under .output/.",
    )
    parser.add_argument(
        "--resume",
        default=None,
        help="Resume a previous run from the given output directory.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)

    if args.resume:
        asyncio.run(resume_run(args.resume, config_path=args.config))
        return

    output_dir = None
    if args.dump_run:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_dir = Path(".output") / f"run_{timestamp}"

    sources_dir = Path(args.sources) if args.sources else None

    if args.input_path is None and args.prompt is None:
        if sys.stdin.isatty():
            parser.print_help()
            sys.exit(1)
        prompt_text = sys.stdin.read().strip()
        if not prompt_text:
            logger.error("No input provided.")
            sys.exit(1)
        asyncio.run(
            run_prompt(
                prompt_text,
                config_path=args.config,
                output_dir=output_dir,
                user_sources_dir=sources_dir,
            )
        )
    elif args.input_path is None:
        asyncio.run(
            run_prompt(
                args.prompt,
                config_path=args.config,
                output_dir=output_dir,
                user_sources_dir=sources_dir,
            )
        )
    else:
        asyncio.run(
            run(
                args.input_path,
                prompt=args.prompt,
                config_path=args.config,
                output_dir=output_dir,
                user_sources_dir=sources_dir,
            )
        )


if __name__ == "__main__":
    main()
