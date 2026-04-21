from __future__ import annotations

import json

from src.runtime import TokenTracker


def test_write_report_uses_stage_accurate_source_metrics(tmp_path):
    run_dir = tmp_path
    (run_dir / "essay").mkdir()
    (run_dir / "plan").mkdir()
    (run_dir / "sources" / "notes").mkdir(parents=True)

    (run_dir / "plan" / "plan.json").write_text(
        json.dumps({"total_word_target": 2000}),
        encoding="utf-8",
    )
    (run_dir / "essay" / "draft.md").write_text("draft words only", encoding="utf-8")
    (run_dir / "essay" / "reviewed.md").write_text(
        "Alpha [[s1]] beta [[s2]] gamma [[s1]]",
        encoding="utf-8",
    )

    (run_dir / "sources" / "registry.json").write_text(
        json.dumps(
            {
                "s1": {"title": "One"},
                "s2": {"title": "Two"},
                "s3": {"title": "Three"},
                "s4": {"title": "Four"},
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "sources" / "scores.json").write_text(
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
        encoding="utf-8",
    )
    (run_dir / "sources" / "selected.json").write_text(
        json.dumps({"s1": {"title": "One"}, "s2": {"title": "Two"}}),
        encoding="utf-8",
    )
    (run_dir / "sources" / "notes" / "s1.json").write_text(
        json.dumps({"fetched_fulltext": True}),
        encoding="utf-8",
    )
    (run_dir / "sources" / "notes" / "s2.json").write_text(
        json.dumps({"fetched_fulltext": False}),
        encoding="utf-8",
    )

    tracker = TokenTracker()
    tracker.record("openai:gpt-5.4", 10, 5, step="write")
    tracker.record_duration("write", 12.0)

    report_path = tracker.write_report(run_dir)

    assert report_path is not None
    report = report_path.read_text(encoding="utf-8")

    assert "| Sources registered | 4 |" in report
    assert "| Sources scored | 4 |" in report
    assert "| Sources above threshold | 3 (score >= 3) |" in report
    assert "| Sources available for writing | 2 |" in report
    assert "| Selected source detail | 1 full text / 1 abstract-only |" in report
    assert "| Sources cited | 2 |" in report

    assert "| Sources fetched |" not in report
    assert "| Sources usable |" not in report
    assert "| Sources selected |" not in report
