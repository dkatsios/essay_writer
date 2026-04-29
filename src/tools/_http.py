"""Shared HTTP utilities for tools."""

from __future__ import annotations

import html as html_mod
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import quote, urlparse

import httpx
from curl_cffi import requests as curl_requests

DEFAULT_MAILTO = "essay-writer@example.com"
DEFAULT_TIMEOUT = 30.0

logger = logging.getLogger(__name__)

_CLIENT_LOCK = threading.Lock()
_HTTP_CLIENT: httpx.Client | None = None


def _default_headers() -> dict[str, str]:
    return {"User-Agent": "essay-writer/0.1"}


def get_http_client() -> httpx.Client:
    """Return a shared HTTP client with connection pooling."""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        with _CLIENT_LOCK:
            if _HTTP_CLIENT is None:
                _HTTP_CLIENT = httpx.Client(
                    headers=_default_headers(),
                    limits=httpx.Limits(
                        max_connections=20,
                        max_keepalive_connections=10,
                    ),
                    verify=get_ssl_verify(),
                )
    return _HTTP_CLIENT


def http_get(
    url: str,
    *,
    params: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    follow_redirects: bool = False,
    max_retries: int = 0,
    initial_backoff: float = 1.0,
    retry_statuses: tuple[int, ...] = (429, 500, 502, 503, 504),
    request_name: str | None = None,
    log_retries: bool = True,
) -> httpx.Response:
    """Issue a GET request with shared transport and optional retries."""
    client = get_http_client()
    label = request_name or url
    delay = initial_backoff
    last_response: httpx.Response | None = None

    for attempt in range(max_retries + 1):
        try:
            response = client.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
                follow_redirects=follow_redirects,
            )
        except httpx.RequestError:
            if attempt < max_retries:
                if log_retries:
                    logger.warning(
                        "%s request failed (attempt %d/%d); retrying in %.1fs",
                        label,
                        attempt + 1,
                        max_retries + 1,
                        delay,
                    )
                time.sleep(delay)
                delay *= 2
                continue
            raise

        last_response = response
        if response.status_code in retry_statuses and attempt < max_retries:
            if log_retries:
                logger.warning(
                    "%s returned HTTP %d (attempt %d/%d); retrying in %.1fs",
                    label,
                    response.status_code,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                )
            time.sleep(delay)
            delay *= 2
            continue

        response.raise_for_status()
        return response

    if last_response is not None:
        last_response.raise_for_status()
    raise RuntimeError("http_get exhausted retries without a response")


# ---------------------------------------------------------------------------
# PDF fetching — curl_cffi with Chrome TLS fingerprint impersonation
# ---------------------------------------------------------------------------


@dataclass
class PdfResponse:
    """Minimal response wrapper for PDF fetch results."""

    status_code: int
    content: bytes
    headers: dict[str, str]


@dataclass(frozen=True)
class ProxySettings:
    """Proxy settings used for PDF fetches.

    Explicit instances are intended to come from runtime config. When omitted,
    ``pdf_get`` falls back to the configured defaults with environment overrides.
    """

    proxy_prefix: str = ""
    username: str = ""
    password: str = ""

    @classmethod
    def from_config(cls) -> ProxySettings:
        from config.schemas import load_config

        search = load_config().search
        return cls(
            proxy_prefix=search.proxy_prefix,
            username=search.proxy_username,
            password=search.proxy_password,
        )

    def with_prefix(self, proxy_prefix: str | None) -> ProxySettings:
        if proxy_prefix is None:
            return self
        return ProxySettings(
            proxy_prefix=proxy_prefix,
            username=self.username,
            password=self.password,
        )

    def has_proxy(self) -> bool:
        return bool(self.proxy_prefix)

    def has_credentials(self) -> bool:
        return bool(self.username and self.password)


_PROXY_DOMAINS_SKIP = frozenset(
    {
        "arxiv.org",
        "www.mdpi.com",
        "zenodo.org",
        "journals.plos.org",
    }
)
"""Open-access domains that never need proxy rewriting."""


