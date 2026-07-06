"""Quick test of Polymarket RTDS Chainlink BTC feed."""
import asyncio
import json

import websockets

URL = "wss://ws-live-data.polymarket.com"


async def main():
    async with websockets.connect(URL, ping_interval=None) as ws:
        await ws.send(json.dumps({
            "action": "subscribe",
            "subscriptions": [{
                "topic": "crypto_prices_chainlink",
                "type": "*",
                "filters": '{"symbol":"btc/usd"}',
            }],
        }))
        for _ in range(5):
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            print(raw[:500])


if __name__ == "__main__":
    asyncio.run(main())