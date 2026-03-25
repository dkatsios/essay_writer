"""Pydantic configuration schemas for the essay writer."""

from __future__ import annotations
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import (
    PydanticBaseSettingsSource,
    YamlConfigSettingsSource,
)


class ModelsConfig(BaseModel):
    """Model selection per agent role."""

    worker: str = "google_genai:gemini-2.5-flash"
    writer: str = "google_genai:gemini-3.1-pro-preview"


class WritingConfig(BaseModel):
    """Writing phase settings."""

    word_count_tolerance: float = 0.10
    long_essay_threshold: int = 4000


class FormattingConfig(BaseModel):
    """Document formatting defaults."""

    font: str = "Times New Roman"
    font_size: int = 12
    line_spacing: float = 1.5
    margins_cm: float = 2.5
    citation_style: str = "apa7"
    page_numbers: str = "bottom_center"
    paragraph_indent: bool = True
    text_alignment: str = "justified"


class SearchConfig(BaseModel):
    """Academic search settings."""

    max_sources_per_direction: int = 5
    prefer_greek_sources: bool = True
    search_language: list[str] = ["el", "en"]
    sources_per_1k_words: int = 3
    min_sources: int = 5
    max_sources: int = 25


class PathsConfig(BaseModel):
    """File system paths."""

    output_dir: str = "./output"
    skills_dir: str = "/skills/"


class EssayWriterConfig(BaseSettings):
    """Root configuration for the essay writer.

    Config priority (highest wins):
      1. Environment variables (prefix: ESSAY_WRITER_)
      2. Custom YAML config file (via --config)
      3. Field defaults above
    """

    model_config = SettingsConfigDict(
        env_prefix="ESSAY_WRITER_",
        env_nested_delimiter="__",
    )

    models: ModelsConfig = ModelsConfig()
    writing: WritingConfig = WritingConfig()
    formatting: FormattingConfig = FormattingConfig()
    search: SearchConfig = SearchConfig()
    paths: PathsConfig = PathsConfig()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        yaml_file = cls.model_config.get("yaml_file")
        sources = [init_settings, env_settings]
        if yaml_file:
            sources.append(YamlConfigSettingsSource(settings_cls))
        return tuple(sources)


def load_config(yaml_path: str | None = None) -> EssayWriterConfig:
    """Load configuration, optionally from a custom YAML file.

    Args:
        yaml_path: Path to a YAML config file. If None, uses field defaults
            with environment variable overrides.

    Returns:
        Validated EssayWriterConfig instance.
    """
    if yaml_path is None:
        return EssayWriterConfig()

    # Override the yaml_file path for this instance
    class _CustomConfig(EssayWriterConfig):
        model_config = SettingsConfigDict(
            env_prefix="ESSAY_WRITER_",
            env_nested_delimiter="__",
            yaml_file=yaml_path,
            yaml_file_encoding="utf-8",
        )

    return _CustomConfig()