# ---------------------------------------------------------------------------
# Authenticated proxy session (Shibboleth / EZProxy)
# ---------------------------------------------------------------------------


@dataclass
class _ProxySession:
    """Manages an authenticated curl_cffi session for institutional proxy access.

    Supports two EZProxy modes, auto-detected from the proxy login page:
    - **URL-prefix**: simple ``proxy_prefix + encoded_url`` rewriting (no auth page).
    - **Shibboleth/SAML hostname-rewriting**: SAML auth flow, then URLs are rewritten
      as ``https://<host-with-dashes>.proxy.domain/path``.
    """

    proxy_prefix: str = ""
    username: str = ""
    password: str = ""
    _session: curl_requests.Session | None = field(default=None, repr=False)
    _authenticated: bool = False
    _uses_hostname_rewrite: bool = False
    _proxy_base: str = ""  # e.g. "proxy.eap.gr"
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _ensure_session(self) -> curl_requests.Session:
        if self._session is None:
            self._session = curl_requests.Session(
                impersonate="chrome", verify=get_ssl_verify()
            )
        return self._session

    def _resolve_url(self, action: str, current_url: str) -> str:
        if action.startswith(("http://", "https://")):
            return html_mod.unescape(action)
        parsed = urlparse(current_url)
        if action.startswith("/"):
            return f"{parsed.scheme}://{parsed.netloc}{action}"
        base = current_url.rsplit("/", 1)[0]
        return f"{base}/{action}"

    def _extract_form_action(self, html: str, fallback_url: str) -> str:
        match = re.search(r'<form[^>]*action=["\']([^"\']+)', html, re.I)
        if not match:
            return fallback_url
        return self._resolve_url(match.group(1), fallback_url)

    def _extract_form_inputs(self, html: str) -> dict[str, str]:
        inputs: dict[str, str] = {}
        for match in re.finditer(r"<input\b([^>]*)>", html, re.I):
            attrs = match.group(1)
            name_match = re.search(r'name=["\']([^"\']+)', attrs, re.I)
            if not name_match:
                continue
            value_match = re.search(r'value=["\']([^"\']*)', attrs, re.I)
            inputs[name_match.group(1)] = value_match.group(1) if value_match else ""
        return inputs

    def _detect_credential_fields(self, html: str) -> tuple[str, str]:
        user_field = "user"
        pass_field = "pass"

        password_match = re.search(
            r'<input[^>]*type=["\']password["\'][^>]*name=["\']([^"\']+)',
            html,
            re.I,
        ) or re.search(
            r'<input[^>]*name=["\']([^"\']+)["\'][^>]*type=["\']password',
            html,
            re.I,
        )
        if password_match:
            pass_field = password_match.group(1)

        user_match = re.search(
            r'<input[^>]*type=["\'](?:text|email)["\'][^>]*name=["\']([^"\']+)',
            html,
            re.I,
        ) or re.search(
            r'<input[^>]*name=["\']([^"\']+)["\'][^>]*type=["\'](?:text|email)',
            html,
            re.I,
        )
        if user_match:
            user_field = user_match.group(1)

        return user_field, pass_field

    def _has_session_cookie(self) -> bool:
        if self._session is None:
            return False
        cookie_names = [str(name).lower() for name in dict(self._session.cookies).keys()]
        return any(
            name.startswith("ezproxy") or "session" in name or name.startswith("shib")
            for name in cookie_names
        )

    def _authenticate_form_login(self, login_resp: curl_requests.Response) -> bool:
        """Authenticate against a simple EZProxy login form."""
        sess = self._ensure_session()
        form_action = self._extract_form_action(login_resp.text, str(login_resp.url))
        payload = self._extract_form_inputs(login_resp.text)
        user_field, pass_field = self._detect_credential_fields(login_resp.text)
        payload[user_field] = self.username
        payload[pass_field] = self.password

        try:
            resp = sess.post(form_action, data=payload, allow_redirects=True)
        except Exception:
            logger.exception("Proxy auth: simple EZProxy login POST failed")
            return False

        final_url = str(resp.url).lower()
        lower_text = resp.text.lower()
        if any(token in lower_text for token in ("invalid", "incorrect", "failed")):
            logger.error("Proxy auth: simple EZProxy login rejected credentials")
            return False
        if self._has_session_cookie():
            logger.info("Proxy auth: simple EZProxy authentication succeeded")
            return True
        if resp.status_code < 400 and "/login" not in final_url:
            logger.info("Proxy auth: simple EZProxy login redirected successfully")
            return True

        logger.error("Proxy auth: simple EZProxy login did not establish a session")
        return False

    # -- Shibboleth SAML authentication flow ----------------------------------

    def _authenticate_shibboleth(self, login_resp: curl_requests.Response) -> bool:
        """Follow the Shibboleth SAML flow starting from the EZProxy login page.

        Steps: EZProxy SAML form → Shibboleth IdP login → credentials POST →
        SAML response back to EZProxy → session cookies set.
        """
        sess = self._ensure_session()

        # Step 1: parse the SAML auto-submit form from the proxy login page
        relay = re.search(r"name='RelayState'\s+value='([^']+)'", login_resp.text)
        saml = re.search(r"name='SAMLRequest'\s+value='([^']+)'", login_resp.text)
        action = re.search(r"action='([^']+)'", login_resp.text)
        if not all([relay, saml, action]):
            logger.error("Proxy auth: SAML form not found on login page")
            return False

        # Step 2: POST SAML request to the Shibboleth IdP
        idp_resp = sess.post(
            html_mod.unescape(action.group(1)),
            data={"RelayState": relay.group(1), "SAMLRequest": saml.group(1)},
            allow_redirects=True,
        )

        # Step 3: find and submit the credential form
        fa = re.search(r'<form[^>]*action="([^"]+)"', idp_resp.text)
        form_action = html_mod.unescape(fa.group(1)) if fa else ""
        if not form_action:
            logger.error("Proxy auth: no login form action found on IdP page")
            return False

        # Detect username/password field names from the form
        user_field = "j_username"
        pass_field = "j_password"
        for pattern, target in [
            (r'type=["\']password["\'][^>]*name=["\']([^"\']+)', "pass"),
            (r'name=["\']([^"\']+)["\'][^>]*type=["\']password', "pass"),
        ]:
            m = re.search(pattern, idp_resp.text, re.I)
            if m:
                pass_field = m.group(1)
                break
        for pattern, target in [
            (r'type=["\']text["\'][^>]*name=["\']([^"\']+)', "user"),
            (r'name=["\']([^"\']+)["\'][^>]*type=["\']text', "user"),
        ]:
            m = re.search(pattern, idp_resp.text, re.I)
            if m:
                user_field = m.group(1)
                break

        # Resolve form action URL
        login_url = form_action
        if form_action.startswith("/"):
            parsed_idp = urlparse(str(idp_resp.url))
            login_url = f"{parsed_idp.scheme}://{parsed_idp.netloc}{form_action}"

        cred_resp = sess.post(
            login_url,
            data={user_field: self.username, pass_field: self.password},
            allow_redirects=True,
        )

        # Step 4: extract and submit the SAML response back to the SP
        if "SAMLResponse" not in cred_resp.text:
            logger.error(
                "Proxy auth: no SAMLResponse after credential POST (bad credentials?)"
            )
            return False

        saml_resp = re.search(r'name="SAMLResponse"\s+value="([^"]+)"', cred_resp.text)
        action_resp = re.search(r'action="([^"]+)"', cred_resp.text)
        relay_resp = re.search(r'name="RelayState"\s+value="([^"]+)"', cred_resp.text)
        if not saml_resp or not action_resp:
            logger.error("Proxy auth: could not parse SAML response form")
            return False

        sp_data: dict[str, str] = {"SAMLResponse": saml_resp.group(1)}
        if relay_resp:
            sp_data["RelayState"] = relay_resp.group(1)

        sp_resp = sess.post(
            html_mod.unescape(action_resp.group(1)),
            data=sp_data,
            allow_redirects=True,
        )

        if sp_resp.status_code != 200:
            logger.error("Proxy auth: SP POST returned HTTP %d", sp_resp.status_code)
            return False

        logger.info("Proxy auth: Shibboleth authentication succeeded")
        return True

    def authenticate(self) -> bool:
        """Authenticate with the proxy if credentials are configured.

        Returns True if auth succeeded or no auth is needed, False on failure.
        """
        with self._lock:
            if self._authenticated:
                return True
            if not self.proxy_prefix or not self.username or not self.password:
                return True  # no auth needed

            sess = self._ensure_session()
            parsed = urlparse(self.proxy_prefix)
            self._proxy_base = parsed.netloc.replace("login.", "", 1)

            # GET the proxy login page to detect auth method
            login_url = (
                f"{parsed.scheme}://{parsed.netloc}/login?url=https://www.jstor.org"
            )
            try:
                login_resp = sess.get(login_url, allow_redirects=False)
            except Exception:
                logger.exception("Proxy auth: could not reach login page")
                return False

            if "SAMLRequest" in login_resp.text:
                # Shibboleth SAML flow → hostname-rewriting proxy
                self._uses_hostname_rewrite = True
                ok = self._authenticate_shibboleth(login_resp)
            elif "<form" in login_resp.text.lower():
                self._uses_hostname_rewrite = False
                ok = self._authenticate_form_login(login_resp)
            else:
                # No login form detected; assume URL-prefix mode without an auth step.
                ok = True
                logger.info(
                    "Proxy auth: no login form detected, using URL-prefix mode"
                )

            self._authenticated = ok
            return ok

    def rewrite_url(self, url: str) -> str:
        """Rewrite *url* through the proxy using the appropriate mode."""
        if not self.proxy_prefix:
            return url
        host = urlparse(url).netloc.lower()
        if host in _PROXY_DOMAINS_SKIP:
            return url

        if self._uses_hostname_rewrite and self._proxy_base:
            parsed = urlparse(url)
            proxy_host = parsed.netloc.replace(".", "-") + "." + self._proxy_base
            rewritten = f"https://{proxy_host}{parsed.path}"
            if parsed.query:
                rewritten += f"?{parsed.query}"
            return rewritten

        # URL-prefix mode
        return self.proxy_prefix + quote(url, safe="")

    def get(
        self, url: str, *, timeout: float = DEFAULT_TIMEOUT, **kwargs
    ) -> curl_requests.Response:
        """GET through the authenticated session."""
        sess = self._ensure_session()
        return sess.get(url, timeout=timeout, allow_redirects=True, **kwargs)


