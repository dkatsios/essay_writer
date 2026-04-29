"""Tests for src/tools/research_sources.py — concurrency and config-backed behavior."""

from __future__ import annotations

import threading
import time

import httpx


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


class TestResearchConcurrency:
    def test_query_worker_count_is_bounded(self):
        from src.tools.research_sources import query_worker_count

        assert query_worker_count(0) == 1
        assert query_worker_count(1) == 1
        assert query_worker_count(2) == 2
        assert query_worker_count(10) == 3

    def test_run_queries_parallelizes_but_preserves_query_order(self, monkeypatch):
        from src.tools import research_sources

        active = 0
        max_active = 0
        lock = threading.Lock()

        def fake_search_one_query(query, max_per_api, *, prefer_fulltext=False):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                if query == "q1":
                    time.sleep(0.03)
                else:
                    time.sleep(0.01)
            finally:
                with lock:
                    active -= 1

            return ([{"title": query}], {"crossref": {"query": query}})

        monkeypatch.setattr(
            research_sources, "_search_one_query", fake_search_one_query
        )

        all_results, all_raw = research_sources._run_queries(["q1", "q2", "q3"], 2)

        assert max_active > 1
        assert [item["title"] for item in all_results] == ["q1", "q2", "q3"]
        assert [item["query"] for item in all_raw] == ["q1", "q2", "q3"]


class TestConfigBackedBehavior:
    def test_citation_rank_sorts_higher_citations_first(self):
        from src.tools.research_sources import build_registry

        raw_results = [
            {
                "title": "Low citations",
                "authors": ["A Smith"],
                "year": 2024,
                "abstract": "",
                "doi": "10.1/low",
                "url": "https://example.com/low",
                "pdf_url": "https://example.com/low.pdf",
                "source_type": "",
                "citation_count": 5,
            },
            {
                "title": "High citations",
                "authors": ["B Jones"],
                "year": 2024,
                "abstract": "",
                "doi": "10.1/high",
                "url": "https://example.com/high",
                "pdf_url": "https://example.com/high.pdf",
                "source_type": "",
                "citation_count": 500,
            },
        ]
        registry = build_registry(raw_results, 10)
        ids = list(registry.keys())
        assert len(ids) == 2
        # Higher citations should come first (both have same accessibility)
        assert registry[ids[0]]["title"] == "High citations"
        assert registry[ids[1]]["title"] == "Low citations"

    def test_build_registry_ranks_by_citations_then_accessibility(self):
        from src.tools.research_sources import build_registry

        raw_results = [
            {
                "title": "DOI only paper",
                "authors": ["Alice Smith"],
                "year": 2024,
                "abstract": "Some abstract",
                "doi": "10.1/a",
                "url": "",
                "pdf_url": "",
                "source_type": "journal-article",
                "citation_count": 100,
            },
            {
                "title": "OA PDF paper",
                "authors": ["Bob Jones"],
                "year": 2024,
                "abstract": "Another abstract",
                "doi": "10.1/b",
                "url": "https://example.com/b",
                "pdf_url": "https://example.com/b.pdf",
                "source_type": "journal-article",
                "citation_count": 10,
            },
        ]

        registry = build_registry(raw_results, 10)
        ids = list(registry.keys())
        assert len(ids) == 2
        # Higher citations should rank first; accessibility is tiebreaker
        assert registry[ids[0]]["title"] == "DOI only paper"

    def test_build_registry_keeps_full_deduplicated_candidate_pool(self):
        from src.tools.research_sources import build_registry

        raw_results = [
            {
                "title": f"Paper {index}",
                "authors": ["Alice Smith"],
                "year": 2024,
                "abstract": "Useful abstract",
                "doi": f"10.1/{index}",
                "url": f"https://example.com/{index}",
                "pdf_url": "",
                "source_type": "journal-article",
                "citation_count": index,
            }
            for index in range(12)
        ]

        registry = build_registry(raw_results, 5)

        assert len(registry) == 12

    def test_rendered_review_prompt_uses_configured_tolerance(self):
        from src.rendering import render_prompt
        from src.pipeline_support import Section

        prompt = render_prompt(
            "section_review.j2",
            section=Section(
                position=1,
                number=1,
                title="Intro",
                heading="Intro",
                word_target=100,
            ),
            full_essay="Body",
            section_words=96,
            tolerance_ratio=0.05,
            tolerance_percent=5,
            tolerance_ratio_over=0.20,
            tolerance_percent_over=20,
            language="English",
        )

        assert "at least" in prompt.user.lower()
