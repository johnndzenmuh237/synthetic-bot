"""
check_symbols.py — Ask Deriv directly which symbols are available for
YOUR specific account (not just documentation guesswork).

Run with:
    python check_symbols.py

This calls active_symbols AFTER authorizing with your demo token, since
available symbols can differ by landing company / account type. It
prints whether R_75 (and a few others) actually show up as tradeable
for this exact account, and whether each is currently open for trading.
"""
import asyncio
import json
import os
from dotenv import load_dotenv

load_dotenv()

APP_ID     = os.getenv("DERIV_APP_ID", "1089")
DEMO_TOKEN = os.getenv("DERIV_DEMO_TOKEN", "")
URL = "wss://ws.derivws.com/websockets/v3?app_id={}".format(APP_ID)

CHECK_SYMBOLS = ["R_75", "R_100", "R_50", "R_25", "R_10",
                  "1HZ75V", "1HZ100V", "BOOM500", "CRASH500"]


async def main():
    import websockets

    print("Connecting and authorizing...")
    async with websockets.connect(URL) as ws:
        await ws.send(json.dumps({"authorize": DEMO_TOKEN}))
        auth_resp = json.loads(await ws.recv())
        if "error" in auth_resp:
            print("AUTH FAILED:", auth_resp["error"])
            return
        print("Authorized as:", auth_resp["authorize"]["loginid"])
        print("Landing company:", auth_resp["authorize"].get("landing_company_name"))
        print()

        await ws.send(json.dumps({
            "active_symbols": "brief",
            "product_type": "basic",
        }))
        resp = json.loads(await ws.recv())

        if "error" in resp:
            print("active_symbols FAILED:", resp["error"])
            return

        symbols = resp.get("active_symbols", [])
        by_code = {s["symbol"]: s for s in symbols}

        print("Total symbols available for this account:", len(symbols))
        print("-" * 60)
        for code in CHECK_SYMBOLS:
            if code in by_code:
                s = by_code[code]
                print("{:10s} FOUND   | display={:30s} | exchange_is_open={} | market={}".format(
                    code, s.get("display_name", ""), s.get("exchange_is_open"), s.get("market")))
            else:
                print("{:10s} NOT FOUND in active_symbols for this account".format(code))

        # Also try actually subscribing to R_75 ticks (not candles) as a
        # second, independent check.
        print()
        print("Testing direct tick subscribe on R_75...")
        await ws.send(json.dumps({"ticks": "R_75", "subscribe": 1}))
        tick_resp = json.loads(await ws.recv())
        print(json.dumps(tick_resp, indent=2)[:800])


if __name__ == "__main__":
    asyncio.run(main())
