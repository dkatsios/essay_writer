"""Tests for refactored modules."""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ── _ExecutionContext ──────────────────────────────────────────────────────


class TestExecutionContext:
    """Tests for the _ExecutionContext helper in runner.py."""

    def test_none_output_dir_gives_defaults(self):
        from src.runner import _ExecutionContext

        ctx = _ExecutionContext(None)
        assert ctx.thread_id == "default"
        assert ctx.checkpointer is None
        assert ctx.log_handler is None

    def test_teardown_noop_when_no_handler(self):
        from src.runner import _ExecutionContext

        ctx = _ExecutionContext(None)
        ctx.teardown()  # should not raise

    def test_output_dir_creates_checkpointer(self, tmp_path):
        from src.runner import _ExecutionContext

        run_dir = tmp_path / "run_20260321_145536"
        run_dir.mkdir()
        ctx = _ExecutionContext(run_dir)

        assert ctx.thread_id == "20260321_145536"
        assert ctx.checkpointer is not None
        assert ctx.log_handler is not None
        assert (run_dir / "checkpoints.db").exists()
        assert (run_dir / "run.log").exists()
        ctx.teardown()

    def test_require_checkpoint_db_raises_when_missing(self, tmp_path):
        from src.runner import _ExecutionContext

        run_dir = tmp_path / "run_20260321_145536"
        run_dir.mkdir()
        with pytest.raises(FileNotFoundError, match="No checkpoint DB"):
            _ExecutionContext(run_dir, require_checkpoint_db=True)

    def test_require_checkpoint_db_succeeds_when_present(self, tmp_path):
        from src.runner import _ExecutionContext

        run_dir = tmp_path / "run_20260321_145536"
        run_dir.mkdir()
        (run_dir / "checkpoints.db").touch()  # pre-create
        ctx = _ExecutionContext(run_dir, require_checkpoint_db=True)
        assert ctx.checkpointer is not None
        ctx.teardown()

    def test_teardown_removes_handler(self, tmp_path):
        from src.runner import _ExecutionContext

        run_dir = tmp_path / "run_20260321_145536"
        run_dir.mkdir()
        ctx = _ExecutionContext(run_dir)

        root = logging.getLogger()
        assert ctx.log_handler in root.handlers
        ctx.teardown()
        assert ctx.log_handler not in root.handlers


# ── subagents ─────────────────────────────────────────────────────────────


class TestMakeSubagent:
    """Tests for the data-driven subagent factory."""

    @pytest.fixture()
    def config(self):
        from config.schemas import EssayWriterConfig

        return EssayWriterConfig()

    def test_all_names_are_valid(self, config):
        from src.subagents import _SUBAGENT_SPECS, make_subagent

        for name, *_ in _SUBAGENT_SPECS:
            agent = make_subagent(name, config, tools=[])
            assert agent["name"] == name
            assert "description" in agent
            assert "system_prompt" in agent
            assert "model" in agent

    def test_unknown_name_raises(self, config):
        from src.subagents import make_subagent

        with pytest.raises(ValueError, match="Unknown subagent"):
            make_subagent("nonexistent", config, tools=[])

    def test_skills_key_present_only_when_expected(self, config):
        from src.subagents import _SUBAGENT_SPECS, make_subagent

        for name, _, _, _, has_skills in _SUBAGENT_SPECS:
            agent = make_subagent(name, config, tools=[])
            if has_skills:
                assert "skills" in agent
            else:
                assert "skills" not in agent

    def test_convenience_aliases_match(self, config):
        from src.subagents import make_intake, make_reader, make_reviewer, make_subagent

        assert make_intake(config, []) == make_subagent("intake", config, [])
        assert make_reader(config, []) == make_subagent("reader", config, [])
        assert make_reviewer(config, []) == make_subagent("reviewer", config, [])


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
        result = render_prompt("intake.j2", config=config)
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
