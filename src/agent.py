"""Model factories — creates configured LangChain chat models.

The Python pipeline (``src/pipeline.py``) calls models directly:
- ``model.with_structured_output(Schema)`` for JSON steps (with auto-retry)
- ``model.invoke(messages)`` for text steps (essay writing/review)

No agents, no VFS, no middleware — just models.
"""

from __future__ import annotations

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


def _is_retryable(exc: Exception) -> bool:
    """Return True for transient API errors (503, 429, RESOURCE_EXHAUSTED)."""
    s = str(exc)
    if type(exc).__name__ == "ServerError":
        return True
    if "RESOURCE_EXHAUSTED" in s or "429" in s:
        return True
    if "UNAVAILABLE" in s or "503" in s:
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


# ---------------------------------------------------------------------------
# Model creation
# ---------------------------------------------------------------------------


def create_model(model_spec: str) -> BaseChatModel:
    """Create a LangChain chat model from a spec like ``google_genai:gemini-2.5-flash``.

    When ``AI_BASE_URL`` is set, routes through an OpenAI-compatible endpoint.
    """
    from langchain.chat_models import init_chat_model

    base_url = os.environ.get("AI_BASE_URL")
    if base_url:
        model_name = os.environ.get("AI_MODEL")
        if not model_name:
            _, _, model_name = model_spec.partition(":")
            model_name = model_name or model_spec
        return init_chat_model(
            f"openai:{model_name}",
            base_url=base_url,
            api_key=os.environ.get("AI_API_KEY", ""),
        )

    return init_chat_model(model_spec)
