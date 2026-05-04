from __future__ import annotations

import json


def test_runtime_summary_upsert_persists_latest_values():
    from src.run_history_store import RunHistoryStore

    store = RunHistoryStore()
    store.save_runtime_summary(
        "job123",
        status="done",
        provider="google",
        total_cost_usd=1.25,
        total_input_tokens=100,
        total_output_tokens=40,
        total_thinking_tokens=10,
        total_duration_seconds=12.5,
        step_count=4,
        target_words=1500,
        final_words=1450,
        updated_at=10.0,
    )
    store.save_runtime_summary(
        "job123",
        status="error",
        provider="openai",
        total_cost_usd=2.5,
        total_input_tokens=200,
        total_output_tokens=80,
        total_thinking_tokens=20,
        total_duration_seconds=25.0,
        step_count=5,
        target_words=2000,
        final_words=0,
        updated_at=20.0,
    )

    summary = store.get_runtime_summary("job123")

    assert summary is not None
    assert summary["status"] == "error"
    assert summary["provider"] == "openai"
    assert summary["total_cost_usd"] == 2.5
    assert summary["step_count"] == 5
    assert summary["updated_at"] == 20.0


def test_step_metrics_upsert_by_job_and_step_name():
    from src.run_history_store import RunHistoryStore

    store = RunHistoryStore()
    store.save_step_metric(
        "job123",
        "plan",
        status="completed",
        model="gemini-2.5-flash",
        cost_usd=0.12,
        call_count=1,
        input_tokens=120,
        output_tokens=45,
        thinking_tokens=12,
        duration_seconds=3.4,
        step_index=2,
        step_count=7,
        updated_at=10.0,
    )
    store.save_step_metric(
        "job123",
        "plan",
        status="completed",
        model="gpt-5.4",
        cost_usd=0.34,
        call_count=2,
        input_tokens=220,
        output_tokens=90,
        thinking_tokens=30,
        duration_seconds=4.8,
        step_index=2,
        step_count=7,
        updated_at=12.0,
    )
    store.save_step_metric(
        "job123",
        "write",
        status="failed",
        model="gpt-5.4",
        cost_usd=0.0,
        call_count=1,
        input_tokens=300,
        output_tokens=0,
        thinking_tokens=0,
        duration_seconds=8.0,
        step_index=5,
        step_count=7,
        updated_at=13.0,
    )

    rows = store.list_step_metrics("job123")

    assert [row["step_name"] for row in rows] == ["plan", "write"]
    assert rows[0]["model"] == "gpt-5.4"
    assert rows[0]["cost_usd"] == 0.34
    assert rows[0]["call_count"] == 2
    assert rows[1]["status"] == "failed"


def test_sync_artifacts_upserts_and_marks_missing_files_unavailable():
    from src.run_history_store import RunHistoryStore
    from src.storage import MemoryRunStorage

    store = RunHistoryStore()
    storage = MemoryRunStorage("test/")

    storage.write_text("brief/assignment.json", json.dumps({"topic": "x"}))
    storage.write_text("essay/draft.md", "draft")
    storage.write_bytes("essay.docx", b"PK\x03\x04docx")
    storage.write_text("sources/notes/s1.json", json.dumps({"title": "note"}))

    artifacts = store.sync_artifacts("job123", storage, current_time=10.0)

    by_path = {row["relative_path"]: row for row in artifacts}
    assert by_path["brief/assignment.json"]["artifact_type"] == "assignment_brief"
    assert by_path["essay/draft.md"]["artifact_type"] == "draft"
    assert by_path["essay.docx"]["artifact_type"] == "document"
    assert by_path["sources/notes/s1.json"]["artifact_type"] == "source_note"
    assert all(row["is_available"] for row in artifacts)

    storage.delete("essay/draft.md")

    artifacts = store.sync_artifacts("job123", storage, current_time=20.0)
    by_path = {row["relative_path"]: row for row in artifacts}
    assert by_path["essay/draft.md"]["is_available"] is False
    assert by_path["essay/draft.md"]["deleted_at"] == 20.0
    assert by_path["brief/assignment.json"]["is_available"] is True


def test_mark_artifacts_deleted_marks_all_available_rows():
    from src.run_history_store import RunHistoryStore
    from src.storage import MemoryRunStorage

    store = RunHistoryStore()
    storage = MemoryRunStorage("test/")
    storage.write_text("run.log", "hello")
    store.sync_artifacts("job123", storage, current_time=10.0)

    store.mark_artifacts_deleted("job123", current_time=30.0)

    artifacts = store.list_artifacts("job123")
    assert len(artifacts) == 1
    assert artifacts[0]["is_available"] is False
    assert artifacts[0]["deleted_at"] == 30.0
