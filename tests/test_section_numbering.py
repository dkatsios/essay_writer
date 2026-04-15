"""Regression tests for duplicate section numbering in long-essay runs."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call

from src.pipeline_support import PipelineContext, _parse_sections
from src.pipeline_writing import make_review_sections, make_write_sections


def _write_plan(run_dir: Path) -> None:
    (run_dir / "plan").mkdir(parents=True, exist_ok=True)
    plan = {
        "title": "Test essay",
        "thesis": "Test thesis",
        "sections": [
            {
                "number": 1,
                "title": "Intro",
                "heading": "1. Intro",
                "word_target": 300,
                "key_points": "Context",
                "content_outline": "Open the essay",
            },
            {
                "number": 2,
                "title": "Body A",
                "heading": "1.1 Body A",
                "word_target": 500,
                "key_points": "First point",
                "content_outline": "Develop first argument",
            },
            {
                "number": 2,
                "title": "Body B",
                "heading": "1.2 Body B",
                "word_target": 500,
                "key_points": "Second point",
                "content_outline": "Develop second argument",
            },
        ],
        "research_queries": ["test query"],
        "total_word_target": 1300,
    }
    (run_dir / "plan" / "plan.json").write_text(json.dumps(plan), encoding="utf-8")


def _write_brief(run_dir: Path) -> None:
    (run_dir / "brief").mkdir(parents=True, exist_ok=True)
    brief = {
        "topic": "Test topic",
        "description": "Test description",
        "language": "English",
    }
    (run_dir / "brief" / "assignment.json").write_text(
        json.dumps(brief), encoding="utf-8"
    )


def _make_ctx(run_dir: Path, tracker: object | None = None) -> PipelineContext:
    config = SimpleNamespace(
        search=SimpleNamespace(section_source_full_detail_max=3),
        writing=SimpleNamespace(
            word_count_tolerance=0.1,
            word_count_tolerance_over=0.2,
        ),
    )
    return PipelineContext(
        worker=MagicMock(),
        async_worker=None,
        writer=MagicMock(),
        reviewer=MagicMock(),
        run_dir=run_dir,
        config=config,
        tracker=tracker,
    )


def test_parse_sections_uses_plan_position_as_internal_id(tmp_path: Path) -> None:
    _write_plan(tmp_path)

    sections = _parse_sections(tmp_path)

    assert [section.position for section in sections] == [1, 2, 3]
    assert [section.number for section in sections] == [1, 2, 2]


def test_write_sections_keeps_duplicate_numbers_distinct(
    tmp_path: Path, monkeypatch
) -> None:
    _write_plan(tmp_path)
    _write_brief(tmp_path)
    (tmp_path / "plan" / "source_assignments.json").write_text(
        json.dumps(
            {
                "assignments": [
                    {"section_number": 1, "source_ids": ["intro-source"]},
                    {"section_number": 2, "source_ids": ["body-a-source"]},
                    {"section_number": 2, "source_ids": ["body-b-source"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    sections = _parse_sections(tmp_path)
    tracker = MagicMock()
    captured_assignments: dict[int, list[str]] = {}

    monkeypatch.setattr(
        "src.pipeline_writing._load_selected_source_notes", lambda _run_dir: []
    )

    def fake_render_prompt(template: str, **kwargs) -> str:
        section = kwargs["section"]
        captured_assignments[section.position] = list(kwargs["assigned_source_ids"])
        return section.title

    monkeypatch.setattr("src.pipeline_writing.render_prompt", fake_render_prompt)
    monkeypatch.setattr(
        "src.pipeline_writing._text_call",
        lambda _client, _system, user_prompt, _tracker=None: f"draft:{user_prompt}",
    )

    make_write_sections(sections, target_words=1300, citation_min_sources=1)(
        _make_ctx(tmp_path, tracker=tracker)
    )

    assert captured_assignments == {
        1: ["intro-source"],
        2: ["body-a-source"],
        3: ["body-b-source"],
    }
    assert (tmp_path / "essay" / "sections" / "01.md").read_text(
        encoding="utf-8"
    ) == "draft:Intro"
    assert (tmp_path / "essay" / "sections" / "02.md").read_text(
        encoding="utf-8"
    ) == "draft:Body A"
    assert (tmp_path / "essay" / "sections" / "03.md").read_text(
        encoding="utf-8"
    ) == "draft:Body B"
    assert (tmp_path / "essay" / "draft.md").read_text(encoding="utf-8") == (
        "draft:Intro\n\ndraft:Body A\n\ndraft:Body B"
    )
    assert tracker.set_current_step.call_args_list == [
        call("write:2"),
        call("write:3"),
        call("write:1"),
    ]


def test_review_sections_keeps_duplicate_numbers_distinct(
    tmp_path: Path, monkeypatch
) -> None:
    _write_plan(tmp_path)
    _write_brief(tmp_path)
    sections = _parse_sections(tmp_path)
    tracker = MagicMock()
    sections_dir = tmp_path / "essay" / "sections"
    sections_dir.mkdir(parents=True, exist_ok=True)
    (sections_dir / "01.md").write_text("draft:Intro", encoding="utf-8")
    (sections_dir / "02.md").write_text("draft:Body A", encoding="utf-8")
    (sections_dir / "03.md").write_text("draft:Body B", encoding="utf-8")

    monkeypatch.setattr(
        "src.pipeline_writing.render_prompt",
        lambda _template, **kwargs: kwargs["section"].title,
    )
    monkeypatch.setattr(
        "src.pipeline_writing._text_call",
        lambda _client, _system, user_prompt, _tracker=None: f"review:{user_prompt}",
    )

    make_review_sections(sections, target_words=1300)(
        _make_ctx(tmp_path, tracker=tracker)
    )

    assert (tmp_path / "essay" / "reviewed" / "01.md").read_text(
        encoding="utf-8"
    ) == "review:Intro"
    assert (tmp_path / "essay" / "reviewed" / "02.md").read_text(
        encoding="utf-8"
    ) == "review:Body A"
    assert (tmp_path / "essay" / "reviewed" / "03.md").read_text(
        encoding="utf-8"
    ) == "review:Body B"
    assert (tmp_path / "essay" / "reviewed.md").read_text(encoding="utf-8") == (
        "review:Intro\n\nreview:Body A\n\nreview:Body B"
    )
    assert sorted(args.args[0] for args in tracker.set_current_step.call_args_list) == [
        "review:1",
        "review:2",
        "review:3",
    ]
