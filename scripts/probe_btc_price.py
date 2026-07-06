"""Probe Polymarket APIs for BTC spot/oracle price."""
import json
import math
from datetime import datetime, timezone

import requests

ts = int(math.floor(datetime.now(timezone.utc).timestamp() / 300) * 300)
slug = f"btc-updown-5m-{ts}"

print("=== Gamma event ===")
r = requests.get(f"https://gamma-api.polymarket.com/events/slug/{slug}", timeout=10)
e = r.json()
print("event keys:", sorted(e.keys()))
m = e["markets"][0]
print("market keys:", sorted(m.keys()))
for k in ("question", "description", "resolutionSource", "startDate", "endDate"):
    if k in e:
        print(f"event.{k}:", str(e[k])[:300])
    if k in m:
        print(f"market.{k}:", str(m[k])[:300])

urls = [
    "https://polymarket.com/api/crypto/prices?symbol=BTC",
    "https://gamma-api.polymarket.com/crypto-prices",
    "https://data-api.polymarket.com/crypto-prices",
    "https://clob.polymarket.com/crypto-prices",
    "https://gamma-api.polymarket.com/prices?symbol=BTC",
]
print("\n=== Probe URLs ===")
for url in urls:
    try:
        resp = requests.get(url, timeout=8)
        print(url, resp.status_code, resp.text[:200])
    except Exception as exc:
        print(url, "ERR", exc)