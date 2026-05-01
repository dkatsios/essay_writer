"""Quick smoke test: verify each provider's model specs resolve through the gateway."""

from __future__ import annotations

import sys

from config.settings import ModelsConfig, PROVIDER_PRESETS, load_config
from src.agent import _GATEWAY_PROVIDER_MAP, create_client, extract_text


def gateway_name(spec: str) -> str:
    lc_provider, _, bare = spec.partition(":")
    bare = bare or spec
    gw_prefix = _GATEWAY_PROVIDER_MAP.get(lc_provider, "")
    return f"{gw_prefix}{bare}"


base_url = load_config().ai_base_url
if not base_url:
    print("AI_BASE_URL not set — skipping live test.")
    sys.exit(0)

print(f"AI_BASE_URL = {base_url}\n")

failed = []
for provider in PROVIDER_PRESETS:
    cfg = ModelsConfig(provider=provider)
    print(f"=== {provider} ===")
    for role in ("worker", "writer", "reviewer"):
        spec = getattr(cfg, role)
        gw = gateway_name(spec)
        print(f"  {role:10s}  {gw:50s}", end=" ", flush=True)
        try:
            model = create_client(spec)
            resp = model.client.chat.completions.create(
                model=model.model,
                messages=[{"role": "user", "content": "Reply with just the word OK"}],
            )
            text = extract_text(resp)[:60].replace("\n", " ") or "(empty)"
            print(f"OK — {text}")
        except Exception as exc:
            print(f"FAILED — {exc}")
            failed.append(f"{provider}/{role} ({gw})")

print()
if failed:
    print(f"FAILURES ({len(failed)}):")
    for f in failed:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All providers and roles passed.")
