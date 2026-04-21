"""Thin model factories and retry helpers built on Instructor.

The runtime uses a deterministic Python pipeline:
- Instructor ``chat.completions.create(response_model=Schema)`` for JSON steps
- The same client without ``response_model`` for text steps

Provider selection comes from the configured ``provider:model`` spec or from
the OpenAI-compatible gateway when ``AI_BASE_URL`` is set.
"""

from __future__ import annotations

import asyncio
import json
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
_GOOGLE_VERTEX_API_KEY_PREFIX = "AQ."
_GOOGLE_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

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


@dataclass(frozen=True)
class GoogleCredential:
    """Normalized Google credential input for direct provider routing."""

    kind: str
    raw_value: str | None = None
    service_account_info: dict[str, Any] | None = None
    project_id: str | None = None


def _resolve_api_key(provider: str, api_key: str | None) -> str | None:
    """Resolve the effective provider API key from args or environment."""
    if api_key:
        return api_key
    key_env = _PROVIDER_KEY_ENV.get(provider, "OPENAI_API_KEY")
    value = os.environ.get(key_env)
    return value or None


def _is_google_vertex_api_key(api_key: str | None) -> bool:
    """Return True when the key is a Vertex AI API key."""
    return bool(api_key and api_key.startswith(_GOOGLE_VERTEX_API_KEY_PREFIX))


def _parse_google_service_account_info(
    raw_value: str | None,
) -> dict[str, Any] | None:
    """Parse pasted service-account JSON or return None for non-JSON values."""
    if raw_value is None:
        return None
    text = raw_value.strip()
    if not text or not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Google credential looks like JSON but could not be parsed. "
            "Paste the full service-account JSON document."
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError("Google credential JSON must be an object.")
    if parsed.get("type") != "service_account":
        raise ValueError("Google credential JSON must be a service-account document.")
    missing = [
        field
        for field in ("client_email", "private_key", "token_uri")
        if not parsed.get(field)
    ]
    if missing:
        raise ValueError(
            "Google service-account JSON is missing required fields: "
            f"{', '.join(missing)}"
        )
    return parsed


def _classify_google_credential(raw_value: str | None) -> GoogleCredential:
    """Classify Google credentials as classic API key, AQ key, or service account."""
    if raw_value is None:
        return GoogleCredential(kind="missing")
    text = raw_value.strip()
    if not text:
        return GoogleCredential(kind="missing")
    if _is_google_vertex_api_key(text):
        return GoogleCredential(kind="vertex_api_key", raw_value=text)
    service_account_info = _parse_google_service_account_info(text)
    if service_account_info is None:
        return GoogleCredential(kind="api_key", raw_value=text)
    project_id = service_account_info.get("project_id") or None
    if project_id is not None and not isinstance(project_id, str):
        project_id = str(project_id)
    return GoogleCredential(
        kind="service_account",
        raw_value=text,
        service_account_info=service_account_info,
        project_id=project_id,
    )


def _require_vertex_project_and_location(
    *,
    project_fallback: str | None = None,
    credential_kind: str = "api_key",
) -> tuple[str, str]:
    """Return Vertex routing metadata or raise a configuration error."""
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or project_fallback
    location = os.environ.get("GOOGLE_CLOUD_LOCATION")
    if project and location:
        return project, location
    if credential_kind == "service_account":
        raise ValueError(
            "Vertex AI service-account credentials require GOOGLE_CLOUD_LOCATION "
            "and either GOOGLE_CLOUD_PROJECT or a service-account JSON project_id "
            "when using the direct Google provider."
        )
    raise ValueError(
        "Vertex AI Google API keys require GOOGLE_CLOUD_PROJECT and "
        "GOOGLE_CLOUD_LOCATION when using the direct Google provider."
    )


def _normalize_google_model(
    provider: str,
    bare_name: str,
    credential: GoogleCredential,
) -> tuple[str, dict[str, Any]]:
    """Normalize Google model specs across classic, Vertex key, and service account."""
    resolved_provider = provider
    if provider == "google_genai" and credential.kind in {
        "vertex_api_key",
        "service_account",
    }:
        resolved_provider = "google_vertexai"

    alias = _PROVIDER_ALIASES.get(resolved_provider, resolved_provider)
    kwargs: dict[str, Any] = {}
    if credential.kind != "service_account":
        kwargs["api_key"] = credential.raw_value or "not-set"
    if resolved_provider == "google_vertexai":
        project, location = _require_vertex_project_and_location(
            project_fallback=credential.project_id,
            credential_kind=credential.kind,
        )
        kwargs["project"] = project
        kwargs["location"] = location
    return f"{alias}/{bare_name}", kwargs


def _build_google_service_account_client(
    *,
    normalized_model: str,
    model_spec: str,
    service_account_info: dict[str, Any],
    project: str,
    location: str,
    use_async: bool,
) -> ModelClient | AsyncModelClient:
    """Build an Instructor-wrapped Google GenAI client from pasted service-account JSON."""
    from google import genai
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=[_GOOGLE_CLOUD_PLATFORM_SCOPE],
    )
    raw_client = genai.Client(
        vertexai=True,
        credentials=credentials,
        project=project,
        location=location,
    )
    client = instructor.from_genai(raw_client, use_async=use_async)
    wrapper_cls = AsyncModelClient if use_async else ModelClient
    return wrapper_cls(
        client=client,
        model=normalized_model.split("/", 1)[1],
        model_spec=model_spec,
    )


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