_PROXY_SESSION_LOCK = threading.Lock()
_PROXY_SESSIONS: dict[tuple[str, str, str], _ProxySession] = {}


def _get_proxy_session(proxy: ProxySettings) -> _ProxySession:
    """Return a cached proxy session for the provided settings."""
    key = (proxy.proxy_prefix, proxy.username, proxy.password)
    with _PROXY_SESSION_LOCK:
        session = _PROXY_SESSIONS.get(key)
        if session is None:
            session = _ProxySession(
                proxy_prefix=proxy.proxy_prefix,
                username=proxy.username,
                password=proxy.password,
            )
            _PROXY_SESSIONS[key] = session
    ps = session
    if not ps._authenticated and proxy.has_credentials():
        ps.authenticate()
    return ps


def _resolve_proxy_settings(
    proxy: ProxySettings | None = None,
    *,
    proxy_prefix: str | None = None,
) -> ProxySettings:
    settings = proxy if proxy is not None else ProxySettings.from_config()
    return settings.with_prefix(proxy_prefix)


def _apply_proxy_prefix(url: str, proxy_prefix: str) -> str:
    """Rewrite *url* through an EZProxy prefix if the domain is paywalled."""
    if not proxy_prefix:
        return url
    host = urlparse(url).netloc.lower()
    if host in _PROXY_DOMAINS_SKIP:
        return url
    return proxy_prefix + quote(url, safe="")


