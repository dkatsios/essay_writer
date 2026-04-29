"""Benchmark PDF downloads: direct vs proxy vs parallel (pdf_get).

Compares three methods for each URL:
  1. Direct curl_cffi (no proxy) — baseline
  2. Authenticated proxy (Shibboleth + hostname rewriting) — proxy only
  3. pdf_get() — parallel direct+proxy, picks best

Usage:
    uv run python scripts/benchmarks/benchmark_proxy_pdf.py
"""

import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.tools._http import (
    ProxySettings,
    _ProxySession,
    get_ssl_verify,
    pdf_get,
)
from curl_cffi import requests as curl_requests

PDF_MAGIC = b"%PDF"
BENCHMARK_URLS = Path("scripts/benchmarks/benchmark_urls.json")
OUTPUT = Path("scripts/benchmarks/proxy_benchmark_results.json")

# EAP credentials from config defaults
PROXY_PREFIX = "https://login.proxy.eap.gr/login?url="
USERNAME = "std523991"
PASSWORD = "f7mv9u"


def fetch_direct(url: str, timeout: float = 30.0) -> dict:
    """Fetch with curl_cffi directly (no proxy)."""
    t0 = time.monotonic()
    try:
        resp = curl_requests.get(
            url,
            impersonate="chrome",
            timeout=timeout,
            allow_redirects=True,
            verify=get_ssl_verify(),
        )
        elapsed = time.monotonic() - t0
        got_pdf = resp.content[:4] == PDF_MAGIC and len(resp.content) > 1000
        return {
            "ok": got_pdf,
            "status": resp.status_code,
            "size": len(resp.content),
            "elapsed": round(elapsed, 2),
        }
    except Exception as e:
        return {
            "ok": False,
            "status": None,
            "size": 0,
            "elapsed": round(time.monotonic() - t0, 2),
            "error": str(e)[:120],
        }


def fetch_proxied(session: _ProxySession, url: str, timeout: float = 30.0) -> dict:
    """Fetch through the authenticated proxy session."""
    rewritten = session.rewrite_url(url)
    t0 = time.monotonic()
    try:
        resp = session.get(rewritten, timeout=timeout)
        elapsed = time.monotonic() - t0
        got_pdf = resp.content[:4] == PDF_MAGIC and len(resp.content) > 1000
        return {
            "ok": got_pdf,
            "status": resp.status_code,
            "size": len(resp.content),
            "elapsed": round(elapsed, 2),
        }
    except Exception as e:
        return {
            "ok": False,
            "status": None,
            "size": 0,
            "elapsed": round(time.monotonic() - t0, 2),
            "error": str(e)[:120],
        }


def fetch_combined(url: str, timeout: float = 30.0) -> dict:
    """Fetch using pdf_get() — the production parallel path."""
    t0 = time.monotonic()
    try:
        resp = pdf_get(
            url,
            timeout=timeout,
            max_retries=2,
            initial_backoff=1.0,
            proxy=ProxySettings(
                proxy_prefix=PROXY_PREFIX,
                username=USERNAME,
                password=PASSWORD,
            ),
        )
        elapsed = time.monotonic() - t0
        got_pdf = resp.content[:4] == PDF_MAGIC and len(resp.content) > 1000
        return {
            "ok": got_pdf,
            "status": resp.status_code,
            "size": len(resp.content),
            "elapsed": round(elapsed, 2),
        }
    except Exception as e:
        return {
            "ok": False,
            "status": None,
            "size": 0,
            "elapsed": round(time.monotonic() - t0, 2),
            "error": str(e)[:120],
        }


