"""Subagent factory functions for the essay writer pipeline."""

from __future__ import annotations

from deepagents import SubAgent

from config.schemas import EssayWriterConfig
from src.rendering import render_prompt


def make_planner(config: EssayWriterConfig, tools: list) -> SubAgent:
    """Create the planner subagent."""
    return {
        "name": "planner",
        "description": (
            "Creates and refines essay plans. Produces section breakdowns with "
            "word count targets, research directions, and source-to-section mappings. "
            "Use for both draft planning (before research) and plan refinement (after research)."
        ),
        "system_prompt": render_prompt("planner.j2", config=config),
        "model": config.models.planner,
        "tools": tools,
        "skills": [config.paths.skills_dir],
    }


def make_researcher(config: EssayWriterConfig, tools: list) -> SubAgent:
    """Create the researcher subagent."""
    return {
        "name": "researcher",
        "description": (
            "Searches academic databases for credible sources on specific topics. "
            "Writes structured source metadata to VFS. Use for targeted research "
            "based on specific research directions from the essay plan."
        ),
        "system_prompt": render_prompt("researcher.j2", config=config),
        "model": config.models.researcher,
        "tools": tools,
    }


def make_cataloguer(config: EssayWriterConfig, tools: list) -> SubAgent:
    """Create the source cataloguer subagent."""
    return {
        "name": "cataloguer",
        "description": (
            "Reads a raw PDF or DOCX source and produces a lightweight structured "
            "metadata entry (title, authors, abstract, introduction summary). "
            "Uses a cheaper model. Use when a source lacks structured metadata."
        ),
        "system_prompt": render_prompt("cataloguer.j2", config=config),
        "model": config.models.cataloguer,
        "tools": tools,
    }


def make_extractor(config: EssayWriterConfig, tools: list) -> SubAgent:
    """Create the source extractor subagent."""
    return {
        "name": "extractor",
        "description": (
            "Reads a single source document in full and produces exhaustive, "
            "self-contained VFS entries for each section that uses this source. "
            "Includes quotes with page numbers, data, citation keys, and full "
            "bibliographic information. This is the sole access point for the source."
        ),
        "system_prompt": render_prompt("extractor.j2", config=config),
        "model": config.models.extractor,
        "tools": tools,
        "skills": [config.paths.skills_dir],
    }


def make_writer(config: EssayWriterConfig, tools: list) -> SubAgent:
    """Create the section writer subagent."""
    return {
        "name": "writer",
        "description": (
            "Writes a single essay section in academic Greek. Receives the full "
            "plan, prior sections context, and pre-extracted source material. "
            "Respects the word count target for the section."
        ),
        "system_prompt": render_prompt("writer.j2", config=config),
        "model": config.models.writer,
        "tools": tools,
        "skills": [config.paths.skills_dir],
    }


def make_reviewer(config: EssayWriterConfig, tools: list) -> SubAgent:
    """Create the reviewer/polisher subagent."""
    return {
        "name": "reviewer",
        "description": (
            "Reviews the assembled essay for coherence, language quality, "
            "citation correctness, and completeness. Can refine earlier sections. "
            "Produces feedback and a polished version of the essay."
        ),
        "system_prompt": render_prompt("reviewer.j2", config=config),
        "model": config.models.reviewer,
        "tools": tools,
        "skills": [config.paths.skills_dir],
    }


def make_builder(config: EssayWriterConfig, tools: list) -> SubAgent:
    """Create the document builder subagent."""
    return {
        "name": "builder",
        "description": (
            "Converts the final essay text into a formatted .docx file with "
            "cover page, table of contents, headings, citations, references, "
            "and page numbers. Writes to /output/."
        ),
        "system_prompt": render_prompt("builder.j2", config=config),
        "model": config.models.builder,
        "tools": tools,
    }
