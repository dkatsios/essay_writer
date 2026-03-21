"""Main agent assembly — creates the essay writer deep agent."""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph

from config.schemas import EssayWriterConfig, load_config
from src.rendering import render_prompt
from src.subagents import (
    make_builder,
    make_cataloguer,
    make_extractor,
    make_planner,
    make_researcher,
    make_reviewer,
    make_writer,
)
from src.tools import (
    academic_search,
    build_docx,
    count_words,
    fetch_url,
    openalex_search,
    read_docx,
    read_pdf,
)


def _create_backend(config: EssayWriterConfig, input_staging_dir: str | None = None):
    """Return a backend factory for create_deep_agent.

    Args:
        config: Project configuration (provides output_dir).
        input_staging_dir: Temp directory with staged input files.
            If None, the /input/ route is omitted (prompt-only mode).
    """
    from pathlib import Path

    from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend

    # Ensure output directory exists
    Path(config.paths.output_dir).mkdir(parents=True, exist_ok=True)

    def factory(runtime):
        routes = {
            "/output/": FilesystemBackend(
                root_dir=config.paths.output_dir,
                virtual_mode=True,
            ),
        }
        if input_staging_dir is not None:
            routes["/input/"] = FilesystemBackend(
                root_dir=input_staging_dir,
                virtual_mode=True,
            )
        return CompositeBackend(
            default=StateBackend(runtime),
            routes=routes,
        )

    return factory


def create_essay_agent(
    config: EssayWriterConfig | None = None,
    input_staging_dir: str | None = None,
) -> CompiledStateGraph:
    """Create and return the essay writer agent graph.

    Args:
        config: Configuration object. If None, loads from default.yaml.
        input_staging_dir: Temp directory with staged input files (from intake).
            If None, the /input/ backend route is omitted (prompt-only mode).

    Returns:
        A compiled LangGraph agent ready to invoke.
    """
    from deepagents import create_deep_agent

    if config is None:
        config = load_config()

    # Tool sets for different agent roles
    common_tools = [read_pdf, read_docx, count_words, fetch_url]
    research_tools = [academic_search, openalex_search, fetch_url]
    builder_tools = [build_docx]

    # Render orchestrator system prompt
    orchestrator_prompt = render_prompt("orchestrator.j2", config=config)

    # Define all subagents
    subagents = [
        make_planner(config, common_tools),
        make_researcher(config, research_tools),
        make_cataloguer(config, common_tools),
        make_extractor(config, common_tools),
        make_writer(config, common_tools),
        make_reviewer(config, common_tools),
        make_builder(config, builder_tools),
    ]

    # Checkpointer for multi-turn conversations (needed for human checkpoints)
    checkpointer = MemorySaver()

    return create_deep_agent(
        model=config.models.orchestrator,
        tools=common_tools,
        system_prompt=orchestrator_prompt,
        subagents=subagents,
        skills=[config.paths.skills_dir],
        backend=_create_backend(config, input_staging_dir),
        checkpointer=checkpointer,
        name="essay-orchestrator",
    )
