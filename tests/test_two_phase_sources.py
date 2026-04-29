"""Tests for two-phase source reading: filter, batch-score, select, extract."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from src.pipeline_sources import (
    _async_batch_triage_sources,
    _async_fetch_pdf_content,
    _build_borderline_source_list,
    _filter_scorable_sources,
    _metadata_pretrim_score,
    _pretrim_scorable_sources,
    _select_top_sources,
    _write_source_decision_artifacts,
)
from src.schemas import SourceScoreBatch


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
        result = _filter_scorable_sources(registry)
        assert len(result) == 1
        assert result[0]["source_id"] == "s1"
        assert result[0]["abstract"] != ""

    def test_drops_source_with_fulltext_but_no_abstract(self):
        registry = {
            "s2": {
                "title": "Paper B",
                "abstract": "short",
                "authors": [],
                "year": "2021",
                "doi": "",
            }
        }
        result = _filter_scorable_sources(registry)
        assert len(result) == 0

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
        result = _filter_scorable_sources(registry)
        assert len(result) == 0

    def test_keeps_source_with_both(self):
        abstract = " ".join(["abstract"] * 25)
        registry = {
            "s4": {
                "title": "Paper D",
                "abstract": abstract,
                "authors": ["Author"],
                "year": "2022",
                "doi": "10.1234/test",
            }
        }
        result = _filter_scorable_sources(registry)
        assert len(result) == 1
        assert result[0]["abstract"] == abstract

    def test_drops_source_with_no_authors(self):
        abstract = " ".join(["abstract"] * 25)
        registry = {
            "s5": {
                "title": "Paper E",
                "abstract": abstract,
                "authors": [],
                "year": "2022",
                "doi": "10.1234/test",
            }
        }
        result = _filter_scorable_sources(registry)
        assert len(result) == 0

    def test_drops_source_with_blank_authors(self):
        abstract = " ".join(["abstract"] * 25)
        registry = {
            "s6": {
                "title": "Paper F",
                "abstract": abstract,
                "authors": ["", "  "],
                "year": "2022",
                "doi": "10.1234/test",
            }
        }
        result = _filter_scorable_sources(registry)
        assert len(result) == 0


# -- _select_top_sources ----------------------------------------------------


class TestPretrimScorableSources:
    def test_caps_candidate_pool_to_target_multiplier(self):
        registry = {}
        scorable = []
        for index in range(11):
            source_id = f"s{index:02d}"
            registry[source_id] = {"citation_count": 0, "pdf_url": ""}
            scorable.append(
                {
                    "source_id": source_id,
                    "title": f"Cold war topic {index}",
                    "abstract": "brief abstract",
                }
            )

        result = _pretrim_scorable_sources(
            scorable,
            registry,
            {"cold", "war"},
            target_sources=2,
        )

        assert len(result) == 10
        assert [source["source_id"] for source in result] == [
            f"s{index:02d}" for index in range(10)
        ]

    def test_uses_abstract_overlap_when_title_is_generic(self):
        corpus = {"cold", "war", "collapse", "europe"}
        keep = {
            "source_id": "keep",
            "title": "Comparative study",
            "abstract": "Cold war collapse eastern europe transitions comparative politics",
        }
        drop = {
            "source_id": "drop",
            "title": "Comparative study",
            "abstract": "Marine biology coastal erosion fisheries ocean habitats",
        }

        keep_score = _metadata_pretrim_score(
            keep, {"citation_count": 0, "pdf_url": ""}, corpus
        )
        drop_score = _metadata_pretrim_score(
            drop, {"citation_count": 0, "pdf_url": ""}, corpus
        )

        assert keep_score > drop_score

    def test_direct_pdf_bonus_breaks_tie(self):
        corpus = {"cold", "war", "europe", "transitions"}
        source = {
            "source_id": "pdf",
            "title": "Cold war transitions",
            "abstract": "Eastern Europe comparative politics",
        }

        pdf_score = _metadata_pretrim_score(
            source,
            {"citation_count": 10, "pdf_url": "https://example.com/paper.pdf"},
            corpus,
        )
        metadata_only_score = _metadata_pretrim_score(
            source,
            {"citation_count": 10, "pdf_url": ""},
            corpus,
        )

        assert pdf_score > metadata_only_score


# -- _select_top_sources ----------------------------------------------------


class TestSelectTopSources:
    def test_filters_low_relevance(self):
        scores = {"s1": 4, "s2": 2, "s3": 3}
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

    def test_high_citations_compensate_lower_relevance(self):
        """A score-4 source with many citations should outrank a score-5 with zero."""
        scores = {"s1": 4, "s2": 5}
        registry = {
            "s1": {"authors": ["A"], "citation_count": 2000},
            "s2": {"authors": ["B"], "citation_count": 0},
        }
        result = _select_top_sources(scores, registry, 2, {})
        assert result[0] == "s1"  # citations compensate for 1-point gap


# -- _async_batch_triage_sources -------------------------------------------


class TestAsyncBatchTriageSources:
    def test_batches_and_scores_sources(self, monkeypatch):
        scorable = [
            {
                "source_id": "keep",
                "title": "AI in higher education",
                "abstract": "Useful",
            },
            {
                "source_id": "drop",
                "title": "Network intrusion detection",
                "abstract": "Security",
            },
        ]
        rendered_templates: list[str] = []

        def fake_render_prompt(template: str, **kwargs):
            rendered_templates.append(template)
            return "PROMPT"

        async def fake_async_structured_call(_worker, _prompt, schema, _tracker=None):
            assert schema is SourceScoreBatch
            return SourceScoreBatch(
                scores=[
                    {"source_id": "keep", "relevance_score": 5},
                    {"source_id": "drop", "relevance_score": 1},
                ]
            )

        monkeypatch.setattr("src.pipeline_sources.render_prompt", fake_render_prompt)
        monkeypatch.setattr(
            "src.pipeline_sources.async_structured_call", fake_async_structured_call
        )

        result = asyncio.run(
            _async_batch_triage_sources(
                scorable,
                "AI in Greek higher education",
                "Policy thesis",
                async_worker=SimpleNamespace(),
                batch_size=50,
            )
        )

        assert result == {"keep": 5, "drop": 1}
        assert rendered_templates == ["source_triage.j2"]

    def test_processes_multiple_batches_concurrently(self, monkeypatch):
        scorable = [
            {
                "source_id": "s1",
                "title": "AI in higher education",
                "abstract": "Useful",
            },
            {
                "source_id": "s2",
                "title": "AI policy",
                "abstract": "Useful",
            },
        ]
        started: list[str] = []
        both_started = asyncio.Event()

        def fake_render_prompt(template: str, **kwargs):
            assert template == "source_triage.j2"
            return kwargs["sources"][0]["source_id"]

        async def fake_async_structured_call(_worker, prompt, schema, _tracker=None):
            assert schema is SourceScoreBatch
            started.append(prompt)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=0.1)
            return SourceScoreBatch(
                scores=[{"source_id": prompt, "relevance_score": 4}]
            )

        monkeypatch.setattr("src.pipeline_sources.render_prompt", fake_render_prompt)
        monkeypatch.setattr(
            "src.pipeline_sources.async_structured_call", fake_async_structured_call
        )

        result = asyncio.run(
            _async_batch_triage_sources(
                scorable,
                "AI in Greek higher education",
                "Policy thesis",
                async_worker=SimpleNamespace(),
                batch_size=1,
            )
        )

        assert result == {"s1": 4, "s2": 4}
        assert sorted(started) == ["s1", "s2"]


# -- _write_source_decision_artifacts --------------------------------------


class TestWriteSourceDecisionArtifacts:
    def test_writes_score_artifacts(self, tmp_path):
        run_dir = tmp_path / "run"
        sources_dir = run_dir / "sources"
        sources_dir.mkdir(parents=True)
        registry = {
            "keep": {"title": "Relevant paper", "doi": "10.1/keep"},
            "drop": {"title": "Irrelevant paper", "doi": "10.1/drop"},
        }

        _write_source_decision_artifacts(
            run_dir,
            registry,
            {"keep": 5, "drop": 1},
            ["keep"],
            min_relevance_score=3,
        )

        scores = json.loads((sources_dir / "scores.json").read_text(encoding="utf-8"))

        assert scores["min_relevance_score"] == 3
        assert scores["scores"]["keep"]["relevance_score"] == 5
        assert scores["scores"]["keep"]["selected_for_writing"] is True
        assert scores["scores"]["drop"]["relevance_score"] == 1
        assert scores["scores"]["drop"]["selected_for_writing"] is False


# -- _async_fetch_pdf_content -----------------------------------------------


class TestAsyncFetchPdfContent:
    def test_user_provided_loads_content_path(self, tmp_path):
        content_file = tmp_path / "source.txt"
        content_file.write_text("This is user content.", encoding="utf-8")
        meta = {"user_provided": True, "content_path": str(content_file)}
        sid, content, did_fail = asyncio.run(
            _async_fetch_pdf_content("user_001", meta, str(tmp_path))
        )
        assert sid == "user_001"
        assert "user content" in content
        assert did_fail is False

    def test_no_pdf_url_returns_empty(self):
        meta = {"url": "https://example.com/page", "pdf_url": ""}
        sid, content, did_fail = asyncio.run(
            _async_fetch_pdf_content("s1", meta, "/tmp/sources")
        )
        assert sid == "s1"
        assert content == ""
        assert did_fail is False

    def test_domain_throttled_returns_empty(self):
        from src.pipeline_sources import DomainFailureTracker

        tracker = DomainFailureTracker(max_failures=1)
        tracker.record_failure("https://example.com/paper.pdf")
        tracker.record_failure("https://example.com/paper.pdf")
        meta = {"pdf_url": "https://example.com/paper.pdf"}
        sid, content, did_fail = asyncio.run(
            _async_fetch_pdf_content("s1", meta, "/tmp/sources", domain_tracker=tracker)
        )
        assert sid == "s1"
        assert content == ""
        assert did_fail is False


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


# -- _build_borderline_source_list -------------------------------------------


class TestBuildBorderlineSourceList:
    def _registry(self, entries):
        """Build a registry dict from a list of (id, meta) tuples."""
        return {sid: meta for sid, meta in entries}

    def test_returns_score_2_sources(self):
        scores = {"a": 4, "b": 2, "c": 2, "d": 1}
        registry = {
            "a": {"title": "Above", "citation_count": 10, "authors": ["X"]},
            "b": {"title": "Border 1", "citation_count": 50, "authors": ["Y"]},
            "c": {"title": "Border 2", "citation_count": 20, "authors": ["Z"]},
            "d": {"title": "Below", "citation_count": 5, "authors": ["W"]},
        }
        result = _build_borderline_source_list(
            scores, registry, ["a"], min_relevance_score=3
        )
        assert len(result) == 2
        # Sorted by citations desc
        assert result[0]["source_id"] == "b"
        assert result[1]["source_id"] == "c"

    def test_excludes_already_selected(self):
        scores = {"a": 3, "b": 2}
        registry = {
            "a": {"title": "Selected", "citation_count": 10, "authors": ["X"]},
            "b": {"title": "Border", "citation_count": 5, "authors": ["Y"]},
        }
        result = _build_borderline_source_list(
            scores, registry, ["a", "b"], min_relevance_score=3
        )
        assert len(result) == 0

    def test_caps_at_30(self):
        scores = {f"s{i}": 2 for i in range(50)}
        registry = {
            f"s{i}": {"title": f"Paper {i}", "citation_count": i, "authors": [f"A{i}"]}
            for i in range(50)
        }
        result = _build_borderline_source_list(
            scores, registry, [], min_relevance_score=3
        )
        assert len(result) == 30

    def test_empty_when_no_borderline(self):
        scores = {"a": 4, "b": 3}
        registry = {
            "a": {"title": "A", "citation_count": 10, "authors": ["X"]},
            "b": {"title": "B", "citation_count": 5, "authors": ["Y"]},
        }
        result = _build_borderline_source_list(
            scores, registry, [], min_relevance_score=3
        )
        assert result == []

    def test_includes_metadata_fields(self):
        scores = {"x": 2}
        registry = {
            "x": {
                "title": "Important Paper",
                "citation_count": 42,
                "authors": ["John Smith", "Jane Doe"],
                "year": "2024",
                "pdf_url": "https://example.com/paper.pdf",
                "abstract": "This is a long abstract " * 20,
            },
        }
        result = _build_borderline_source_list(
            scores, registry, [], min_relevance_score=3
        )
        assert len(result) == 1
        item = result[0]
        assert item["source_id"] == "x"
        assert item["title"] == "Important Paper"
        assert item["first_author"] == "John Smith"
        assert item["year"] == "2024"
        assert item["citation_count"] == 42
        assert item["relevance_score"] == 2
        assert item["has_fulltext"] is True
        assert len(item["abstract"]) <= 300
