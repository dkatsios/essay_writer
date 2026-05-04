"""End-to-end pipeline test — exercises run_pipeline() with fake LLM responses."""

from __future__ import annotations

import json

import pytest

from config.settings import EssayWriterConfig
from src.agent import AsyncModelClient
from src.pipeline import run_pipeline
from src.schemas import (
    AssignmentBrief,
    EssayPlan,
    PlanSection,
    SourceNote,
    SourceScoreBatch,
    SourceScoreItem,
    ValidationResult,
)
from src.storage import MemoryRunStorage

# ---------------------------------------------------------------------------
# Fake data
# ---------------------------------------------------------------------------

_ABSTRACT = (
    "Software testing is essential for ensuring the quality and reliability "
    "of modern software systems and applications across all domains of "
    "development and engineering practice today"
)

_REGISTRY = {
    "smith2024": {
        "authors": ["Smith, J."],
        "author_families": ["Smith"],
        "year": "2024",
        "title": "On Software Testing Fundamentals",
        "abstract": _ABSTRACT,
        "doi": "10.1234/test1",
        "url": "",
        "pdf_url": "https://example.com/smith2024.pdf",
        "source_type": "journal-article",
        "citation_count": 50,
    },
    "jones2023": {
        "authors": ["Jones, A."],
        "author_families": ["Jones"],
        "year": "2023",
        "title": "Quality Assurance in Practice",
        "abstract": _ABSTRACT,
        "doi": "10.1234/test2",
        "url": "",
        "pdf_url": "https://example.com/jones2023.pdf",
        "source_type": "journal-article",
        "citation_count": 30,
    },
    "lee2022": {
        "authors": ["Lee, B."],
        "author_families": ["Lee"],
        "year": "2022",
        "title": "Test Automation Strategies",
        "abstract": _ABSTRACT,
        "doi": "10.1234/test3",
        "url": "",
        "pdf_url": "https://example.com/lee2022.pdf",
        "source_type": "journal-article",
        "citation_count": 20,
    },
}

_SOURCE_IDS = list(_REGISTRY)

_BRIEF = AssignmentBrief(
    topic="Software Testing",
    language="English",
    description="An essay about software testing practices.",
    word_count="1000",
)

_PLAN = EssayPlan(
    title="Software Testing Practices",
    thesis="Testing is fundamental to software quality.",
    sections=[
        PlanSection(
            number=1,
            title="Introduction",
            heading="## 1. Introduction",
            word_target=300,
        ),
        PlanSection(
            number=2,
            title="Body",
            heading="## 2. Body",
            word_target=400,
        ),
        PlanSection(
            number=3,
            title="Conclusion",
            heading="## 3. Conclusion",
            word_target=300,
        ),
    ],
    research_queries=["software testing quality"],
    total_word_target=1000,
)

_DRAFT = (
    "## 1. Introduction\n\n"
    "This essay examines software testing [[smith2024]]. "
    "Testing is fundamental [[jones2023]].\n\n"
    "## 2. Body\n\n"
    "Key findings in testing [[lee2022]]. "
    "Multiple studies confirm this [[smith2024]].\n\n"
    "## 3. Conclusion\n\n"
    "In conclusion, testing matters [[jones2023]] [[lee2022]].\n"
)

_REVIEWED = (
    "## 1. Introduction\n\n"
    "This reviewed essay examines software testing [[smith2024]]. "
    "Testing is fundamental [[jones2023]].\n\n"
    "## 2. Body\n\n"
    "The reviewed body discusses findings [[lee2022]]. "
    "Studies confirm this [[smith2024]].\n\n"
    "## 3. Conclusion\n\n"
    "In conclusion, testing matters [[jones2023]] [[lee2022]].\n"
)

# ---------------------------------------------------------------------------
# Fake LLM client
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(content)]
        self.model = "fake-model"
        self.usage = None


class FakeInstructorClient:
    """Mimics the Instructor client interface used by async_structured_call / async_text_call."""

    def __init__(self, dispatch: dict):
        self._dispatch = dispatch
        self.chat = self
        self.completions = self

    async def create(self, **kwargs):
        response_model = kwargs.get("response_model")
        value = self._dispatch.get(response_model)
        if callable(value) and not isinstance(value, _FakeResponse):
            return value(kwargs)
        return value


