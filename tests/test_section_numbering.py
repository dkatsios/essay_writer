"""Regression tests for duplicate section numbering in long-essay runs."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.pipeline_support import PipelineContext, parse_sections
from src.pipeline_writing import make_review_sections, make_write_sections
from src.schemas import AssignmentBrief
from src.storage import MemoryRunStorage


def _write_plan(storage: MemoryRunStorage) -> None:
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
                "requires_full_context": True,
                "deferred_order": 2,
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
    storage.write_text("plan/plan.json", json.dumps(plan))


def _write_brief(storage: MemoryRunStorage) -> None:
    brief = {
        "topic": "Test topic",
        "description": "Test description",
        "language": "English",
    }
    storage.write_text("brief/assignment.json", json.dumps(brief))


def _make_ctx(
    storage: MemoryRunStorage, tracker: object | None = None
) -> PipelineContext:
    config = SimpleNamespace(
        search=SimpleNamespace(section_source_full_detail_max=3),
        writing=SimpleNamespace(
            word_count_tolerance=0.1,
            word_count_tolerance_over=0.2,
        ),
    )
    return PipelineContext(
        worker=MagicMock(),
        async_worker=MagicMock(),
        writer=MagicMock(),
        async_writer=MagicMock(),
        reviewer=MagicMock(),
        async_reviewer=MagicMock(),
        storage=storage,
        config=config,
        tracker=tracker,
        brief=AssignmentBrief(
            topic="Test topic", language="English", description="Test description"
        ),
    )


def test_parse_sections_uses_plan_position_as_internal_id() -> None:
    storage = MemoryRunStorage("test/")
    _write_plan(storage)

    sections = parse_sections(storage)

    assert [section.position for section in sections] == [1, 2, 3]
    assert [section.number for section in sections] == [1, 2, 2]


async def test_write_sections_keeps_duplicate_numbers_distinct(monkeypatch) -> None:
    storage = MemoryRunStorage("test/")
    _write_plan(storage)
    _write_brief(storage)
    storage.write_text(
        "plan/source_assignments.json",
        json.dumps(
            {
                "assignments": [
                    {"section_position": 1, "source_ids": ["intro-source"]},
                    {"section_position": 2, "source_ids": ["body-a-source"]},
                    {"section_position": 3, "source_ids": ["body-b-source"]},
                ]
            }
        ),
    )
    sections = parse_sections(storage)
    tracker = MagicMock()
    captured_assignments: dict[int, list[str]] = {}
    captured_context_flags: dict[int, bool] = {}
    captured_context_text: dict[int, str] = {}

    monkeypatch.setattr(
        "src.pipeline_writing.load_selected_source_notes", lambda _storage: []
    )

    def fake_render_prompt(template: str, **kwargs) -> str:
        section = kwargs["section"]
        captured_assignments[section.position] = list(kwargs["assigned_source_ids"])
        captured_context_flags[section.position] = kwargs["has_full_context"]
        captured_context_text[section.position] = kwargs["essay_context"]
        return section.title

    monkeypatch.setattr("src.pipeline_writing.render_prompt", fake_render_prompt)

    async def _fake_async_text_call(_client, prompt, _tracker=None):
        return f"draft:{prompt}"

    monkeypatch.setattr("src.pipeline_writing.async_text_call", _fake_async_text_call)

    await make_write_sections(sections, target_words=1300, citation_min_sources=1)(
        _make_ctx(storage, tracker=tracker)
    )

    assert captured_assignments == {
        1: ["intro-source"],
        2: ["body-a-source"],
        3: ["body-b-source"],
    }
    assert captured_context_flags == {
        1: True,
        2: False,
        3: False,
    }
    assert "draft:Body A" in captured_context_text[1]
    assert "draft:Body B" in captured_context_text[1]
    assert captured_context_text[2] == ""
    assert captured_context_text[3] == ""
    assert storage.read_text("essay/sections/01.md") == "draft:Intro"
    assert storage.read_text("essay/sections/02.md") == "draft:Body A"
    assert storage.read_text("essay/sections/03.md") == "draft:Body B"
    assert storage.read_text("essay/draft.md") == (
        "draft:Intro\n\ndraft:Body A\n\ndraft:Body B"
    )
    write_steps = [args.args[0] for args in tracker.set_current_step.call_args_list]
    assert write_steps[-1] == "write:1"
    assert sorted(write_steps[:-1]) == ["write", "write"]


async def test_review_sections_keeps_duplicate_numbers_distinct(monkeypatch) -> None:
    storage = MemoryRunStorage("test/")
    _write_plan(storage)
    _write_brief(storage)
    sections = parse_sections(storage)
    tracker = MagicMock()
    storage.write_text("essay/sections/01.md", "draft:Intro")
    storage.write_text("essay/sections/02.md", "draft:Body A")
    storage.write_text("essay/sections/03.md", "draft:Body B")

    monkeypatch.setattr(
        "src.pipeline_writing.render_prompt",
        lambda _template, **kwargs: kwargs["section"].title,
    )

    async def _fake_async_text_call(_client, prompt, _tracker=None):
        return f"review:{prompt}"

    monkeypatch.setattr("src.pipeline_writing.async_text_call", _fake_async_text_call)

    await make_review_sections(sections, target_words=1300)(
        _make_ctx(storage, tracker=tracker)
    )

    assert storage.read_text("essay/reviewed/01.md") == "review:Intro"
    assert storage.read_text("essay/reviewed/02.md") == "review:Body A"
    assert storage.read_text("essay/reviewed/03.md") == "review:Body B"
    assert storage.read_text("essay/reviewed.md") == (
        "review:Intro\n\nreview:Body A\n\nreview:Body B"
    )
    assert sorted(args.args[0] for args in tracker.set_current_step.call_args_list) == [
        "review",
        "review",
        "review",
    ]


async def test_review_sections_routes_reconciliation_notes_by_position(
    monkeypatch,
) -> None:
    storage = MemoryRunStorage("test/")
    _write_plan(storage)
    _write_brief(storage)
    sections = parse_sections(storage)
    storage.write_text("essay/sections/01.md", "draft:Intro")
    storage.write_text("essay/sections/02.md", "draft:Body A")
    storage.write_text("essay/sections/03.md", "draft:Body B")
    storage.write_text(
        "essay/reconciliation.json",
        json.dumps(
            {
                "global_notes": [],
                "sections": [
                    {
                        "section_position": 1,
                        "title": "Intro",
                        "instructions": [
                            {
                                "category": "intro_alignment",
                                "priority": "high",
                                "instruction": "Align the introduction with the completed body.",
                                "related_section_positions": [2, 3],
                            }
                        ],
                    },
                    {
                        "section_position": 2,
                        "title": "Body A",
                        "instructions": [
                            {
                                "category": "transition",
                                "priority": "medium",
                                "instruction": "Strengthen the bridge into Body B.",
                                "related_section_positions": [3],
                            }
                        ],
                    },
                    {
                        "section_position": 3,
                        "title": "Body B",
                        "instructions": [
                            {
                                "category": "overlap",
                                "priority": "low",
                                "instruction": "Trim repeated setup already covered in Body A.",
                                "related_section_positions": [2],
                            }
                        ],
                    },
                ],
            }
        ),
    )
    captured_notes: dict[int, list[str]] = {}

    def fake_render_prompt(_template: str, **kwargs) -> str:
        section = kwargs["section"]
        captured_notes[section.position] = [
            item.instruction for item in kwargs["reconciliation_instructions"]
        ]
        return section.title

    monkeypatch.setattr("src.pipeline_writing.render_prompt", fake_render_prompt)

    async def _fake_async_text_call(_client, prompt, _tracker=None):
        return f"review:{prompt}"

    monkeypatch.setattr("src.pipeline_writing.async_text_call", _fake_async_text_call)

    await make_review_sections(sections, target_words=1300)(_make_ctx(storage))

    assert captured_notes == {
        1: ["Align the introduction with the completed body."],
        2: ["Strengthen the bridge into Body B."],
        3: ["Trim repeated setup already covered in Body A."],
    }
