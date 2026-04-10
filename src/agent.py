"""Model factories and retry helpers for direct LangChain model calls.

The runtime uses a deterministic Python pipeline:
- ``model.with_structured_output(Schema)`` for JSON steps
- ``model.invoke(messages)`` for text steps

There is no orchestration framework or virtual filesystem layer in the runtime.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Transient API error retry
# ---------------------------------------------------------------------------

_RETRY_MAX = 5
_RETRY_INITIAL_DELAY = 10.0
_RETRY_MAX_DELAY = 120.0
_REQUEST_TIMEOUT = 300  # seconds per API request


def _is_retryable(exc: Exception) -> bool:
    """Return True for transient API errors (503, 429, RESOURCE_EXHAUSTED, timeouts)."""
    # Timeouts are retryable (hung connections, slow responses)
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    s = str(exc)
    if type(exc).__name__ == "ServerError":
        return True
    if "RESOURCE_EXHAUSTED" in s or "429" in s:
        return True
    if "UNAVAILABLE" in s or "503" in s:
        return True
    if "timeout" in s.lower() or "timed out" in s.lower():
        return True
    code = getattr(exc, "status_code", None)
    return bool(code and (code == 429 or 500 <= code < 600))


def invoke_with_retry(
    model: BaseChatModel, messages: list, *, config: dict | None = None
):
    """Invoke a model with exponential backoff on transient errors."""
    delay = _RETRY_INITIAL_DELAY
    for attempt in range(_RETRY_MAX + 1):
        try:
            return model.invoke(messages, config=config)
        except Exception as exc:
            if attempt < _RETRY_MAX and _is_retryable(exc):
                logger.warning(
                    "Transient API error (attempt %d/%d): %s — retrying in %.0fs",
                    attempt + 1,
                    _RETRY_MAX,
                    exc,
                    delay,
                )
                time.sleep(delay)
                delay = min(delay * 2, _RETRY_MAX_DELAY)
                continue
            raise


async def ainvoke_with_retry(
    model: BaseChatModel, messages: list, *, config: dict | None = None
):
    """Async version of invoke_with_retry using ainvoke + asyncio.sleep."""
    delay = _RETRY_INITIAL_DELAY
    for attempt in range(_RETRY_MAX + 1):
        try:
            return await model.ainvoke(messages, config=config)
        except Exception as exc:
            if attempt < _RETRY_MAX and _is_retryable(exc):
                logger.warning(
                    "Transient API error (attempt %d/%d): %s — retrying in %.0fs",
                    attempt + 1,
                    _RETRY_MAX,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RETRY_MAX_DELAY)
                continue
            raise


# ---------------------------------------------------------------------------
# Model creation
# ---------------------------------------------------------------------------


# Map LangChain provider prefixes to gateway (LiteLLM-style) dot-prefixes.
_GATEWAY_PROVIDER_MAP: dict[str, str] = {
    "google_vertexai": "vertex_ai.",
    "google_genai": "vertex_ai.",
    "openai": "openai.",
    "anthropic": "vertex_ai.anthropic.",
}


def create_model(model_spec: str) -> BaseChatModel:
    """Create a LangChain chat model from a spec like ``google_genai:gemini-2.5-flash``.

    When ``AI_BASE_URL`` is set, routes through an OpenAI-compatible endpoint.
    The LangChain provider prefix (e.g. ``google_genai``) is translated to
    the gateway's expected prefix (e.g. ``gemini/``) so that each model role
    keeps its own model name.
    """
    from langchain.chat_models import init_chat_model

    base_url = os.environ.get("AI_BASE_URL")
    if base_url:
        model_name = os.environ.get("AI_MODEL")
        if not model_name:
            provider, _, bare_name = model_spec.partition(":")
            bare_name = bare_name or model_spec
            gateway_prefix = _GATEWAY_PROVIDER_MAP.get(provider, "")
            model_name = f"{gateway_prefix}{bare_name}"
        return init_chat_model(
            f"openai:{model_name}",
            base_url=base_url,
            api_key=os.environ.get("AI_API_KEY", ""),
            timeout=_REQUEST_TIMEOUT,
        )

    return init_chat_model(model_spec, timeout=_REQUEST_TIMEOUT)
