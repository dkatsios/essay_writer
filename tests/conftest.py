from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def reset_settings_cache(monkeypatch, tmp_path: Path):
    from alembic import command
    from alembic.config import Config
    from config.settings import EssayWriterConfig, reset_config_cache
    from src.web_jobs import jobs

    original_env_file = EssayWriterConfig.model_config.get("env_file")
    EssayWriterConfig.model_config["env_file"] = None
    database_url = f"sqlite+pysqlite:///{tmp_path / 'test-web-jobs.db'}"
    monkeypatch.setenv(
        "ESSAY_WRITER_DATABASE__URL",
        database_url,
    )
    reset_config_cache()
    jobs.reset_for_tests()

    alembic_config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")
    reset_config_cache()

    yield
    jobs.reset_for_tests()
    EssayWriterConfig.model_config["env_file"] = original_env_file
    reset_config_cache()
