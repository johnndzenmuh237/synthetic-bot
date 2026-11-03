"""T51: every request type is paced and a Deriv 'rate limit' answer is retried transparently."""
import asyncio, json, os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_bot  # noqa (env + stubs)
import broker as B


class FakeWS:
    """Answers like Deriv: rate-limit errors for the first N requests of a kind, then success."""
    def __init__(self, br, limit=None):
        self.br, self.limit, self.sent = br, dict(limit or {}), []
    async def send(self, raw):
        p = json.loads(raw); self.sent.append((asyncio.get_event_loop().time(), p))
        kind = next(k for k in ("proposal_open_contract", "proposal", "buy", "sell") if k in p)
        rid = p["req_id"]
        if self.limit.get(kind, 0) > 0:
            self.limit[kind] -= 1
            msg = {"req_id": rid, "error": {"code": "RateLimit",
                   "message": "You have reached the rate limit for %s." % kind}}
        else:
            msg = {"req_id": rid, kind: {"id": "P1", "ask_price": 1.0, "contract_id": 7}, "echo": 1}
        fut = self.br._pending.get(rid)
        def deliver():
            if "error" in msg:
                fut.set_exception(B.DerivError(msg["error"]["message"], msg["error"]["code"]))
            else:
                fut.set_result(msg)
        asyncio.get_event_loop().call_soon(deliver)


def make(limit=None):
    b = B.DerivBroker("demo"); b.connected = True
    b.ws = FakeWS(b, limit)
    return b


class T51_BrokerRateLimit(unittest.TestCase):
    def setUp(self):
        self._g, self._r = dict(B._GAPS), B.RATE_LIMIT_RETRIES
        B._GAPS.update(proposal=0.05, buy=0.05, sell=0.05, proposal_open_contract=0.02)
    def tearDown(self):
        B._GAPS.clear(); B._GAPS.update(self._g); B.RATE_LIMIT_RETRIES = self._r
    def test_proposal_rate_limit_is_retried_and_trade_placed(self):
        async def go():
            b = make({"proposal": 2})
            sleep = asyncio.sleep
            r = await b.place_trade("R_75", "BUY", 1.0)
            return b, r
        orig = asyncio.sleep
        async def quick(d, *a, **k): return await orig(min(d, 0.03))
        asyncio.sleep = quick
        try:
            b, r = asyncio.run(go())
        finally:
            asyncio.sleep = orig
        self.assertEqual(r.get("contract_id"), 7)
        self.assertEqual(b.last_error, "")
        self.assertEqual(sum(1 for _, p in b.ws.sent if "proposal" in p), 3)    # 2 limited + 1 ok
    def test_requests_of_one_kind_are_spaced(self):
        async def go():
            b = make()
            await asyncio.gather(*[b._send({"proposal": 1}) for _ in range(4)])
            return [t for t, p in b.ws.sent]
        times = asyncio.run(go())
        gaps = [y - x for x, y in zip(times, times[1:])]
        self.assertTrue(all(g >= 0.04 for g in gaps), gaps)
    def test_gives_up_after_the_retry_limit_with_the_original_message(self):
        B.RATE_LIMIT_RETRIES = 2
        async def go():
            b = make({"proposal": 99})
            return await b.place_trade("R_75", "BUY", 1.0), b
        orig = asyncio.sleep
        async def quick(d, *a, **k): return await orig(min(d, 0.02))
        asyncio.sleep = quick
        try:
            r, b = asyncio.run(go())
        finally:
            asyncio.sleep = orig
        self.assertEqual(r, {})
        self.assertIn("rate limit", b.last_error.lower())
        self.assertEqual(sum(1 for _, p in b.ws.sent if "proposal" in p), 3)
    def test_other_errors_are_not_retried(self):
        async def go():
            b = make()
            fut_orig = b.ws.send
            async def bad(raw):
                p = json.loads(raw); fut = b._pending[p["req_id"]]
                asyncio.get_event_loop().call_soon(lambda: fut.set_exception(B.DerivError("Market is closed", "MarketIsClosed")))
            b.ws.send = bad
            try:
                await b._send({"proposal": 1})
            except Exception as e:
                return str(e)
        self.assertEqual(asyncio.run(go()), "Market is closed")
    def test_error_text_unchanged_for_callers(self):
        e = B.DerivError("hello", "X"); self.assertEqual(str(e), "hello")
        self.assertTrue(B.is_rate_limit(B.DerivError("x", "RateLimit")))
        self.assertTrue(B.is_rate_limit(Exception("You have reached the rate limit for sell.")))
        self.assertFalse(B.is_rate_limit(Exception("Market is closed")))


if __name__ == "__main__":
    unittest.main()


class T52_LongTrades(unittest.TestCase):
    def test_longest_duration_deriv_accepts_is_used_and_remembered(self):
        sent = []
        class WS(FakeWS):
            async def send(self, raw):
                p = json.loads(raw)
                if "proposal" in p and "duration" in p:
                    sent.append((p["duration"], p["duration_unit"]))
                    if (p["duration"], p["duration_unit"]) in ((1, "d"), (12, "h")):
                        fut = self.br._pending.get(p["req_id"])
                        asyncio.get_event_loop().call_soon(lambda: fut.set_exception(
                            B.DerivError("Trading is not offered for this duration.", "ContractBuyValidationError")))
                        return
                return await super().send(raw)
        async def go():
            b = B.DerivBroker("demo"); b.connected = True; b.ws = WS(b)
            r1 = await b.place_trade("R_75", "BUY", 1.0)
            r2 = await b.place_trade("R_75", "SELL", 1.0)
            return r1, r2
        g = dict(B._GAPS); B._GAPS.update(proposal=0.01, buy=0.01, sell=0.01, proposal_open_contract=0.01)
        B.LONG_TRADES = True
        try:
            r1, r2 = asyncio.run(go())
        finally:
            B.LONG_TRADES = False
            B._GAPS.clear(); B._GAPS.update(g)
        self.assertTrue(r1 and r2)
        self.assertEqual(sent[:3], [(1, "d"), (12, "h"), (4, "h")])        # walks down the ladder once ...
        self.assertEqual(sent[3], (4, "h"))                                # ... then remembers what worked
    def test_other_rejections_are_not_retried_with_other_durations(self):
        class WS(FakeWS):
            async def send(self, raw):
                p = json.loads(raw); self.sent.append(p)
                fut = self.br._pending.get(p["req_id"])
                asyncio.get_event_loop().call_soon(lambda: fut.set_exception(B.DerivError("Market is closed", "MarketIsClosed")))
        async def go():
            b = B.DerivBroker("demo"); b.connected = True; b.ws = WS(b)
            return await b.place_trade("R_75", "BUY", 1.0), b
        B.LONG_TRADES = True
        try:
            r, b = asyncio.run(go())
        finally:
            B.LONG_TRADES = False
        self.assertEqual(r, {}); self.assertIn("Market is closed", b.last_error); self.assertEqual(len(b.ws.sent), 1)