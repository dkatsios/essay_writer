"""Web job UI history helpers (clarification / optional PDF replay after reload)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from src.web import (
    Job,
    _append_clarification_round_for_ui,
    _append_optional_pdf_round_for_ui,
    _build_status_payload,
    _jobs,
)


def test_append_clarification_round_resolves_letter_to_option_text() -> None:
    job = Job(job_id="abc123456789", run_dir=Path("/tmp"))
    job.questions = [
        {"question": "Pick one?", "options": ["Apple", "Banana"]},
    ]
    _append_clarification_round_for_ui(job, "1. a")
    assert len(job.clarification_rounds) == 1
    assert job.clarification_rounds[0]["items"][0]["question"] == "Pick one?"
    assert job.clarification_rounds[0]["items"][0]["answer"] == "Apple"


def test_append_clarification_round_skip_shows_em_dash() -> None:
    job = Job(job_id="abc123456789", run_dir=Path("/tmp"))
    job.questions = [{"question": "Q?", "options": ["x"]}]
    _append_clarification_round_for_ui(job, "")
    assert job.clarification_rounds[0]["items"][0]["answer"] == "—"


def test_status_payload_includes_submit_snapshot() -> None:
    jid = "deadbeef0001"
    job = Job(
        job_id=jid,
        run_dir=Path("/tmp"),
        academic_level="undergraduate",
        submit_prompt="Topic line",
        target_words=3000,
        min_sources=5,
    )
    _jobs[jid] = job
    try:
        body = _build_status_payload(job)
        assert body["submit"]["academic_level"] == "undergraduate"
        assert body["submit"]["prompt"] == "Topic line"
        assert body["submit"]["target_words"] == 3000
        assert body["submit"]["min_sources"] == 5
    finally:
        _jobs.pop(jid, None)


def test_append_optional_pdf_round_from_choices() -> None:
    job = Job(job_id="abc123456789", run_dir=Path("/tmp"))
    job.optional_pdf_items = [
        {"source_id": "s1", "title": "Paper A"},
        {"source_id": "s2", "title": "Paper B"},
    ]
    job.optional_pdf_choices["s1"] = "file"
    _append_optional_pdf_round_for_ui(job)
    assert len(job.optional_pdf_rounds) == 1
    items = job.optional_pdf_rounds[0]["items"]
    assert items[0]["answer"] == "PDF from file"
    assert items[1]["answer"] == "— skipped / none"
    assert job.optional_pdf_choices == {}