def _is_pdf_content(resp_content: bytes) -> bool:
    """Check if response content starts with the PDF magic bytes."""
    return resp_content[:5] == b"%PDF-"


def _pdf_fetch_one(
    fetch_url: str,
    *,
    session: _ProxySession | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = 2,
    initial_backoff: float = 1.0,
    retry_statuses: tuple[int, ...] = (429, 500, 502, 503, 504),
    label: str = "",
) -> PdfResponse:
    """Single-path PDF fetch with retries. Returns PdfResponse or raises."""
    delay = initial_backoff
    last_status: int | None = None

    for attempt in range(max_retries + 1):
        try:
            if session is not None:
                resp = session.get(fetch_url, timeout=timeout)
            else:
                resp = curl_requests.get(
                    fetch_url,
                    impersonate="chrome",
                    timeout=timeout,
                    allow_redirects=True,
                    verify=get_ssl_verify(),
                )
        except Exception:
            if attempt < max_retries:
                logger.warning(
                    "pdf_get[%s] %s failed (attempt %d/%d); retrying in %.1fs",
                    label,
                    fetch_url,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                )
                time.sleep(delay)
                delay *= 2
                continue
            raise

        last_status = resp.status_code
        if resp.status_code in retry_statuses and attempt < max_retries:
            logger.warning(
                "pdf_get[%s] %s returned HTTP %d (attempt %d/%d); retrying in %.1fs",
                label,
                fetch_url,
                resp.status_code,
                attempt + 1,
                max_retries + 1,
                delay,
            )
            time.sleep(delay)
            delay *= 2
            continue

        return PdfResponse(
            status_code=resp.status_code,
            content=resp.content,
            headers=dict(resp.headers),
        )

    raise httpx.HTTPStatusError(
        f"HTTP {last_status} for {fetch_url} after {max_retries + 1} attempts",
        request=httpx.Request("GET", fetch_url),
        response=httpx.Response(last_status or 0),
    )


