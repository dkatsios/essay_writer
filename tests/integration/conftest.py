"""Shared fixtures for integration tests that hit real external services.

These tests are **not** run by the default ``pytest tests/`` invocation.
Run them explicitly::

    uv run python -m pytest tests/integration/ -v

They require live R2 credentials in ``.env`` (or environment variables).
Tests are auto-skipped when credentials are missing.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from dotenv import load_dotenv

from src.storage import RunStorage, get_s3_client

# Load .env directly so integration fixtures see real credentials
# (the autouse reset_settings_cache from tests/conftest.py strips env_file).
_repo_root = Path(__file__).resolve().parents[2]
load_dotenv(_repo_root / ".env", override=True)


def _r2_config():
    """Build a StorageConfig-like namespace from env vars."""
    from types import SimpleNamespace

    return SimpleNamespace(
        r2_endpoint_url=os.environ.get("ESSAY_WRITER_STORAGE__R2_ENDPOINT_URL", ""),
        r2_access_key_id=os.environ.get("ESSAY_WRITER_STORAGE__R2_ACCESS_KEY_ID", ""),
        r2_secret_access_key=os.environ.get(
            "ESSAY_WRITER_STORAGE__R2_SECRET_ACCESS_KEY", ""
        ),
        r2_bucket=os.environ.get("ESSAY_WRITER_STORAGE__R2_BUCKET", ""),
    )


def _has_r2_creds() -> bool:
    cfg = _r2_config()
    return bool(
        cfg.r2_endpoint_url
        and cfg.r2_access_key_id
        and cfg.r2_secret_access_key
        and cfg.r2_bucket
    )


pytestmark = pytest.mark.skipif(
    not _has_r2_creds(), reason="R2 credentials not configured"
)


@pytest.fixture()
def r2_storage():
    """Yield a RunStorage pointed at a unique temporary prefix, then clean up."""
    cfg = _r2_config()
    client = get_s3_client(cfg)
    test_prefix = f"_test/{uuid.uuid4().hex[:12]}/"
    storage = RunStorage(client, cfg.r2_bucket, test_prefix)
    yield storage
    # Cleanup: delete everything under the test prefix
    storage.delete_all()
