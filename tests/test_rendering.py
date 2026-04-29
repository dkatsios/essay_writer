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
