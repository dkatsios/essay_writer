#!/usr/bin/env python3
"""Benchmark PDF fetching: httpx vs curl_cffi.

Usage:
    uv run --with curl_cffi python scripts/benchmark_pdf_fetch.py [--limit N] [--timeout S]

Fetches URLs from scripts/benchmark_urls.json with both httpx and curl_cffi,
compares success rates, and writes results to scripts/benchmark_results.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from src.tools._http import get_ssl_verify

# ---------------------------------------------------------------------------
# SSL verification — reuse project convention
# ---------------------------------------------------------------------------


def _ssl_verify() -> str | bool:
    return get_ssl_verify()


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

_SIMPLE_HEADERS = {"User-Agent": "essay-writer/0.1"}

PDF_MAGIC = b"%PDF"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_pdf(content: bytes, content_type: str) -> bool:
    """Check if response is actually a PDF."""
    if content[:4] == PDF_MAGIC:
        return True
    if "pdf" in content_type.lower() and len(content) > 1000:
        # Some servers claim PDF but serve HTML error pages
        return content[:4] == PDF_MAGIC
    return False


def _result(
    url: str,
    method: str,
    ok: bool,
    status: int | None,
    content_type: str,
    size: int,
    elapsed: float,
    error: str | None = None,
) -> dict:
    return {
        "url": url,
        "domain": urlparse(url).netloc,
        "method": method,
        "ok": ok,
        "status": status,
        "content_type": content_type,
        "size_bytes": size,
        "elapsed_s": round(elapsed, 2),
        "error": error,
    }


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


def fetch_httpx_simple(url: str, timeout: float) -> dict:
    """Fetch with httpx using the project's default simple headers."""
    import httpx

    t0 = time.monotonic()
    try:
        with httpx.Client(headers=_SIMPLE_HEADERS, verify=_ssl_verify()) as client:
            resp = client.get(url, follow_redirects=True, timeout=timeout)
        elapsed = time.monotonic() - t0
        ct = resp.headers.get("content-type", "")
        ok = _is_pdf(resp.content, ct)
        return _result(
            url, "httpx_simple", ok, resp.status_code, ct, len(resp.content), elapsed
        )
    except Exception as e:
        return _result(
            url, "httpx_simple", False, None, "", 0, time.monotonic() - t0, str(e)[:200]
        )


def fetch_httpx_browser_headers(url: str, timeout: float) -> dict:
    """Fetch with httpx but using full browser-like headers."""
    import httpx

    t0 = time.monotonic()
    try:
        with httpx.Client(headers=_BROWSER_HEADERS, verify=_ssl_verify()) as client:
            resp = client.get(url, follow_redirects=True, timeout=timeout)
        elapsed = time.monotonic() - t0
        ct = resp.headers.get("content-type", "")
        ok = _is_pdf(resp.content, ct)
        return _result(
            url,
            "httpx_browser_headers",
            ok,
            resp.status_code,
            ct,
            len(resp.content),
            elapsed,
        )
    except Exception as e:
        return _result(
            url,
            "httpx_browser_headers",
            False,
            None,
            "",
            0,
            time.monotonic() - t0,
            str(e)[:200],
        )


def fetch_curl_cffi_simple(url: str, timeout: float) -> dict:
    """Fetch with curl_cffi using Chrome TLS impersonation + simple headers."""
    from curl_cffi import requests as curl_requests

    t0 = time.monotonic()
    try:
        resp = curl_requests.get(
            url,
            impersonate="chrome",
            timeout=timeout,
            allow_redirects=True,
            headers=_SIMPLE_HEADERS,
            verify=_ssl_verify(),
        )
        elapsed = time.monotonic() - t0
        ct = resp.headers.get("content-type", "")
        ok = _is_pdf(resp.content, ct)
        return _result(
            url,
            "curl_cffi_simple",
            ok,
            resp.status_code,
            ct,
            len(resp.content),
            elapsed,
        )
    except Exception as e:
        return _result(
            url,
            "curl_cffi_simple",
            False,
            None,
            "",
            0,
            time.monotonic() - t0,
            str(e)[:200],
        )


