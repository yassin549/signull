"""Measure end-to-end update latency from /api/status."""
import json
import time
import urllib.request

URL = "http://127.0.0.1:8080/api/status"


def fetch():
    with urllib.request.urlopen(URL, timeout=10) as r:
        return json.loads(r.read())


samples = []
for _ in range(8):
    t0 = time.time()
    d = fetch()
    t1 = time.time()
    now = t1
    btc = d.get("btc") or {}
    feed = d.get("feed") or {}
    samples.append({
        "api_ms": round((t1 - t0) * 1000, 1),
        "btc_age_ms": round((now - (btc.get("updated_at") or now)) * 1000),
        "cl_age_ms": round((now - (btc.get("chainlink_at") or now)) * 1000),
        "feed_age_ms": round((now - (feed.get("last_update_at") or now)) * 1000),
        "feed_hz": feed.get("updates_per_sec"),
    })
    time.sleep(0.3)

if samples:
    hist = d.get("btc_history") or []
    if len(hist) >= 3:
        gaps = [round(hist[i]["t"] - hist[i - 1]["t"], 3) for i in range(1, len(hist))]
        print("btc_history_gap_sec min/median/max:", min(gaps), sorted(gaps)[len(gaps) // 2], max(gaps))

for s in samples:
    print(s)

print("config_push_ms:", fetch().get("version"))