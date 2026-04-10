"""Quick smoke test: verify each provider's model specs resolve through the gateway."""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

load_dotenv()

from config.schemas import ModelsConfig, _PROVIDER_PRESETS
from src.agent import _GATEWAY_PROVIDER_MAP, create_model


def gateway_name(spec: str) -> str:
    lc_provider, _, bare = spec.partition(":")
    bare = bare or spec
    gw_prefix = _GATEWAY_PROVIDER_MAP.get(lc_provider, "")
    return f"{gw_prefix}{bare}"


base_url = os.environ.get("AI_BASE_URL")
if not base_url:
    print("AI_BASE_URL not set — skipping live test.")
    sys.exit(0)

print(f"AI_BASE_URL = {base_url}\n")

failed = []
for provider in _PROVIDER_PRESETS:
    cfg = ModelsConfig(provider=provider)
    print(f"=== {provider} ===")
    for role in ("worker", "writer", "reviewer"):
        spec = getattr(cfg, role)
        gw = gateway_name(spec)
        print(f"  {role:10s}  {gw:50s}", end=" ", flush=True)
        try:
            model = create_model(spec)
            resp = model.invoke("Reply with just the word OK")
            text = resp.content[:60].replace("\n", " ") if resp.content else "(empty)"
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
