"""OpenAlex search request shaping."""

from src.tools import openalex_search


def test_search_openalex_requests_abstract_filter(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Response:
        def json(self) -> dict:
            return {"results": []}

    def _fake_http_get(url: str, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr(openalex_search, "http_get", _fake_http_get)

    results, raw = openalex_search.search_openalex("climate policy", max_results=7)

    assert results == []
    assert raw == {"results": []}
    assert captured["url"] == "https://api.openalex.org/works"
    assert captured["params"] == {
        "search": "climate policy",
        "filter": "has_abstract:true",
        "per_page": 7,
        "mailto": openalex_search.DEFAULT_MAILTO,
        "sort": "relevance_score:desc",
    }
    assert captured["request_name"] == "OpenAlex"
