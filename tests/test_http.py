"""Tests for src/tools/_http.py — error responses, HTTP client, proxy sessions, PDF fetch."""

from __future__ import annotations

import json

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


class TestSearchErrorResponse:
    def test_format(self):
        from src.tools._http import search_error_response

        result = json.loads(
            search_error_response("crossref", "test query", ValueError("oops"))
        )
        assert result["error"] == "request_failed"
        assert result["source"] == "crossref"
        assert result["query"] == "test query"
        assert "oops" in result["message"]


class TestSharedHttp:
    def test_http_get_retries_request_errors(self, monkeypatch):
        from src.tools import _http

        calls = {"count": 0}

        class FakeClient:
            def get(self, *args, **kwargs):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise httpx.ConnectError(
                        "nope", request=httpx.Request("GET", "https://example.com")
                    )
                return _FakeResponse()

        monkeypatch.setattr(_http, "get_http_client", lambda: FakeClient())
        monkeypatch.setattr(_http.time, "sleep", lambda _: None)

        response = _http.http_get(
            "https://example.com", max_retries=1, request_name="test"
        )

        assert response.status_code == 200
        assert calls["count"] == 2

    def test_http_get_retries_retryable_statuses(self, monkeypatch):
        from src.tools import _http

        responses = [_FakeResponse(status_code=503), _FakeResponse(status_code=200)]

        class FakeClient:
            def get(self, *args, **kwargs):
                return responses.pop(0)

        monkeypatch.setattr(_http, "get_http_client", lambda: FakeClient())
        monkeypatch.setattr(_http.time, "sleep", lambda _: None)

        response = _http.http_get(
            "https://example.com", max_retries=1, request_name="test"
        )

        assert response.status_code == 200


class TestProxySession:
    """Tests for ProxySession URL rewriting and auth detection."""

    class _FakeCurlResponse:
        def __init__(
            self,
            *,
            text: str = "",
            status_code: int = 200,
            url: str = "https://proxy.example/login",
        ):
            self.text = text
            self.status_code = status_code
            self.url = url
            self.headers = {}
            self.content = text.encode("utf-8")

    class _FakeCurlSession:
        def __init__(self, responses: list[object]):
            self._responses = list(responses)
            self.cookies: dict[str, str] = {}
            self.posts: list[dict[str, object]] = []

        def get(self, *_args, **_kwargs):
            return self._responses.pop(0)

        def post(self, url, data=None, allow_redirects=True, **_kwargs):
            self.posts.append(
                {
                    "url": url,
                    "data": dict(data or {}),
                    "allow_redirects": allow_redirects,
                }
            )
            self.cookies = {"ezproxy": "session-cookie"}
            return TestProxySession._FakeCurlResponse(
                text="welcome",
                status_code=200,
                url="https://proxy.example/menu",
            )

    def test_hostname_rewriting(self):
        from src.tools._http import ProxySession

        ps = ProxySession(proxy_prefix="https://login.proxy.eap.gr/login?url=")
        ps._uses_hostname_rewrite = True
        ps._proxy_base = "proxy.eap.gr"

        url = "https://link.springer.com/content/pdf/10.1007/test.pdf"
        rewritten = ps.rewrite_url(url)
        assert (
            rewritten
            == "https://link-springer-com.proxy.eap.gr/content/pdf/10.1007/test.pdf"
        )

    def test_hostname_rewriting_preserves_query_string(self):
        from src.tools._http import ProxySession

        ps = ProxySession(proxy_prefix="https://login.proxy.eap.gr/login?url=")
        ps._uses_hostname_rewrite = True
        ps._proxy_base = "proxy.eap.gr"

        url = "https://www.tandfonline.com/doi/pdf/10.1080/123?needAccess=true"
        rewritten = ps.rewrite_url(url)
        assert (
            rewritten
            == "https://www-tandfonline-com.proxy.eap.gr/doi/pdf/10.1080/123?needAccess=true"
        )

    def test_url_prefix_mode_fallback(self):
        from src.tools._http import ProxySession

        ps = ProxySession(proxy_prefix="https://proxy.uoa.gr/login?url=")
        ps._uses_hostname_rewrite = False

        url = "https://link.springer.com/content/pdf/test.pdf"
        rewritten = ps.rewrite_url(url)
        assert rewritten.startswith("https://proxy.uoa.gr/login?url=")
        assert "link.springer.com" in rewritten

    def test_skips_open_access_domains(self):
        from src.tools._http import ProxySession

        ps = ProxySession(proxy_prefix="https://login.proxy.eap.gr/login?url=")
        ps._uses_hostname_rewrite = True
        ps._proxy_base = "proxy.eap.gr"

        for oa_url in [
            "https://arxiv.org/pdf/2301.00001.pdf",
            "https://www.mdpi.com/2071-1050/12/1/1/pdf",
            "https://zenodo.org/record/123/files/paper.pdf",
            "https://journals.plos.org/plosone/article/file?id=10.1371",
        ]:
            assert ps.rewrite_url(oa_url) == oa_url, f"Should skip {oa_url}"

    def test_no_prefix_returns_original(self):
        from src.tools._http import ProxySession

        ps = ProxySession(proxy_prefix="")
        assert (
            ps.rewrite_url("https://example.com/test.pdf")
            == "https://example.com/test.pdf"
        )

    def test_authenticate_returns_true_when_no_creds(self):
        from src.tools._http import ProxySession

        ps = ProxySession(proxy_prefix="https://proxy.uoa.gr/login?url=")
        assert ps.authenticate() is True
        assert ps._uses_hostname_rewrite is False

    def test_authenticate_simple_ezproxy_form_posts_credentials(self):
        from src.tools._http import ProxySession

        login_page = self._FakeCurlResponse(
            text=(
                '<form action="/login">'
                '<input type="hidden" name="url" value="https://target.example/file.pdf">'
                '<input type="text" name="user">'
                '<input type="password" name="pass">'
                "</form>"
            ),
            url="https://proxy.example/login?url=https://www.jstor.org",
        )
        fake_session = self._FakeCurlSession([login_page])
        ps = ProxySession(
            proxy_prefix="https://proxy.example/login?url=",
            username="student",
            password="secret",
        )
        ps._ensure_session = lambda: fake_session

        assert ps.authenticate() is True
        assert ps._authenticated is True
        assert ps._uses_hostname_rewrite is False
        assert fake_session.posts == [
            {
                "url": "https://proxy.example/login",
                "data": {
                    "url": "https://target.example/file.pdf",
                    "user": "student",
                    "pass": "secret",
                },
                "allow_redirects": True,
            }
        ]


