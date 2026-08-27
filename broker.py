"""
broker.py — Async Deriv WebSocket API client
Fixed: proper auth error reporting, balance loading, reconnection
"""
import asyncio
import json
from typing import Callable, Dict, Optional
import aiohttp
import websockets
from config import (
    DERIV_WS_URL, DERIV_APP_ID,
    DERIV_REST_BASE, DERIV_OTP_TTL_SEC,
    DERIV_DEMO_TOKEN, DERIV_LIVE_TOKEN,
    CANDLE_COUNT, DURATION, DURATION_UNIT, TIMEFRAMES,
)
from logger import log


class DerivBroker:
    def __init__(self, mode="demo"):
        self.mode        = mode
        self.token       = DERIV_DEMO_TOKEN if mode == "demo" else DERIV_LIVE_TOKEN
        self.ws          = None
        self.balance     = 0.0
        self.account_id  = ""
        self.currency    = "USD"
        self.connected   = False
        self._req_id     = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._on_candle  = None
        self._subscriptions = set()   # {(symbol, tf_key), ...} — resubscribed after a reconnect
        self._reconnecting  = False
        self._closing        = False  # True once disconnect() is called, stops auto-reconnect

    def _next_id(self):
        self._req_id += 1
        return self._req_id

    async def _send(self, payload, timeout=30.0):
        if not self.connected or self.ws is None:
            raise ConnectionError("Not connected to Deriv (reconnecting...)")
        rid = self._next_id()
        payload["req_id"] = rid
        future = asyncio.get_event_loop().create_future()
        self._pending[rid] = future
        try:
            await self.ws.send(json.dumps(payload))
        except Exception as e:
            self._pending.pop(rid, None)
            self.connected = False
            self._trigger_reconnect()
            raise ConnectionError("WebSocket send failed, reconnecting: {}".format(e))
        return await asyncio.wait_for(future, timeout=timeout)

    def _trigger_reconnect(self):
        if not self._closing and not self._reconnecting:
            asyncio.create_task(self._reconnect())

    async def _listen(self, on_candle: Callable):
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                    rid = msg.get("req_id")
                    if rid and rid in self._pending:
                        fut = self._pending.pop(rid)
                        if not fut.done():
                            if "error" in msg:
                                fut.set_exception(
                                    Exception(msg["error"].get("message", "Unknown error")))
                            else:
                                fut.set_result(msg)
                    elif msg.get("msg_type") == "ohlc":
                        on_candle(msg["ohlc"])
                except Exception as e:
                    log.error("Message parse error: %s", e)
        except Exception as e:
            log.error("WebSocket listener closed: %s", e)
        finally:
            self.connected = False
            # Fail any requests still waiting on this dead connection so
            # callers don't hang until their timeout.
            for rid, fut in list(self._pending.items()):
                if not fut.done():
                    fut.set_exception(ConnectionError("WebSocket disconnected"))
            self._pending.clear()
            self._trigger_reconnect()

    async def _reconnect(self):
        """OTPs are single-use/short-lived, so reconnecting means running the
        full GET /accounts -> POST /otp -> connect flow again, then
        re-subscribing to whatever candle streams were active."""
        if self._reconnecting or self._closing:
            return
        self._reconnecting = True
        backoff = 2
        while not self.connected and not self._closing:
            try:
                log.warning("Reconnecting to Deriv (%s)...", self.mode.upper())
                url = await self._get_otp_ws_url()
                self.ws = await websockets.connect(
                    url, ping_interval=20, ping_timeout=20, close_timeout=10)
                asyncio.create_task(self._listen(self._on_candle))
                self.connected = True
                log.info("✅ Reconnected | Account: %s | Balance: %.2f %s",
                         self.account_id, self.balance, self.currency)
                for symbol, tf_key in list(self._subscriptions):
                    try:
                        await self._subscribe_candles_raw(symbol, tf_key)
                    except Exception as e:
                        log.error("Resubscribe failed [%s %s]: %s", symbol, tf_key, e)
            except Exception as e:
                log.error("Reconnect attempt failed: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
        self._reconnecting = False

    async def _read_response(self, resp):
        """Read as text first, then try JSON — Deriv sometimes returns
        plain-text error bodies which .json() would choke on."""
        raw = await resp.text()
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {"errors": [{"message": raw.strip() or "Empty response body"}]}

    async def _get_otp_ws_url(self) -> str:
        """
        New Deriv auth flow for 'pat_...' Personal Access Tokens:
        1. GET  /accounts            (Bearer token) -> find the account matching self.mode
        2. POST /accounts/{id}/otp   (Bearer token) -> one-time password + ready WS URL
        The OTP is single-use and expires in ~DERIV_OTP_TTL_SEC seconds, so this
        must be called fresh immediately before every websocket connect — never cached.
        """
        headers = {
            "Deriv-App-ID": str(DERIV_APP_ID),
            "Authorization": "Bearer {}".format(self.token),
        }
        # Deriv's account_type field uses "real"/"demo", while this bot's
        # internal mode uses "live"/"demo" — map between them here only.
        account_type = "real" if self.mode == "live" else "demo"

        async with aiohttp.ClientSession() as session:
            # 1. List accounts and find the one matching demo/real
            try:
                async with session.get(
                        "{}/accounts".format(DERIV_REST_BASE),
                        headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    body = await self._read_response(resp)
                    if resp.status == 401:
                        raise ConnectionError(
                            "Invalid or expired {} API token. Go to app.deriv.com → "
                            "Security and safety → API token, generate a new one, and "
                            "update your .env file.".format(self.mode.upper()))
                    if resp.status != 200:
                        errs = body.get("errors", [{"message": "Unknown error"}])
                        raise ConnectionError(
                            "Deriv /accounts error (HTTP {}): {}".format(
                                resp.status, errs[0].get("message")))
            except aiohttp.ClientError as e:
                raise ConnectionError("Could not reach Deriv REST API: {}".format(e))

            accounts = body.get("data", [])
            if not isinstance(accounts, list):
                accounts = [accounts]
            account = next(
                (a for a in accounts if a.get("account_type") == account_type), None)
            if not account:
                raise ConnectionError(
                    "This token has no {} account attached. Create/select a {} "
                    "account on app.deriv.com and generate the token from there."
                    .format(self.mode.upper(), self.mode.upper()))

            self.account_id = account.get("account_id", "")
            self.balance    = float(account.get("balance", 0))
            self.currency   = account.get("currency", "USD")

            # 2. Request the OTP + WebSocket URL for that account
            try:
                async with session.post(
                        "{}/accounts/{}/otp".format(DERIV_REST_BASE, self.account_id),
                        headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    body = await self._read_response(resp)
                    if resp.status != 200:
                        errs = body.get("errors", [{"message": "Unknown error"}])
                        raise ConnectionError(
                            "Deriv OTP request error (HTTP {}): {}".format(
                                resp.status, errs[0].get("message")))
                    ws_url = body["data"]["url"]
            except aiohttp.ClientError as e:
                raise ConnectionError("Could not reach Deriv REST API: {}".format(e))

        return ws_url

    async def connect(self, on_candle: Callable):
        # Validate token is set
        if not self.token or self.token in ("YOUR_DEMO_TOKEN_HERE", "YOUR_LIVE_TOKEN_HERE", ""):
            raise ConnectionError(
                "No valid {} token found. Please add your Deriv {} API token "
                "to the .env file.".format(self.mode.upper(), self.mode.upper()))

        log.info("Connecting to Deriv (%s)...", self.mode.upper())
        self._on_candle = on_candle
        self._closing   = False

        # New-style "pat_..." tokens: fetch a fresh OTP and connect using
        # the URL Deriv gives back. The OTP already embeds authentication,
        # so no `{"authorize": token}` message is sent or needed.
        url = await self._get_otp_ws_url()

        try:
            self.ws = await websockets.connect(
                url, ping_interval=20, ping_timeout=20,
                close_timeout=10)
        except Exception as e:
            raise ConnectionError(
                "Could not open Deriv WebSocket (OTP may have expired — it's only "
                "valid for {}s): {}".format(DERIV_OTP_TTL_SEC, e))

        # Start listener background task
        asyncio.create_task(self._listen(on_candle))
        self.connected = True

        log.info("✅ Connected | Account: %s | Balance: %.2f %s | Mode: %s",
                 self.account_id, self.balance, self.currency, self.mode.upper())

    async def disconnect(self):
        self._closing  = True
        self.connected = False
        # Cancel pending futures
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

    async def get_candles(self, symbol, tf_key):
        resp = await self._send({
            "ticks_history": symbol,
            "style":         "candles",
            "granularity":   TIMEFRAMES[tf_key],
            "count":         CANDLE_COUNT,
            "end":           "latest",
        }, timeout=60)
        if "error" in resp:
            log.error("get_candles error [%s %s]: %s",
                      symbol, tf_key, resp["error"]["message"])
            return []
        candles = resp.get("candles", [])
        log.info("Fetched %d candles | %s %s", len(candles), symbol, tf_key)
        return candles

    async def _subscribe_candles_raw(self, symbol, tf_key):
        """Sends the subscribe request without touching self._subscriptions
        — used both for a fresh subscribe and for resubscribing after a
        reconnect (where the entry is already tracked)."""
        await self._send({
            "ticks_history": symbol,
            "style":         "candles",
            "granularity":   TIMEFRAMES[tf_key],
            "count":         1,
            "end":           "latest",
            "subscribe":     1,
        }, timeout=20)
        log.info("Subscribed to live candles | %s %s", symbol, tf_key)

    async def subscribe_candles(self, symbol, tf_key):
        try:
            await self._subscribe_candles_raw(symbol, tf_key)
            self._subscriptions.add((symbol, tf_key))
        except Exception as e:
            log.error("subscribe_candles error [%s %s]: %s", symbol, tf_key, e)

    async def get_balance(self):
        try:
            # The connection is already scoped to one account via the OTP
            # URL (see _get_otp_ws_url) — "account": "current" was the old
            # authorize-based way to select an account and is no longer a
            # valid property on this connection.
            resp = await self._send({"balance": 1}, timeout=10)
            if "error" not in resp:
                self.balance = float(resp["balance"]["balance"])
        except Exception as e:
            log.error("get_balance error: %s", e)
        return self.balance

    async def place_trade(self, symbol, direction, stake):
        ctype = "CALL" if direction == "BUY" else "PUT"

        # Get proposal
        try:
            prop = await self._send({
                "proposal":      1,
                "amount":        stake,
                "basis":         "stake",
                "contract_type": ctype,
                "currency":      self.currency,
                "symbol":        symbol,
                "duration":      DURATION,
                "duration_unit": DURATION_UNIT,
            }, timeout=15)
        except Exception as e:
            log.error("Proposal error: %s", e)
            return {}

        if "error" in prop:
            log.error("Proposal rejected [%s %s $%.2f]: %s",
                      direction, symbol, stake, prop["error"]["message"])
            return {}

        # Buy
        try:
            buy = await self._send({
                "buy":   prop["proposal"]["id"],
                "price": stake,
            }, timeout=15)
        except Exception as e:
            log.error("Buy error: %s", e)
            return {}

        if "error" in buy:
            log.error("Buy rejected: %s", buy["error"]["message"])
            return {}

        contract = buy["buy"]
        log.info("✅ TRADE OPENED | %s %s | Contract=%s | Stake=$%.2f",
                 direction, symbol, contract["contract_id"], stake)
        return contract

    async def close_trade(self, contract_id):
        try:
            resp = await self._send({"sell": contract_id, "price": 0}, timeout=15)
        except Exception as e:
            log.error("close_trade error: %s", e)
            return {}
        if "error" in resp:
            log.error("Close rejected [%s]: %s", contract_id, resp["error"]["message"])
            return {}
        log.info("🔒 TRADE CLOSED | Contract=%s", contract_id)
        return resp.get("sell", {})
