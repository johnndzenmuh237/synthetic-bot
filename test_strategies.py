"""All three strategies are selectable and each one trades, on both timeframes.
Each strategy is driven through the real engine (mock broker) with candles that produce its signal."""
import itertools, time as _t, unittest
import pandas as pd
import test_bot as T                       # env, stubs, FakeBroker, helpers
import test_follow as F                    # (also installs the fake clock for bot_engine)
import bot_engine, config, database, strategy

GRAN = {"M1": 60, "M5": 300}


def prices(direction):
    down = [100 - 0.3 * i for i in range(60)]
    up = [down[-1] + 0.8 * i for i in range(1, 16)]
    return (down + up) if direction == "BUY" else [200 - p for p in (down + up)]


def candles(series, n, tf):
    """First n closed candles, the last one closing 5 seconds ago (fresh), then a forming candle."""
    g, now = GRAN[tf], int(_t.time())
    last_epoch = now - 5 - g
    out = []
    for i in range(n):
        p = series[i]
        out.append({"epoch": last_epoch - (n - 1 - i) * g, "open": p, "high": p + .1, "low": p - .1, "close": p})
    f = series[n - 1]
    out.append({"epoch": last_epoch + g, "open": f, "high": f, "low": f, "close": f})
    return out


def signal_prefix(strat, direction, tf):
    """The first prefix length at which the strategy itself produces a signal."""
    strategy._in_daily_reset_window = lambda now=None: False
    s = prices(direction)
    for n in range(45, len(s) + 1):
        c = candles(s, n, tf)[:-1]
        df = pd.DataFrame(c, columns=["epoch", "open", "high", "low", "close"])
        sig = strategy.evaluate(None, df, "R_50", strat, tf=tf)
        if sig and sig.direction == direction:
            return n
    return None


class T30_AllStrategies(unittest.TestCase):
    def test_every_strategy_trades_both_directions_on_both_timeframes(self):
        for strat, tf, direction in itertools.product(("EMA_CROSS", "REVERSAL_SCALPER"), ("M1", "M5"), ("BUY", "SELL")):
            with self.subTest(strategy=strat, tf=tf, direction=direction):
                n = signal_prefix(strat, direction, tf)
                self.assertIsNotNone(n, "no {} signal found".format(direction))
                s = T.make("demo", lot=1.5, maxpos=1, strategy=strat, timeframe=tf)
                s.broker = F.FollowBroker("demo")
                s.candles[tf] = candles(prices(direction), n, tf)
                s.candles["M5"] = s.candles["M5"] or candles(prices(direction), n, "M5")
                T.run(s, s._evaluate_locked)
                self.assertEqual([(p[1], p[2]) for p in T.FakeBroker.placed], [(direction, 1.5)])
                sig = [p for e, p in s.events if e == "signal"][-1]
                self.assertTrue(sig["taken"]); self.assertEqual(sig["strategy"], strat)

    def test_candle_follow_trades_both_timeframes(self):
        for tf in ("M1", "M5"):
            s = F.make_follow(tf=tf)
            async def go():
                F.at(s, 0, 100.5); await s._follow_step(); F.at(s, 6, 100.5); await s._follow_step()
            T.run(s, go)
            self.assertEqual([p[1] for p in T.FakeBroker.placed], ["BUY"], tf)

    def test_ema_strategies_only_react_to_the_selected_timeframe(self):
        for strat in ("EMA_CROSS", "REVERSAL_SCALPER"):
            s = T.make("demo", strategy=strat, timeframe="M5")
            calls = []
            async def fake_eval(c=calls): c.append(1)
            s._evaluate = fake_eval
            s.candles["M1"] = [{"epoch": 100, "open": 1, "high": 1, "low": 1, "close": 1}]
            s.candles["M5"] = [{"epoch": 0, "open": 1, "high": 1, "low": 1, "close": 1}]
            async def go():
                # a NEW M1 candle must not trigger the M5 strategy ...
                s._on_candle_tick({"underlying_symbol": "R_50", "granularity": 60, "open_time": 160, "epoch": 161,
                                   "open": 1, "high": 1, "low": 1, "close": 1})
                await asyncio_sleep()
                self.assertEqual(calls, [], strat)
                # ... a NEW M5 candle does
                s._on_candle_tick({"underlying_symbol": "R_50", "granularity": 300, "open_time": 300, "epoch": 301,
                                   "open": 1, "high": 1, "low": 1, "close": 1})
                await asyncio_sleep()
                self.assertEqual(calls, [1], strat)
            T.run(s, go)

    def test_all_three_strategies_selectable_in_dashboard_and_settings(self):
        import app
        c = app.app.test_client()
        c.post("/register", data={"username": "picker", "password": "password123", "confirm_password": "password123"})
        c.post("/login", data={"username": "picker", "password": "password123"})
        page = c.get("/dashboard").get_data(as_text=True)
        for key in ("CANDLE_FOLLOW", "REVERSAL_SCALPER", "EMA_CROSS"):
            self.assertIn('value="{}"'.format(key), page)
            r = c.post("/api/settings", json={"strategy": key, "timeframe": "M5"})
            self.assertTrue(r.get_json()["ok"], key)
            self.assertEqual(c.get("/api/settings").get_json()["saved"]["strategy"], key)
        for key in ("M1", "M5"):
            self.assertIn('value="{}"'.format(key), page)

    def test_switching_timeframe_live_restarts_evaluation(self):
        s = T.make("demo", strategy="EMA_CROSS", timeframe="M1")
        s._last_eval_epoch = 123
        s.update_settings(timeframe="M5")
        self.assertEqual((s.timeframe, s._last_eval_epoch), ("M5", None))


async def asyncio_sleep():
    import asyncio
    await asyncio.sleep(0)
    await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main(verbosity=2)