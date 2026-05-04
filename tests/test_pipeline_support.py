"""Tests for src/pipeline_support.py — structured calls, source notes, scaling, context helpers."""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import MagicMock

import pytest

from config.settings import EssayWriterConfig
from src.schemas import AssignmentBrief


class TestStructuredCallRepair:
    def test_essay_plan_parses_stringified_sections(self):
        from src.schemas import EssayPlan

        plan = EssayPlan.model_validate(
            {
                "title": "Test",
                "thesis": "Test thesis",
                "sections": json.dumps(
                    [
                        {
                            "number": 1,
                            "title": "Intro",
                            "heading": "Intro",
                            "word_target": 400,
                        },
                        {
                            "number": 2,
                            "title": "Body",
                            "heading": "Body",
                            "word_target": 600,
                        },
                    ]
                ),
                "research_queries": ["query"],
                "total_word_target": 1000,
            }
        )

        assert len(plan.sections) == 2
        assert plan.sections[0].title == "Intro"

    def test_essay_plan_rejects_missing_sections(self):
        from src.schemas import EssayPlan

        with pytest.raises(ValueError, match="sections must be a non-empty array"):
            EssayPlan(
                title="Η πτώση της εμπιστοσύνης",
                thesis="Η ανάλυση απαιτεί σύνθεση κοινωνικών και θεσμικών παραγόντων.",
                research_queries=["πτώση εμπιστοσύνης θεσμοί Ελλάδα"],
                total_word_target=1200,
            )

    def test_structured_call_uses_instructor(self, monkeypatch):
        """Verify structured_call delegates to Instructor's create()."""
        from src.pipeline_support import structured_call
        from src.schemas import EssayPlan
        from src.agent import ModelClient

        complete_plan = EssayPlan.model_validate(
            {
                "title": "Test",
                "thesis": "Test thesis",
                "research_queries": ["query"],
                "total_word_target": 1000,
                "sections": [
                    {
                        "number": 1,
                        "title": "Intro",
                        "heading": "Intro",
                        "word_target": 1000,
                    }
                ],
            }
        )

        mock_instructor = MagicMock()
        mock_instructor.chat.completions.create.return_value = complete_plan
        client = ModelClient(
            client=mock_instructor, model="test-model", model_spec="openai:test-model"
        )

        # Patch retry_with_backoff to just call the fn
        monkeypatch.setattr(
            "src.pipeline_support.retry_with_backoff", lambda fn, **kw: fn()
        )

        result = structured_call(client, "Plan prompt", EssayPlan)

        assert len(result.sections) == 1
        mock_instructor.chat.completions.create.assert_called_once()
        call_kwargs = mock_instructor.chat.completions.create.call_args
        assert call_kwargs.kwargs["response_model"] is EssayPlan
        assert call_kwargs.kwargs["model"] == "test-model"

    def test_async_structured_call_uses_instructor(self, monkeypatch):
        """Verify async_structured_call delegates to async Instructor."""
        from src.pipeline_support import async_structured_call
        from src.schemas import EssayPlan
        from src.agent import AsyncModelClient

        complete_plan = EssayPlan.model_validate(
            {
                "title": "Test",
                "thesis": "Test thesis",
                "research_queries": ["query"],
                "total_word_target": 1000,
                "sections": [
                    {
                        "number": 1,
                        "title": "Intro",
                        "heading": "Intro",
                        "word_target": 1000,
                    }
                ],
            }
        )

        mock_instructor = MagicMock()

        async def fake_create(**kwargs):
            return complete_plan

        mock_instructor.chat.completions.create = fake_create
        client = AsyncModelClient(
            client=mock_instructor, model="test-model", model_spec="openai:test-model"
        )

        # Patch retry_with_backoff to handle async
        async def fake_retry(fn, *, is_async=False):
            return await fn()

        monkeypatch.setattr("src.pipeline_support.retry_with_backoff", fake_retry)

        result = asyncio.run(async_structured_call(client, "Plan prompt", EssayPlan))

        assert len(result.sections) == 1


