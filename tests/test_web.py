"""Smoke tests for the FastAPI web app."""

import time
from pathlib import Path

from fastapi.testclient import TestClient

from src.web import Job, _jobs, app, job_ttl_sweep_once

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


def test_job_ttl_sweep_removes_stale_done(tmp_path, monkeypatch):
    monkeypatch.setenv("ESSAY_WEB_JOB_TTL_SECONDS", "120")
    run_dir = Path(tmp_path) / "ttl_run"
    run_dir.mkdir()
    (run_dir / "f.txt").write_text("x", encoding="utf-8")
    jid = "ttltestjob01"
    _jobs[jid] = Job(
        job_id=jid,
        run_dir=run_dir,
        status="done",
        finished_at=time.time() - 200,
    )
    assert job_ttl_sweep_once() == 1
    assert jid not in _jobs
    assert not run_dir.exists()


def test_job_ttl_sweep_keeps_recent_done(tmp_path, monkeypatch):
    monkeypatch.setenv("ESSAY_WEB_JOB_TTL_SECONDS", "3600")
    run_dir = Path(tmp_path) / "fresh"
    run_dir.mkdir()
    jid = "ttltestjob02"
    _jobs[jid] = Job(
        job_id=jid,
        run_dir=run_dir,
        status="done",
        finished_at=time.time() - 10,
    )
    assert job_ttl_sweep_once() == 0
    assert jid in _jobs
    assert run_dir.exists()


def test_job_ttl_zero_disables_sweep(tmp_path, monkeypatch):
    monkeypatch.setenv("ESSAY_WEB_JOB_TTL_SECONDS", "0")
    run_dir = Path(tmp_path) / "stale"
    run_dir.mkdir()
    jid = "ttltestjob03"
    _jobs[jid] = Job(
        job_id=jid,
        run_dir=run_dir,
        status="done",
        finished_at=time.time() - 999_999,
    )
    assert job_ttl_sweep_once() == 0
    assert jid in _jobs


def test_status_404_when_job_missing():
    r = client.get("/status/nonexistentjob")
    assert r.status_code == 404
