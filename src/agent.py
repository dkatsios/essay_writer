"""Agent factories — creates standalone worker and writer agents.

Each agent uses ``create_agent`` (low-level) with only
``FilesystemMiddleware`` for file tools.  No orchestrator, no
TodoList, no SubAgent, no Summarization middleware — keeping
the system prompt lean and the agent focused on a single task.
The Python pipeline (``src/pipeline.py``) invokes agents in sequence.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware, ModelRetryMiddleware
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph

from config.schemas import EssayWriterConfig
from src.rendering import render_prompt
from src.tools import (
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

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

_MALFORMED = "MALFORMED_FUNCTION_CALL"
_RETRY_MAX = 3
_RETRY_DELAY = 1.0


class _RetryMalformedMiddleware(AgentMiddleware):
    """Retry on Gemini's non-deterministic MALFORMED_FUNCTION_CALL glitch."""

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
            out_tok = usage.get("output_tokens", -1) if isinstance(usage, dict) else -1
            needs_retry = finish == _MALFORMED or (finish == "STOP" and out_tok == 0)
            if not needs_retry:
                return response
            if attempt < _RETRY_MAX:
                logger.warning(
                    "%s (output_tokens=%s) — retrying (%d/%d)",
                    finish,
                    out_tok,
                    attempt + 1,
                    _RETRY_MAX,
                )
                time.sleep(_RETRY_DELAY)
        return response


class _BlockToolsMiddleware(AgentMiddleware):
    """Block specific framework-injected tools at the code level.

    LLMs ignore prompt-level restrictions.  This middleware short-circuits
    blocked tool calls with an error message.
    """

    def __init__(self, blocked: frozenset[str]):
        super().__init__()
        self._blocked = blocked

    def _error(self, request):
        from langchain_core.messages import ToolMessage

        return ToolMessage(
            content=f"ERROR: '{request.tool_call['name']}' is disabled.",
            tool_call_id=request.tool_call["id"],
            status="error",
        )

    def wrap_tool_call(self, request, handler):
        if request.tool_call["name"] in self._blocked:
            return self._error(request)
        return handler(request)

    async def awrap_tool_call(self, request, handler):
        if request.tool_call["name"] in self._blocked:
            return self._error(request)
        return await handler(request)


def _is_retryable_api_error(exc: Exception) -> bool:
    """Return True for transient API errors (503, 429)."""
    s = str(exc)
    if type(exc).__name__ == "ServerError":
        return True
    if "RESOURCE_EXHAUSTED" in s or "429" in s:
        return True
    if "UNAVAILABLE" in s or "503" in s:
        return True
    code = getattr(exc, "status_code", None)
    return bool(code and (code == 429 or 500 <= code < 600))


def _server_retry() -> ModelRetryMiddleware:
    return ModelRetryMiddleware(
        max_retries=5,
        retry_on=_is_retryable_api_error,
        backoff_factor=2.0,
        initial_delay=10.0,
        max_delay=120.0,
        jitter=True,
        on_failure="error",
    )


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


def _resolve_model(model_spec: str) -> str | BaseChatModel:
    """Route through AI_BASE_URL when set, else return raw spec."""
    base_url = os.environ.get("AI_BASE_URL")
    if not base_url:
        return model_spec
    from langchain.chat_models import init_chat_model

    model_name = os.environ.get("AI_MODEL")
    if not model_name:
        _, _, model_name = model_spec.partition(":")
        model_name = model_name or model_spec
    return init_chat_model(
        f"openai:{model_name}",
        base_url=base_url,
        api_key=os.environ.get("AI_API_KEY", ""),
    )


# ---------------------------------------------------------------------------
# Backend — all VFS paths map to run_dir subdirectories on disk
# ---------------------------------------------------------------------------


def _create_backend(
    run_dir: Path,
    input_staging_dir: str | None = None,
):
    """Return a backend factory where VFS paths map to disk.

    Paths like ``/brief/``, ``/plan/``, ``/sources/``, ``/essay/``
    all resolve to subdirectories of *run_dir*.  ``/skills/`` routes
    to the actual skills directory on disk so agents can read SKILL.md
    files via the VFS.
    """
    from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend

    skills_dir = str(Path(__file__).resolve().parent / "skills")

    def factory(runtime):
        routes = {}
        for subdir in ("brief", "plan", "sources", "essay", "output"):
            path = run_dir / subdir
            path.mkdir(parents=True, exist_ok=True)
            routes[f"/{subdir}/"] = FilesystemBackend(
                root_dir=str(path),
                virtual_mode=True,
            )
        if input_staging_dir is not None:
            routes["/input/"] = FilesystemBackend(
                root_dir=input_staging_dir,
                virtual_mode=True,
            )
        routes["/skills/"] = FilesystemBackend(
            root_dir=skills_dir,
            virtual_mode=True,
        )
        return CompositeBackend(
            default=StateBackend(runtime),
            routes=routes,
        )

    return factory


# ---------------------------------------------------------------------------
# Agent factories
# ---------------------------------------------------------------------------


def create_worker(
    config: EssayWriterConfig,
    run_dir: Path,
    input_staging_dir: str | None = None,
) -> CompiledStateGraph:
    """Create a standalone worker agent (fast/cheap model).

    Uses ``create_agent`` with ``FilesystemMiddleware`` only — no
    TodoList, SubAgent, or Summarization middleware.  The pipeline
    tells the agent exactly which skill to read.
    """
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from langchain.agents import create_agent

    sources_dir = str(run_dir / "sources")
    tools = [
        make_read_pdf(sources_dir),
        read_docx,
        make_fetch_url(sources_dir),
        make_research_sources(sources_dir),
    ]

    fs_mw = FilesystemMiddleware(
        backend=_create_backend(run_dir, input_staging_dir),
    )

    return create_agent(
        model=_resolve_model(config.models.worker),
        tools=tools,
        system_prompt=render_prompt("worker.j2", config=config),
        middleware=[fs_mw, _server_retry(), _RetryMalformedMiddleware()],
        checkpointer=MemorySaver(),
        name="worker",
    )


def create_writer(
    config: EssayWriterConfig,
    run_dir: Path,
) -> CompiledStateGraph:
    """Create a standalone writer agent (quality model).

    Framework-injected tools (edit_file, grep, glob) are blocked
    via middleware so the writer focuses on writing.
    """
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from langchain.agents import create_agent

    block = _BlockToolsMiddleware(frozenset({"edit_file", "grep", "glob"}))
    fs_mw = FilesystemMiddleware(
        backend=_create_backend(run_dir),
    )

    return create_agent(
        model=_resolve_model(config.models.writer),
        tools=[],
        system_prompt=render_prompt("writer.j2", config=config),
        middleware=[fs_mw, _server_retry(), _RetryMalformedMiddleware(), block],
        checkpointer=MemorySaver(),
        name="writer",
    )