class TestSelectedSourceNotes:
    def test_uses_selected_accessible_notes_when_available(self):
        from src.pipeline_support import load_selected_source_notes
        from src.schemas import SourceNote
        from src.storage import MemoryRunStorage

        storage = MemoryRunStorage("test/")

        note_a = SourceNote(source_id="alpha2024", is_accessible=True, title="A")
        note_b = SourceNote(source_id="beta2024", is_accessible=True, title="B")
        storage.write_text("sources/notes/alpha2024.json", note_a.model_dump_json())
        storage.write_text("sources/notes/beta2024.json", note_b.model_dump_json())
        storage.write_text(
            "sources/selected.json",
            json.dumps({"beta2024": {"title": "B"}}),
        )

        notes = load_selected_source_notes(storage)
        assert [note.source_id for note in notes] == ["beta2024"]

    def test_falls_back_to_all_accessible_notes_when_selection_is_unusable(
        self, caplog
    ):
        from src.pipeline_support import load_selected_source_notes
        from src.schemas import SourceNote
        from src.storage import MemoryRunStorage

        storage = MemoryRunStorage("test/")

        note_a = SourceNote(source_id="alpha2024", is_accessible=True, title="A")
        note_b = SourceNote(source_id="beta2024", is_accessible=True, title="B")
        storage.write_text("sources/notes/alpha2024.json", note_a.model_dump_json())
        storage.write_text("sources/notes/beta2024.json", note_b.model_dump_json())
        storage.write_text(
            "sources/selected.json",
            json.dumps({"missing2024": {"title": "Missing"}}),
        )

        with caplog.at_level(logging.WARNING):
            notes = load_selected_source_notes(storage)

        assert [note.source_id for note in notes] == ["alpha2024", "beta2024"]
        assert "Selected sources had no accessible notes" in caplog.text

    def test_empty_selected_set_stays_empty(self):
        from src.pipeline_support import load_selected_source_notes
        from src.schemas import SourceNote
        from src.storage import MemoryRunStorage

        storage = MemoryRunStorage("test/")

        note_a = SourceNote(source_id="alpha2024", is_accessible=True, title="A")
        storage.write_text("sources/notes/alpha2024.json", note_a.model_dump_json())
        storage.write_text("sources/selected.json", json.dumps({}))

        assert load_selected_source_notes(storage) == []

    async def test_write_full_clamps_min_sources_to_selected_usable_count(
        self, monkeypatch
    ):
        from types import SimpleNamespace

        from src.pipeline_support import PipelineContext
        from src.pipeline_writing import make_write_full
        from src.schemas import SourceNote
        from src.storage import MemoryRunStorage

        storage = MemoryRunStorage("test/")
        storage.write_text(
            "brief/assignment.json",
            json.dumps(
                {
                    "language": "English",
                    "topic": "Test topic",
                    "description": "Test description",
                }
            ),
        )
        storage.write_text(
            "plan/plan.json",
            json.dumps(
                {"title": "Test plan", "sections": [], "total_word_target": 1000}
            ),
        )

        source_notes = [
            SourceNote(source_id="a", is_accessible=True, title="A"),
            SourceNote(source_id="b", is_accessible=True, title="B"),
        ]
        captured: dict[str, object] = {}

        monkeypatch.setattr(
            "src.pipeline_writing.load_selected_source_notes",
            lambda _storage: source_notes,
        )

        def fake_render_prompt(_template: str, **kwargs) -> str:
            captured.update(kwargs)
            return "PROMPT"

        monkeypatch.setattr("src.pipeline_writing.render_prompt", fake_render_prompt)

        async def _fake_async_text_call(_client, _prompt, _tracker=None):
            return "essay body"

        monkeypatch.setattr(
            "src.pipeline_writing.async_text_call", _fake_async_text_call
        )

        ctx = PipelineContext(
            worker=MagicMock(),
            async_worker=MagicMock(),
            writer=MagicMock(),
            async_writer=MagicMock(),
            reviewer=MagicMock(),
            storage=storage,
            config=SimpleNamespace(
                search=SimpleNamespace(section_source_full_detail_max=3),
                writing=SimpleNamespace(word_count_tolerance=0.1),
            ),
            tracker=None,
            brief=AssignmentBrief(
                topic="Test topic",
                language="English",
                description="Test description",
            ),
        )

        await make_write_full(target_words=1000, citation_min_sources=5)(ctx)

        assert captured["min_sources"] == 2
        assert storage.read_text("essay/draft.md") == "essay body"


class TestSourceTargetScaling:
    def test_compute_max_sources_log_scaling(self):
        from src.pipeline_support import compute_max_sources, suggested_sources

        cfg = EssayWriterConfig()
        target, fetch = compute_max_sources(24000, cfg, None)
        expected = suggested_sources(24000, cfg.search.sources_per_1k_words)
        assert target == max(cfg.search.min_sources, expected)
        assert fetch == int(target * cfg.search.overfetch_multiplier)
        # Log scaling should produce fewer sources than the old linear formula
        old_linear = 24 * cfg.search.sources_per_1k_words  # 120
        assert target < old_linear

    def test_suggested_sources_values(self):
        """Spot-check the log-based formula at key word counts."""
        from src.pipeline_support import suggested_sources

        assert suggested_sources(0) == 0
        assert 22 <= suggested_sources(2000) <= 26
        assert 37 <= suggested_sources(5000) <= 41
        assert 50 <= suggested_sources(10000) <= 55
        assert 63 <= suggested_sources(20000) <= 69
        assert 72 <= suggested_sources(30000) <= 77

    def test_compute_max_sources_respects_user_floor_above_raw(self):
        from src.pipeline_support import compute_max_sources

        cfg = EssayWriterConfig()
        target, fetch = compute_max_sources(24000, cfg, 130)
        assert target == 130
        assert fetch == int(130 * cfg.search.overfetch_multiplier)

    def test_compute_max_sources_explicit_user_above_raw(self):
        """User min (e.g. 90) above log-based suggestion (~65 for 24k) wins."""
        from src.pipeline_support import compute_max_sources

        cfg = EssayWriterConfig()
        target, fetch = compute_max_sources(24000, cfg, 90)
        assert target == 90
        assert fetch == int(90 * cfg.search.overfetch_multiplier)

    def test_compute_max_sources_explicit_user_below_raw(self):
        """User min below the log-based suggestion still uses user value."""
        from src.pipeline_support import compute_max_sources

        cfg = EssayWriterConfig()
        target, fetch = compute_max_sources(24000, cfg, 30)
        assert target == 30
        assert fetch == int(30 * cfg.search.overfetch_multiplier)


