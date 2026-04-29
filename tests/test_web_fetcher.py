"""Tests for src/tools/web_fetcher.py — HTML stripping."""

from __future__ import annotations


class TestHtmlToText:
    def test_strips_tags(self):
        from src.tools.web_fetcher import html_to_text

        assert html_to_text("<p>Hello <b>world</b></p>") == "Hello world"

    def test_skips_script_and_style(self):
        from src.tools.web_fetcher import html_to_text

        html = "<div>before</div><script>var x = 1;</script><div>after</div>"
        text = html_to_text(html)
        assert "var x" not in text
        assert "before" in text
        assert "after" in text

    def test_collapses_whitespace(self):
        from src.tools.web_fetcher import html_to_text

        html = "<p>a</p>" + "<br>" * 10 + "<p>b</p>"
        text = html_to_text(html)
        assert "\n\n\n" not in text

    def test_handles_attributes_with_angle_brackets(self):
        from src.tools.web_fetcher import html_to_text

        html = '<div title="a > b">content</div>'
        text = html_to_text(html)
        assert "content" in text
