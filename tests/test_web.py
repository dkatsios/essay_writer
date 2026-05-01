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
    app,
    job_ttl_sweep_once,
)
from src.web_jobs import async_wait_for_job_signal as _async_wait_for_job_signal

client = TestClient(app)


def _write_assignment_brief(run_dir: Path) -> None:
    brief_dir = run_dir / "brief"
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "assignment.json").write_text(
        json.dumps(
            {
                "topic": "Test topic",
                "language": "English",
                "description": "Test description",
            }
        ),
        encoding="utf-8",
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


def test_history_job_detail_returns_summary_steps_artifacts_and_live_status(tmp_path):
    from src.run_history_store import run_history

    run_dir = Path(tmp_path) / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "report.md").write_text("# Report", encoding="utf-8")

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
    run_history.sync_artifacts("jobdetail001", run_dir, current_time=12.0)

    live_job = Job(job_id="jobdetail001", run_dir=run_dir, status="running")
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


def test_history_job_detail_can_filter_unavailable_artifacts(tmp_path):
    from src.run_history_store import run_history

    run_dir = Path(tmp_path) / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "report.md").write_text("# Report", encoding="utf-8")

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
    run_history.sync_artifacts("jobfilter001", run_dir, current_time=12.0)
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


def test_submit_uses_timestamped_run_dir_prefix(tmp_path, monkeypatch):
    captured: dict[str, object] = {}
    created_dir = tmp_path / "essay_created"

    class _Uuid:
        hex = "abcdef1234567890"

    def fake_mkdtemp(*, prefix: str):
        captured["prefix"] = prefix
        created_dir.mkdir()
        return str(created_dir)

    def fake_create_task(coro):
        captured["task_created"] = True
        coro.close()
        return MagicMock()

    monkeypatch.setattr("src.web.uuid.uuid4", lambda: _Uuid())
    monkeypatch.setattr("src.web.tempfile.mkdtemp", fake_mkdtemp)
    monkeypatch.setattr("src.web.asyncio.create_task", fake_create_task)

    response = client.post("/submit", data={"prompt": "Test prompt"})

    assert response.status_code == 200
    assert response.json() == {"job_id": "abcdef123456"}
    assert captured["task_created"] is True
    assert re.fullmatch(r"essay_\d{8}_\d{6}_\d{6}_", str(captured["prefix"]))
    assert "abcdef123456" not in str(captured["prefix"])

    job = _jobs.pop("abcdef123456")
    assert job.run_dir == created_dir


def test_submit_adds_running_job_to_history_before_background_finishes(
    tmp_path, monkeypatch
):
    from src.run_history_store import run_history

    created_dir = tmp_path / "essay_created"

    class _Uuid:
        hex = "feedfacecafe1234"

    def fake_mkdtemp(*, prefix: str):
        created_dir.mkdir()
        return str(created_dir)

    def fake_create_task(coro):
        coro.close()
        return MagicMock()

    monkeypatch.setattr("src.web.uuid.uuid4", lambda: _Uuid())
    monkeypatch.setattr("src.web.tempfile.mkdtemp", fake_mkdtemp)
    monkeypatch.setattr("src.web.asyncio.create_task", fake_create_task)

    response = client.post(
        "/submit",
        data={"prompt": "Test prompt", "target_words": 1200, "provider": "openai"},
    )

    assert response.status_code == 200
    job_id = response.json()["job_id"]

    summary = run_history.get_runtime_summary(job_id)
    history = client.get("/history/jobs").json()["jobs"]

    assert summary is not None
    assert summary["status"] == "running"
    assert summary["provider"] == "openai"
    assert summary["target_words"] == 1200
    assert any(
        item["job_id"] == job_id and item["status"] == "running" for item in history
    )

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


def test_download_keeps_job_until_cleanup(tmp_path):
    from src.run_history_store import run_history

    run_dir = Path(tmp_path) / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "hello.txt").write_text("hello", encoding="utf-8")
    job_id = "abc123def456"
    _jobs[job_id] = Job(job_id=job_id, run_dir=run_dir, status="done")
    run_history.sync_artifacts(job_id, run_dir, current_time=10.0)

    response = client.get(f"/download/{job_id}")
    assert response.status_code == 200
    assert response.content[:2] == b"PK"  # zip

    assert job_id in _jobs
    assert run_dir.exists()

    cleanup = client.post(f"/download/{job_id}/cleanup")
    assert cleanup.status_code == 200

    artifacts = run_history.list_artifacts(job_id)
    assert job_id not in _jobs
    assert not run_dir.exists()
    assert artifacts[0]["is_available"] is False


def test_download_uses_run_dir_name_for_zip_filename(tmp_path):
    run_dir = Path(tmp_path) / "essay_20260430_130501_123456_abcd1234"
    run_dir.mkdir(parents=True)
    (run_dir / "hello.txt").write_text("hello", encoding="utf-8")
    job_id = "518c425779f4"
    _jobs[job_id] = Job(job_id=job_id, run_dir=run_dir, status="done")

    try:
        response = client.get(f"/download/{job_id}")
        assert response.status_code == 200
        assert response.headers["content-disposition"] == (
            "attachment; filename=essay_20260430_130501_123456_abcd1234.zip"
        )
    finally:
        _jobs.pop(job_id, None)


