"""Crossref metadata normalisation."""

from src.tools import crossref_search
from src.tools.crossref_search import _strip_inline_markup


def test_strip_inline_markup_removes_jats_scp() -> None:
    raw = (
        "Canadian Network (<scp>CANMAT</scp>) and International (<scp>ISBD</scp>) "
        "2018 guidelines"
    )
    assert _strip_inline_markup(raw) == (
        "Canadian Network (CANMAT) and International (ISBD) 2018 guidelines"
    )


def test_strip_inline_markup_plain_text_unchanged() -> None:
    assert _strip_inline_markup("Plain title") == "Plain title"


def test_strip_inline_markup_empty() -> None:
    assert _strip_inline_markup("") == ""


def test_search_crossref_requests_abstract_filter(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Response:
        def json(self) -> dict:
            return {"message": {"items": []}}

    def _fake_http_get(url: str, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr(crossref_search, "http_get", _fake_http_get)

    results, raw = crossref_search.search_crossref("climate policy", max_results=7)

    assert results == []
    assert raw == {"message": {"items": []}}
    assert captured["url"] == "https://api.crossref.org/works"
    assert captured["params"] == {
        "query": "climate policy",
        "filter": "has-abstract:true",
        "rows": 7,
        "mailto": crossref_search.DEFAULT_MAILTO,
        "select": "title,author,published,abstract,DOI,URL,type,is-referenced-by-count",
    }
    assert captured["request_name"] == "Crossref"


def test_search_crossref_can_prefer_fulltext(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Response:
        def json(self) -> dict:
            return {"message": {"items": []}}

    def _fake_http_get(url: str, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr(crossref_search, "http_get", _fake_http_get)

    crossref_search.search_crossref(
        "climate policy", max_results=7, prefer_fulltext=True
    )

    assert captured["params"]["filter"] == "has-abstract:true,has-full-text:true"
