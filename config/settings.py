"""Pydantic configuration schemas for the essay writer."""

from __future__ import annotations
from functools import lru_cache
from pathlib import Path

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ENV_FILE = _PROJECT_ROOT / ".env"
_DEFAULT_MAILTO = "essay-writer@example.com"
_DEFAULT_DATABASE_URL = f"sqlite+pysqlite:///{_PROJECT_ROOT / '.essay_writer_jobs.db'}"
_DEFAULT_JOB_TTL_SECONDS = 86_400
_DEFAULT_JOB_SWEEP_INTERVAL_SECONDS = 300
_DEFAULT_INTERACTION_TIMEOUT_SECONDS = 1_800
_DEFAULT_WORKER_POLL_INTERVAL_SECONDS = 2
_DEFAULT_WORKER_LEASE_SECONDS = 60
_DEFAULT_WORKER_HEARTBEAT_INTERVAL_SECONDS = 15


def _alias_choices(*names: str) -> AliasChoices:
    return AliasChoices(*names)


class ProviderModels(BaseModel):
    """Model specs for a single provider."""

    worker: str
    writer: str
    reviewer: str


class GoogleModels(ProviderModels):
    worker: str = "google_genai:gemini-2.5-flash"
    writer: str = "google_genai:gemini-3.1-pro-preview"
    reviewer: str = "google_genai:gemini-3.1-pro-preview"


class OpenAIModels(ProviderModels):
    worker: str = "openai:gpt-5.4-nano"
    writer: str = "openai:gpt-5.4"
    reviewer: str = "openai:gpt-5.4"


class AnthropicModels(ProviderModels):
    worker: str = "anthropic:claude-haiku-4-5"
    writer: str = "anthropic:claude-sonnet-4-6"
    reviewer: str = "anthropic:claude-opus-4-6"


PROVIDER_PRESETS: dict[str, type[ProviderModels]] = {
    "google": GoogleModels,
    "openai": OpenAIModels,
    "anthropic": AnthropicModels,
}


class ModelsConfig(BaseModel):
    """Model selection per agent role."""

    provider: str | None = None
    worker: str = GoogleModels.model_fields["worker"].default
    writer: str = GoogleModels.model_fields["writer"].default
    reviewer: str = GoogleModels.model_fields["reviewer"].default

    @model_validator(mode="before")
    @classmethod
    def apply_provider_preset(cls, values: dict) -> dict:
        provider = values.get("provider")
        if not provider:
            return values
        preset_cls = PROVIDER_PRESETS.get(provider)
        if not preset_cls:
            raise ValueError(
                f"Unknown provider {provider!r}. "
                f"Choose from: {', '.join(sorted(PROVIDER_PRESETS))}"
            )
        preset = preset_cls()
        if "worker" not in values:
            values["worker"] = preset.worker
        if "writer" not in values:
            values["writer"] = preset.writer
        if "reviewer" not in values:
            values["reviewer"] = preset.reviewer
        return values


class WritingConfig(BaseModel):
    """Writing phase settings."""

    word_count_tolerance: float = 0.10
    word_count_tolerance_over: float = 0.20
    long_essay_threshold: int = 4000
    interactive_validation: bool = True


class FormattingConfig(BaseModel):
    """Document formatting defaults."""

    font: str = "Times New Roman"
    font_size: int = 12
    line_spacing: float = 1.5
    margins_cm: float = 2.5
    citation_style: str = "apa7"
    page_numbers: str = "bottom_center"
    paragraph_indent: bool = False
    text_alignment: str = "justified"


class SearchConfig(BaseModel):
    """Academic search settings."""

    model_config = ConfigDict(extra="ignore")

    fetch_per_api: int = 20
    sources_per_1k_words: int = 5
    min_sources: int = 12
    overfetch_multiplier: float = 3.0
    recovery_overfetch_multiplier: float = 2.0
    recovery_fetch_per_api_multiplier: float = 2.0
    recovery_prefer_fulltext: bool = True
    section_source_full_detail_max: int = 22
    """Per section (long path) or single shot (short path): max sources with full summary/extracts in the writer prompt; all selected sources still appear in a compact catalog."""
    optional_pdf_prompt_top_n: int = 5
    """Offer optional PDF upload for up to this many API sources without full text (0 = off)."""
    optional_pdf_min_body_words: int = 50
    """Minimum word count of fetched/local body text to count as full text (not abstract-only)."""
    triage_batch_size: int = 50
    """Maximum number of title+abstract candidates per triage LLM call."""
    min_relevance_score: int = 3
    """Minimum 1–5 relevance score required for final source selection."""
    proxy_prefix: str = ""
    """EZProxy URL prefix for institutional access (e.g. 'https://proxy.uoa.gr/login?url='). When set, PDF fetch URLs are rewritten through the proxy. Simple EZProxy uses URL-prefix rewriting; Shibboleth-based proxies use hostname rewriting (auto-detected)."""
    proxy_username: str = ""
    """Username for institutional proxy authentication (Shibboleth/EZProxy). When set with proxy_password, the proxy session is authenticated before PDF downloads."""
    proxy_password: str = ""
    """Password for institutional proxy authentication."""


