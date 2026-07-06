import asyncio
import json
import websockets

URL = "wss://ws-live-data.polymarket.com"


async def try_sub(sub):
    print("\n===", sub, "===")
    async with websockets.connect(URL, ping_interval=None) as ws:
        await ws.send(json.dumps({"action": "subscribe", "subscriptions": [sub]}))
        for _ in range(4):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=6)
                print(raw[:500])
            except TimeoutError:
                print("timeout")
                break


async def main():
    await try_sub({"topic": "crypto_prices", "type": "*", "filters": "btcusdt"})
    await try_sub({"topic": "crypto_prices", "type": "update", "filters": "btcusdt"})
    await try_sub({"topic": "crypto_prices", "type": "update"})


asyncio.run(main())