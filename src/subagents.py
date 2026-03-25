"""Subagent factories for the essay writer pipeline."""

from __future__ import annotations

from deepagents import SubAgent

from config.schemas import EssayWriterConfig
from src.rendering import render_prompt


def make_worker(config: EssayWriterConfig, tools: list) -> SubAgent:
    """Create the worker subagent (fast/cheap model).

    Used for intake, planning, research, and source reading — tasks that
    require instruction-following but not deep creative writing.
    """
    return {
        "name": "worker",
        "description": (
            "Fast research assistant for intake, planning, research, "
            "and source reading. Reads the skill file specified in "
            "the task description, then follows its instructions."
        ),
        "system_prompt": render_prompt("worker.j2", config=config),
        "model": config.models.worker,
        "tools": tools,
        "skills": ["/skills/worker/"],
    }


def make_writer(config: EssayWriterConfig, tools: list) -> SubAgent:
    """Create the writer subagent (quality model).

    Used for essay writing and reviewing — tasks that demand high-quality
    prose, argumentation, and academic depth.
    """
    return {
        "name": "writer",
        "description": (
            "Expert academic writer for essay composition and review. "
            "Reads the skill file specified in the task description, "
            "then follows its instructions."
        ),
        "system_prompt": render_prompt("writer.j2", config=config),
        "model": config.models.writer,
        "tools": tools,
        "skills": ["/skills/writer/"],
    }
