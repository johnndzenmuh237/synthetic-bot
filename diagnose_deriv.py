"""
diagnose_deriv.py — Standalone connection test, run this directly:
    python diagnose_deriv.py

Tests the NEW Deriv auth flow used by "pat_..." Personal Access Tokens:
  1. GET  /trading/v1/options/accounts           (Bearer token)
  2. POST /trading/v1/options/accounts/{id}/otp  (Bearer token)
  3. Connect the returned wss:// URL directly (OTP = auth, no
     `{"authorize": token}` message needed).

Prints exactly what each step returns so you can see precisely where
things fail, instead of a generic "InvalidToken" from deep inside the bot.
"""
import asyncio
import json
import os

import aiohttp
import websockets
from dotenv import load_dotenv

load_dotenv()

APP_ID     = os.getenv("DERIV_APP_ID", "1089")
DEMO_TOKEN = os.getenv("DERIV_DEMO_TOKEN", "")
LIVE_TOKEN = os.getenv("DERIV_LIVE_TOKEN", "")

REST_BASE = "https://api.derivws.com/trading/v1/options"


async def _read_response(resp):
    """Read the response as text first, then try to parse as JSON.
    Deriv sometimes returns plain-text error bodies, which aiohttp's
    .json() refuses to auto-parse (ContentTypeError) — this avoids that."""
    raw = await resp.text()
    try:
        return json.loads(raw), raw
    except (json.JSONDecodeError, ValueError):
        return None, raw


async def test_token(label, mode, token):
    print("=" * 60)
    print("Testing: {}".format(label))
    print("Token set?   :", bool(token), "| length:", len(token))
    if not token:
        print("(skipping — no token set for this mode)")
        print()
        return

    headers = {
        "Deriv-App-ID": str(APP_ID),
        "Authorization": "Bearer {}".format(token),
    }

    async with aiohttp.ClientSession() as session:
        # Step 1: list accounts
        print("-" * 60)
        print("Step 1: GET {}/accounts".format(REST_BASE))
        try:
            async with session.get("{}/accounts".format(REST_BASE), headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                body, raw = await _read_response(resp)
                print("HTTP status:", resp.status)
                print("Content-Type:", resp.headers.get("Content-Type"))
                if body is not None:
                    print("Response:", json.dumps(body, indent=2))
                else:
                    print("Raw response body (non-JSON):", repr(raw))
                if resp.status != 200 or body is None:
                    print("❌ FAILED at accounts step.")
                    print()
                    return
        except Exception as e:
            print("❌ FAILED to reach REST API:", repr(e))
            print()
            return

        accounts = body.get("data", [])
        if not isinstance(accounts, list):
            accounts = [accounts]
        account = next((a for a in accounts if a.get("account_type") == mode), None)
        if not account:
            print("❌ No '{}' account found on this token. Available types: {}".format(
                mode, [a.get("account_type") for a in accounts]))
            print()
            return

        account_id = account["account_id"]
        print("✅ Found {} account: {} | balance {} {}".format(
            mode, account_id, account.get("balance"), account.get("currency")))

        # Step 2: get OTP + ws url
        print("-" * 60)
        print("Step 2: POST {}/accounts/{}/otp".format(REST_BASE, account_id))
        try:
            async with session.post("{}/accounts/{}/otp".format(REST_BASE, account_id),
                                     headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=15)) as resp:
                body, raw = await _read_response(resp)
                print("HTTP status:", resp.status)
                print("Content-Type:", resp.headers.get("Content-Type"))
                if body is not None:
                    print("Response:", json.dumps(body, indent=2))
                else:
                    print("Raw response body (non-JSON):", repr(raw))
                if resp.status != 200 or body is None:
                    print("❌ FAILED at OTP step.")
                    print()
                    return
                ws_url = body["data"]["url"]
        except Exception as e:
            print("❌ FAILED to reach REST API:", repr(e))
            print()
            return

        # Step 3: connect the websocket (OTP is short-lived — do this immediately)
        print("-" * 60)
        print("Step 3: connecting to", ws_url)
        try:
            async with websockets.connect(ws_url) as ws:
                print("✅ WebSocket handshake succeeded — token is fully valid.")
                await ws.send(json.dumps({"ping": 1}))
                resp2 = await ws.recv()
                print("Ping response:", resp2)
        except Exception as e:
            print("❌ FAILED to open WebSocket (OTP may have expired, it's single-use "
                  "and only valid ~120s):", repr(e))
    print()


async def main():
    print("Using app_id :", repr(APP_ID))
    print()
    await test_token("DEMO", "demo", DEMO_TOKEN)
    await test_token("LIVE", "real", LIVE_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
