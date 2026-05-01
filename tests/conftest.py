from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def reset_settings_cache(monkeypatch, tmp_path: Path):
    from config.settings import EssayWriterConfig, reset_config_cache
    from src.web_jobs import jobs

    original_env_file = EssayWriterConfig.model_config.get("env_file")
    EssayWriterConfig.model_config["env_file"] = None
    monkeypatch.setenv(
        "ESSAY_WRITER_DATABASE__URL",
        f"sqlite+pysqlite:///{tmp_path / 'test-web-jobs.db'}",
    )
    reset_config_cache()
    jobs.reset_for_tests()
    yield
    jobs.reset_for_tests()
    EssayWriterConfig.model_config["env_file"] = original_env_file
    reset_config_cache()