def _compact_retry_error(exc: Exception) -> str:
    """Return a short, single-line summary for retryable API errors."""
    message = " ".join(str(exc).split())
    lowered = message.lower()

    if (
        "resource_exhausted" in lowered
        or " 429 " in f" {lowered} "
        or lowered.startswith("429")
    ):
        return "429 RESOURCE_EXHAUSTED"
    if (
        "unavailable" in lowered
        or " 503 " in f" {lowered} "
        or lowered.startswith("503")
    ):
        return "503 UNAVAILABLE"
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"

    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return f"HTTP {status_code}"
    if message:
        return message[:160]
    return type(exc).__name__


def _should_log_retry_warning(attempt: int) -> bool:
    """Log the first retry and the final retry; suppress the middle ones."""
    return attempt == 0 or attempt == _RETRY_MAX - 1


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
                        if _should_log_retry_warning(attempt):
                            logger.warning(
                                "Transient API error (attempt %d/%d): %s — retrying in %.0fs",
                                attempt + 1,
                                _RETRY_MAX,
                                _compact_retry_error(exc),
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
                if _should_log_retry_warning(attempt):
                    logger.warning(
                        "Transient API error (attempt %d/%d): %s — retrying in %.0fs",
                        attempt + 1,
                        _RETRY_MAX,
                        _compact_retry_error(exc),
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
    effective_api_key = _resolve_api_key(provider, api_key)
    google_credential = (
        _classify_google_credential(effective_api_key)
        if provider in {"google_genai", "google_vertexai"}
        else None
    )

    base_url = os.environ.get("AI_BASE_URL")
    if base_url:
        gateway_api_key = api_key or os.environ.get("AI_API_KEY", "not-set")
        if google_credential is not None and google_credential.kind in {
            "vertex_api_key",
            "service_account",
        }:
            logger.warning(
                "AI_BASE_URL is set; direct Google Vertex credentials are skipped "
                "and gateway credentials are used instead."
            )
            gateway_api_key = os.environ.get("AI_API_KEY", "not-set")
        gateway_prefix = _GATEWAY_PROVIDER_MAP.get(provider, "")
        model_name = os.environ.get("AI_MODEL") or f"{gateway_prefix}{bare_name}"
        return f"openai/{model_name}", {
            "base_url": base_url,
            "api_key": gateway_api_key,
        }

    if google_credential is not None:
        return _normalize_google_model(provider, bare_name, google_credential)

    alias = _PROVIDER_ALIASES.get(provider, provider)
    return f"{alias}/{bare_name}", {"api_key": effective_api_key or "not-set"}


@dataclass
class ModelClient:
    """Thin wrapper around a sync Instructor client."""

    client: Any
    model: str
    model_spec: str

    def to_async(self) -> AsyncModelClient:
        return create_async_client(self.model_spec)


@dataclass
class AsyncModelClient:
    """Thin wrapper around an async Instructor client."""

    client: Any
    model: str
    model_spec: str


def create_client(model_spec: str, *, api_key: str | None = None) -> ModelClient:
    """Build the sync Instructor client for the selected provider."""
    normalized_model, kwargs = _normalize_model_spec(model_spec, api_key=api_key)
    provider, _, _bare_name = model_spec.partition(":")
    if provider in {"google_genai", "google_vertexai"} and "api_key" not in kwargs:
        google_credential = _classify_google_credential(
            _resolve_api_key(provider, api_key)
        )
        if google_credential.service_account_info is not None:
            return _build_google_service_account_client(
                normalized_model=normalized_model,
                model_spec=model_spec,
                service_account_info=google_credential.service_account_info,
                project=kwargs["project"],
                location=kwargs["location"],
                use_async=False,
            )
    client = instructor.from_provider(normalized_model, **kwargs)
    return ModelClient(
        client=client,
        model=normalized_model.split("/", 1)[1],
        model_spec=model_spec,
    )


def create_async_client(
    model_spec: str,
    *,
    api_key: str | None = None,
) -> AsyncModelClient:
    """Build the async Instructor client for the selected provider."""
    normalized_model, kwargs = _normalize_model_spec(model_spec, api_key=api_key)
    provider, _, _bare_name = model_spec.partition(":")
    if provider in {"google_genai", "google_vertexai"} and "api_key" not in kwargs:
        google_credential = _classify_google_credential(
            _resolve_api_key(provider, api_key)
        )
        if google_credential.service_account_info is not None:
            return _build_google_service_account_client(
                normalized_model=normalized_model,
                model_spec=model_spec,
                service_account_info=google_credential.service_account_info,
                project=kwargs["project"],
                location=kwargs["location"],
                use_async=True,
            )
    client = instructor.from_provider(normalized_model, async_client=True, **kwargs)
    return AsyncModelClient(
        client=client,
        model=normalized_model.split("/", 1)[1],
        model_spec=model_spec,
    )
