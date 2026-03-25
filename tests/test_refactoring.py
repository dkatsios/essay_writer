"""Tests for refactored modules."""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ── subagents ─────────────────────────────────────────────────────────────


class TestSubagentFactories:
    """Tests for the worker and writer subagent factories."""

    @pytest.fixture()
    def config(self):
        from config.schemas import EssayWriterConfig

        return EssayWriterConfig()

    def test_worker_returns_valid_subagent(self, config):
        from src.subagents import make_worker

        agent = make_worker(config, tools=[])
        assert agent["name"] == "worker"
        assert "description" in agent
        assert "system_prompt" in agent
        assert "model" in agent
        assert "skills" in agent
        assert "tools" in agent

    def test_writer_returns_valid_subagent(self, config):
        from src.subagents import make_writer

        agent = make_writer(config, tools=[])
        assert agent["name"] == "writer"
        assert "description" in agent
        assert "system_prompt" in agent
        assert "model" in agent
        assert "skills" in agent
        assert "tools" in agent

    def test_tools_passed_through(self, config):
        from src.subagents import make_worker

        tools = ["tool1", "tool2"]
        agent = make_worker(config, tools=tools)
        assert agent["tools"] == tools

    def test_worker_uses_worker_model(self, config):
        from src.subagents import make_worker

        agent = make_worker(config, tools=[])
        assert agent["model"] == config.models.worker

    def test_writer_uses_writer_model(self, config):
        from src.subagents import make_writer

        agent = make_writer(config, tools=[])
        assert agent["model"] == config.models.writer


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

        # All templates require config; just verify it doesn't crash
        from config.schemas import EssayWriterConfig

        config = EssayWriterConfig()
        result = render_prompt("assistant.j2", config=config)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_cached_env_is_same_object(self):
        from src.rendering import _get_env

        _get_env.cache_clear()
        env1 = _get_env("/tmp/dummy")
        env2 = _get_env("/tmp/dummy")
        assert env1 is env2


# ── dump_vfs ──────────────────────────────────────────────────────────────


class TestDumpVfs:
    def _mock_agent(self, files: dict):
        """Create a mock agent whose get_state returns the given files."""
        agent = MagicMock()
        state = MagicMock()
        state.values = {"files": files}
        agent.get_state.return_value = state
        return agent

    def test_writes_files_and_skips_skills(self, tmp_path):
        from src.runner import dump_vfs

        files = {
            "/essay/draft.md": {"content": ["hello", "world"]},
            "/skills/section-writing/SKILL.md": {"content": ["skip me"]},
        }
        dump_vfs(self._mock_agent(files), "t1", tmp_path)
        assert (tmp_path / "vfs" / "essay" / "draft.md").read_text() == "hello\nworld"
        assert not (tmp_path / "vfs" / "skills").exists()

    def test_empty_files_warns(self, tmp_path, caplog):
        from src.runner import dump_vfs

        with caplog.at_level(logging.WARNING):
            dump_vfs(self._mock_agent({}), "t1", tmp_path)
        assert "No VFS files" in caplog.text