def main():
    urls = json.loads(BENCHMARK_URLS.read_text())
    print(f"Loaded {len(urls)} URLs from {BENCHMARK_URLS}\n")

    # Authenticate proxy session (used for the isolated proxy-only test)
    print("Authenticating with EAP Shibboleth proxy...")
    ps = _ProxySession(
        proxy_prefix=PROXY_PREFIX, username=USERNAME, password=PASSWORD
    )
    auth_ok = ps.authenticate()
    if not auth_ok:
        print("ERROR: Proxy authentication failed.")
        sys.exit(1)
    print(f"Auth OK (hostname_rewrite={ps._uses_hostname_rewrite})\n")

    # Run benchmark
    results = []
    direct_ok = 0
    proxy_ok = 0
    combined_ok = 0

    for i, entry in enumerate(urls, 1):
        url = entry["url"]
        domain = entry.get("domain", urlparse(url).netloc)
        print(f"[{i:2d}/{len(urls)}] {domain:45s}", end="", flush=True)

        d = fetch_direct(url)
        p = fetch_proxied(ps, url)
        c = fetch_combined(url)

        direct_ok += d["ok"]
        proxy_ok += p["ok"]
        combined_ok += c["ok"]

        d_icon = "PDF" if d["ok"] else "   "
        p_icon = "PDF" if p["ok"] else "   "
        c_icon = "PDF" if c["ok"] else "   "
        d_st = str(d.get("status") or "ERR")
        p_st = str(p.get("status") or "ERR")
        c_st = str(c.get("status") or "ERR")
        print(f"  D:{d_icon} {d_st:>3}  P:{p_icon} {p_st:>3}  C:{c_icon} {c_st:>3}  {d['elapsed']:.1f}s/{p['elapsed']:.1f}s/{c['elapsed']:.1f}s")

        results.append({
            "url": url,
            "domain": domain,
            "title": entry.get("title", ""),
            "direct": d,
            "proxy": p,
            "combined": c,
        })

    # Summary
    print(f"\n{'=' * 70}")
    print(f"Direct only:   {direct_ok}/{len(urls)} ({direct_ok/len(urls)*100:.1f}%)")
    print(f"Proxy only:    {proxy_ok}/{len(urls)} ({proxy_ok/len(urls)*100:.1f}%)")
    print(f"Combined:      {combined_ok}/{len(urls)} ({combined_ok/len(urls)*100:.1f}%)")
    new_from_proxy = sum(1 for r in results if r["proxy"]["ok"] and not r["direct"]["ok"])
    new_from_combined = sum(1 for r in results if r["combined"]["ok"] and not r["direct"]["ok"])
    print(f"NEW from proxy-only: {new_from_proxy}")
    print(f"NEW from combined:   {new_from_combined}")

    # Per-domain breakdown
    by_domain = {}
    for r in results:
        by_domain.setdefault(r["domain"], []).append(r)

    print(f"\nPer-domain:")
    print(f"  {'Domain':45s}  {'Direct':>6s}  {'Proxy':>6s}  {'Combi':>6s}  {'New':>3s}")
    print(f"  {'-'*45}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*3}")
    for d in sorted(by_domain):
        dr = by_domain[d]
        dok = sum(1 for r in dr if r["direct"]["ok"])
        pok = sum(1 for r in dr if r["proxy"]["ok"])
        cok = sum(1 for r in dr if r["combined"]["ok"])
        new = sum(1 for r in dr if r["combined"]["ok"] and not r["direct"]["ok"])
        new_s = str(new) if new else ""
        print(f"  {d:45s}  {dok}/{len(dr):>4}  {pok}/{len(dr):>4}  {cok}/{len(dr):>4}  {new_s:>3s}")

    # Timing comparison
    d_times = [r["direct"]["elapsed"] for r in results]
    p_times = [r["proxy"]["elapsed"] for r in results]
    c_times = [r["combined"]["elapsed"] for r in results]
    print(f"\nTiming (mean per URL):")
    print(f"  Direct:   {sum(d_times)/len(d_times):.2f}s")
    print(f"  Proxy:    {sum(p_times)/len(p_times):.2f}s")
    print(f"  Combined: {sum(c_times)/len(c_times):.2f}s")

    # Save
    summary = {
        "total": len(urls),
        "direct_ok": direct_ok,
        "proxy_ok": proxy_ok,
        "combined_ok": combined_ok,
        "results": results,
    }
    OUTPUT.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nResults saved to {OUTPUT}")


if __name__ == "__main__":
    main()
