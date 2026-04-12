"""Thin model factories and retry helpers built on Instructor.

The runtime uses a deterministic Python pipeline:
- Instructor ``chat.completions.create(response_model=Schema)`` for JSON steps
- The same client without ``response_model`` for text steps

Provider selection comes from the configured ``provider:model`` spec or from
the OpenAI-compatible gateway when ``AI_BASE_URL`` is set.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import instructor

logger = logging.getLogger(__name__)

_RETRY_MAX = 5
_RETRY_INITIAL_DELAY = 10.0
_RETRY_MAX_DELAY = 120.0

_PROVIDER_ALIASES: dict[str, str] = {
    "google_genai": "google",
    "google_vertexai": "vertexai",
    "openai": "openai",
    "anthropic": "anthropic",
}

_PROVIDER_KEY_ENV: dict[str, str] = {
    "google_genai": "GOOGLE_API_KEY",
    "google_vertexai": "GOOGLE_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}

_GATEWAY_PROVIDER_MAP: dict[str, str] = {
    "google_vertexai": "vertex_ai.",
    "google_genai": "vertex_ai.",
    "openai": "openai.",
    "anthropic": "vertex_ai.anthropic.",
}


def _is_retryable(exc: Exception) -> bool:
    """Return True for transient API errors (503, 429, RESOURCE_EXHAUSTED, timeouts)."""
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    message = str(exc)
    if type(exc).__name__ == "ServerError":
        return True
    if "RESOURCE_EXHAUSTED" in message or "429" in message:
        return True
    if "UNAVAILABLE" in message or "503" in message:
        return True
    if "timeout" in message.lower() or "timed out" in message.lower():
        return True
    code = getattr(exc, "status_code", None)
    return bool(code and (code == 429 or 500 <= code < 600))


def _retry_with_backoff(fn, *, is_async: bool = False):
    """Run a sync or async callable with exponential backoff on transient errors."""
    if is_async:

        async def _async_inner():
            delay = _RETRY_INITIAL_DELAY
            for attempt in range(_RETRY_MAX + 1):
                try:
                    return await fn()
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

        return _async_inner()

    delay = _RETRY_INITIAL_DELAY
    for attempt in range(_RETRY_MAX + 1):
        try:
            return fn()
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


def extract_usage(response: Any) -> dict[str, int | str]:
    """Extract usage metadata from OpenAI, Anthropic, or Google responses."""
    input_tokens = 0
    output_tokens = 0
    thinking_tokens = 0
    model_name = getattr(response, "model", "") or getattr(
        response, "model_version", ""
    )

    usage = getattr(response, "usage", None)
    if usage is not None:
        input_tokens = (
            getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0
        )
        raw_output_tokens = (
            getattr(usage, "completion_tokens", 0)
            or getattr(usage, "output_tokens", 0)
            or 0
        )
        details = getattr(usage, "completion_tokens_details", None)
        thinking_tokens = getattr(details, "reasoning_tokens", 0) if details else 0
        output_tokens = max(raw_output_tokens - thinking_tokens, 0)

    usage_metadata = getattr(response, "usage_metadata", None)
    if usage_metadata is not None and not input_tokens:
        input_tokens = getattr(usage_metadata, "prompt_token_count", 0) or 0
        raw_output_tokens = getattr(usage_metadata, "candidates_token_count", 0) or 0
        thinking_tokens = getattr(usage_metadata, "thoughts_token_count", 0) or 0
        output_tokens = max(raw_output_tokens - thinking_tokens, 0)

    return {
        "input": input_tokens,
        "output": output_tokens,
        "thinking": thinking_tokens,
        "model": model_name,
    }


def extract_text(response: Any) -> str:
    """Extract plain text from OpenAI, Anthropic, or Google responses."""
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text

    choices = getattr(response, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", "") if message else ""
        if isinstance(content, str):
            return content

    content = getattr(response, "content", None)
    if content:
        first = content[0]
        return getattr(first, "text", "") or ""

    return str(response)


def _normalize_model_spec(
    model_spec: str,
    *,
    api_key: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Convert config model specs into Instructor provider strings and kwargs."""
    provider, _, bare_name = model_spec.partition(":")
    bare_name = bare_name or model_spec

    base_url = os.environ.get("AI_BASE_URL")
    if base_url:
        gateway_prefix = _GATEWAY_PROVIDER_MAP.get(provider, "")
        model_name = os.environ.get("AI_MODEL") or f"{gateway_prefix}{bare_name}"
        return f"openai/{model_name}", {
            "base_url": base_url,
            "api_key": api_key or os.environ.get("AI_API_KEY", "not-set"),
        }

    alias = _PROVIDER_ALIASES.get(provider, provider)
    key_env = _PROVIDER_KEY_ENV.get(provider, "OPENAI_API_KEY")
    return f"{alias}/{bare_name}", {
        "api_key": api_key or os.environ.get(key_env, "not-set")
    }


@dataclass
class ModelClient:
    """Thin wrapper around a sync Instructor client."""

    client: Any
    model: str
    model_spec: str
    api_key: str | None = None

    def to_async(self) -> AsyncModelClient:
        return create_async_client(self.model_spec, api_key=self.api_key)


@dataclass
class AsyncModelClient:
    """Thin wrapper around an async Instructor client."""

    client: Any
    model: str
    model_spec: str
    api_key: str | None = None


def create_client(model_spec: str, *, api_key: str | None = None) -> ModelClient:
    """Build the sync Instructor client for the selected provider."""
    normalized_model, kwargs = _normalize_model_spec(model_spec, api_key=api_key)
    client = instructor.from_provider(normalized_model, **kwargs)
    return ModelClient(
        client=client,
        model=normalized_model.split("/", 1)[1],
        model_spec=model_spec,
        api_key=api_key,
    )


def create_async_client(
    model_spec: str,
    *,
    api_key: str | None = None,
) -> AsyncModelClient:
    """Build the async Instructor client for the selected provider."""
    normalized_model, kwargs = _normalize_model_spec(model_spec, api_key=api_key)
    client = instructor.from_provider(normalized_model, async_client=True, **kwargs)
    return AsyncModelClient(
        client=client,
        model=normalized_model.split("/", 1)[1],
        model_spec=model_spec,
        api_key=api_key,
    )
