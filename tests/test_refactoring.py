"""Tests for the essay writer pipeline modules."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest


# ── agent retry logic ─────────────────────────────────────────────────


class TestInvokeWithRetry:
    """Tests for invoke_with_retry in agent.py."""

    def test_immediate_success(self):
        from src.agent import invoke_with_retry

        model = MagicMock()
        model.invoke.return_value = MagicMock(content="hello")
        result = invoke_with_retry(model, ["test"])
        assert result.content == "hello"
        model.invoke.assert_called_once()

    def test_retries_on_resource_exhausted(self):
        from src.agent import invoke_with_retry

        model = MagicMock()
        model.invoke.side_effect = [
            Exception("429 RESOURCE_EXHAUSTED"),
            MagicMock(content="ok"),
        ]
        # Patch sleep to avoid waiting
        import src.agent

        original_sleep = src.agent.time.sleep
        src.agent.time.sleep = lambda _: None
        try:
            result = invoke_with_retry(model, ["test"])
            assert result.content == "ok"
            assert model.invoke.call_count == 2
        finally:
            src.agent.time.sleep = original_sleep


# ── web_fetcher HTML stripping ────────────────────────────────────────────


class TestHtmlToText:
    def test_strips_tags(self):
        from src.tools.web_fetcher import _html_to_text

        assert _html_to_text("<p>Hello <b>world</b></p>") == "Hello world"

    def test_skips_script_and_style(self):
        from src.tools.web_fetcher import _html_to_text

        html = "<div>before</div><script>var x = 1;</script><div>after</div>"
        text = _html_to_text(html)
        assert "var x" not in text
        assert "before" in text
        assert "after" in text

    def test_collapses_whitespace(self):
        from src.tools.web_fetcher import _html_to_text

        html = "<p>a</p>" + "<br>" * 10 + "<p>b</p>"
        text = _html_to_text(html)
        assert "\n\n\n" not in text

    def test_handles_attributes_with_angle_brackets(self):
        from src.tools.web_fetcher import _html_to_text

        html = '<div title="a > b">content</div>'
        text = _html_to_text(html)
        assert "content" in text


# ── search error response ────────────────────────────────────────────────


class TestSearchErrorResponse:
    def test_format(self):
        import json

        from src.tools._http import search_error_response

        result = json.loads(
            search_error_response("crossref", "test query", ValueError("oops"))
        )
        assert result["error"] == "request_failed"
        assert result["source"] == "crossref"
        assert result["query"] == "test query"
        assert "oops" in result["message"]


class _FakeResponse:
    def __init__(self, status_code=200, text="ok", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.content = text.encode("utf-8")

    @property
    def is_error(self):
        return self.status_code >= 400

    def raise_for_status(self):
        if self.is_error:
            request = httpx.Request("GET", "https://example.com")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("boom", request=request, response=response)


class TestSharedHttp:
    def test_http_get_retries_request_errors(self, monkeypatch):
        from src.tools import _http

        calls = {"count": 0}

        class FakeClient:
            def get(self, *args, **kwargs):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise httpx.ConnectError(
                        "nope", request=httpx.Request("GET", "https://example.com")
                    )
                return _FakeResponse()

        monkeypatch.setattr(_http, "get_http_client", lambda: FakeClient())
        monkeypatch.setattr(_http.time, "sleep", lambda _: None)

        response = _http.http_get(
            "https://example.com", max_retries=1, request_name="test"
        )

        assert response.status_code == 200
        assert calls["count"] == 2

    def test_http_get_retries_retryable_statuses(self, monkeypatch):
        from src.tools import _http

        responses = [_FakeResponse(status_code=503), _FakeResponse(status_code=200)]

        class FakeClient:
            def get(self, *args, **kwargs):
                return responses.pop(0)

        monkeypatch.setattr(_http, "get_http_client", lambda: FakeClient())
        monkeypatch.setattr(_http.time, "sleep", lambda _: None)

        response = _http.http_get(
            "https://example.com", max_retries=1, request_name="test"
        )

        assert response.status_code == 200


# ── intake classify ──────────────────────────────────────────────────────


class TestClassify:
    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("doc.pdf", "pdf"),
            ("doc.docx", "docx"),
            ("doc.pptx", "pptx"),
            ("image.png", "image"),
            ("image.jpg", "image"),
            ("notes.txt", "text"),
            ("notes.md", "text"),
            ("data.csv", "text"),
            ("file.xyz", "unsupported"),
        ],
    )
    def test_classify(self, filename, expected):
        from src.intake import _classify

        assert _classify(Path(filename)) == expected


# ── intake base64 helper ─────────────────────────────────────────────────


class TestMakeImageBlock:
    def test_produces_valid_block(self):
        from src.intake import _make_image_block

        block = _make_image_block(b"fake-png-data", "image/png")
        assert block["type"] == "image_url"
        url = block["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        # Verify the base64 decodes back
        encoded = url.split(",", 1)[1]
        assert base64.standard_b64decode(encoded) == b"fake-png-data"


class TestBuildExtractedText:
    def test_builds_extracted_text_with_prompt_and_warnings(self):
        from src.intake import InputFile, build_extracted_text

        files = [
            InputFile(Path("topic.txt"), "text", text="Essay topic"),
            InputFile(Path("scan.pdf"), "pdf", image_blocks=[{"type": "image_url"}]),
            InputFile(
                Path("legacy.doc"),
                "unsupported",
                warning="Old Word binary format — save as .docx first",
            ),
        ]

        extracted = build_extracted_text(files, extra_prompt="Focus on economics")

        assert "### File: topic.txt" in extracted
        assert "Essay topic" in extracted
        assert "### Image: scan.pdf" in extracted
        assert "text extraction was sparse" in extracted
        assert "## Warnings" in extracted
        assert "legacy.doc" in extracted
        assert "## Additional Instructions" in extracted
        assert "Focus on economics" in extracted


# ── docx extraction dedup ────────────────────────────────────────────────


class TestExtractDocxText:
    def test_extracts_headings_and_body(self):
        from src.tools.docx_reader import extract_docx_text

        from docx import Document

        doc = Document()
        doc.add_heading("Title", level=1)
        doc.add_paragraph("Body text here.")
        doc.add_heading("Sub", level=2)

        result = extract_docx_text(doc)
        assert "# Title" in result
        assert "Body text here." in result
        assert "## Sub" in result


# ── rendering cache ──────────────────────────────────────────────────────


class TestRendering:
    def test_render_prompt_returns_string(self):
        from src.rendering import render_prompt

        # Test new per-task templates
        result = render_prompt(
            "intake.j2", extracted_text="Test content", extra_prompt=None
        )
        assert isinstance(result, str)
        assert len(result) > 0

        result = render_prompt("validate.j2", brief_json='{"topic": "test"}')
        assert isinstance(result, str)
        assert len(result) > 0

    def test_cached_env_is_same_object(self):
        from src.rendering import _get_env

        _get_env.cache_clear()
        env1 = _get_env("/tmp/dummy")
        env2 = _get_env("/tmp/dummy")
        assert env1 is env2


# ── selected source filtering ─────────────────────────────────────────────


class TestSelectedSourceNotes:
    def test_uses_selected_accessible_notes_when_available(self, tmp_path):
        from src.pipeline import _load_selected_source_notes
        from src.schemas import SourceNote

        notes_dir = tmp_path / "sources" / "notes"
        notes_dir.mkdir(parents=True)

        note_a = SourceNote(source_id="alpha2024", is_accessible=True, title="A")
        note_b = SourceNote(source_id="beta2024", is_accessible=True, title="B")
        (notes_dir / "alpha2024.json").write_text(
            note_a.model_dump_json(), encoding="utf-8"
        )
        (notes_dir / "beta2024.json").write_text(
            note_b.model_dump_json(), encoding="utf-8"
        )

        (tmp_path / "sources" / "selected.json").write_text(
            json.dumps({"beta2024": {"title": "B"}}), encoding="utf-8"
        )

        notes = _load_selected_source_notes(tmp_path)
        assert [note.source_id for note in notes] == ["beta2024"]

    def test_falls_back_to_all_accessible_notes_when_selection_is_unusable(
        self, tmp_path, caplog
    ):
        from src.pipeline import _load_selected_source_notes
        from src.schemas import SourceNote

        notes_dir = tmp_path / "sources" / "notes"
        notes_dir.mkdir(parents=True)

        note_a = SourceNote(source_id="alpha2024", is_accessible=True, title="A")
        note_b = SourceNote(source_id="beta2024", is_accessible=True, title="B")
        (notes_dir / "alpha2024.json").write_text(
            note_a.model_dump_json(), encoding="utf-8"
        )
        (notes_dir / "beta2024.json").write_text(
            note_b.model_dump_json(), encoding="utf-8"
        )

        (tmp_path / "sources" / "selected.json").write_text(
            json.dumps({"missing2024": {"title": "Missing"}}), encoding="utf-8"
        )

        with caplog.at_level(logging.WARNING):
            notes = _load_selected_source_notes(tmp_path)

        assert [note.source_id for note in notes] == ["alpha2024", "beta2024"]
        assert "Selected sources had no accessible notes" in caplog.text


class TestLongEssayContextHelpers:
    def test_prior_section_context_uses_recent_sections_only(self):
        from src.pipeline import Section, _build_prior_sections_context

        sections = [
            (Section(number=1, title="One", heading="One", word_target=100), "intro"),
            (Section(number=2, title="Two", heading="Two", word_target=100), "body a"),
            (
                Section(number=3, title="Three", heading="Three", word_target=100),
                "body b",
            ),
        ]

        context = _build_prior_sections_context(sections, max_sections=2)

        assert "intro" not in context
        assert "body a" in context
        assert "body b" in context

    def test_review_context_uses_only_adjacent_sections(self):
        from src.pipeline import Section, _build_review_context

        sections = [
            Section(number=1, title="One", heading="One", word_target=100),
            Section(number=2, title="Two", heading="Two", word_target=100),
            Section(number=3, title="Three", heading="Three", word_target=100),
            Section(number=4, title="Four", heading="Four", word_target=100),
            Section(number=5, title="Five", heading="Five", word_target=100),
        ]
        section_texts = {
            2: "section two",
            3: "section three",
            4: "section four",
        }

        context = _build_review_context(sections[2], sections, section_texts)

        assert "section two" in context
        assert "section three" in context
        assert "section four" in context
        assert "SECTION TO REVIEW: START" in context
        assert "SECTION TO REVIEW: END" in context
        assert "section one" not in context
        assert "section five" not in context
