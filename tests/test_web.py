"""Smoke tests for the FastAPI web app."""

from fastapi.testclient import TestClient

from src.web import app

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
