"""Post-run analysis — generates a cost/timing report from LangSmith traces."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Pipeline steps in execution order.
# Update this list when adding, removing, or renaming subagents.
STEPS = ["intake", "reader", "writer", "reviewer"]


def generate_run_report(output_dir: Path, run_tag: str) -> Path | None:
    """Query LangSmith for runs tagged with *run_tag* and write a report.

    Args:
        output_dir: Directory to write the report file.
        run_tag: Tag applied to all invoke calls for this run session.

    Returns:
        Path to the report file, or None if LangSmith is unavailable.
    """
    if os.environ.get("LANGSMITH_TRACING", "").lower() != "true":
        return None

    try:
        from langsmith import Client

        client = Client()
    except Exception:
        logger.debug("LangSmith client unavailable, skipping report.")
        return None

    # Flush pending traces so data is queryable
    _flush_traces()

    project = os.environ.get("LANGCHAIN_PROJECT", "default")
    try:
        # Find all root runs for this session (retries create multiple roots)
        root_runs = list(
            client.list_runs(
                project_name=project,
                is_root=True,
                filter=f'has(tags, "{run_tag}")',
            )
        )
        if not root_runs:
            logger.warning("No LangSmith runs found with tag '%s'.", run_tag)
            return None

        # Collect all runs across all traces
        all_runs: list = []
        for root in root_runs:
            all_runs.extend(
                client.list_runs(
                    project_name=project,
                    trace_id=root.trace_id,
                )
            )

        report = _build_report(root_runs, all_runs)
        report_path = output_dir / "report.md"
        report_path.write_text(report, encoding="utf-8")
        logger.info("Run report written to %s", report_path)
        print(f"  Report: {report_path}", file=sys.stderr)
        return report_path

    except Exception:
        logger.exception("Failed to generate run report")
        return None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _flush_traces() -> None:
    """Best-effort flush of pending LangSmith traces."""
    try:
        from langchain_core.tracers.langchain import wait_for_all_tracers

        wait_for_all_tracers()
    except Exception:
        import time

        time.sleep(3)


def _duration_str(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s"


def _get_tokens(run) -> tuple[int, int]:
    """Extract input/output tokens from a LangSmith run.

    Checks top-level fields first, falls back to extra.metadata.usage_metadata
    (where Gemini stores them).
    """
    inp = run.prompt_tokens or 0
    out = run.completion_tokens or 0
    if inp or out:
        return inp, out
    # Gemini stores tokens in extra.metadata.usage_metadata
    meta = (run.extra or {}).get("metadata", {}).get("usage_metadata", {})
    return meta.get("input_tokens", 0), meta.get("output_tokens", 0)


def _aggregate(runs: list) -> dict:
    """Sum cost, tokens, and duration for a list of LangSmith runs."""
    total_in = 0
    total_out = 0
    for r in runs:
        inp, out = _get_tokens(r)
        total_in += inp
        total_out += out
    return {
        "count": len(runs),
        "cost": float(sum(r.total_cost or 0 for r in runs)),
        "input_tokens": total_in,
        "output_tokens": total_out,
        "duration": sum(
            (r.end_time - r.start_time).total_seconds()
            for r in runs
            if r.end_time and r.start_time
        ),
    }


def _find_step_for_run(run, run_map: dict, step_ids: dict) -> str:
    """Walk the parent chain to find which pipeline step a run belongs to."""
    current = run
    visited = set()
    while current:
        if current.id in visited:
            break
        visited.add(current.id)
        if current.id in step_ids:
            name = step_ids[current.id]
            return "orchestrator" if name == "essay-orchestrator" else name
        current = run_map.get(current.parent_run_id)
    return "orchestrator"


def _build_report(root_runs: list, all_runs: list) -> str:
    """Build a markdown report from the trace data."""
    root_agg = _aggregate(root_runs)

    # Wall-clock: first start → last end across all root runs
    starts = [r.start_time for r in root_runs if r.start_time]
    ends = [r.end_time for r in root_runs if r.end_time]
    wall_clock = (
        (max(ends) - min(starts)).total_seconds()
        if starts and ends
        else root_agg["duration"]
    )

    # Build parent map and identify step chain runs
    run_map = {r.id: r for r in all_runs}
    step_names = set(STEPS) | {"essay-orchestrator"}
    step_ids = {
        r.id: r.name for r in all_runs if r.run_type == "chain" and r.name in step_names
    }

    # Map each LLM run to its ancestor step
    llm_runs = [r for r in all_runs if r.run_type == "llm"]
    step_llm_runs: dict[str, list] = {s: [] for s in STEPS}
    step_llm_runs["orchestrator"] = []
    for r in llm_runs:
        step = _find_step_for_run(r, run_map, step_ids)
        step_llm_runs.setdefault(step, []).append(r)

    # Get chain runs per step for duration and invocation count
    step_chains: dict[str, list] = {s: [] for s in STEPS}
    for r in all_runs:
        if r.run_type == "chain" and r.name in set(STEPS):
            step_chains[r.name].append(r)

    # --- Build rows ---
    rows: list[tuple[str, dict]] = []
    step_token_sum = {"input_tokens": 0, "output_tokens": 0, "duration": 0.0}

    for step in STEPS:
        llm_agg = _aggregate(step_llm_runs.get(step, []))
        chains = step_chains.get(step, [])
        # Use chain duration (wall-clock for the subagent), LLM tokens
        chain_dur = sum(
            (r.end_time - r.start_time).total_seconds()
            for r in chains
            if r.end_time and r.start_time
        )
        row = {
            "count": len(chains),
            "input_tokens": llm_agg["input_tokens"],
            "output_tokens": llm_agg["output_tokens"],
            "duration": chain_dur,
        }
        rows.append((step, row))
        step_token_sum["input_tokens"] += row["input_tokens"]
        step_token_sum["output_tokens"] += row["output_tokens"]
        step_token_sum["duration"] += chain_dur

    # Orchestrator = LLM runs attributed to orchestrator
    orch_llm = _aggregate(step_llm_runs.get("orchestrator", []))
    orch_row = {
        "count": "—",
        "input_tokens": orch_llm["input_tokens"],
        "output_tokens": orch_llm["output_tokens"],
        "duration": max(0.0, wall_clock - step_token_sum["duration"]),
    }

    # Compute cost proportionally from root total (since per-run cost is None)
    total_tokens = root_agg["input_tokens"] + root_agg["output_tokens"]
    all_rows = [("orchestrator", orch_row)] + rows
    for _, row_data in all_rows:
        row_tokens = row_data["input_tokens"] + row_data["output_tokens"]
        row_data["cost"] = (
            root_agg["cost"] * row_tokens / total_tokens if total_tokens else 0.0
        )

    # --- Assemble report ---
    lines = [
        "# Run Report",
        "",
        f"- **Duration**: {_duration_str(wall_clock)}",
        f"- **Total Cost**: ${root_agg['cost']:.4f}",
        f"- **Input Tokens**: {root_agg['input_tokens']:,}",
        f"- **Output Tokens**: {root_agg['output_tokens']:,}",
        "",
        "## Step Breakdown",
        "",
        "| Step | Runs | Duration | Input Tokens | Output Tokens | Cost |",
        "|------|------|----------|-------------|--------------|------|",
    ]

    for name, agg in all_rows:
        count = agg["count"]
        if isinstance(count, int) and count == 0:
            lines.append(f"| {name} | 0 | — | — | — | — |")
        else:
            lines.append(
                f"| {name} | {count} | {_duration_str(agg['duration'])} "
                f"| {agg['input_tokens']:,} | {agg['output_tokens']:,} "
                f"| ${agg['cost']:.4f} |"
            )

    # Total row
    lines.append(
        f"| **Total** | — | {_duration_str(wall_clock)} "
        f"| {root_agg['input_tokens']:,} | {root_agg['output_tokens']:,} "
        f"| ${root_agg['cost']:.4f} |"
    )

    return "\n".join(lines) + "\n"
