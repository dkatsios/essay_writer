"""Pydantic configuration schemas for the essay writer."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import PydanticBaseSettingsSource, YamlConfigSettingsSource

_DEFAULT_YAML = Path(__file__).parent / "default.yaml"


class ModelsConfig(BaseModel):
    """Model selection per agent role."""

    orchestrator: str = "google_genai:gemini-2.5-flash"
    planner: str = "google_genai:gemini-2.5-flash"
    researcher: str = "google_genai:gemini-2.5-flash"
    cataloguer: str = "google_genai:gemini-2.5-flash"
    extractor: str = "google_genai:gemini-2.5-flash"
    writer: str = "google_genai:gemini-2.5-flash"
    reviewer: str = "google_genai:gemini-2.5-flash"
    builder: str = "google_genai:gemini-2.5-flash"


class PipelineConfig(BaseModel):
    """Pipeline behavior settings."""

    checkpoint_after_draft_plan: bool = False
    checkpoint_after_final_plan: bool = True
    checkpoint_after_review: bool = False
    default_mode: Literal["autonomous", "interactive"] = "autonomous"


class WritingConfig(BaseModel):
    """Writing phase settings."""

    word_count_tolerance: float = 0.10
    max_word_count_retries: int = 2
    long_essay_threshold: int = 3000
    intro_strategy: Literal["placeholder", "write_last"] = "placeholder"


class FormattingConfig(BaseModel):
    """Document formatting defaults."""

    font: str = "Times New Roman"
    font_size: int = 12
    line_spacing: float = 1.5
    margins_cm: float = 2.5
    citation_style: str = "apa7"
    page_numbers: str = "bottom_center"
    paragraph_indent: bool = True


class SearchConfig(BaseModel):
    """Academic search settings."""

    max_sources_per_direction: int = 5
    prefer_greek_sources: bool = True
    search_language: list[str] = ["el", "en"]


class PathsConfig(BaseModel):
    """File system paths."""

    output_dir: str = "./output"
    skills_dir: str = "/skills/"


class EssayWriterConfig(BaseSettings):
    """Root configuration for the essay writer.

    Config priority (highest wins):
      1. Environment variables (prefix: ESSAY_WRITER_)
      2. YAML config file (config/default.yaml or custom path)
      3. Field defaults above
    """

    model_config = SettingsConfigDict(
        env_prefix="ESSAY_WRITER_",
        env_nested_delimiter="__",
        yaml_file=str(_DEFAULT_YAML),
        yaml_file_encoding="utf-8",
    )

    models: ModelsConfig = ModelsConfig()
    pipeline: PipelineConfig = PipelineConfig()
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
        return (
            init_settings,
            env_settings,
            YamlConfigSettingsSource(settings_cls),
        )


def load_config(yaml_path: str | None = None) -> EssayWriterConfig:
    """Load configuration, optionally from a custom YAML file.

    Args:
        yaml_path: Path to a YAML config file. If None, uses config/default.yaml.

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
