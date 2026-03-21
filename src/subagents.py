"""Subagent factory functions for the essay writer pipeline."""

from __future__ import annotations

from deepagents import SubAgent

from config.schemas import EssayWriterConfig
from src.rendering import render_prompt

# Each entry: (name, template, model_attr, description, has_skills)
_SUBAGENT_SPECS: list[tuple[str, str, str, str, bool]] = [
    (
        "planner",
        "planner.j2",
        "planner",
        "Creates and refines essay plans. Produces section breakdowns with "
        "word count targets, research directions, and source-to-section mappings. "
        "Use for both draft planning (before research) and plan refinement (after research).",
        True,
    ),
    (
        "researcher",
        "researcher.j2",
        "researcher",
        "Searches academic databases for credible sources on specific topics. "
        "Writes structured source metadata to VFS. Use for targeted research "
        "based on specific research directions from the essay plan.",
        False,
    ),
    (
        "cataloguer",
        "cataloguer.j2",
        "cataloguer",
        "Reads a raw PDF or DOCX source and produces a lightweight structured "
        "metadata entry (title, authors, abstract, introduction summary). "
        "Uses a cheaper model. Use when a source lacks structured metadata.",
        False,
    ),
    (
        "extractor",
        "extractor.j2",
        "extractor",
        "Reads a single source document in full and produces exhaustive, "
        "self-contained VFS entries for each section that uses this source. "
        "Includes quotes with page numbers, data, citation keys, and full "
        "bibliographic information. This is the sole access point for the source.",
        True,
    ),
    (
        "writer",
        "writer.j2",
        "writer",
        "Writes a single essay section in academic Greek. Receives the full "
        "plan, prior sections context, and pre-extracted source material. "
        "Respects the word count target for the section.",
        True,
    ),
    (
        "reviewer",
        "reviewer.j2",
        "reviewer",
        "Reviews the assembled essay for coherence, language quality, "
        "citation correctness, and completeness. Can refine earlier sections. "
        "Produces feedback and a polished version of the essay.",
        True,
    ),
    (
        "builder",
        "builder.j2",
        "builder",
        "Converts the final essay text into a formatted .docx file with "
        "cover page, table of contents, headings, citations, references, "
        "and page numbers. Writes to /output/.",
        False,
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


# Convenience aliases — keep the existing call sites working
def make_planner(config: EssayWriterConfig, tools: list) -> SubAgent:
    return make_subagent("planner", config, tools)


def make_researcher(config: EssayWriterConfig, tools: list) -> SubAgent:
    return make_subagent("researcher", config, tools)


def make_cataloguer(config: EssayWriterConfig, tools: list) -> SubAgent:
    return make_subagent("cataloguer", config, tools)


def make_extractor(config: EssayWriterConfig, tools: list) -> SubAgent:
    return make_subagent("extractor", config, tools)


def make_writer(config: EssayWriterConfig, tools: list) -> SubAgent:
    return make_subagent("writer", config, tools)


def make_reviewer(config: EssayWriterConfig, tools: list) -> SubAgent:
    return make_subagent("reviewer", config, tools)


def make_builder(config: EssayWriterConfig, tools: list) -> SubAgent:
    return make_subagent("builder", config, tools)
