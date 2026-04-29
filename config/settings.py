"""Pydantic configuration schemas for the essay writer."""

from __future__ import annotations
from pydantic import BaseModel, ConfigDict, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    proxy_prefix: str = "https://login.proxy.eap.gr/login?url="
    """EZProxy URL prefix for institutional access (e.g. 'https://proxy.uoa.gr/login?url='). When set, PDF fetch URLs are rewritten through the proxy. Simple EZProxy uses URL-prefix rewriting; Shibboleth-based proxies use hostname rewriting (auto-detected)."""
    proxy_username: str = "std523991"
    """Username for institutional proxy authentication (Shibboleth/EZProxy). When set with proxy_password, the proxy session is authenticated before PDF downloads."""
    proxy_password: str = "f7mv9u"
    """Password for institutional proxy authentication."""


class EssayWriterConfig(BaseSettings):
    """Root configuration for the essay writer.

    Config priority (highest wins):
      1. Environment variables (prefix: ESSAY_WRITER_)
    2. Field defaults above
    """

    model_config = SettingsConfigDict(
        env_prefix="ESSAY_WRITER_",
        env_nested_delimiter="__",
    )

    models: ModelsConfig = ModelsConfig()
    writing: WritingConfig = WritingConfig()
    formatting: FormattingConfig = FormattingConfig()
    search: SearchConfig = SearchConfig()


def load_config() -> EssayWriterConfig:
    """Load configuration from environment variables and model defaults."""
    return EssayWriterConfig()
