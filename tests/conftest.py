from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def reset_settings_cache():
    from config.settings import EssayWriterConfig, reset_config_cache

    original_env_file = EssayWriterConfig.model_config.get("env_file")
    EssayWriterConfig.model_config["env_file"] = None
    reset_config_cache()
    yield
    EssayWriterConfig.model_config["env_file"] = original_env_file
    reset_config_cache()