def _make_source_note(kwargs: dict) -> SourceNote:
    """Extract source_id from the prompt and return a matching SourceNote."""
    messages = kwargs.get("messages", [])
    text = " ".join(m.get("content", "") for m in messages)
    for sid in _SOURCE_IDS:
        if sid in text:
            meta = _REGISTRY[sid]
            return SourceNote(
                source_id=sid,
                is_accessible=True,
                fetched_fulltext=True,
                title=meta["title"],
                authors=meta["authors"],
                year=meta["year"],
                summary=f"Summary of {meta['title']}.",
                relevant_extracts=["Key finding."],
                relevance_score=5,
            )
    return SourceNote(source_id="unknown", is_accessible=False, title="Unknown")


def _make_score_batch(kwargs: dict) -> SourceScoreBatch:
    return SourceScoreBatch(
        scores=[
            SourceScoreItem(source_id=sid, relevance_score=5) for sid in _SOURCE_IDS
        ]
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_run_research(
    queries,
    max_sources,
    sources_dir=None,
    fetch_per_api=20,
    prefer_fulltext=False,
    storage=None,
):
    if storage is not None:
        storage.write_text(
            "sources/registry.json",
            json.dumps(_REGISTRY, indent=2),
        )


def _fake_fetch_url_content(url, sources_dir=None):
    return "This is fake PDF content about software testing. " * 20


async def _fake_retry(fn, *, is_async=False):
    if is_async:
        return await fn()
    return fn()


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_e2e_short_essay(monkeypatch):
    storage = MemoryRunStorage("test/")

    config = EssayWriterConfig()
    config.search.min_sources = 2

    worker_client = FakeInstructorClient(
        {
            AssignmentBrief: _BRIEF,
            ValidationResult: ValidationResult(is_pass=True),
            EssayPlan: _PLAN,
            SourceScoreBatch: _make_score_batch,
            SourceNote: _make_source_note,
        }
    )
    writer_client = FakeInstructorClient({None: _FakeResponse(_DRAFT)})
    reviewer_client = FakeInstructorClient({None: _FakeResponse(_REVIEWED)})

    async_worker = AsyncModelClient(
        client=worker_client, model="fake-worker", model_spec="openai:fake-worker"
    )
    async_writer = AsyncModelClient(
        client=writer_client, model="fake-writer", model_spec="openai:fake-writer"
    )
    async_reviewer = AsyncModelClient(
        client=reviewer_client, model="fake-reviewer", model_spec="openai:fake-reviewer"
    )

    monkeypatch.setattr("src.pipeline_support.retry_with_backoff", _fake_retry)
    monkeypatch.setattr("src.pipeline_sources.run_research", _fake_run_research)
    monkeypatch.setattr(
        "src.pipeline_sources.fetch_url_content", _fake_fetch_url_content
    )

    await run_pipeline(
        worker=None,
        writer=None,
        reviewer=None,
        storage=storage,
        config=config,
        async_worker=async_worker,
        async_writer=async_writer,
        async_reviewer=async_reviewer,
        min_sources=3,
    )

    # -- Brief --
    brief = AssignmentBrief.model_validate_json(
        storage.read_text("brief/assignment.json")
    )
    assert brief.topic == "Software Testing"

    # -- Validation --
    validation = ValidationResult.model_validate_json(
        storage.read_text("brief/validation.json")
    )
    assert validation.is_pass is True

    # -- Plan --
    plan = EssayPlan.model_validate_json(storage.read_text("plan/plan.json"))
    assert len(plan.sections) == 3
    assert plan.total_word_target == 1000

    # -- Sources --
    registry = json.loads(storage.read_text("sources/registry.json"))
    assert set(registry) == set(_SOURCE_IDS)

    scores = json.loads(storage.read_text("sources/scores.json"))
    assert scores["scores"]

    selected = json.loads(storage.read_text("sources/selected.json"))
    assert len(selected) >= 1

    note_files = [
        f for f in storage.list_files("sources/notes/") if f.endswith(".json")
    ]
    assert len(note_files) >= 1

    # -- Essay --
    draft = storage.read_text("essay/draft.md")
    assert "[[smith2024]]" in draft

    reviewed = storage.read_text("essay/reviewed.md")
    assert "[[smith2024]]" in reviewed

    # -- DOCX export --
    assert storage.exists("essay.docx")

    # -- Checkpoint covers all steps --
    checkpoint = json.loads(storage.read_text("checkpoint.json"))
    expected_steps = {
        "intake",
        "validate",
        "plan",
        "research",
        "read_sources",
        "write",
        "review",
        "export",
    }
    assert expected_steps.issubset(set(checkpoint["completed"]))
