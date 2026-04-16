"""Tests for two-phase source reading: filter, batch-score, select, extract."""

from __future__ import annotations

import asyncio

from src.pipeline_sources import (
    _async_fetch_pdf_content,
    _content_snippet,
    _filter_scorable_sources,
    _select_top_sources,
)
from src.schemas import SourceScoreBatch


# -- _content_snippet -------------------------------------------------------


class TestContentSnippet:
    def test_short_text_returned_as_is(self):
        text = "This is a short text."
        assert _content_snippet(text, max_words=50) == text

    def test_long_text_truncated(self):
        words = ["word"] * 100
        result = _content_snippet(" ".join(words), max_words=10)
        assert result.endswith("…")
        # 10 words + " …"
        assert result.count("word") == 10

    def test_empty_string(self):
        assert _content_snippet("") == ""


# -- _filter_scorable_sources -----------------------------------------------


class TestFilterScorableSources:
    def test_keeps_source_with_abstract(self):
        registry = {
            "s1": {
                "title": "Paper A",
                "abstract": " ".join(["word"] * 25),
                "authors": ["Author A"],
                "year": "2020",
                "doi": "",
            }
        }
        result = _filter_scorable_sources(registry, {}, min_body_words=50)
        assert len(result) == 1
        assert result[0]["source_id"] == "s1"
        assert result[0]["abstract"] != ""
        assert result[0]["content_snippet"] == ""

    def test_keeps_source_with_fulltext_no_abstract(self):
        registry = {
            "s2": {
                "title": "Paper B",
                "abstract": "short",
                "authors": [],
                "year": "2021",
                "doi": "",
            }
        }
        body = " ".join(["content"] * 100)
        result = _filter_scorable_sources(registry, {"s2": body}, min_body_words=50)
        assert len(result) == 1
        assert result[0]["source_id"] == "s2"
        assert result[0]["content_snippet"] != ""
        assert result[0]["abstract"] == ""  # abstract was not useful

    def test_drops_source_with_neither(self):
        registry = {
            "s3": {
                "title": "Paper C",
                "abstract": "short",
                "authors": [],
                "year": "2019",
                "doi": "",
            }
        }
        result = _filter_scorable_sources(registry, {}, min_body_words=50)
        assert len(result) == 0

    def test_keeps_source_with_both(self):
        abstract = " ".join(["abstract"] * 25)
        body = " ".join(["body"] * 100)
        registry = {
            "s4": {
                "title": "Paper D",
                "abstract": abstract,
                "authors": ["Author"],
                "year": "2022",
                "doi": "10.1234/test",
            }
        }
        result = _filter_scorable_sources(registry, {"s4": body}, min_body_words=50)
        assert len(result) == 1
        assert result[0]["abstract"] == abstract
        assert result[0]["content_snippet"] == ""  # abstract is used, no snippet needed


# -- _select_top_sources ----------------------------------------------------


class TestSelectTopSources:
    def test_filters_low_relevance(self):
        scores = {"s1": 4, "s2": 1, "s3": 3}
        registry = {
            "s1": {"authors": ["A"], "citation_count": 10},
            "s2": {"authors": ["B"], "citation_count": 20},
            "s3": {"authors": ["C"], "citation_count": 5},
        }
        result = _select_top_sources(scores, registry, 10, {})
        assert "s2" not in result
        assert "s1" in result
        assert "s3" in result

    def test_respects_target(self):
        scores = {"s1": 5, "s2": 4, "s3": 3}
        registry = {
            "s1": {"authors": ["A"], "citation_count": 10},
            "s2": {"authors": ["B"], "citation_count": 20},
            "s3": {"authors": ["C"], "citation_count": 5},
        }
        result = _select_top_sources(scores, registry, 2, {})
        assert len(result) == 2

    def test_user_provided_first(self):
        scores = {"s1": 3, "s2": 5}
        registry = {
            "s1": {"authors": ["A"], "citation_count": 0, "user_provided": True},
            "s2": {"authors": ["B"], "citation_count": 100},
        }
        result = _select_top_sources(scores, registry, 2, {})
        assert result[0] == "s1"  # user-provided ranked first despite lower score

    def test_fulltext_tiebreaker(self):
        scores = {"s1": 4, "s2": 4}
        registry = {
            "s1": {"authors": ["A"], "citation_count": 10},
            "s2": {"authors": ["B"], "citation_count": 10},
        }
        body = " ".join(["content"] * 100)
        fetch_results = {"s1": body, "s2": ""}
        result = _select_top_sources(
            scores, registry, 2, fetch_results, min_body_words=50
        )
        assert result[0] == "s1"  # has fulltext → ranked higher


# -- _async_fetch_pdf_content -----------------------------------------------


class TestAsyncFetchPdfContent:
    def test_user_provided_loads_content_path(self, tmp_path):
        content_file = tmp_path / "source.txt"
        content_file.write_text("This is user content.", encoding="utf-8")
        meta = {"user_provided": True, "content_path": str(content_file)}
        sid, content = asyncio.run(
            _async_fetch_pdf_content("user_001", meta, str(tmp_path))
        )
        assert sid == "user_001"
        assert "user content" in content

    def test_no_pdf_url_returns_empty(self):
        meta = {"url": "https://example.com/page", "pdf_url": ""}
        sid, content = asyncio.run(_async_fetch_pdf_content("s1", meta, "/tmp/sources"))
        assert sid == "s1"
        assert content == ""

    def test_domain_throttled_returns_empty(self):
        from src.pipeline_sources import _DomainFailureTracker

        tracker = _DomainFailureTracker(max_failures=1)
        tracker.record_failure("https://example.com/paper.pdf")
        tracker.record_failure("https://example.com/paper.pdf")
        meta = {"pdf_url": "https://example.com/paper.pdf"}
        sid, content = asyncio.run(
            _async_fetch_pdf_content("s1", meta, "/tmp/sources", domain_tracker=tracker)
        )
        assert content == ""


# -- SourceScoreBatch schema ------------------------------------------------


class TestSourceScoreBatch:
    def test_parses_valid_batch(self):
        data = {
            "scores": [
                {"source_id": "s1", "relevance_score": 4},
                {"source_id": "s2", "relevance_score": 2},
            ]
        }
        batch = SourceScoreBatch.model_validate(data)
        assert len(batch.scores) == 2
        assert batch.scores[0].source_id == "s1"
        assert batch.scores[0].relevance_score == 4

    def test_handles_stringified_scores(self):
        data = {"scores": '[{"source_id": "s1", "relevance_score": 3}]'}
        batch = SourceScoreBatch.model_validate(data)
        assert len(batch.scores) == 1
