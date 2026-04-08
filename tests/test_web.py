"""Smoke tests for the FastAPI web app."""

from pathlib import Path

from fastapi.testclient import TestClient

from src.web import Job, _jobs, app

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_download_removes_job_and_run_dir(tmp_path):
    run_dir = Path(tmp_path) / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "hello.txt").write_text("hello", encoding="utf-8")
    job_id = "abc123def456"
    _jobs[job_id] = Job(job_id=job_id, run_dir=run_dir, status="done")

    response = client.get(f"/download/{job_id}")
    assert response.status_code == 200
    assert response.content[:2] == b"PK"  # zip

    assert job_id not in _jobs
    assert not run_dir.exists()