def fetch_curl_cffi_full(url: str, timeout: float) -> dict:
    """Fetch with curl_cffi using Chrome TLS impersonation + browser headers."""
    from curl_cffi import requests as curl_requests

    t0 = time.monotonic()
    try:
        resp = curl_requests.get(
            url,
            impersonate="chrome",
            timeout=timeout,
            allow_redirects=True,
            headers=_BROWSER_HEADERS,
            verify=_ssl_verify(),
        )
        elapsed = time.monotonic() - t0
        ct = resp.headers.get("content-type", "")
        ok = _is_pdf(resp.content, ct)
        return _result(
            url, "curl_cffi_full", ok, resp.status_code, ct, len(resp.content), elapsed
        )
    except Exception as e:
        return _result(
            url,
            "curl_cffi_full",
            False,
            None,
            "",
            0,
            time.monotonic() - t0,
            str(e)[:200],
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

FETCHERS = [
    ("httpx_simple", fetch_httpx_simple),
    ("httpx_browser_headers", fetch_httpx_browser_headers),
    ("curl_cffi_simple", fetch_curl_cffi_simple),
    ("curl_cffi_full", fetch_curl_cffi_full),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark PDF fetch approaches")
    parser.add_argument("--limit", type=int, default=0, help="Max URLs to test (0=all)")
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="Per-request timeout (seconds)"
    )
    parser.add_argument(
        "--urls",
        type=str,
        default="scripts/benchmarks/benchmark_urls.json",
        help="URL list file",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="scripts/benchmarks/benchmark_results.json",
        help="Results output file",
    )
    parser.add_argument(
        "--methods",
        type=str,
        default="",
        help="Comma-separated methods to run (default: all)",
    )
    args = parser.parse_args()

    # Verify curl_cffi is available
    try:
        from curl_cffi import requests as _
    except ImportError:
        print("ERROR: curl_cffi not installed. Run:")
        print("  uv run --with curl_cffi python scripts/benchmark_pdf_fetch.py")
        sys.exit(1)

    urls_path = Path(args.urls)
    if not urls_path.exists():
        print(f"ERROR: {urls_path} not found")
        sys.exit(1)

    entries = json.loads(urls_path.read_text())
    if args.limit > 0:
        entries = entries[: args.limit]

    active_methods = set()
    if args.methods:
        active_methods = set(args.methods.split(","))

    fetchers = [
        (name, fn)
        for name, fn in FETCHERS
        if not active_methods or name in active_methods
    ]
    if not fetchers:
        print(f"ERROR: no valid methods in --methods={args.methods}")
        print(f"  Available: {', '.join(n for n, _ in FETCHERS)}")
        sys.exit(1)

    print(f"Benchmarking {len(entries)} URLs with {len(fetchers)} methods")
    print(f"Methods: {', '.join(n for n, _ in fetchers)}")
    print(f"Timeout: {args.timeout}s per request")
    print()

    all_results: list[dict] = []
    totals: dict[str, dict[str, int]] = {
        name: {"ok": 0, "fail": 0} for name, _ in fetchers
    }

    for i, entry in enumerate(entries, 1):
        url = entry["url"]
        domain = entry["domain"]
        title = entry.get("title", "")[:50]
        print(f"[{i}/{len(entries)}] {domain}: {title}...")

        for method_name, fetch_fn in fetchers:
            result = fetch_fn(url, args.timeout)
            all_results.append(result)

            status_str = f"HTTP {result['status']}" if result["status"] else "ERR"
            size_kb = result["size_bytes"] / 1024
            icon = "✓ PDF" if result["ok"] else "✗    "
            print(
                f"  {method_name:25s}  {icon}  {status_str:8s}  {size_kb:7.1f}KB  {result['elapsed_s']:.1f}s"
            )

            if result["ok"]:
                totals[method_name]["ok"] += 1
            else:
                totals[method_name]["fail"] += 1

        print()

    # Summary
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    total_urls = len(entries)
    for method_name, _ in fetchers:
        ok = totals[method_name]["ok"]
        pct = ok / total_urls * 100 if total_urls else 0
        print(f"  {method_name:25s}  {ok:3d}/{total_urls}  ({pct:5.1f}%)")
    print()

    # Per-domain breakdown
    print("PER-DOMAIN BREAKDOWN")
    print("-" * 70)
    domain_results: dict[str, dict[str, dict[str, int]]] = {}
    for r in all_results:
        d = r["domain"]
        m = r["method"]
        if d not in domain_results:
            domain_results[d] = {}
        if m not in domain_results[d]:
            domain_results[d][m] = {"ok": 0, "fail": 0}
        if r["ok"]:
            domain_results[d][m]["ok"] += 1
        else:
            domain_results[d][m]["fail"] += 1

    for domain in sorted(domain_results):
        print(f"  {domain}")
        for method_name, _ in fetchers:
            stats = domain_results[domain].get(method_name, {"ok": 0, "fail": 0})
            total = stats["ok"] + stats["fail"]
            print(f"    {method_name:25s}  {stats['ok']}/{total}")
        print()

    # Improvement analysis: where curl_cffi succeeded but httpx failed
    print("IMPROVEMENT ANALYSIS (curl_cffi_full wins over httpx_simple)")
    print("-" * 70)
    by_url: dict[str, dict[str, bool]] = {}
    for r in all_results:
        by_url.setdefault(r["url"], {})[r["method"]] = r["ok"]

    improved = []
    for url, methods in by_url.items():
        if methods.get("curl_cffi_full") and not methods.get("httpx_simple"):
            domain = urlparse(url).netloc
            improved.append((domain, url))

    if improved:
        for domain, url in sorted(improved):
            print(f"  {domain:40s}  {url[:70]}")
    else:
        print("  (none)")
    print()

    # Save results
    output = {
        "summary": {name: totals[name] for name, _ in fetchers},
        "total_urls": total_urls,
        "results": all_results,
    }
    output_path = Path(args.output)
    output_path.write_text(json.dumps(output, indent=2))
    print(f"Results saved to {output_path}")


if __name__ == "__main__":
    main()
