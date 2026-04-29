"""Tests for src/agent.py — retry logic, Google provider normalization, async client."""

from __future__ import annotations

import json
import logging

import pytest


def _google_service_account_json(project_id: str = "service-project") -> str:
    return json.dumps(
        {
            "type": "service_account",
            "project_id": project_id,
            "private_key_id": "private-key-id",
            "private_key": "-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----\n",
            "client_email": "essay-writer@test-project.iam.gserviceaccount.com",
            "client_id": "1234567890",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/test",
        },
        indent=2,
    )


class TestRetryWithBackoff:
    """Tests for retry_with_backoff in agent.py."""

    def test_immediate_success(self):
        from src.agent import retry_with_backoff

        result = retry_with_backoff(lambda: "hello")
        assert result == "hello"

    def test_retries_on_resource_exhausted(self):
        from src.agent import retry_with_backoff

        calls = []

        def fn():
            calls.append(1)
            if len(calls) == 1:
                raise Exception("429 RESOURCE_EXHAUSTED")
            return "ok"

        import src.agent

        original_sleep = src.agent.time.sleep
        src.agent.time.sleep = lambda _: None
        try:
            result = retry_with_backoff(fn)
            assert result == "ok"
            assert len(calls) == 2
        finally:
            src.agent.time.sleep = original_sleep

    def test_retries_on_timeout(self):
        from src.agent import retry_with_backoff

        calls = []

        def fn():
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError("Request timed out")
            return "ok"

        import src.agent

        original_sleep = src.agent.time.sleep
        src.agent.time.sleep = lambda _: None
        try:
            result = retry_with_backoff(fn)
            assert result == "ok"
            assert len(calls) == 2
        finally:
            src.agent.time.sleep = original_sleep


