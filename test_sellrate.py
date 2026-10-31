"""T50: sells are paced and a Deriv 'rate limit' answer is retried, not reported as a failed close."""
import asyncio, os, sys, time, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_follow as F
import test_bot as T
import bot_engine, database


class RateLimitBroker(F.FollowBroker):
    limit_first = 0          # answer "rate limit" this many times before accepting
    calls = []
    async def close_trade(self, cid):
        RateLimitBroker.calls.append(time.monotonic())
        if RateLimitBroker.limit_first > 0:
            RateLimitBroker.limit_first -= 1
            self.last_error = "Sell failed: You have reached the rate limit for sell."
            return {}
        return await super().close_trade(cid)


class T50_SellRate(unittest.TestCase):
    def setUp(self):
        RateLimitBroker.calls = []; RateLimitBroker.limit_first = 0
        self._gap, self._retries = bot_engine.SELL_MIN_GAP_SEC, bot_engine.SELL_RATE_RETRIES
        bot_engine.SELL_MIN_GAP_SEC = 0.05
    def tearDown(self):
        bot_engine.SELL_MIN_GAP_SEC, bot_engine.SELL_RATE_RETRIES = self._gap, self._retries
    def _session(self, n=3):
        s = F.make_follow(maxpos=n)
        s.broker = RateLimitBroker("demo")
        return s
    def _open_all(self, s):
        async def go():
            F.at(s, 0, 100.5); await s._follow_step(); F.at(s, 6, 100.5); await s._follow_step()
        F.run(s, go)
    def test_rate_limit_is_retried_and_trade_still_closes(self):
        s = self._session(1); self._open_all(s)
        t = database.get_open_trades(s.user_id, "demo")[0]
        RateLimitBroker.limit_first = 2
        async def go():
            old = asyncio.sleep
            return await s._sell_position(t, "test", fast=True)
        orig = asyncio.sleep
        async def quick(d, *a, **k): return await orig(min(d, 0.02))
        asyncio.sleep = quick
        try:
            ok = F.run(s, go)
        finally:
            asyncio.sleep = orig
        self.assertTrue(ok)
        self.assertEqual(len(RateLimitBroker.calls), 3)                     # 2 limited + 1 accepted
        self.assertFalse([e for e, p in s.events if e == "trade_error"])   # user never sees an error
        self.assertEqual(database.get_open_trades(s.user_id, "demo"), [])
    def test_real_refusal_is_not_retried(self):
        s = self._session(1); self._open_all(s)
        t = database.get_open_trades(s.user_id, "demo")[0]
        F.FollowBroker.fail_sell = {str(t["contract_id"])}
        ok = F.run(s, lambda: s._sell_position(t, "test", fast=True))
        self.assertFalse(ok)
        self.assertEqual(len(RateLimitBroker.calls), 1)
        self.assertTrue([e for e, p in s.events if e == "trade_error"])
    def test_persistent_rate_limit_gives_up_cleanly(self):
        s = self._session(1); self._open_all(s)
        t = database.get_open_trades(s.user_id, "demo")[0]
        RateLimitBroker.limit_first = 99
        bot_engine.SELL_RATE_RETRIES = 2
        orig = asyncio.sleep
        async def quick(d, *a, **k): return await orig(min(d, 0.02))
        asyncio.sleep = quick
        try:
            ok = F.run(s, lambda: s._sell_position(t, "test", fast=True))
        finally:
            asyncio.sleep = orig
        self.assertFalse(ok)
        self.assertEqual(len(database.get_open_trades(s.user_id, "demo")), 1)   # still open, retried later by the bot
    def test_simultaneous_sells_are_spaced_out(self):
        s = self._session(3); self._open_all(s)
        trades = database.get_open_trades(s.user_id, "demo")
        self.assertEqual(len(trades), 3)
        bot_engine.SELL_MIN_GAP_SEC = 0.15
        async def go():
            await asyncio.gather(*[s._sell_position(t, "x", fast=True) for t in trades])
        F.run(s, go)
        gaps = [b - a for a, b in zip(RateLimitBroker.calls, RateLimitBroker.calls[1:])]
        self.assertEqual(len(gaps), 2)
        self.assertTrue(all(g >= 0.12 for g in gaps), gaps)


if __name__ == "__main__":
    unittest.main()