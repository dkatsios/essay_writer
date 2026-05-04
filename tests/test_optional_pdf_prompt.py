"""Tests for optional PDF prompt ranking and payload building."""

from __future__ import annotations

from src.pipeline_sources import (
    _build_optional_pdf_prompt_payload,
    _lexical_relevance_score,
    _optional_pdf_corpus_tokens,
)
from src.schemas import SourceNote


def test_lexical_relevance_prefers_overlap() -> None:
    corpus = {"climate", "policy", "carbon"}
    a = _lexical_relevance_score(corpus, "Climate policy in the EU", "")
    b = _lexical_relevance_score(corpus, "Unrelated sports news", "")
    assert a > b


def test_optional_pdf_payload_orders_by_lex_then_citations() -> None:
    corpus = {"neural", "network", "learning"}
    registry = {
        "s_low": {
            "title": "Sports and games",
            "abstract": "We study football statistics over decades.",
            "citation_count": 9999,
            "doi": "10.1/lo",
            "url": "https://example.com/low",
            "pdf_url": "https://example.com/low.pdf",
            "user_provided": False,
        },
        "s_high": {
            "title": "Neural networks for deep learning",
            "abstract": "We propose neural network architectures for representation learning.",
            "citation_count": 10,
            "doi": "10.1/hi",
            "url": "https://example.com/high",
            "pdf_url": "https://example.com/high.pdf",
            "user_provided": False,
        },
    }
    results: list[tuple[str, SourceNote | None]] = [
        (
            "s_low",
            SourceNote(
                source_id="s_low",
                is_accessible=True,
                fetched_fulltext=False,
                relevance_score=4,
            ),
        ),
        (
            "s_high",
            SourceNote(
                source_id="s_high",
                is_accessible=True,
                fetched_fulltext=False,
                relevance_score=2,
            ),
        ),
    ]
    items, sids = _build_optional_pdf_prompt_payload(
        results, registry, {"s_low", "s_high"}, corpus, top_n=5
    )
    assert sids[0] == "s_high"
    assert items[0]["source_id"] == "s_high"
    assert items[0]["pdf_url"] == "https://example.com/high.pdf"
    assert items[0]["article_url"] == "https://doi.org/10.1/hi"


def test_optional_pdf_payload_derives_article_url_from_pdf_url() -> None:
    corpus = {"covid"}
    registry = {
        "s1": {
            "title": "COVID paper",
            "abstract": "COVID analysis.",
            "citation_count": 1,
            "doi": "",
            "url": "https://onlinelibrary.wiley.com/doi/pdfdirect/10.1002/test",
            "pdf_url": "https://onlinelibrary.wiley.com/doi/pdfdirect/10.1002/test",
            "user_provided": False,
        },
    }
    results = [
        (
            "s1",
            SourceNote(
                source_id="s1",
                is_accessible=True,
                fetched_fulltext=False,
                relevance_score=2,
            ),
        ),
    ]

    items, _ = _build_optional_pdf_prompt_payload(
        results, registry, {"s1"}, corpus, top_n=5
    )

    assert (
        items[0]["pdf_url"]
        == "https://onlinelibrary.wiley.com/doi/pdfdirect/10.1002/test"
    )
    assert items[0]["article_url"] == "https://onlinelibrary.wiley.com/doi/10.1002/test"


def test_optional_pdf_excludes_user_provided_and_fulltext() -> None:
    corpus = set()
    registry = {
        "u1": {
            "title": "User doc",
            "abstract": "",
            "user_provided": True,
            "citation_count": 0,
        },
        "a1": {
            "title": "API with PDF",
            "abstract": "",
            "user_provided": False,
            "citation_count": 5,
        },
    }
    results = [
        (
            "u1",
            SourceNote(
                source_id="u1",
                is_accessible=True,
                fetched_fulltext=False,
            ),
        ),
        (
            "a1",
            SourceNote(
                source_id="a1",
                is_accessible=True,
                fetched_fulltext=True,
            ),
        ),
    ]
    items, _ = _build_optional_pdf_prompt_payload(
        results, registry, {"u1", "a1"}, corpus, top_n=5
    )
    assert items == []


def test_optional_pdf_corpus_reads_brief_and_plan() -> None:
    from src.storage import MemoryRunStorage

    storage = MemoryRunStorage("test/")
    storage.write_text(
        "brief/assignment.json",
        '{"topic": "quantum computing ethics"}',
    )
    storage.write_text(
        "plan/plan.json",
        '{"title": "Plan", "thesis": "Privacy matters", '
        '"sections": [{"title": "Quantum threats"}], '
        '"research_queries": ["post-quantum crypto"]}',
    )
    t = _optional_pdf_corpus_tokens(storage)
    assert "quantum" in t
    assert "privacy" in t
    assert "crypto" in t
