#!/usr/bin/env python3
"""Test EZProxy authentication and PDF downloads with university credentials.

Usage:
    uv run python scripts/benchmarks/test_proxy_auth.py \\
        --proxy-url https://proxy.uoa.gr \\
        --username student@uoa.gr \\
        --password secret123

    # With custom form field names (defaults: user/pass)
    uv run python scripts/benchmarks/test_proxy_auth.py \\
        --proxy-url https://proxy.lib.auth.gr \\
        --username student \\
        --password secret123 \\
        --user-field username \\
        --pass-field passwd

    # Test specific URLs
    uv run python scripts/benchmarks/test_proxy_auth.py \\
        --proxy-url https://proxy.uoa.gr \\
        --username student@uoa.gr \\
        --password secret123 \\
        --urls "https://link.springer.com/content/pdf/10.1007/foo.pdf,https://doi.org/10.1234/bar"

    # Skip auth (just test proxied fetches with existing cookies/IP auth)
    uv run python scripts/benchmarks/test_proxy_auth.py \\
        --proxy-url https://proxy.uoa.gr \\
        --no-auth
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from curl_cffi import requests as curl_requests

PDF_MAGIC = b"%PDF"

# ---------------------------------------------------------------------------
# Test URLs — paywalled publishers across multiple domains
# ---------------------------------------------------------------------------

DEFAULT_TEST_URLS = [
    # Springer (usually paywalled)
    "https://link.springer.com/content/pdf/10.1007/s10640-019-00399-w.pdf",
    "https://link.springer.com/content/pdf/10.1007/978-3-319-21674-4_3.pdf",
    # SAGE (403 without auth)
    "https://journals.sagepub.com/doi/pdf/10.1068/c10171r",
    "https://journals.sagepub.com/doi/pdf/10.1007/s12290-016-0407-5",
    # Taylor & Francis
    "https://www.tandfonline.com/doi/pdf/10.1080/21693277.2016.1192517?needAccess=true",
    "https://www.tandfonline.com/doi/pdf/10.1080/03066150.2016.1141198?needAccess=true",
    # Wiley
    "https://onlinelibrary.wiley.com/doi/pdfdirect/10.1002/art.39859",
    "https://onlinelibrary.wiley.com/doi/pdfdirect/10.1111/rsp3.12340",
    # Elsevier / ScienceDirect
    "https://www.sciencedirect.com/science/article/pii/S0169204614002692/pdfft",
    # Oxford Academic (OUP)
    "http://academic.oup.com/histres/article-pdf/74/183/95/31573289/1468-2281.00118.pdf",
    # Cambridge
    "https://www.cambridge.org/core/services/aop-cambridge-core/content/view/0DCD6F79DE1F989E4C2AA6E201F6FEEF/S0003975602001066a.pdf/ethnicity-without-groups.pdf",
    # MIT Press
    "https://direct.mit.edu/isec/article-pdf/34/2/82/693298/isec.2009.34.2.82.pdf",
    # Nature
    "https://www.nature.com/articles/s41599-018-0152-2.pdf",
    # The Lancet
    "http://www.thelancet.com/article/S0140673615001907/pdf",
    # IEEE
    "https://ieeexplore.ieee.org/ielx7/6287639/8600701/08721134.pdf",
]


def _ssl_verify() -> str | bool:
    return (
        os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE") or True
    )


def _is_pdf(content: bytes) -> bool:
    return content[:4] == PDF_MAGIC and len(content) > 1000


# ---------------------------------------------------------------------------
# EZProxy authentication
# ---------------------------------------------------------------------------


def authenticate_ezproxy(
    session: curl_requests.Session,
    proxy_url: str,
    username: str,
    password: str,
    user_field: str = "user",
    pass_field: str = "pass",
) -> bool:
    """Authenticate with EZProxy and store session cookies.

    EZProxy login flow:
    1. GET proxy_url/login → login form
    2. POST credentials to the form action
    3. Session cookies are set on success
    """
    login_url = proxy_url.rstrip("/") + "/login"
    print(f"  Fetching login page: {login_url}")

    try:
        resp = session.get(login_url, timeout=30, allow_redirects=True)
    except Exception as e:
        print(f"  ERROR: Could not reach login page: {e}")
        return False

    print(f"  Login page status: {resp.status_code}, length: {len(resp.text)}")

    # Try to find the form action URL
    form_action = login_url  # default: POST back to same URL
    action_match = re.search(r'<form[^>]*action=["\']([^"\']+)["\']', resp.text, re.I)
    if action_match:
        action = action_match.group(1)
        if action.startswith("http"):
            form_action = action
        elif action.startswith("/"):
            parsed = urlparse(proxy_url)
            form_action = f"{parsed.scheme}://{parsed.netloc}{action}"
        else:
            form_action = proxy_url.rstrip("/") + "/" + action
        print(f"  Found form action: {form_action}")

    # Detect form field names from HTML (override defaults if found)
    # Look for input fields with type=text/email and type=password
    detected_user = re.search(
        r'<input[^>]*name=["\']([^"\']+)["\'][^>]*type=["\'](?:text|email)["\']',
        resp.text,
        re.I,
    )
    detected_pass = re.search(
        r'<input[^>]*type=["\']password["\'][^>]*name=["\']([^"\']+)["\']',
        resp.text,
        re.I,
    )
    # Also try reverse order: type before name
    if not detected_user:
        detected_user = re.search(
            r'<input[^>]*type=["\'](?:text|email)["\'][^>]*name=["\']([^"\']+)["\']',
            resp.text,
            re.I,
        )
    if not detected_pass:
        detected_pass = re.search(
            r'<input[^>]*name=["\']([^"\']+)["\'][^>]*type=["\']password["\']',
            resp.text,
            re.I,
        )

    if detected_user:
        auto_user_field = detected_user.group(1)
        print(f"  Auto-detected user field: '{auto_user_field}'")
        if user_field == "user":  # only override if default
            user_field = auto_user_field
    if detected_pass:
        auto_pass_field = detected_pass.group(1)
        print(f"  Auto-detected password field: '{auto_pass_field}'")
        if pass_field == "pass":  # only override if default
            pass_field = auto_pass_field

    print(f"  Posting credentials ({user_field}=***, {pass_field}=***)")

    try:
        resp = session.post(
            form_action,
            data={user_field: username, pass_field: password},
            timeout=30,
            allow_redirects=True,
        )
    except Exception as e:
        print(f"  ERROR: Login POST failed: {e}")
        return False

    print(f"  Login response status: {resp.status_code}, length: {len(resp.text)}")

    # Check for common success indicators
    cookies = dict(session.cookies)
    print(f"  Session cookies: {list(cookies.keys())}")

    if any(k.lower().startswith("ezproxy") for k in cookies):
        print("  ✓ EZProxy session cookie detected — auth likely succeeded")
        return True

    # Check for failure indicators
    lower_text = resp.text.lower()
    if "invalid" in lower_text or "incorrect" in lower_text or "failed" in lower_text:
        print("  ✗ Login page indicates authentication failure")
        return False

    if resp.status_code == 200 and cookies:
        print("  ? Got cookies but no EZProxy cookie — auth may have succeeded")
        return True

    print("  ? Uncertain auth result — proceeding anyway")
    return True


# ---------------------------------------------------------------------------
# PDF fetch through proxy
# ---------------------------------------------------------------------------


def fetch_proxied_pdf(
    session: curl_requests.Session,
    url: str,
    proxy_prefix: str,
) -> dict:
    """Fetch a URL through the EZProxy and check if we got a PDF."""
    proxied_url = proxy_prefix + quote(url, safe="")
    domain = urlparse(url).netloc

    t0 = time.monotonic()
    try:
        resp = session.get(
            proxied_url,
            timeout=30,
            allow_redirects=True,
        )
        elapsed = time.monotonic() - t0
        ct = resp.headers.get("content-type", "")
        got_pdf = _is_pdf(resp.content)
        return {
            "url": url,
            "proxied_url": proxied_url,
            "domain": domain,
            "ok": got_pdf,
            "status": resp.status_code,
            "content_type": ct,
            "size_bytes": len(resp.content),
            "elapsed_s": round(elapsed, 2),
            "final_url": str(resp.url) if hasattr(resp, "url") else "",
        }
    except Exception as e:
        elapsed = time.monotonic() - t0
        return {
            "url": url,
            "proxied_url": proxied_url,
            "domain": domain,
            "ok": False,
            "status": None,
            "content_type": "",
            "size_bytes": 0,
            "elapsed_s": round(elapsed, 2),
            "error": str(e)[:200],
        }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test EZProxy authentication and PDF downloads",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--proxy-url",
        required=True,
        help="Base EZProxy URL (e.g. https://proxy.uoa.gr)",
    )
    parser.add_argument("--username", default="", help="University username/email")
    parser.add_argument("--password", default="", help="University password")
    parser.add_argument(
        "--user-field",
        default="user",
        help="Login form username field name (default: user)",
    )
    parser.add_argument(
        "--pass-field",
        default="pass",
        help="Login form password field name (default: pass)",
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Skip login, just test proxied fetches (IP-based auth)",
    )
    parser.add_argument(
        "--urls",
        default="",
        help="Comma-separated URLs to test (default: built-in paywalled set)",
    )
    parser.add_argument(
        "--output",
        default="scripts/benchmarks/proxy_auth_results.json",
        help="Results output file",
    )
    args = parser.parse_args()

    if not args.no_auth and (not args.username or not args.password):
        print(
            "ERROR: --username and --password required (or use --no-auth for IP-based access)"
        )
        sys.exit(1)

    proxy_url = args.proxy_url.rstrip("/")
    proxy_prefix = proxy_url + "/login?url="

    test_urls = args.urls.split(",") if args.urls else DEFAULT_TEST_URLS
    test_urls = [u.strip() for u in test_urls if u.strip()]

    print(f"Proxy: {proxy_url}")
    print(f"URLs to test: {len(test_urls)}")
    print()

    # Create session with Chrome TLS fingerprint
    session = curl_requests.Session(
        impersonate="chrome",
        verify=_ssl_verify(),
    )

    # Authenticate
    if not args.no_auth:
        print("AUTHENTICATING")
        print("-" * 60)
        auth_ok = authenticate_ezproxy(
            session,
            proxy_url,
            args.username,
            args.password,
            args.user_field,
            args.pass_field,
        )
        if not auth_ok:
            print("\nAuthentication failed. Continuing anyway to see what happens...\n")
        else:
            print("\nAuthentication succeeded.\n")

    # Test PDF downloads
    print("TESTING PDF DOWNLOADS")
    print("-" * 60)
    results = []
    ok_count = 0

    for i, url in enumerate(test_urls, 1):
        domain = urlparse(url).netloc
        print(f"[{i}/{len(test_urls)}] {domain}...")

        result = fetch_proxied_pdf(session, url, proxy_prefix)
        results.append(result)

        size_kb = result["size_bytes"] / 1024
        status_str = f"HTTP {result['status']}" if result["status"] else "ERR"
        icon = "✓ PDF" if result["ok"] else "✗    "
        print(
            f"  {icon}  {status_str:8s}  {size_kb:7.1f}KB  {result['elapsed_s']:.1f}s"
        )

        if result.get("error"):
            print(f"  Error: {result['error'][:100]}")

        if result["ok"]:
            ok_count += 1

    # Summary
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    pct = ok_count / len(test_urls) * 100 if test_urls else 0
    print(f"  PDFs downloaded: {ok_count}/{len(test_urls)} ({pct:.1f}%)")
    print()

    # Per-domain breakdown
    by_domain: dict[str, list[dict]] = {}
    for r in results:
        by_domain.setdefault(r["domain"], []).append(r)

    print("PER-DOMAIN RESULTS")
    print("-" * 60)
    for domain in sorted(by_domain):
        domain_results = by_domain[domain]
        domain_ok = sum(1 for r in domain_results if r["ok"])
        icon = (
            "✓" if domain_ok == len(domain_results) else "✗" if domain_ok == 0 else "◐"
        )
        print(f"  {icon} {domain:45s}  {domain_ok}/{len(domain_results)}")

    # Save results
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "proxy_url": proxy_url,
        "auth_used": not args.no_auth,
        "total_urls": len(test_urls),
        "pdfs_downloaded": ok_count,
        "results": results,
    }
    output_path.write_text(json.dumps(output, indent=2))
    print(f"\nResults saved to {output_path}")


if __name__ == "__main__":
    main()
