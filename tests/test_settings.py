"""Tests for config.settings env loading behavior."""

from __future__ import annotations


def test_load_config_reads_proxy_credentials_from_env_file(tmp_path, monkeypatch):
    from config.settings import EssayWriterConfig

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "ESSAY_WRITER_SEARCH__PROXY_PREFIX=https://proxy.example/login?url=",
                "ESSAY_WRITER_SEARCH__PROXY_USERNAME=test-user",
                "ESSAY_WRITER_SEARCH__PROXY_PASSWORD=test-pass",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("ESSAY_WRITER_SEARCH__PROXY_PREFIX", raising=False)
    monkeypatch.delenv("ESSAY_WRITER_SEARCH__PROXY_USERNAME", raising=False)
    monkeypatch.delenv("ESSAY_WRITER_SEARCH__PROXY_PASSWORD", raising=False)

    cfg = EssayWriterConfig(_env_file=env_file)

    assert cfg.search.proxy_prefix == "https://proxy.example/login?url="
    assert cfg.search.proxy_username == "test-user"
    assert cfg.search.proxy_password == "test-pass"


def test_load_config_reads_direct_credentials_from_env_file(tmp_path, monkeypatch):
    from config.settings import EssayWriterConfig

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "GOOGLE_API_KEY=AQ.vertex-key",
                "GOOGLE_CLOUD_PROJECT=env-project",
                "GOOGLE_CLOUD_LOCATION=us-central1",
                "AI_BASE_URL=https://gateway.example.com",
                "AI_API_KEY=gateway-key",
                "AI_MODEL=vertex_ai.gemini-2.5-flash",
                "SEMANTIC_SCHOLAR_API_KEY=semantic-key",
                "SSL_CERT_FILE=/tmp/test-ca.pem",
                "ESSAY_WEB_JOB_TTL_SECONDS=120",
                "ESSAY_WEB_LOG_FORMAT=text",
            ]
        ),
        encoding="utf-8",
    )

    for key in (
        "GOOGLE_API_KEY",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "AI_BASE_URL",
        "AI_API_KEY",
        "AI_MODEL",
        "SEMANTIC_SCHOLAR_API_KEY",
        "SSL_CERT_FILE",
        "ESSAY_WEB_JOB_TTL_SECONDS",
        "ESSAY_WEB_LOG_FORMAT",
    ):
        monkeypatch.delenv(key, raising=False)

    cfg = EssayWriterConfig(_env_file=env_file)

    assert cfg.google_api_key == "AQ.vertex-key"
    assert cfg.google_cloud_project == "env-project"
    assert cfg.google_cloud_location == "us-central1"
    assert cfg.ai_base_url == "https://gateway.example.com"
    assert cfg.ai_api_key == "gateway-key"
    assert cfg.ai_model == "vertex_ai.gemini-2.5-flash"
    assert cfg.semantic_scholar_api_key == "semantic-key"
    assert cfg.ssl_cert_file == "/tmp/test-ca.pem"
    assert cfg.web_job_ttl_seconds == 120
    assert cfg.web_log_format == "text"


def test_load_config_reads_worker_count_from_env_file(tmp_path, monkeypatch):
    from config.settings import EssayWriterConfig

    env_file = tmp_path / ".env"
    env_file.write_text(
        "ESSAY_WORKER_COUNT=4\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("ESSAY_WORKER_COUNT", raising=False)
    monkeypatch.delenv("ESSAY_WRITER_WORKER_COUNT", raising=False)

    cfg = EssayWriterConfig(_env_file=env_file)

    assert cfg.worker_count == 4