class TestGoogleProviderNormalization:
    def test_google_classic_api_key_stays_on_google_provider(self, monkeypatch):
        from src.agent import normalize_model_spec

        monkeypatch.delenv("AI_BASE_URL", raising=False)
        monkeypatch.setenv("GOOGLE_API_KEY", "AIza-classic-key")
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)

        model, kwargs = normalize_model_spec("google_genai:gemini-2.5-flash")

        assert model == "google/gemini-2.5-flash"
        assert kwargs == {"api_key": "AIza-classic-key"}

    def test_google_vertex_api_key_routes_to_vertex_provider(self, monkeypatch):
        from src.agent import normalize_model_spec

        monkeypatch.delenv("AI_BASE_URL", raising=False)
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-project")
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

        model, kwargs = normalize_model_spec(
            "google_genai:gemini-2.5-flash",
            api_key="AQ.vertex-key",
        )

        assert model == "vertexai/gemini-2.5-flash"
        assert kwargs == {
            "api_key": "AQ.vertex-key",
            "project": "demo-project",
            "location": "us-central1",
        }

    def test_google_vertex_api_key_requires_project_and_location(self, monkeypatch):
        from src.agent import normalize_model_spec

        monkeypatch.delenv("AI_BASE_URL", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)

        with pytest.raises(
            ValueError,
            match="GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION",
        ):
            normalize_model_spec(
                "google_genai:gemini-2.5-flash",
                api_key="AQ.vertex-key",
            )

    def test_google_service_account_json_routes_to_vertex_provider(self, monkeypatch):
        from src.agent import normalize_model_spec

        monkeypatch.delenv("AI_BASE_URL", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

        model, kwargs = normalize_model_spec(
            "google_genai:gemini-2.5-flash",
            api_key=_google_service_account_json(),
        )

        assert model == "vertexai/gemini-2.5-flash"
        assert kwargs == {
            "project": "service-project",
            "location": "us-central1",
        }

    def test_google_service_account_json_prefers_env_project(self, monkeypatch):
        from src.agent import normalize_model_spec

        monkeypatch.delenv("AI_BASE_URL", raising=False)
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "env-project")
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

        model, kwargs = normalize_model_spec(
            "google_genai:gemini-2.5-flash",
            api_key=_google_service_account_json(project_id="json-project"),
        )

        assert model == "vertexai/gemini-2.5-flash"
        assert kwargs == {
            "project": "env-project",
            "location": "us-central1",
        }

    def test_google_service_account_json_requires_location(self, monkeypatch):
        from src.agent import normalize_model_spec

        monkeypatch.delenv("AI_BASE_URL", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)

        with pytest.raises(
            ValueError,
            match="GOOGLE_CLOUD_LOCATION and either GOOGLE_CLOUD_PROJECT or a service-account JSON project_id",
        ):
            normalize_model_spec(
                "google_genai:gemini-2.5-flash",
                api_key=_google_service_account_json(),
            )

    def test_google_service_account_json_parse_error_is_explicit(self, monkeypatch):
        from src.agent import normalize_model_spec

        monkeypatch.delenv("AI_BASE_URL", raising=False)

        with pytest.raises(
            ValueError,
            match="looks like JSON but could not be parsed",
        ):
            normalize_model_spec(
                "google_genai:gemini-2.5-flash",
                api_key='{"type": "service_account",',
            )

    def test_explicit_google_vertexai_provider_uses_vertex_metadata(self, monkeypatch):
        from src.agent import normalize_model_spec

        monkeypatch.delenv("AI_BASE_URL", raising=False)
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-project")
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "europe-west4")
        monkeypatch.setenv("GOOGLE_API_KEY", "AQ.vertex-key")

        model, kwargs = normalize_model_spec("google_vertexai:gemini-2.5-flash")

        assert model == "vertexai/gemini-2.5-flash"
        assert kwargs == {
            "api_key": "AQ.vertex-key",
            "project": "demo-project",
            "location": "europe-west4",
        }

    def test_gateway_mode_warns_when_vertex_google_key_is_present(
        self, monkeypatch, caplog
    ):
        from src.agent import normalize_model_spec

        monkeypatch.setenv("AI_BASE_URL", "https://gateway.example.com")
        monkeypatch.setenv("AI_API_KEY", "gateway-key")
        monkeypatch.setenv("GOOGLE_API_KEY", "AQ.vertex-key")
        monkeypatch.delenv("AI_MODEL", raising=False)

        with caplog.at_level(logging.WARNING):
            model, kwargs = normalize_model_spec("google_genai:gemini-2.5-flash")

        assert model == "openai/vertex_ai.gemini-2.5-flash"
        assert kwargs == {
            "base_url": "https://gateway.example.com",
            "api_key": "gateway-key",
        }
        assert "direct Google Vertex credentials are skipped" in caplog.text

    def test_gateway_mode_warns_when_google_service_account_json_is_present(
        self, monkeypatch, caplog
    ):
        from src.agent import normalize_model_spec

        monkeypatch.setenv("AI_BASE_URL", "https://gateway.example.com")
        monkeypatch.setenv("AI_API_KEY", "gateway-key")
        monkeypatch.delenv("AI_MODEL", raising=False)

        with caplog.at_level(logging.WARNING):
            model, kwargs = normalize_model_spec(
                "google_genai:gemini-2.5-flash",
                api_key=_google_service_account_json(),
            )

        assert model == "openai/vertex_ai.gemini-2.5-flash"
        assert kwargs == {
            "base_url": "https://gateway.example.com",
            "api_key": "gateway-key",
        }
        assert "direct Google Vertex credentials are skipped" in caplog.text

    def test_create_client_uses_from_genai_for_service_account(self, monkeypatch):
        from src.agent import create_client

        captured: dict[str, object] = {}

        monkeypatch.delenv("AI_BASE_URL", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

        def fake_from_service_account_info(info, scopes):
            captured["service_account_info"] = info
            captured["scopes"] = scopes
            return "credentials"

        def fake_genai_client(**kwargs):
            captured["genai_client_kwargs"] = kwargs
            return "raw-client"

        def fake_from_genai(raw_client, use_async=False, **kwargs):
            captured["from_genai"] = {
                "raw_client": raw_client,
                "use_async": use_async,
                "kwargs": kwargs,
            }
            return "wrapped-client"

        monkeypatch.setattr(
            "google.oauth2.service_account.Credentials.from_service_account_info",
            fake_from_service_account_info,
        )
        monkeypatch.setattr("google.genai.Client", fake_genai_client)
        monkeypatch.setattr("src.agent.instructor.from_genai", fake_from_genai)
        monkeypatch.setattr(
            "src.agent.instructor.from_provider",
            lambda *_args, **_kwargs: pytest.fail("from_provider should not be used"),
        )

        client = create_client(
            "google_genai:gemini-2.5-flash",
            api_key=_google_service_account_json(),
        )

        assert client.client == "wrapped-client"
        assert client.model == "gemini-2.5-flash"
        assert captured["scopes"] == ["https://www.googleapis.com/auth/cloud-platform"]
        assert captured["genai_client_kwargs"] == {
            "vertexai": True,
            "credentials": "credentials",
            "project": "service-project",
            "location": "us-central1",
        }
        assert captured["from_genai"] == {
            "raw_client": "raw-client",
            "use_async": False,
            "kwargs": {},
        }

    def test_create_async_client_uses_from_genai_for_service_account(self, monkeypatch):
        from src.agent import create_async_client

        captured: dict[str, object] = {}

        monkeypatch.delenv("AI_BASE_URL", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

        monkeypatch.setattr(
            "google.oauth2.service_account.Credentials.from_service_account_info",
            lambda info, scopes: (
                captured.update({"service_account_info": info, "scopes": scopes})
                or "credentials"
            ),
        )
        monkeypatch.setattr(
            "google.genai.Client",
            lambda **kwargs: (
                captured.update({"genai_client_kwargs": kwargs}) or "raw-client"
            ),
        )
        monkeypatch.setattr(
            "src.agent.instructor.from_genai",
            lambda raw_client, use_async=False, **kwargs: (
                captured.update(
                    {
                        "from_genai": {
                            "raw_client": raw_client,
                            "use_async": use_async,
                            "kwargs": kwargs,
                        }
                    }
                )
                or "wrapped-client"
            ),
        )
        monkeypatch.setattr(
            "src.agent.instructor.from_provider",
            lambda *_args, **_kwargs: pytest.fail("from_provider should not be used"),
        )

        client = create_async_client(
            "google_genai:gemini-2.5-flash",
            api_key=_google_service_account_json(),
        )

        assert client.client == "wrapped-client"
        assert client.model == "gemini-2.5-flash"
        assert captured["from_genai"] == {
            "raw_client": "raw-client",
            "use_async": True,
            "kwargs": {},
        }


def test_model_client_to_async_does_not_store_api_key(monkeypatch):
    from src.agent import ModelClient

    captured = {}

    def fake_create_async_client(model_spec, *, api_key=None):
        captured["model_spec"] = model_spec
        captured["api_key"] = api_key
        return "async-client"

    monkeypatch.setattr("src.agent.create_async_client", fake_create_async_client)

    client = ModelClient(
        client=object(),
        model="gpt-5.4",
        model_spec="openai:gpt-5.4",
    )

    result = client.to_async()

    assert result == "async-client"
    assert captured == {
        "model_spec": "openai:gpt-5.4",
        "api_key": None,
    }