class TestLongEssayContextHelpers:
    def test_partition_sections_for_writing_defers_intro_conclusion_and_marked_sections(
        self,
    ):
        from src.pipeline_support import Section
        from src.pipeline_writing import partition_sections_for_writing

        sections = [
            Section(
                position=1,
                number=1,
                title="Intro",
                heading="Intro",
                word_target=100,
                requires_full_context=True,
                deferred_order=2,
            ),
            Section(
                position=2,
                number=2,
                title="Body A",
                heading="Body A",
                word_target=100,
            ),
            Section(
                position=3,
                number=3,
                title="Synthesis",
                heading="Synthesis",
                word_target=100,
                requires_full_context=True,
                deferred_order=0,
            ),
            Section(
                position=4,
                number=4,
                title="Conclusion",
                heading="Conclusion",
                word_target=100,
                requires_full_context=True,
                deferred_order=1,
            ),
        ]

        parallel_sections, deferred_sections = partition_sections_for_writing(sections)

        assert [section.position for section in parallel_sections] == [2]
        assert [section.position for section in deferred_sections] == [3, 4, 1]

    def test_prior_section_context_uses_recent_sections_only(self):
        from src.pipeline_support import Section, build_prior_sections_context

        sections = [
            (
                Section(
                    position=1,
                    number=1,
                    title="One",
                    heading="One",
                    word_target=100,
                ),
                "intro",
            ),
            (
                Section(
                    position=2,
                    number=2,
                    title="Two",
                    heading="Two",
                    word_target=100,
                ),
                "body a",
            ),
            (
                Section(
                    position=3,
                    number=3,
                    title="Three",
                    heading="Three",
                    word_target=100,
                ),
                "body b",
            ),
        ]

        context = build_prior_sections_context(sections, max_sections=2)

        assert "intro" not in context
        assert "body a" in context
        assert "body b" in context

    def test_review_context_uses_only_adjacent_sections(self):
        from src.pipeline_support import Section, build_review_context

        sections = [
            Section(position=1, number=1, title="One", heading="One", word_target=100),
            Section(position=2, number=2, title="Two", heading="Two", word_target=100),
            Section(
                position=3, number=3, title="Three", heading="Three", word_target=100
            ),
            Section(
                position=4, number=4, title="Four", heading="Four", word_target=100
            ),
            Section(
                position=5, number=5, title="Five", heading="Five", word_target=100
            ),
        ]
        section_texts = {
            2: "section two",
            3: "section three",
            4: "section four",
        }

        context = build_review_context(sections[2], sections, section_texts)

        assert "section two" in context
        assert "section three" in context
        assert "section four" in context
        assert "SECTION TO REVIEW: START" in context
        assert "SECTION TO REVIEW: END" in context
        assert "section one" not in context
        assert "section five" not in context

    def test_parse_sections_passes_through_deferred_fields(self):
        from src.pipeline_support import parse_sections
        from src.storage import MemoryRunStorage

        storage = MemoryRunStorage("test/")
        plan = {
            "title": "Test essay",
            "thesis": "Test thesis",
            "sections": [
                {
                    "number": 1,
                    "title": "Introduction",
                    "heading": "Introduction",
                    "word_target": 200,
                    "requires_full_context": True,
                    "deferred_order": 2,
                },
                {
                    "number": 2,
                    "title": "Body",
                    "heading": "Body",
                    "word_target": 400,
                    "requires_full_context": False,
                },
                {
                    "number": 3,
                    "title": "Conclusion",
                    "heading": "Conclusion",
                    "word_target": 200,
                    "requires_full_context": True,
                    "deferred_order": 1,
                },
            ],
            "research_queries": ["test"],
            "total_word_target": 800,
        }
        storage.write_text("plan/plan.json", json.dumps(plan))

        sections = parse_sections(storage)

        assert [section.requires_full_context for section in sections] == [
            True,
            False,
            True,
        ]
        assert [section.deferred_order for section in sections] == [2, None, 1]

    @pytest.mark.asyncio
    async def test_make_review_full_does_not_pass_source_context(self, monkeypatch):
        from src.pipeline_support import PipelineContext
        from src.pipeline_writing import make_review_full
        from src.storage import MemoryRunStorage

        storage = MemoryRunStorage("test/")
        storage.write_text("brief/assignment.json", "{}")
        storage.write_text("plan/plan.json", "{}")
        storage.write_text("essay/draft.md", "# Draft\n\nParagraph with [[s1]].")
        storage.write_text("sources/selected.json", json.dumps(["s1"]))
        storage.write_text(
            "sources/notes/s1.json",
            json.dumps(
                {
                    "source_id": "s1",
                    "title": "Source One",
                    "authors": ["Author"],
                    "summary": "Summary.",
                    "is_accessible": True,
                    "fetched_fulltext": True,
                }
            ),
        )

        captured: dict[str, object] = {}

        def fake_render_prompt(template: str, **kwargs):
            captured["template"] = template
            captured["kwargs"] = kwargs
            return type("Prompt", (), {"system": None, "user": "prompt"})()

        async def fake_async_text_call(_client, _prompt, _tracker=None):
            return "Reviewed text"

        monkeypatch.setattr("src.pipeline_writing.render_prompt", fake_render_prompt)
        monkeypatch.setattr(
            "src.pipeline_writing.async_text_call", fake_async_text_call
        )

        ctx = PipelineContext(
            worker=None,
            async_worker=None,
            writer=None,
            reviewer=None,
            storage=storage,
            config=EssayWriterConfig(),
            async_writer=None,
            async_reviewer=object(),
            brief=AssignmentBrief(topic="Test", language="English", description="Test"),
        )

        await make_review_full(target_words=1000, citation_min_sources=3)(ctx)

        kwargs = captured["kwargs"]
        assert captured["template"] == "essay_review.j2"
        assert "source_catalog" not in kwargs
        assert "uncited_ids" not in kwargs
        assert "total_selected_sources" not in kwargs


