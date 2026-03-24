"""Main agent assembly — creates the essay writer deep agent."""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph

from config.schemas import EssayWriterConfig, load_config
from src.rendering import render_prompt
from src.subagents import (
    make_intake,
    make_reader,
    make_reviewer,
    make_writer,
)
from src.tools import (
    count_words,
    make_build_docx,
    make_fetch_url,
    make_read_pdf,
    make_research_sources,
    read_docx,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain.agents.middleware.types import (
        ModelRequest,
        ModelResponse,
    )

logger = logging.getLogger(__name__)

_MALFORMED = "MALFORMED_FUNCTION_CALL"
_RETRY_MAX = 3
_RETRY_DELAY = 1.0


class _RetryMalformedMiddleware(AgentMiddleware):
    """Middleware that retries model calls returning MALFORMED_FUNCTION_CALL.

    Google Gemini sometimes returns finish_reason=MALFORMED_FUNCTION_CALL with
    0 output tokens.  This is non-deterministic; retrying the same request
    usually succeeds.
    """

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        for attempt in range(_RETRY_MAX + 1):
            response = handler(request)
            msgs = response.result if hasattr(response, "result") else []
            if not msgs:
                return response
            last = msgs[-1]
            finish = getattr(last, "response_metadata", {}).get("finish_reason")
            usage = getattr(last, "usage_metadata", None) or {}
            output_tokens = (
                usage.get("output_tokens", -1) if isinstance(usage, dict) else -1
            )
            # Retry on MALFORMED_FUNCTION_CALL or zero-output STOP (Gemini glitch)
            needs_retry = finish == _MALFORMED or (
                finish == "STOP" and output_tokens == 0
            )
            if not needs_retry:
                return response
            if attempt < _RETRY_MAX:
                logger.warning(
                    "%s (output_tokens=%s) on model call — retrying (%d/%d)",
                    finish,
                    output_tokens,
                    attempt + 1,
                    _RETRY_MAX,
                )
                time.sleep(_RETRY_DELAY)
        return response


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
) -> CompiledStateGraph:
    """Create and return the essay writer agent graph.

    Args:
        config: Configuration object. If None, loads from default.yaml.
        input_staging_dir: Temp directory with staged input files (from intake).
            If None, the /input/ backend route is omitted (prompt-only mode).
        sources_dir: Directory to persist downloaded source PDFs.
            If None, /sources/ lives in VFS state only.

    Returns:
        A compiled LangGraph agent ready to invoke.
    """
    from deepagents import create_deep_agent

    if config is None:
        config = load_config()

    # Orchestrator tools: research_sources replaces the researcher subagent
    build_docx = make_build_docx(config.paths.output_dir)
    fetch_url = make_fetch_url(sources_dir)
    read_pdf = make_read_pdf(sources_dir)
    research_sources = make_research_sources(sources_dir)
    orchestrator_tools = [count_words, build_docx, research_sources]

    # Document reading tools for reader subagent
    doc_tools = [read_pdf, read_docx, fetch_url, count_words]

    # Render orchestrator system prompt
    orchestrator_prompt = render_prompt("orchestrator.j2", config=config)

    # 4 subagent types (researcher replaced by research_sources tool)
    retry_middleware = _RetryMalformedMiddleware()
    subagents = [
        make_intake(config, []),
        make_reader(config, doc_tools),
        make_writer(config, [count_words]),
        make_reviewer(config, [count_words]),
    ]

    # Pre-resolve models when AI_BASE_URL is set so they use the custom endpoint
    for sa in subagents:
        sa["model"] = _resolve_model(sa["model"])
        sa.setdefault("middleware", []).append(retry_middleware)

    return create_deep_agent(
        model=_resolve_model(config.models.orchestrator),
        tools=orchestrator_tools,
        system_prompt=orchestrator_prompt,
        subagents=subagents,
        skills=[config.paths.skills_dir],
        backend=_create_backend(config, input_staging_dir, sources_dir),
        checkpointer=MemorySaver(),
        name="essay-orchestrator",
        middleware=[retry_middleware],
    )
