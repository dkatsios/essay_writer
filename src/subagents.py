"""Subagent factory for the essay writer pipeline."""

from __future__ import annotations

from deepagents import SubAgent

from config.schemas import EssayWriterConfig
from src.rendering import render_prompt


def make_assistant(config: EssayWriterConfig, tools: list) -> SubAgent:
    """Create the assistant subagent.

    The assistant is a general-purpose academic writing agent. The orchestrator
    directs it to read the appropriate skill file for each task (intake,
    planning, source reading, writing, or reviewing).
    """
    return {
        "name": "assistant",
        "description": (
            "General-purpose academic writing assistant. Reads the skill file "
            "specified in the task description, then follows its instructions. "
            "Can handle intake, planning, source reading, writing, and reviewing."
        ),
        "system_prompt": render_prompt("assistant.j2", config=config),
        "model": config.models.assistant,
        "tools": tools,
        "skills": [config.paths.skills_dir],
    }
