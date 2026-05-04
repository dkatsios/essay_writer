from __future__ import annotations

import json

from src.runtime import TokenTracker
from src.storage import MemoryRunStorage


def test_write_report_uses_stage_accurate_source_metrics():
    storage = MemoryRunStorage("test/")

    storage.write_text(
        "plan/plan.json",
        json.dumps({"total_word_target": 2000}),
    )
    storage.write_text("essay/draft.md", "draft words only")
    storage.write_text(
        "essay/reviewed.md",
        "Alpha [[s1]] beta [[s2]] gamma [[s1]]",
    )

    storage.write_text(
        "sources/registry.json",
        json.dumps(
            {
                "s1": {"title": "One"},
                "s2": {"title": "Two"},
                "s3": {"title": "Three"},
                "s4": {"title": "Four"},
            }
        ),
    )
    storage.write_text(
        "sources/scores.json",
        json.dumps(
            {
                "min_relevance_score": 3,
                "scores": {
                    "s1": {"relevance_score": 5, "selected_for_writing": True},
                    "s2": {"relevance_score": 4, "selected_for_writing": True},
                    "s3": {"relevance_score": 3, "selected_for_writing": False},
                    "s4": {"relevance_score": 2, "selected_for_writing": False},
                },
            }
        ),
    )
    storage.write_text(
        "sources/selected.json",
        json.dumps({"s1": {"title": "One"}, "s2": {"title": "Two"}}),
    )
    storage.write_text(
        "sources/notes/s1.json",
        json.dumps({"fetched_fulltext": True}),
    )
    storage.write_text(
        "sources/notes/s2.json",
        json.dumps({"fetched_fulltext": False}),
    )

    tracker = TokenTracker()
    tracker.record("openai:gpt-5.4", 10, 5, step="write")
    tracker.record_duration("write", 12.0)

    result = tracker.write_report(storage)

    assert result is True
    report = storage.read_text("report.md")

    assert "| Sources registered | 4 |" in report
    assert "| Sources scored | 4 |" in report
    assert "| Sources above threshold | 3 (score >= 3) |" in report
    assert "| Sources available for writing | 2 |" in report
    assert "| Selected source detail | 1 full text / 1 abstract-only |" in report
    assert "| Sources cited | 2 |" in report

    assert "| Sources fetched |" not in report
    assert "| Sources usable |" not in report
    assert "| Sources selected |" not in report