def _pick_best_pdf(
    direct: PdfResponse | None,
    proxy: PdfResponse | None,
    url: str,
) -> PdfResponse:
    """Choose the best result from parallel direct + proxy fetches.

    Priority: (1) response with actual PDF content, (2) any 2xx response,
    (3) raise an error. When both paths return a PDF, prefer direct to
    avoid unnecessary proxy load.
    """
    direct_is_pdf = direct is not None and _is_pdf_content(direct.content)
    proxy_is_pdf = proxy is not None and _is_pdf_content(proxy.content)

    if direct_is_pdf and proxy_is_pdf:
        return direct  # both work — prefer direct
    if proxy_is_pdf:
        logger.info("pdf_get: proxy returned PDF for %s (direct did not)", url)
        return proxy
    if direct_is_pdf:
        return direct

    # Neither returned a PDF — prefer any 2xx response
    direct_ok = direct is not None and direct.status_code < 400
    proxy_ok = proxy is not None and proxy.status_code < 400
    if direct_ok:
        return direct
    if proxy_ok:
        return proxy

    # Both failed — return direct if it exists, else proxy, else raise
    if direct is not None:
        return direct
    if proxy is not None:
        return proxy

    raise httpx.HTTPStatusError(
        f"Both direct and proxy failed for {url}",
        request=httpx.Request("GET", url),
        response=httpx.Response(0),
    )