class DatabaseConfig(BaseModel):
    """Persistent job-state database settings."""

    url: str = _DEFAULT_DATABASE_URL
    echo: bool = False
    mark_stale_jobs_on_startup: bool = True

    @field_validator("url", mode="before")
    @classmethod
    def _normalize_url(cls, value: object) -> str:
        text = str(value).strip() if value is not None else ""
        return text or _DEFAULT_DATABASE_URL


class StorageConfig(BaseModel):
    """Artifact storage settings — supports ``r2`` and ``local`` backends."""

    backend: str = "r2"
    """Storage backend: ``"r2"`` for Cloudflare R2 (production), ``"local"`` for filesystem (development)."""
    local_dir: str = "runs"
    """Base directory for local-backend artifacts (relative to project root)."""
    r2_endpoint_url: str = ""
    """S3-compatible endpoint URL (e.g. https://<account>.r2.cloudflarestorage.com)."""
    r2_bucket: str = ""
    """R2 bucket name."""
    r2_access_key_id: str = ""
    """R2 API access key ID."""
    r2_secret_access_key: str = ""
    """R2 API secret access key."""
    run_prefix: str = "runs/"
    """Key prefix for all run artifacts within the bucket (R2) or under local_dir (local)."""


class EssayWriterConfig(BaseSettings):
    """Root configuration for the essay writer.

    Config priority (highest wins):
      1. Environment variables (prefix: ESSAY_WRITER_)
    2. Field defaults above
    """

    model_config = SettingsConfigDict(
        env_prefix="ESSAY_WRITER_",
        env_nested_delimiter="__",
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    models: ModelsConfig = ModelsConfig()
    writing: WritingConfig = WritingConfig()
    formatting: FormattingConfig = FormattingConfig()
    search: SearchConfig = SearchConfig()
    database: DatabaseConfig = DatabaseConfig()
    storage: StorageConfig = StorageConfig()
    google_api_key: str | None = Field(
        default=None,
        validation_alias=_alias_choices(
            "GOOGLE_API_KEY",
            "ESSAY_WRITER_GOOGLE_API_KEY",
        ),
    )
    google_cloud_project: str | None = Field(
        default=None,
        validation_alias=_alias_choices(
            "GOOGLE_CLOUD_PROJECT",
            "ESSAY_WRITER_GOOGLE_CLOUD_PROJECT",
        ),
    )
    google_cloud_location: str | None = Field(
        default=None,
        validation_alias=_alias_choices(
            "GOOGLE_CLOUD_LOCATION",
            "ESSAY_WRITER_GOOGLE_CLOUD_LOCATION",
        ),
    )
    openai_api_key: str | None = Field(
        default=None,
        validation_alias=_alias_choices(
            "OPENAI_API_KEY",
            "ESSAY_WRITER_OPENAI_API_KEY",
        ),
    )
    anthropic_api_key: str | None = Field(
        default=None,
        validation_alias=_alias_choices(
            "ANTHROPIC_API_KEY",
            "ESSAY_WRITER_ANTHROPIC_API_KEY",
        ),
    )
    ai_base_url: str | None = Field(
        default=None,
        validation_alias=_alias_choices(
            "AI_BASE_URL",
            "ESSAY_WRITER_AI_BASE_URL",
        ),
    )
    ai_api_key: str | None = Field(
        default=None,
        validation_alias=_alias_choices(
            "AI_API_KEY",
            "ESSAY_WRITER_AI_API_KEY",
        ),
    )
    ai_model: str | None = Field(
        default=None,
        validation_alias=_alias_choices(
            "AI_MODEL",
            "ESSAY_WRITER_AI_MODEL",
        ),
    )
    semantic_scholar_api_key: str | None = Field(
        default=None,
        validation_alias=_alias_choices(
            "SEMANTIC_SCHOLAR_API_KEY",
            "ESSAY_WRITER_SEMANTIC_SCHOLAR_API_KEY",
        ),
    )
    openalex_mailto: str = Field(
        default=_DEFAULT_MAILTO,
        validation_alias=_alias_choices(
            "OPENALEX_MAILTO",
            "ESSAY_WRITER_OPENALEX_MAILTO",
        ),
    )
    crossref_mailto: str = Field(
        default=_DEFAULT_MAILTO,
        validation_alias=_alias_choices(
            "CROSSREF_MAILTO",
            "ESSAY_WRITER_CROSSREF_MAILTO",
        ),
    )
    ssl_cert_file: str | None = Field(
        default=None,
        validation_alias=_alias_choices(
            "SSL_CERT_FILE",
            "ESSAY_WRITER_SSL_CERT_FILE",
        ),
    )
    requests_ca_bundle: str | None = Field(
        default=None,
        validation_alias=_alias_choices(
            "REQUESTS_CA_BUNDLE",
            "ESSAY_WRITER_REQUESTS_CA_BUNDLE",
        ),
    )
    web_job_ttl_seconds: int = Field(
        default=_DEFAULT_JOB_TTL_SECONDS,
        validation_alias=_alias_choices(
            "ESSAY_WEB_JOB_TTL_SECONDS",
            "ESSAY_WRITER_WEB_JOB_TTL_SECONDS",
        ),
        ge=0,
    )
    web_job_sweep_interval_seconds: int = Field(
        default=_DEFAULT_JOB_SWEEP_INTERVAL_SECONDS,
        validation_alias=_alias_choices(
            "ESSAY_WEB_JOB_SWEEP_INTERVAL_SECONDS",
            "ESSAY_WRITER_WEB_JOB_SWEEP_INTERVAL_SECONDS",
        ),
        ge=60,
    )
    web_interaction_timeout_seconds: int = Field(
        default=_DEFAULT_INTERACTION_TIMEOUT_SECONDS,
        validation_alias=_alias_choices(
            "ESSAY_WEB_INTERACTION_TIMEOUT_SECONDS",
            "ESSAY_WRITER_WEB_INTERACTION_TIMEOUT_SECONDS",
        ),
        ge=1,
    )
    web_log_format: str = Field(
        default="json",
        validation_alias=_alias_choices(
            "ESSAY_WEB_LOG_FORMAT",
            "ESSAY_WRITER_WEB_LOG_FORMAT",
        ),
    )
    worker_poll_interval_seconds: int = Field(
        default=_DEFAULT_WORKER_POLL_INTERVAL_SECONDS,
        validation_alias=_alias_choices(
            "ESSAY_WORKER_POLL_INTERVAL_SECONDS",
            "ESSAY_WRITER_WORKER_POLL_INTERVAL_SECONDS",
        ),
        ge=1,
    )
    worker_lease_seconds: int = Field(
        default=_DEFAULT_WORKER_LEASE_SECONDS,
        validation_alias=_alias_choices(
            "ESSAY_WORKER_LEASE_SECONDS",
            "ESSAY_WRITER_WORKER_LEASE_SECONDS",
        ),
        ge=5,
    )
    worker_heartbeat_interval_seconds: int = Field(
        default=_DEFAULT_WORKER_HEARTBEAT_INTERVAL_SECONDS,
        validation_alias=_alias_choices(
            "ESSAY_WORKER_HEARTBEAT_INTERVAL_SECONDS",
            "ESSAY_WRITER_WORKER_HEARTBEAT_INTERVAL_SECONDS",
        ),
        ge=1,
    )

    @field_validator(
        "google_api_key",
        "google_cloud_project",
        "google_cloud_location",
        "openai_api_key",
        "anthropic_api_key",
        "ai_base_url",
        "ai_api_key",
        "ai_model",
        "semantic_scholar_api_key",
        "ssl_cert_file",
        "requests_ca_bundle",
        mode="before",
    )
    @classmethod
    def _blank_optional_strings_to_none(cls, value: object) -> object:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("openalex_mailto", "crossref_mailto", mode="before")
    @classmethod
    def _normalize_mailto(cls, value: object) -> str:
        text = str(value).strip() if value is not None else ""
        return text or _DEFAULT_MAILTO

    @field_validator("web_log_format", mode="before")
    @classmethod
    def _normalize_log_format(cls, value: object) -> str:
        text = str(value).strip().lower() if value is not None else ""
        return text or "json"


@lru_cache(maxsize=1)
def load_config() -> EssayWriterConfig:
    """Load configuration from environment variables and model defaults."""
    return EssayWriterConfig()


def reset_config_cache() -> None:
    """Clear the cached settings object for tests and config-sensitive tooling."""
    load_config.cache_clear()