class _HistoryRecorder:
    def __init__(self) -> None:
        self.saved_steps: list[tuple[str, str, dict]] = []
        self.synced_job_ids: list[str] = []

    def save_step_metric(self, job_id: str, step_name: str, **payload):
        self.saved_steps.append((job_id, step_name, payload))

    def sync_artifacts(self, job_id: str, _run_dir):
        self.synced_job_ids.append(job_id)


async def test_execute_persists_completed_step_history():
    from src.pipeline_support import PipelineContext, PipelineStep, execute
    from src.runtime import TokenTracker
    from src.storage import MemoryRunStorage

    storage = MemoryRunStorage("test/")
    tracker = TokenTracker()
    history = _HistoryRecorder()
    ctx = PipelineContext(
        worker=None,
        async_worker=None,
        writer=None,
        reviewer=None,
        storage=storage,
        config=EssayWriterConfig(),
        tracker=tracker,
        job_id="job123",
        run_history_store=history,
    )

    async def _step(current_ctx):
        current_ctx.tracker.record("openai:gpt-4o", 100, 25, 5)
        current_ctx.storage.write_text("brief/assignment.json", "{}")

    await execute(
        [PipelineStep("plan", _step)],
        ctx,
        step_offset=2,
        total_steps=7,
    )

    assert history.synced_job_ids == ["job123"]
    assert len(history.saved_steps) == 1
    job_id, step_name, payload = history.saved_steps[0]
    assert job_id == "job123"
    assert step_name == "plan"
    assert payload["status"] == "completed"
    assert payload["step_index"] == 2
    assert payload["step_count"] == 7
    assert payload["cost_usd"] > 0


async def test_execute_persists_failed_step_history():
    from src.pipeline_support import PipelineContext, PipelineStep, execute
    from src.runtime import TokenTracker
    from src.storage import MemoryRunStorage

    storage = MemoryRunStorage("test/")
    tracker = TokenTracker()
    history = _HistoryRecorder()
    ctx = PipelineContext(
        worker=None,
        async_worker=None,
        writer=None,
        reviewer=None,
        storage=storage,
        config=EssayWriterConfig(),
        tracker=tracker,
        job_id="job123",
        run_history_store=history,
    )

    async def _step(current_ctx):
        current_ctx.tracker.record("openai:gpt-4o", 10, 0, 0)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await execute(
            [PipelineStep("write", _step)],
            ctx,
            step_offset=5,
            total_steps=7,
        )

    assert history.synced_job_ids == []
    assert len(history.saved_steps) == 1
    _, step_name, payload = history.saved_steps[0]
    assert step_name == "write"
    assert payload["status"] == "failed"