class TestParallelPdfFetch:
    """Tests for the parallel direct+proxy PDF fetch logic."""

    def test_is_pdf_content(self):
        from src.tools._http import is_pdf_content

        assert is_pdf_content(b"%PDF-1.4 ...") is True
        assert is_pdf_content(b"<html>not a pdf</html>") is False
        assert is_pdf_content(b"") is False

    def test_pick_best_prefers_pdf_over_non_pdf(self):
        from src.tools._http import PdfResponse, pick_best_pdf

        pdf_resp = PdfResponse(200, b"%PDF-1.4 content", {"content-type": "application/pdf"})
        html_resp = PdfResponse(200, b"<html>page</html>", {"content-type": "text/html"})

        # proxy PDF wins when direct returns HTML
        assert pick_best_pdf(html_resp, pdf_resp, "http://x") is pdf_resp
        # direct PDF wins when proxy returns HTML
        assert pick_best_pdf(pdf_resp, html_resp, "http://x") is pdf_resp

    def test_pick_best_prefers_direct_when_both_pdf(self):
        from src.tools._http import PdfResponse, pick_best_pdf

        direct = PdfResponse(200, b"%PDF-1.4 direct", {})
        proxy = PdfResponse(200, b"%PDF-1.4 proxy", {})
        assert pick_best_pdf(direct, proxy, "http://x") is direct

    def test_pick_best_prefers_2xx_over_error(self):
        from src.tools._http import PdfResponse, pick_best_pdf

        ok = PdfResponse(200, b"<html>", {})
        err = PdfResponse(403, b"Forbidden", {})
        assert pick_best_pdf(err, ok, "http://x") is ok
        assert pick_best_pdf(ok, err, "http://x") is ok

    def test_pick_best_handles_none(self):
        from src.tools._http import PdfResponse, pick_best_pdf

        resp = PdfResponse(200, b"%PDF-1.4", {})
        assert pick_best_pdf(resp, None, "http://x") is resp
        assert pick_best_pdf(None, resp, "http://x") is resp

    def test_pdf_get_uses_authenticated_session_for_url_prefix_mode(self, monkeypatch):
        from src.tools import _http

        proxy = _http.ProxySettings(
            proxy_prefix="https://proxy.example/login?url=",
            username="student",
            password="secret",
        )
        session = _http.ProxySession(
            proxy_prefix=proxy.proxy_prefix,
            username=proxy.username,
            password=proxy.password,
        )
        session._authenticated = True
        session._uses_hostname_rewrite = False

        calls: list[dict[str, object]] = []

        def fake_fetch(fetch_url, *, session=None, label="", **_kwargs):
            calls.append({"url": fetch_url, "session": session, "label": label})
            if label == "proxy":
                return _http.PdfResponse(200, b"%PDF-1.4 proxied", {})
            return _http.PdfResponse(403, b"forbidden", {})

        monkeypatch.setattr(_http, "_get_proxy_session", lambda value: session)
        monkeypatch.setattr(_http, "_pdf_fetch_one", fake_fetch)

        result = _http.pdf_get("https://link.springer.com/test.pdf", proxy=proxy)

        assert result.content == b"%PDF-1.4 proxied"
        assert any(
            call["label"] == "proxy" and call["session"] is session
            for call in calls
        )
