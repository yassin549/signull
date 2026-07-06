import math
import requests
from datetime import datetime, timezone

ts = int(math.floor(datetime.now(timezone.utc).timestamp() / 300) * 300)
slug = f"btc-updown-5m-{ts}"
paths = [
    f"https://polymarket.com/api/crypto/price-to-beat/{slug}",
    f"https://polymarket.com/api/price-to-beat/{slug}",
    f"https://gamma-api.polymarket.com/events/slug/{slug}",
    f"https://data-api.polymarket.com/price-to-beat?slug={slug}",
]
for url in paths:
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "signull/0.1"})
        print(url, r.status_code, r.text[:300].replace("\n", " "))
    except Exception as e:
        print(url, "ERR", e)