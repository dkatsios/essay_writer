"""Main agent assembly — creates the essay writer deep agent."""

from __future__ import annotations

import os

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph

from config.schemas import EssayWriterConfig, load_config
from src.rendering import render_prompt
from src.subagents import make_intake, make_reader, make_reviewer
from src.tools import (
    academic_search,
    count_words,
    crossref_search,
    fetch_url,
    make_build_docx,
    openalex_search,
    read_docx,
    read_pdf,
)


def _resolve_model(model_spec: str) -> str | BaseChatModel:
    """Resolve a model spec, routing through AI_BASE_URL when set.

    When AI_BASE_URL (and optionally AI_API_KEY) are present in the
    environment, the model is pre-resolved as a ChatOpenAI instance
    pointing at that endpoint.  Otherwise the raw spec string is
    returned for deepagents' default resolution.

    AI_MODEL can override the model name sent to the custom endpoint.
    If unset, the model name from the config spec is used as-is.
    """
    base_url = os.environ.get("AI_BASE_URL")
    if not base_url:
        return model_spec

    from langchain.chat_models import init_chat_model

    # AI_MODEL overrides the model name; otherwise strip the provider prefix
    model_name = os.environ.get("AI_MODEL")
    if not model_name:
        _, _, model_name = model_spec.partition(":")
        model_name = model_name or model_spec

    return init_chat_model(
        f"openai:{model_name}",
        base_url=base_url,
        api_key=os.environ.get("AI_API_KEY", ""),
    )


def _create_backend(
    config: EssayWriterConfig,
    input_staging_dir: str | None = None,
    sources_dir: str | None = None,
):
    """Return a backend factory for create_deep_agent.

    Args:
        config: Project configuration (provides output_dir).
        input_staging_dir: Temp directory with staged input files.
            If None, the /input/ route is omitted (prompt-only mode).
        sources_dir: Directory to persist downloaded source PDFs.
            If None, /sources/ lives in VFS state only.
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
        if sources_dir is not None:
            Path(sources_dir).mkdir(parents=True, exist_ok=True)
            routes["/sources/"] = FilesystemBackend(
                root_dir=sources_dir,
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
    sources_dir: str | None = None,
    checkpointer=None,
) -> CompiledStateGraph:
    """Create and return the essay writer agent graph.

    Args:
        config: Configuration object. If None, loads from default.yaml.
        input_staging_dir: Temp directory with staged input files (from intake).
            If None, the /input/ backend route is omitted (prompt-only mode).
        sources_dir: Directory to persist downloaded source PDFs.
            If None, /sources/ lives in VFS state only.
        checkpointer: A LangGraph checkpointer instance (e.g. SqliteSaver).
            If None, uses in-memory checkpointing (lost on exit).

    Returns:
        A compiled LangGraph agent ready to invoke.
    """
    from deepagents import create_deep_agent

    if config is None:
        config = load_config()

    # Orchestrator gets all tools — it does planning, searching, writing, and export
    build_docx = make_build_docx(config.paths.output_dir)
    all_tools = [
        academic_search,
        openalex_search,
        crossref_search,
        fetch_url,
        read_pdf,
        read_docx,
        count_words,
        build_docx,
    ]

    # Document reading tools for intake and reader subagents
    doc_tools = [read_pdf, read_docx, fetch_url, count_words]

    # Render orchestrator system prompt
    orchestrator_prompt = render_prompt("orchestrator.j2", config=config)

    # 3 subagent types: intake, reader, reviewer
    subagents = [
        make_intake(config, []),
        make_reader(config, doc_tools),
        make_reviewer(config, doc_tools),
    ]

    # Pre-resolve models when AI_BASE_URL is set so they use the custom endpoint
    for sa in subagents:
        sa["model"] = _resolve_model(sa["model"])

    if checkpointer is None:
        checkpointer = MemorySaver()

    return create_deep_agent(
        model=_resolve_model(config.models.orchestrator),
        tools=all_tools,
        system_prompt=orchestrator_prompt,
        subagents=subagents,
        skills=[config.paths.skills_dir],
        backend=_create_backend(config, input_staging_dir, sources_dir),
        checkpointer=checkpointer,
        name="essay-orchestrator",
    )
