"""Smoke tests for the FastAPI web app."""

import asyncio
import json
import logging
import re
import time
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from config.settings import EssayWriterConfig
from src.web import (
    Job,
    _jobs,
    _notify_job,
    _run_pipeline_task,
    _save_job,
    app,
    job_ttl_sweep_once,
)
from src.web_jobs import async_wait_for_job_signal as _async_wait_for_job_signal

client = TestClient(app)


def _write_assignment_brief(storage) -> None:
    storage.write_text(
        "brief/assignment.json",
        json.dumps(
            {
                "topic": "Test topic",
                "language": "English",
                "description": "Test description",
            }
        ),
    )


def _essay_console_handlers() -> list[logging.Handler]:
    return [
        handler
        for handler in logging.getLogger("src").handlers
        if getattr(handler, "_essay_web_console_handler", False)
    ]


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_index_links_to_history_page():
    response = client.get("/")

    assert response.status_code == 200
    assert 'href="/history"' in response.text


def test_history_page_renders():
    response = client.get("/history")

    assert response.status_code == 200
    assert "Run History" in response.text
    assert "Loading persisted run summaries" in response.text


def test_history_jobs_lists_runtime_summaries_in_updated_order():
    from src.run_history_store import run_history

    run_history.save_runtime_summary(
        "jobolder001",
        status="done",
        provider="google",
        total_cost_usd=1.0,
        total_input_tokens=100,
        total_output_tokens=50,
        total_thinking_tokens=0,
        total_duration_seconds=10.0,
        step_count=3,
        updated_at=10.0,
    )
    run_history.save_runtime_summary(
        "jobnewer001",
        status="error",
        provider="openai",
        total_cost_usd=2.0,
        total_input_tokens=200,
        total_output_tokens=75,
        total_thinking_tokens=10,
        total_duration_seconds=20.0,
        step_count=4,
        updated_at=20.0,
    )

    response = client.get("/history/jobs", params={"limit": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert [job["job_id"] for job in body["jobs"]] == [
        "jobnewer001",
        "jobolder001",
    ]


def test_history_jobs_can_filter_by_status():
    from src.run_history_store import run_history

    run_history.save_runtime_summary(
        "jobdone001",
        status="done",
        provider="google",
        total_cost_usd=1.0,
        total_input_tokens=100,
        total_output_tokens=50,
        total_thinking_tokens=0,
        total_duration_seconds=10.0,
        step_count=3,
        updated_at=10.0,
    )
    run_history.save_runtime_summary(
        "joberror001",
        status="error",
        provider="openai",
        total_cost_usd=2.0,
        total_input_tokens=200,
        total_output_tokens=75,
        total_thinking_tokens=10,
        total_duration_seconds=20.0,
        step_count=4,
        updated_at=20.0,
    )

    response = client.get("/history/jobs", params={"status": "done"})

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["jobs"][0]["job_id"] == "jobdone001"


def test_history_job_detail_returns_summary_steps_artifacts_and_live_status():
    from src.run_history_store import run_history
    from src.storage import MemoryRunStorage

    storage = MemoryRunStorage("runs/jobdetail001/")
    storage.write_text("report.md", "# Report")

    run_history.save_runtime_summary(
        "jobdetail001",
        status="done",
        provider="google",
        total_cost_usd=1.5,
        total_input_tokens=300,
        total_output_tokens=120,
        total_thinking_tokens=15,
        total_duration_seconds=30.0,
        step_count=5,
        updated_at=10.0,
    )
    run_history.save_step_metric(
        "jobdetail001",
        "plan",
        status="completed",
        model="gpt-5.4",
        cost_usd=0.25,
        call_count=1,
        input_tokens=120,
        output_tokens=60,
        thinking_tokens=5,
        duration_seconds=3.5,
        step_index=2,
        step_count=7,
        updated_at=11.0,
    )
    run_history.sync_artifacts("jobdetail001", storage, current_time=12.0)

    live_job = Job(job_id="jobdetail001", run_dir="runs/jobdetail001", status="running")
    _jobs["jobdetail001"] = live_job

    try:
        response = client.get("/history/jobs/jobdetail001")
    finally:
        _jobs.pop("jobdetail001", None)

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == "jobdetail001"
    assert body["summary"]["provider"] == "google"
    assert body["steps"][0]["step_name"] == "plan"
    assert body["artifacts"][0]["relative_path"] == "report.md"
    assert body["live_status"]["status"] == "running"


def test_history_job_detail_can_filter_unavailable_artifacts():
    from src.run_history_store import run_history
    from src.storage import MemoryRunStorage

    storage = MemoryRunStorage("runs/jobfilter001/")
    storage.write_text("report.md", "# Report")

    run_history.save_runtime_summary(
        "jobfilter001",
        status="done",
        provider="google",
        total_cost_usd=1.5,
        total_input_tokens=300,
        total_output_tokens=120,
        total_thinking_tokens=15,
        total_duration_seconds=30.0,
        step_count=5,
        updated_at=10.0,
    )
    run_history.sync_artifacts("jobfilter001", storage, current_time=12.0)
    run_history.mark_artifacts_deleted("jobfilter001", current_time=13.0)

    response = client.get(
        "/history/jobs/jobfilter001",
        params={"available_artifacts_only": "true"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["artifacts"] == []


def test_history_job_detail_404_when_job_is_unknown():
    response = client.get("/history/jobs/missingjob001")

    assert response.status_code == 404
    assert response.json() == {"error": "Job not found"}


def test_submit_creates_storage_and_queues_job(monkeypatch):
    from src.storage import MemoryRunStorage

    created_storages: list[MemoryRunStorage] = []

    class _Uuid:
        hex = "abcdef1234567890"

    def fake_create(job_id, config=None):
        s = MemoryRunStorage(f"runs/{job_id}/")
        created_storages.append(s)
        return s

    monkeypatch.setattr("src.web.uuid.uuid4", lambda: _Uuid())
    monkeypatch.setattr("src.web.create_run_storage", fake_create)

    response = client.post("/submit", data={"prompt": "Test prompt"})

    assert response.status_code == 200
    assert response.json() == {"job_id": "abcdef123456"}
    assert len(created_storages) == 1

    job = _jobs.pop("abcdef123456")
    assert job.run_dir == "runs/abcdef123456/"
    assert job.status == "pending"


def test_submit_adds_pending_job_to_history_before_worker_claims_it(monkeypatch):
    from src.run_history_store import run_history
    from src.storage import MemoryRunStorage

    class _Uuid:
        hex = "feedfacecafe1234"

    def fake_create(job_id, config=None):
        return MemoryRunStorage(f"runs/{job_id}/")

    monkeypatch.setattr("src.web.uuid.uuid4", lambda: _Uuid())
    monkeypatch.setattr("src.web.create_run_storage", fake_create)

    response = client.post(
        "/submit",
        data={"prompt": "Test prompt", "target_words": 1200, "provider": "openai"},
    )

    assert response.status_code == 200
    job_id = response.json()["job_id"]

    summary = run_history.get_runtime_summary(job_id)
    history = client.get("/history/jobs").json()["jobs"]

    assert summary is not None
    assert summary["status"] == "pending"
    assert summary["provider"] == "openai"
    assert summary["target_words"] == 1200
    assert any(
        item["job_id"] == job_id and item["status"] == "pending" for item in history
    )

    _jobs.pop(job_id, None)


def test_submit_tracks_uploaded_artifacts_before_background_finishes(monkeypatch):
    from src.run_history_store import run_history
    from src.storage import MemoryRunStorage

    created_storages: list[MemoryRunStorage] = []

    class _Uuid:
        hex = "1234567890abcdef"

    def fake_create(job_id, config=None):
        s = MemoryRunStorage(f"runs/{job_id}/")
        created_storages.append(s)
        return s

    monkeypatch.setattr("src.web.uuid.uuid4", lambda: _Uuid())
    monkeypatch.setattr("src.web.create_run_storage", fake_create)

    response = client.post(
        "/submit",
        data={"prompt": "Test prompt"},
        files=[
            ("files", ("assignment.txt", b"assignment body", "text/plain")),
            ("sources", ("paper.txt", b"paper body", "text/plain")),
        ],
    )

    assert response.status_code == 200
    job_id = response.json()["job_id"]

    artifacts = {
        item["relative_path"]: item for item in run_history.list_artifacts(job_id)
    }

    assert "uploads/assignment.txt" in artifacts
    assert "user_sources/000_paper.txt" in artifacts
    assert artifacts["uploads/assignment.txt"]["is_available"] is True
    assert artifacts["user_sources/000_paper.txt"]["is_available"] is True

    _jobs.pop(job_id, None)


def test_web_logging_bootstrap_is_idempotent():
    from src.run_logging import configure_web_logging

    configure_web_logging()
    configure_web_logging()

    assert len(_essay_console_handlers()) == 1


def test_run_logging_captures_root_warnings_and_skips_access_log(tmp_path):
    from src.run_logging import run_id_context, setup_run_logging, teardown_run_logging

    run_dir = Path(tmp_path)
    with run_id_context("job123"):
        handler = setup_run_logging(run_dir, "job123")
        try:
            logging.getLogger("src.test").info("pipeline step visible")
            logging.getLogger("openai").warning("provider warning visible")
            logging.getLogger("uvicorn.access").info("GET /submit 200")
        finally:
            teardown_run_logging(handler)

    content = (run_dir / "run.log").read_text(encoding="utf-8")
    assert "pipeline step visible" in content
    assert "provider warning visible" in content
    assert "GET /submit 200" not in content


def test_download_keeps_job_until_cleanup(monkeypatch):
    from src.run_history_store import run_history
    from src.storage import MemoryRunStorage

    storage = MemoryRunStorage("runs/abc123def456/")
    storage.write_text("hello.txt", "hello")
    job_id = "abc123def456"

    monkeypatch.setattr(
        "src.web_jobs.create_run_storage", lambda jid, config=None: storage
    )

    _jobs[job_id] = Job(job_id=job_id, run_dir="runs/abc123def456", status="done")
    run_history.sync_artifacts(job_id, storage, current_time=10.0)

    response = client.get(f"/download/{job_id}")
    assert response.status_code == 200
    assert response.content[:2] == b"PK"  # zip

    assert job_id in _jobs

    cleanup = client.post(f"/download/{job_id}/cleanup")
    assert cleanup.status_code == 200

    artifacts = run_history.list_artifacts(job_id)
    assert job_id not in _jobs
    assert artifacts[0]["is_available"] is False


def test_download_uses_run_dir_name_for_zip_filename(monkeypatch):
    from src.storage import MemoryRunStorage

    storage = MemoryRunStorage("runs/essay_20260430_130501_123456_abcd1234/")
    storage.write_text("hello.txt", "hello")
    job_id = "518c425779f4"

    monkeypatch.setattr(
        "src.web_jobs.create_run_storage", lambda jid, config=None: storage
    )

    _jobs[job_id] = Job(
        job_id=job_id,
        run_dir="runs/essay_20260430_130501_123456_abcd1234",
        status="done",
    )

    try:
        response = client.get(f"/download/{job_id}")
        assert response.status_code == 200
        assert response.headers["content-disposition"] == (
            "attachment; filename=essay_20260430_130501_123456_abcd1234.zip"
        )
    finally:
        _jobs.pop(job_id, None)


def test_download_includes_full_run_contents(monkeypatch):
    from src.storage import MemoryRunStorage

    storage = MemoryRunStorage("runs/zipcontents01/")
    storage.write_text("sources/registry.json", "{}")
    storage.write_text("sources/scores.json", "{}")
    storage.write_text("sources/notes/source_a.json", '{"id": "source_a"}')
    storage.write_bytes("essay.docx", b"docx")

    monkeypatch.setattr(
        "src.web_jobs.create_run_storage", lambda jid, config=None: storage
    )

    job_id = "zipcontents01"
    _jobs[job_id] = Job(job_id=job_id, run_dir="runs/zipcontents01", status="done")

    try:
        response = client.get(f"/download/{job_id}")
        assert response.status_code == 200

        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            names = set(archive.namelist())

        assert "essay.docx" in names
        assert "sources/registry.json" in names
        assert "sources/scores.json" in names
        assert "sources/notes/source_a.json" in names
    finally:
        _jobs.pop(job_id, None)


def test_job_ttl_sweep_removes_stale_done(monkeypatch):
    from src.run_history_store import run_history
    from src.storage import MemoryRunStorage

    monkeypatch.setenv("ESSAY_WEB_JOB_TTL_SECONDS", "120")
    storage = MemoryRunStorage("runs/ttltestjob01/")
    storage.write_text("f.txt", "x")

    monkeypatch.setattr(
        "src.web_jobs.create_run_storage", lambda jid, config=None: storage
    )

    jid = "ttltestjob01"
    _jobs[jid] = Job(
        job_id=jid,
        run_dir="runs/ttltestjob01",
        status="done",
        finished_at=time.time() - 200,
    )
    run_history.sync_artifacts(jid, storage, current_time=time.time() - 150)
    assert job_ttl_sweep_once() == 1
    artifacts = run_history.list_artifacts(jid)
    assert jid not in _jobs
    assert artifacts[0]["is_available"] is False


def test_job_ttl_sweep_keeps_recent_done(monkeypatch):
    monkeypatch.setenv("ESSAY_WEB_JOB_TTL_SECONDS", "3600")
    jid = "ttltestjob02"
    _jobs[jid] = Job(
        job_id=jid,
        run_dir="runs/ttltestjob02",
        status="done",
        finished_at=time.time() - 10,
    )
    assert job_ttl_sweep_once() == 0
    assert jid in _jobs


def test_job_ttl_zero_disables_sweep(monkeypatch):
    monkeypatch.setenv("ESSAY_WEB_JOB_TTL_SECONDS", "0")
    jid = "ttltestjob03"
    _jobs[jid] = Job(
        job_id=jid,
        run_dir="runs/ttltestjob03",
        status="done",
        finished_at=time.time() - 999_999,
    )
    assert job_ttl_sweep_once() == 0
    assert jid in _jobs


def test_mark_stale_jobs_on_startup_marks_active_jobs_failed():
    from src.web_jobs import mark_stale_jobs_on_startup

    jid = "staleactive01"
    _jobs[jid] = Job(job_id=jid, run_dir="runs/test", status="running")

    count = mark_stale_jobs_on_startup()

    assert count == 1
    reloaded = _jobs[jid]
    assert reloaded.status == "error"
    assert (
        reloaded.error
        == "Server restarted while this job was active. Please submit it again."
    )
    assert reloaded.finished_at is not None


async def test_wait_for_job_signal_times_out_and_marks_error():
    job = Job(job_id="waittimeout01", run_dir="runs/test", status="questions")

    ok = await _async_wait_for_job_signal(
        job,
        asyncio.Event(),
        error_message="Timed out waiting for clarification answers.",
        timeout=0,
    )

    assert ok is False
    assert job.status == "error"
    assert job.error == "Timed out waiting for clarification answers."
    assert job.finished_at is not None


async def test_pipeline_task_respects_interactive_validation_setting(monkeypatch):
    from src.storage import MemoryRunStorage

    storage = MemoryRunStorage("runs/cfgjob000001/")
    monkeypatch.setattr(
        "src.web_jobs.create_run_storage", lambda jid, config=None: storage
    )

    job = Job(job_id="cfgjob000001", run_dir="runs/cfgjob000001", status="running")
    captured = {}

    config = EssayWriterConfig()
    config.writing.interactive_validation = False

    monkeypatch.setattr("src.web.load_config", lambda: config)
    monkeypatch.setattr("src.web.create_async_client", lambda *args, **kwargs: object())

    class _Question:
        question = "Need clarification?"
        options = ["Yes", "No"]
        suggested_option_index = 0

    async def fake_run_pipeline(*args, **kwargs):
        captured.update(kwargs)
        _write_assignment_brief(storage)
        await kwargs["on_questions"]([_Question()], storage)

    monkeypatch.setattr("src.web.run_pipeline", fake_run_pipeline)

    await _run_pipeline_task(job, has_uploads=False, prompt="Test prompt")

    saved = json.loads(storage.read_text("brief/assignment.json"))
    assert callable(captured["on_questions"])
    assert job.questions is None
    assert job.status == "done"
    assert saved["clarifications"] == [
        {"question": "Need clarification?", "answer": "Yes"}
    ]


async def test_pipeline_task_stops_after_question_timeout(monkeypatch):
    from src.storage import MemoryRunStorage

    storage = MemoryRunStorage("runs/timeoutjob001/")
    monkeypatch.setattr(
        "src.web_jobs.create_run_storage", lambda jid, config=None: storage
    )

    job = Job(job_id="timeoutjob001", run_dir="runs/timeoutjob001", status="running")
    captured = {"continued": False}

    config = EssayWriterConfig()
    config.writing.interactive_validation = True

    monkeypatch.setattr("src.web.load_config", lambda: config)
    monkeypatch.setattr("src.web.create_async_client", lambda *args, **kwargs: object())
    monkeypatch.setattr("src.web_jobs.interaction_timeout_seconds", lambda: 0)

    class _Question:
        question = "Need clarification?"
        options = ["Yes", "No"]
        suggested_option_index = 0

    async def fake_run_pipeline(*args, **kwargs):
        await kwargs["on_questions"]([_Question()], storage)
        captured["continued"] = True

    monkeypatch.setattr("src.web.run_pipeline", fake_run_pipeline)

    await _run_pipeline_task(job, has_uploads=False, prompt="Test prompt")

    assert captured["continued"] is False
    assert job.status == "error"
    assert job.error == "Timed out waiting for clarification answers."
    assert job.finished_at is not None


async def test_pipeline_task_stops_after_source_shortfall_timeout(monkeypatch):
    from src.storage import MemoryRunStorage

    storage = MemoryRunStorage("runs/shortfall001/")
    monkeypatch.setattr(
        "src.web_jobs.create_run_storage", lambda jid, config=None: storage
    )

    job = Job(job_id="shortfall001", run_dir="runs/shortfall001", status="running")
    captured = {"continued": False}

    config = EssayWriterConfig()

    monkeypatch.setattr("src.web.load_config", lambda: config)
    monkeypatch.setattr("src.web.create_async_client", lambda *args, **kwargs: object())
    monkeypatch.setattr("src.web_jobs.interaction_timeout_seconds", lambda: 0)

    async def fake_run_pipeline(*args, **kwargs):
        await kwargs["on_source_shortfall"](
            storage,
            {
                "usable_sources": 8,
                "target_sources": 12,
                "scorable_candidates": 10,
                "above_threshold": 9,
                "total_candidates": 18,
                "recovery_attempted": True,
            },
        )
        captured["continued"] = True

    monkeypatch.setattr("src.web.run_pipeline", fake_run_pipeline)

    await _run_pipeline_task(job, has_uploads=False, prompt="Test prompt")

    assert captured["continued"] is False
    assert job.status == "error"
    assert job.error == "Timed out waiting for source shortfall decision."
    assert job.finished_at is not None


async def test_pipeline_task_syncs_extracted_input_before_pipeline_runs(monkeypatch):
    from src.run_history_store import run_history
    from src.storage import MemoryRunStorage

    storage = MemoryRunStorage("runs/extractedsync001/")
    monkeypatch.setattr(
        "src.web_jobs.create_run_storage", lambda jid, config=None: storage
    )

    job = Job(
        job_id="extractedsync001",
        run_dir="runs/extractedsync001",
        status="running",
        target_words=1200,
    )
    captured: dict[str, object] = {}

    config = EssayWriterConfig()

    monkeypatch.setattr("src.web.load_config", lambda: config)
    monkeypatch.setattr("src.web.create_async_client", lambda *args, **kwargs: object())

    async def fake_run_pipeline(*args, **kwargs):
        captured["paths"] = {
            item["relative_path"] for item in run_history.list_artifacts(job.job_id)
        }
        captured["summary"] = run_history.get_runtime_summary(job.job_id)

    monkeypatch.setattr("src.web.run_pipeline", fake_run_pipeline)

    await _run_pipeline_task(job, has_uploads=False, prompt="Test prompt")

    assert "input/extracted.md" in captured["paths"]
    assert captured["summary"] is not None
    assert captured["summary"]["target_words"] == 1200


async def test_pipeline_task_syncs_selected_sources_before_source_shortfall_wait(
    monkeypatch,
):
    from src.run_history_store import run_history
    from src.storage import MemoryRunStorage

    storage = MemoryRunStorage("runs/shortfallart001/")
    monkeypatch.setattr(
        "src.web_jobs.create_run_storage", lambda jid, config=None: storage
    )

    job = Job(
        job_id="shortfallart001", run_dir="runs/shortfallart001", status="running"
    )
    captured: dict[str, object] = {}

    config = EssayWriterConfig()

    monkeypatch.setattr("src.web.load_config", lambda: config)
    monkeypatch.setattr("src.web.create_async_client", lambda *args, **kwargs: object())

    async def fake_wait(current_job, event, **kwargs):
        captured["status"] = current_job.status
        captured["paths"] = {
            item["relative_path"]
            for item in run_history.list_artifacts(current_job.job_id)
        }
        return False

    monkeypatch.setattr("src.web_jobs.async_wait_for_job_signal", fake_wait)

    async def fake_run_pipeline(*args, **kwargs):
        storage.write_text(
            "sources/selected.json",
            json.dumps({"s1": {"title": "Paper A"}}, ensure_ascii=False, indent=2),
        )
        await kwargs["on_source_shortfall"](
            storage,
            {
                "usable_sources": 1,
                "target_sources": 2,
                "scorable_candidates": 4,
                "above_threshold": 2,
                "total_candidates": 5,
                "recovery_attempted": True,
            },
        )

    monkeypatch.setattr("src.web.run_pipeline", fake_run_pipeline)

    await _run_pipeline_task(job, has_uploads=False, prompt="Test prompt")

    assert captured["status"] == "source_shortfall"
    assert "sources/selected.json" in captured["paths"]
    assert job.status == "source_shortfall"


async def test_pipeline_task_passes_async_worker_without_storing_api_key(
    monkeypatch,
):
    from src.storage import MemoryRunStorage

    storage = MemoryRunStorage("runs/apikeyjob001/")
    monkeypatch.setattr(
        "src.web_jobs.create_run_storage", lambda jid, config=None: storage
    )

    job = Job(
        job_id="apikeyjob001",
        run_dir="runs/apikeyjob001",
        status="running",
        api_key="secret-key",
    )
    captured = {}

    config = EssayWriterConfig()

    async_client = object()

    monkeypatch.setattr("src.web.load_config", lambda: config)

    def fake_create_async_client(*args, **kwargs):
        captured["async_api_key"] = kwargs.get("api_key")
        return async_client

    monkeypatch.setattr("src.web.create_async_client", fake_create_async_client)

    async def fake_run_pipeline(*args, **kwargs):
        captured["async_worker"] = kwargs.get("async_worker")
        captured["job_api_key"] = job.api_key

    monkeypatch.setattr("src.web.run_pipeline", fake_run_pipeline)

    await _run_pipeline_task(job, has_uploads=False, prompt="Test prompt")

    assert captured["async_api_key"] == "secret-key"
    assert captured["async_worker"] is async_client
    assert captured["job_api_key"] == ""


def test_optional_pdf_upload_updates_registry(monkeypatch):
    from src.storage import MemoryRunStorage

    storage = MemoryRunStorage("runs/optpdfjob001/")
    reg = {"src_a": {"title": "Paper A", "doi": "10.1000/182", "user_provided": False}}
    storage.write_text(
        "sources/registry.json",
        json.dumps(reg, ensure_ascii=False, indent=2),
    )
    monkeypatch.setattr(
        "src.web_jobs.create_run_storage", lambda jid, config=None: storage
    )
    jid = "optpdfjob001"
    _jobs[jid] = Job(
        job_id=jid,
        run_dir="runs/optpdfjob001",
        status="optional_pdfs",
        optional_pdf_allowed_ids=frozenset({"src_a"}),
    )
    pdf_bytes = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    files = {"file": ("x.pdf", pdf_bytes, "application/pdf")}
    data = {"source_id": "src_a"}
    r = client.post(f"/optional-pdf/{jid}", data=data, files=files)
    assert r.status_code == 200
    assert r.json().get("status") == "ok"
    updated = json.loads(storage.read_text("sources/registry.json"))
    assert "content_path" in updated["src_a"]
    assert storage.exists(updated["src_a"]["content_path"])


def test_optional_pdf_done_requires_active_step():
    jid = "optpdfjob002"
    _jobs[jid] = Job(job_id=jid, run_dir="runs/test", status="running")
    r = client.post(f"/optional-pdf/{jid}/done")
    assert r.status_code == 400


def test_source_shortfall_decision_requires_active_step():
    jid = "shortfall002"
    _jobs[jid] = Job(job_id=jid, run_dir="runs/test", status="running")
    r = client.post(f"/source-shortfall/{jid}", data={"decision": "proceed"})
    assert r.status_code == 400


def test_source_shortfall_decision_unblocks_job():
    jid = "shortfall003"
    _jobs[jid] = Job(
        job_id=jid,
        run_dir="runs/shortfall003",
        status="source_shortfall",
        source_shortfall={
            "usable_sources": 8,
            "target_sources": 12,
        },
    )
    try:
        r = client.post(f"/source-shortfall/{jid}", data={"decision": "proceed"})
        assert r.status_code == 200
        assert r.json()["decision"] == "proceed"
        assert _jobs[jid].source_shortfall_decision == "proceed"
        assert _jobs[jid].status == "running"
    finally:
        _jobs.pop(jid, None)


def test_source_shortfall_decision_with_added_ids():
    jid = "shortfall004"
    _jobs[jid] = Job(
        job_id=jid,
        run_dir="runs/shortfall004",
        status="source_shortfall",
        source_shortfall={
            "usable_sources": 8,
            "target_sources": 12,
            "borderline_sources": [
                {"source_id": "smith2020", "title": "Paper A"},
                {"source_id": "jones2021", "title": "Paper B"},
            ],
        },
    )
    try:
        added = json.dumps(["smith2020", "jones2021"])
        r = client.post(
            f"/source-shortfall/{jid}",
            data={"decision": "proceed", "added_ids": added},
        )
        assert r.status_code == 200
        assert r.json()["decision"] == "proceed"
        assert r.json()["added_count"] == 2
        assert _jobs[jid].source_shortfall_added_ids == ["smith2020", "jones2021"]
        assert _jobs[jid].status == "running"
    finally:
        _jobs.pop(jid, None)


def test_source_shortfall_cancel_ignores_added_ids():
    jid = "shortfall005"
    _jobs[jid] = Job(
        job_id=jid,
        run_dir="runs/shortfall005",
        status="source_shortfall",
        source_shortfall={"usable_sources": 5, "target_sources": 10},
    )
    try:
        added = json.dumps(["smith2020"])
        r = client.post(
            f"/source-shortfall/{jid}",
            data={"decision": "cancel", "added_ids": added},
        )
        assert r.status_code == 200
        assert r.json()["decision"] == "cancel"
        assert r.json()["added_count"] == 0
        assert _jobs[jid].source_shortfall_added_ids == []
    finally:
        _jobs.pop(jid, None)


def test_optional_pdf_url_updates_registry(monkeypatch):
    from src.storage import MemoryRunStorage

    storage = MemoryRunStorage("runs/optpdfjob003/")
    reg = {"src_a": {"title": "Paper A", "doi": "10.1000/182", "user_provided": False}}
    storage.write_text(
        "sources/registry.json",
        json.dumps(reg, ensure_ascii=False, indent=2),
    )
    monkeypatch.setattr(
        "src.web_jobs.create_run_storage", lambda jid, config=None: storage
    )
    jid = "optpdfjob003"
    _jobs[jid] = Job(
        job_id=jid,
        run_dir="runs/optpdfjob003",
        status="optional_pdfs",
        optional_pdf_allowed_ids=frozenset({"src_a"}),
    )
    pdf_bytes = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    mock_resp = MagicMock()
    mock_resp.content = pdf_bytes
    mock_resp.headers = {"content-type": "application/pdf"}

    def _fake_pdf_get(url: str, **kwargs):
        assert url.startswith("http")
        return mock_resp

    monkeypatch.setattr("src.web_jobs.pdf_get", _fake_pdf_get)

    data = {"source_id": "src_a", "pdf_url": "https://example.org/paper.pdf"}
    r = client.post(f"/optional-pdf/{jid}", data=data)
    assert r.status_code == 200
    assert r.json().get("status") == "ok"
    updated = json.loads(storage.read_text("sources/registry.json"))
    assert "content_path" in updated["src_a"]


def test_stream_sse_returns_done_event():
    """SSE endpoint sends the current status as a JSON event and closes on terminal state."""
    jid = "ssejob000001"
    _jobs[jid] = Job(
        job_id=jid, run_dir="runs/ssejob000001", status="done", finished_at=time.time()
    )

    with client.stream("GET", f"/stream/{jid}") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        lines = []
        for line in resp.iter_lines():
            lines.append(line)
    # SSE format: "data: {json}"
    data_lines = [line for line in lines if line.startswith("data: ")]
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
    data_lines = [line for line in lines if line.startswith("data: ")]
    assert len(data_lines) == 1
    payload = json.loads(data_lines[0].removeprefix("data: "))
    assert payload["status"] == "gone"


def test_stream_sse_notify_sends_update():
    """_notify_job causes SSE to send a new event when status changes."""
    import threading

    jid = "ssenotify001"
    job = Job(job_id=jid, run_dir="runs/ssenotify001", status="running")
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
    _save_job(job)
    _notify_job(job)

    t.join(timeout=5)
    _jobs.pop(jid, None)

    assert len(events) >= 2
    assert events[0]["status"] == "running"
    assert events[-1]["status"] == "done"


def test_json_formatter_produces_valid_json_with_run_id():
    """JsonFormatter emits parseable JSON with expected fields and run_id."""
    from src.run_logging import JsonFormatter, run_id_context

    formatter = JsonFormatter()
    logger_name = "src.test_json_fmt"
    test_logger = logging.getLogger(logger_name)
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    record = test_logger.makeRecord(
        logger_name, logging.INFO, "test.py", 1, "hello %s", ("world",), None
    )
    line = formatter.format(record)
    parsed = json.loads(line)

    assert parsed["level"] == "INFO"
    assert parsed["logger"] == logger_name
    assert parsed["message"] == "hello world"
    assert parsed["run_id"] is None
    assert "timestamp" in parsed

    with run_id_context("job_abc123"):
        record2 = test_logger.makeRecord(
            logger_name, logging.WARNING, "test.py", 2, "step done", (), None
        )
        line2 = formatter.format(record2)
    parsed2 = json.loads(line2)
    assert parsed2["run_id"] == "job_abc123"
    assert parsed2["level"] == "WARNING"


def test_json_formatter_captures_exception():
    """JsonFormatter includes exc_info when an exception is logged."""
    from src.run_logging import JsonFormatter

    formatter = JsonFormatter()
    test_logger = logging.getLogger("src.test_json_exc")

    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        exc_info = sys.exc_info()

    record = test_logger.makeRecord(
        "src.test_json_exc",
        logging.ERROR,
        "test.py",
        1,
        "something failed",
        (),
        exc_info,
    )
    line = formatter.format(record)
    parsed = json.loads(line)

    assert parsed["level"] == "ERROR"
    assert "exc_info" in parsed
    assert "ValueError: boom" in parsed["exc_info"]
