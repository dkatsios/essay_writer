"""Deterministic Python pipeline for essay writing.

Replaces the LLM orchestrator with a fixed 7-step sequence.
Each step invokes a pre-built worker or writer agent.
The pipeline controls flow; agents do LLM work.
"""

from __future__ import annotations

import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import monotonic

from langchain_core.messages import HumanMessage
from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)


def _invoke(
    agent: CompiledStateGraph,
    thread_id: str,
    message: str,
    callbacks: list | None = None,
) -> dict:
    """Invoke an agent with a single message."""
    config: dict = {"configurable": {"thread_id": thread_id}}
    if callbacks:
        config["callbacks"] = callbacks
    return agent.invoke(
        {"messages": [HumanMessage(content=message)]},
        config=config,
    )


def _step(name: str, fn, *args, tracker=None):
    """Run a pipeline step with timing and stderr output."""
    print(f"\n{'─' * 50}", file=sys.stderr)
    print(f"  Step: {name}", file=sys.stderr)
    if tracker is not None:
        tracker.current_step = name
    t0 = monotonic()
    try:
        result = fn(*args)
        dur = monotonic() - t0
        print(f"  ✓ {name} ({dur:.1f}s)", file=sys.stderr)
        if tracker is not None:
            tracker.record_duration(name, dur)
        return result
    except Exception:
        dur = monotonic() - t0
        print(f"  ✗ {name} FAILED ({dur:.1f}s)", file=sys.stderr)
        if tracker is not None:
            tracker.record_duration(name, dur)
        raise


def run_pipeline(
    worker: CompiledStateGraph,
    writer: CompiledStateGraph,
    run_dir: Path,
    config,
    *,
    extra_prompt: str | None = None,
    callbacks: list | None = None,
    token_tracker=None,
) -> None:
    """Execute the 7-step essay writing pipeline.

    Args:
        worker: Pre-built worker agent (fast model).
        writer: Pre-built writer agent (quality model).
        run_dir: Run directory — all VFS paths are subdirs of this.
        config: Essay writer configuration.
        extra_prompt: Optional additional user instructions.
        callbacks: LangChain callback handlers (for tool timing).
        token_tracker: Optional TokenTracker for per-step cost tracking.
    """
    extra = (
        f"\nAdditional instructions from the user: {extra_prompt}"
        if extra_prompt
        else ""
    )
    tk = {"tracker": token_tracker}

    # Step 1: Intake
    _step(
        "intake",
        _invoke,
        worker,
        "intake",
        f"Read /skills/worker/intake/SKILL.md. "
        f"Read extracted content from /input/extracted.md.{extra}",
        callbacks,
        **tk,
    )

    # Step 2: Plan
    _step(
        "plan",
        _invoke,
        worker,
        "plan",
        "Read /skills/worker/essay-planning/SKILL.md. "
        "The brief is at /brief/assignment.md.",
        callbacks,
        **tk,
    )

    # Step 3: Research
    _step(
        "research",
        _invoke,
        worker,
        "research",
        "Read /skills/worker/research/SKILL.md. The plan is at /plan/plan.md.",
        callbacks,
        **tk,
    )

    # Step 4: Read sources (parallel)
    _step("read_sources", _read_sources_parallel, worker, run_dir, callbacks, **tk)

    # Step 5: Write
    target_words = _get_target_words(run_dir)
    write_msg = "Read /skills/writer/essay-writing/SKILL.md."
    if target_words:
        write_msg += f" The total word target is {target_words} words. You MUST write at least {target_words} words."
    _step(
        "write",
        _invoke,
        writer,
        "write",
        write_msg,
        callbacks,
        **tk,
    )

    # Step 6: Review
    review_msg = "Read /skills/writer/essay-review/SKILL.md."
    if target_words:
        review_msg += f" The word target is {target_words} words. Do NOT produce fewer words than the draft."
    _step(
        "review",
        _invoke,
        writer,
        "review",
        review_msg,
        callbacks,
        **tk,
    )

    # Step 7: Export (pure Python — no LLM)
    _step("export", _export, config, run_dir)


def _get_target_words(run_dir: Path) -> int:
    """Sum word targets from plan.md."""
    import re

    plan = run_dir / "plan" / "plan.md"
    if not plan.exists():
        return 0
    total = 0
    for m in re.finditer(
        r"\*\*Word target\*\*:\s*(\d[\d,]*)", plan.read_text(encoding="utf-8")
    ):
        total += int(m.group(1).replace(",", ""))
    return total


def _read_sources_parallel(
    worker: CompiledStateGraph,
    run_dir: Path,
    callbacks: list | None,
) -> None:
    """Read source details in parallel using worker agents."""
    registry_path = run_dir / "sources" / "registry.json"
    if not registry_path.exists():
        logger.warning("No registry.json found — skipping source reading.")
        return

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    tasks = [
        (sid, meta.get("url", ""), meta.get("topic", ""))
        for sid, meta in registry.items()
        if meta.get("url")
    ]
    if not tasks:
        logger.info("No sources with URLs to read.")
        return

    logger.info("Reading %d sources in parallel...", len(tasks))

    def read_one(args):
        source_id, url, topic = args
        _invoke(
            worker,
            f"read_{source_id}",
            f"Read /skills/worker/source-reading/SKILL.md. "
            f"Source: {source_id}, URL: {url}, Topic: {topic}.",
            callbacks,
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(read_one, t): t[0] for t in tasks}
        for future in as_completed(futures):
            sid = futures[future]
            try:
                future.result()
            except Exception:
                logger.exception("Failed to read source %s", sid)


def _export(config, run_dir: Path) -> None:
    """Build docx from disk files (pure Python, no LLM)."""
    from src.tools.docx_builder import _build_document

    # Find essay text — prefer reviewed, fall back to draft
    essay_text = None
    for name in ("reviewed.md", "draft.md"):
        p = run_dir / "essay" / name
        if p.exists():
            essay_text = p.read_text(encoding="utf-8")
            break
    if not essay_text:
        logger.error("No essay found — cannot export.")
        return

    # Read source registry
    sources: dict = {}
    registry_path = run_dir / "sources" / "registry.json"
    if registry_path.exists():
        raw = registry_path.read_text(encoding="utf-8")
        try:
            sources = json.loads(raw)
        except json.JSONDecodeError:
            sources = json.loads(raw.encode().decode("unicode_escape"))

    # Build config from settings + cover page from brief
    doc_config = config.formatting.model_dump()
    brief_path = run_dir / "brief" / "assignment.md"
    if brief_path.exists():
        for line in brief_path.read_text(encoding="utf-8").split("\n"):
            if line.startswith("# "):
                doc_config.setdefault("title", line[2:].strip())
                break

    doc = _build_document(essay_text, doc_config, sources)

    output_path = Path(config.paths.output_dir) / "essay.docx"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    logger.info("essay.docx saved to %s", output_path)
    print(f"  ✓ essay.docx → {output_path}", file=sys.stderr)
