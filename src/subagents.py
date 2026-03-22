"""Subagent factory functions for the essay writer pipeline."""

from __future__ import annotations

from deepagents import SubAgent

from config.schemas import EssayWriterConfig
from src.rendering import render_prompt

# Each entry: (name, template, model_attr, description, has_skills)
_SUBAGENT_SPECS: list[tuple[str, str, str, str, bool]] = [
    (
        "intake",
        "intake.j2",
        "intake",
        "Synthesizes pre-extracted document content from the task description "
        "into a structured assignment brief at /brief/assignment.md.",
        False,
    ),
    (
        "reader",
        "reader.j2",
        "reader",
        "Reads a single academic source (URL or document) and returns "
        "condensed notes with relevant quotes and bibliographic data. "
        "Use for full-text extraction to keep large documents out of "
        "the orchestrator's context.",
        False,
    ),
    (
        "reviewer",
        "reviewer.j2",
        "reviewer",
        "Reviews and polishes the essay draft. Reads /brief/assignment.md "
        "and /essay/draft.md, checks coherence, citations, language quality, "
        "and writes the final polished essay to /essay/final.md.",
        True,
    ),
]


def make_subagent(name: str, config: EssayWriterConfig, tools: list) -> SubAgent:
    """Create a subagent by name from the spec table."""
    for spec_name, template, model_attr, description, has_skills in _SUBAGENT_SPECS:
        if spec_name == name:
            agent: SubAgent = {
                "name": spec_name,
                "description": description,
                "system_prompt": render_prompt(template, config=config),
                "model": getattr(config.models, model_attr),
                "tools": tools,
            }
            if has_skills:
                agent["skills"] = [config.paths.skills_dir]
            return agent
    raise ValueError(f"Unknown subagent: {name}")


# Convenience aliases
def make_intake(config: EssayWriterConfig, tools: list) -> SubAgent:
    return make_subagent("intake", config, tools)


def make_reader(config: EssayWriterConfig, tools: list) -> SubAgent:
    return make_subagent("reader", config, tools)


def make_reviewer(config: EssayWriterConfig, tools: list) -> SubAgent:
    return make_subagent("reviewer", config, tools)