def pdf_get(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = 2,
    initial_backoff: float = 1.0,
    retry_statuses: tuple[int, ...] = (429, 500, 502, 503, 504),
    proxy: ProxySettings | None = None,
    proxy_prefix: str | None = None,
) -> PdfResponse:
    """Fetch a URL using curl_cffi with Chrome TLS impersonation.

    Designed for PDF downloads from academic publishers that block plain
    HTTP clients via TLS fingerprinting.

    When proxy credentials are configured (``ESSAY_WRITER_SEARCH__PROXY_USERNAME``
    and ``ESSAY_WRITER_SEARCH__PROXY_PASSWORD``), **both direct and proxy fetches
    run in parallel**. The result with actual PDF content is preferred; when both
    return a PDF, the direct result wins to avoid unnecessary proxy load.

    When only *proxy_prefix* (or ``ESSAY_WRITER_SEARCH__PROXY_PREFIX``) is set
    without credentials, direct and URL-prefix-rewritten fetches run in parallel.

    Open-access domains (arXiv, MDPI, Zenodo, PLOS) always skip the proxy path.
    """
    retry_kw = dict(
        timeout=timeout,
        max_retries=max_retries,
        initial_backoff=initial_backoff,
        retry_statuses=retry_statuses,
    )

    # Determine proxy strategy
    proxy_settings = _resolve_proxy_settings(proxy, proxy_prefix=proxy_prefix)
    ps = _get_proxy_session(proxy_settings)
    use_auth_proxy = proxy_settings.has_proxy() and ps._authenticated
    effective_prefix = proxy_settings.proxy_prefix

    host = urlparse(url).netloc.lower()
    skip_proxy = host in _PROXY_DOMAINS_SKIP

    # Build the proxy URL (if applicable)
    proxy_url: str | None = None
    proxy_session: _ProxySession | None = None
    if not skip_proxy:
        if use_auth_proxy:
            proxy_url = ps.rewrite_url(url)
            proxy_session = ps
        elif effective_prefix:
            proxy_url = _apply_proxy_prefix(url, effective_prefix)
            # URL-prefix fallback uses standalone curl_cffi when auth is unavailable.

    if proxy_url and proxy_url != url:
        # --- Parallel: direct + proxy ---
        logger.debug("pdf_get parallel: direct=%s, proxy=%s", url, proxy_url)

        direct_resp: PdfResponse | None = None
        direct_err: Exception | None = None
        proxy_resp: PdfResponse | None = None
        proxy_err: Exception | None = None

        with ThreadPoolExecutor(max_workers=2) as pool:
            direct_fut = pool.submit(
                _pdf_fetch_one, url, session=None, label="direct", **retry_kw
            )
            proxy_fut = pool.submit(
                _pdf_fetch_one,
                proxy_url,
                session=proxy_session,
                label="proxy",
                **retry_kw,
            )

            try:
                direct_resp = direct_fut.result()
            except Exception as exc:
                direct_err = exc

            try:
                proxy_resp = proxy_fut.result()
            except Exception as exc:
                proxy_err = exc

        # Both raised — re-raise the direct error (more common path)
        if direct_resp is None and proxy_resp is None:
            raise direct_err or proxy_err  # type: ignore[misc]

        result = _pick_best_pdf(direct_resp, proxy_resp, url)

        # If the "best" is still a 4xx+, raise so callers see the error
        if result.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {result.status_code} for {url}",
                request=httpx.Request("GET", url),
                response=httpx.Response(result.status_code),
            )
        return result

    # --- Direct only (no proxy configured or OA domain) ---
    logger.debug("pdf_get direct-only: %s", url)
    return _pdf_fetch_one(url, session=None, label="direct", **retry_kw)


def get_ssl_verify() -> str | bool:
    """Return the CA bundle path if set, otherwise default verification."""
    return (
        os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE") or True
    )


def search_error_response(source: str, query: str, exc: Exception) -> str:
    """Return a JSON error string for a failed search request."""
    return json.dumps(
        {
            "error": "request_failed",
            "message": str(exc),
            "query": query,
            "source": source,
        },
        ensure_ascii=False,
    )