def test_download_includes_full_run_contents(tmp_path):
    run_dir = Path(tmp_path) / "run"
    (run_dir / "sources" / "notes").mkdir(parents=True)
    (run_dir / "sources" / "registry.json").write_text("{}", encoding="utf-8")
    (run_dir / "sources" / "scores.json").write_text("{}", encoding="utf-8")
    (run_dir / "sources" / "notes" / "source_a.json").write_text(
        '{"id": "source_a"}', encoding="utf-8"
    )
    (run_dir / "essay.docx").write_bytes(b"docx")

    job_id = "zipcontents01"
    _jobs[job_id] = Job(job_id=job_id, run_dir=run_dir, status="done")

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


def test_job_ttl_sweep_removes_stale_done(tmp_path, monkeypatch):
    from src.run_history_store import run_history

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
    run_history.sync_artifacts(jid, run_dir, current_time=time.time() - 150)
    assert job_ttl_sweep_once() == 1
    artifacts = run_history.list_artifacts(jid)
    assert jid not in _jobs
    assert not run_dir.exists()
    assert artifacts[0]["is_available"] is False


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


def test_mark_stale_jobs_on_startup_marks_active_jobs_failed(tmp_path):
    from src.web_jobs import mark_stale_jobs_on_startup

    jid = "staleactive01"
    _jobs[jid] = Job(job_id=jid, run_dir=Path(tmp_path), status="running")

    count = mark_stale_jobs_on_startup()

    assert count == 1
    reloaded = _jobs[jid]
    assert reloaded.status == "error"
    assert (
        reloaded.error
        == "Server restarted while this job was active. Please submit it again."
    )
    assert reloaded.finished_at is not None


async def test_wait_for_job_signal_times_out_and_marks_error(tmp_path):
    job = Job(job_id="waittimeout01", run_dir=Path(tmp_path), status="questions")

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


async def test_pipeline_task_respects_interactive_validation_setting(
    tmp_path, monkeypatch
):
    job = Job(job_id="cfgjob000001", run_dir=Path(tmp_path), status="running")
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
        _write_assignment_brief(Path(tmp_path))
        await kwargs["on_questions"]([_Question()], Path(tmp_path))

    monkeypatch.setattr("src.web.run_pipeline", fake_run_pipeline)

    await _run_pipeline_task(job, upload_dir=None, prompt="Test prompt")

    saved = json.loads((Path(tmp_path) / "brief" / "assignment.json").read_text())
    assert callable(captured["on_questions"])
    assert job.questions is None
    assert job.status == "done"
    assert saved["clarifications"] == [
        {"question": "Need clarification?", "answer": "Yes"}
    ]


async def test_pipeline_task_stops_after_question_timeout(tmp_path, monkeypatch):
    job = Job(job_id="timeoutjob001", run_dir=Path(tmp_path), status="running")
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
        await kwargs["on_questions"]([_Question()], Path(tmp_path))
        captured["continued"] = True

    monkeypatch.setattr("src.web.run_pipeline", fake_run_pipeline)

    await _run_pipeline_task(job, upload_dir=None, prompt="Test prompt")

    assert captured["continued"] is False
    assert job.status == "error"
    assert job.error == "Timed out waiting for clarification answers."
    assert job.finished_at is not None


async def test_pipeline_task_stops_after_source_shortfall_timeout(
    tmp_path, monkeypatch
):
    job = Job(job_id="shortfall001", run_dir=Path(tmp_path), status="running")
    captured = {"continued": False}

    config = EssayWriterConfig()

    monkeypatch.setattr("src.web.load_config", lambda: config)
    monkeypatch.setattr("src.web.create_async_client", lambda *args, **kwargs: object())
    monkeypatch.setattr("src.web_jobs.interaction_timeout_seconds", lambda: 0)

    async def fake_run_pipeline(*args, **kwargs):
        await kwargs["on_source_shortfall"](
            Path(tmp_path),
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

    await _run_pipeline_task(job, upload_dir=None, prompt="Test prompt")

    assert captured["continued"] is False
    assert job.status == "error"
    assert job.error == "Timed out waiting for source shortfall decision."
    assert job.finished_at is not None


async def test_pipeline_task_passes_async_worker_without_storing_api_key(
    tmp_path, monkeypatch
):
    job = Job(
        job_id="apikeyjob001",
        run_dir=Path(tmp_path),
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

    await _run_pipeline_task(job, upload_dir=None, prompt="Test prompt")

    assert captured["async_api_key"] == "secret-key"
    assert captured["async_worker"] is async_client
    assert captured["job_api_key"] == ""


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


def test_source_shortfall_decision_requires_active_step():
    jid = "shortfall002"
    _jobs[jid] = Job(job_id=jid, run_dir=Path("."), status="running")
    r = client.post(f"/source-shortfall/{jid}", data={"decision": "proceed"})
    assert r.status_code == 400


def test_source_shortfall_decision_unblocks_job(tmp_path):
    jid = "shortfall003"
    _jobs[jid] = Job(
        job_id=jid,
        run_dir=Path(tmp_path),
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


def test_source_shortfall_decision_with_added_ids(tmp_path):
    jid = "shortfall004"
    _jobs[jid] = Job(
        job_id=jid,
        run_dir=Path(tmp_path),
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


def test_source_shortfall_cancel_ignores_added_ids(tmp_path):
    jid = "shortfall005"
    _jobs[jid] = Job(
        job_id=jid,
        run_dir=Path(tmp_path),
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

    def _fake_pdf_get(url: str, **kwargs):
        assert url.startswith("http")
        return mock_resp

    monkeypatch.setattr("src.web_jobs.pdf_get", _fake_pdf_get)

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
