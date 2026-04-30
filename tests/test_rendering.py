"""Tests for src/rendering.py — prompt rendering and template environment caching."""

from __future__ import annotations


class TestRendering:
    def test_render_prompt_returns_prompt_pair(self):
        from src.rendering import render_prompt, PromptPair

        # Test new per-task templates
        result = render_prompt(
            "intake.j2", extracted_text="Test content", extra_prompt=None
        )
        assert isinstance(result, PromptPair)
        assert result.system is not None
        assert len(result.user) > 0

        result = render_prompt(
            "validate.j2", brief_json='{"topic": "test"}', language="English"
        )
        assert isinstance(result, PromptPair)
        assert result.system is not None
        assert len(result.user) > 0

        result = render_prompt(
            "source_triage.j2",
            essay_topic="AI in higher education",
            thesis="Test thesis",
            sections=[],
            sources=[{"source_id": "s1", "title": "Paper", "abstract": "Abstract"}],
        )
        assert isinstance(result, PromptPair)
        assert result.system is not None
        assert len(result.user) > 0

    def test_cached_env_is_same_object(self):
        from src.rendering import get_env

        get_env.cache_clear()
        env1 = get_env("/tmp/dummy")
        env2 = get_env("/tmp/dummy")
        assert env1 is env2

    def test_writer_and_reviewer_templates_split_style_responsibilities(self):
        from src.rendering import render_prompt

        writer_prompt = render_prompt(
            "essay_writing.j2",
            brief_json="{}",
            plan_json="{}",
            source_notes=[],
            source_catalog="- s1",
            total_selected_sources=1,
            target_words=1000,
            tolerance_percent=10,
            min_words=900,
            language="English",
            min_sources=1,
        )
        assert writer_prompt.system is not None
        assert "Argument-led prose" in writer_prompt.system
        assert "Paragraph progression" in writer_prompt.system
        assert "Paragraph openings" not in writer_prompt.system

        reviewer_prompt = render_prompt(
            "essay_review.j2",
            brief_json="{}",
            plan_json="{}",
            draft_text="# Title\n\nBody text.",
            target_words=1000,
            draft_words=950,
            tolerance_ratio=0.1,
            tolerance_percent=10,
            tolerance_ratio_over=0.2,
            tolerance_percent_over=20,
            language="English",
            min_sources=1,
        )
        assert reviewer_prompt.system is not None
        assert "Active style cleanup" in reviewer_prompt.system
        assert "Essay-about-the-essay scaffolding" in reviewer_prompt.system
        assert "Paraphrastic repetition" in reviewer_prompt.system
        assert "Repeated abstract sentence stems" in reviewer_prompt.system
