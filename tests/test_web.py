"""Smoke tests for the FastAPI web app."""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.web import Job, _jobs, app, job_ttl_sweep_once, _notify_job

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


def test_optional_pdf_upload_updates_registry(tmp_path):
    run_dir = Path(tmp_path) / "run"
    sources = run_dir / "sources"
    sources.mkdir(parents=True)
    reg = {"src_a": {"title": "Paper A", "doi": "10.1000/182", "user_provided": False}}
    (sources / "registry.json").write_text(
        json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    jid = "optpdfjob001"
    _jobs[jid] = Job(
        job_id=jid,
        run_dir=run_dir,
        status="optional_pdfs",
        optional_pdf_allowed_ids=frozenset({"src_a"}),
    )
    pdf_bytes = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    files = {"file": ("x.pdf", pdf_bytes, "application/pdf")}
    data = {"source_id": "src_a"}
    r = client.post(f"/optional-pdf/{jid}", data=data, files=files)
    assert r.status_code == 200
    assert r.json().get("status") == "ok"
    updated = json.loads((sources / "registry.json").read_text(encoding="utf-8"))
    assert "content_path" in updated["src_a"]
    assert Path(updated["src_a"]["content_path"]).exists()


def test_optional_pdf_done_requires_active_step():
    jid = "optpdfjob002"
    _jobs[jid] = Job(job_id=jid, run_dir=Path("."), status="running")
    r = client.post(f"/optional-pdf/{jid}/done")
    assert r.status_code == 400


def test_optional_pdf_url_updates_registry(tmp_path, monkeypatch):
    run_dir = Path(tmp_path) / "run"
    sources = run_dir / "sources"
    sources.mkdir(parents=True)
    reg = {"src_a": {"title": "Paper A", "doi": "10.1000/182", "user_provided": False}}
    (sources / "registry.json").write_text(
        json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    jid = "optpdfjob003"
    _jobs[jid] = Job(
        job_id=jid,
        run_dir=run_dir,
        status="optional_pdfs",
        optional_pdf_allowed_ids=frozenset({"src_a"}),
    )
    pdf_bytes = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    mock_resp = MagicMock()
    mock_resp.content = pdf_bytes
    mock_resp.headers = {"content-type": "application/pdf"}

    def _fake_http_get(url: str, **kwargs):
        assert url.startswith("http")
        return mock_resp

    monkeypatch.setattr("src.web.http_get", _fake_http_get)

    data = {"source_id": "src_a", "pdf_url": "https://example.org/paper.pdf"}
    r = client.post(f"/optional-pdf/{jid}", data=data)
    assert r.status_code == 200
    assert r.json().get("status") == "ok"
    updated = json.loads((sources / "registry.json").read_text(encoding="utf-8"))
    assert "content_path" in updated["src_a"]


def test_stream_sse_returns_done_event(tmp_path):
    """SSE endpoint sends the current status as a JSON event and closes on terminal state."""
    jid = "ssejob000001"
    _jobs[jid] = Job(
        job_id=jid, run_dir=Path(tmp_path), status="done", finished_at=time.time()
    )

    with client.stream("GET", f"/stream/{jid}") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        lines = []
        for line in resp.iter_lines():
            lines.append(line)
    # SSE format: "data: {json}"
    data_lines = [l for l in lines if l.startswith("data: ")]
    assert len(data_lines) >= 1
    payload = json.loads(data_lines[0].removeprefix("data: "))
    assert payload["status"] == "done"
    _jobs.pop(jid, None)


def test_stream_sse_gone_for_missing_job():
    """SSE endpoint returns a 'gone' event for unknown job IDs."""
    jid = "ssemissing01"
    _jobs.pop(jid, None)

    with client.stream("GET", f"/stream/{jid}") as resp:
        assert resp.status_code == 200
        lines = list(resp.iter_lines())
    data_lines = [l for l in lines if l.startswith("data: ")]
    assert len(data_lines) == 1
    payload = json.loads(data_lines[0].removeprefix("data: "))
    assert payload["status"] == "gone"


def test_stream_sse_notify_sends_update(tmp_path):
    """_notify_job causes SSE to send a new event when status changes."""
    import threading

    jid = "ssenotify001"
    job = Job(job_id=jid, run_dir=Path(tmp_path), status="running")
    _jobs[jid] = job

    events = []

    def _consume():
        with client.stream("GET", f"/stream/{jid}") as resp:
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line.removeprefix("data: ")))

    t = threading.Thread(target=_consume, daemon=True)
    t.start()

    # Give SSE time to connect and send initial event
    time.sleep(0.3)

    # Transition to done
    job.status = "done"
    job.finished_at = time.time()
    _notify_job(job)

    t.join(timeout=5)
    _jobs.pop(jid, None)

    assert len(events) >= 2
    assert events[0]["status"] == "running"
    assert events[-1]["status"] == "done"
