"""EMA 10/20 Trend strategy + trading-duration (session) tests. Offline: no Deriv contact."""
import os, sys, time, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
import test_bot as tb
import bot_engine, strategy, config
from ema_trend import prepare_trend, trend_state, exit_decision
from database import get_open_trades, get_recent_trades, save_bot_state, get_bot_state

strategy._in_daily_reset_window = lambda now=None: False


def candles(closes, gran=60, rng=0.5, end=None):
    """Closed candles ending 'now'. Each candle spans close +/- rng so the average range is 2*rng."""
    end = end or int(time.time())
    n = len(closes)
    return pd.DataFrame({"epoch": [end - gran * (n - i) for i in range(n)],
                         "open": closes, "high": [c + rng for c in closes],
                         "low": [c - rng for c in closes], "close": closes})


def ramp(start, step, n):
    return [start + step * i for i in range(1, n + 1)]


FLAT = [100.0 + (0.05 if i % 2 else -0.05) for i in range(45)]
DOWN = FLAT + ramp(FLAT[-1], -0.3, 45)
UP = FLAT + ramp(FLAT[-1], 0.3, 45)


class T100_TrendStates(unittest.TestCase):
    def test_sell_in_downtrend(self):
        st, why = trend_state(prepare_trend(candles(DOWN)))
        self.assertEqual(st, "SELL", why)
    def test_buy_in_uptrend(self):
        st, why = trend_state(prepare_trend(candles(UP)))
        self.assertEqual(st, "BUY", why)
    def test_existing_trend_is_traded_without_a_fresh_cross(self):
        long_up = FLAT + ramp(FLAT[-1], 0.3, 120)          # the cross happened ~120 candles ago
        d = prepare_trend(candles(long_up))
        self.assertTrue((d["gap"].iloc[-30:] > 0).all())    # no crossover anywhere in the last 30 candles
        self.assertEqual(trend_state(d)[0], "BUY")
        s = strategy.evaluate(None, candles(long_up), "R_75", "EMA_TREND")
        self.assertIsNotNone(s); self.assertEqual((s.direction, s.strategy), ("BUY", "EMA_TREND"))
    def test_new_crossover_is_picked_up(self):
        seen = []
        series = FLAT + ramp(FLAT[-1], -0.3, 30)
        for n in range(len(FLAT), len(series) + 1):
            s = strategy.evaluate(None, candles(series[:n]), "R_75", "EMA_TREND")
            seen.append(s.direction if s else None)
        self.assertIn("SELL", seen); self.assertNotIn("BUY", seen)
        self.assertIsNone(seen[0])                          # nothing while it was still flat
    def test_flat_market_is_consolidation(self):
        self.assertEqual(trend_state(prepare_trend(candles(FLAT)))[0], "NONE")
    def test_whipsaw_is_consolidation(self):
        zig = FLAT + [100 + (0.6 if (i // 2) % 2 else -0.6) for i in range(40)]
        self.assertEqual(trend_state(prepare_trend(candles(zig)))[0], "NONE")
        for n in range(60, len(zig) + 1):                  # never a signal anywhere in the chop
            self.assertIsNone(strategy.evaluate(None, candles(zig[:n]), "R_75", "EMA_TREND"), n)
    def test_weakening_trend_is_not_entered(self):
        weak = UP + ramp(UP[-1], 0.1, 3) + ramp(UP[-1] + 0.3, 0.0, 6)
        d = prepare_trend(candles(weak))
        self.assertEqual(trend_state(d)[0], "NONE")
    def test_too_little_data(self):
        self.assertEqual(trend_state(prepare_trend(candles(FLAT[:25])))[0], "NONE")


class T101_Exits(unittest.TestCase):
    def test_hold_while_trend_is_strong(self):
        for n in range(60, len(UP) + 1):
            self.assertIsNone(exit_decision(prepare_trend(candles(UP[:n])), "BUY"), n)
    def test_convergence_exits_before_the_cross(self):
        series = UP + ramp(UP[-1], 0.06, 4) + [UP[-1] + 0.24] * 8
        hit = None
        for n in range(len(UP), len(series) + 1):
            d = prepare_trend(candles(series[:n]))
            r = exit_decision(d, "BUY")
            if r:
                hit = (n, r, float(d["gap"].iloc[-1])); break
        self.assertIsNotNone(hit, "no exit while the averages came together")
        self.assertIn("converging", hit[1]); self.assertGreater(hit[2], 0)      # still BUY side: exit came EARLY
    def test_a_normal_pullback_is_not_an_exit(self):
        series = UP + [UP[-1] - 0.3, UP[-1] - 0.1] + ramp(UP[-1] - 0.1, 0.3, 12)
        for n in range(len(UP), len(series) + 1):
            self.assertIsNone(exit_decision(prepare_trend(candles(series[:n])), "BUY"), n)
    def test_cross_against_trade_exits(self):
        series = UP + ramp(UP[-1], -0.9, 25)
        reasons = [exit_decision(prepare_trend(candles(series[:n])), "BUY") for n in range(len(UP), len(series) + 1)]
        self.assertTrue(any(reasons))
        d = prepare_trend(candles(series))
        self.assertIsNotNone(exit_decision(d, "BUY"))
        self.assertEqual(trend_state(d)[0], "SELL")             # ...and then the new direction is taken
        self.assertIsNone(exit_decision(d, "SELL"))
    def test_sell_side_mirror(self):
        series = DOWN + ramp(DOWN[-1], -0.06, 4) + [DOWN[-1] - 0.24] * 8
        self.assertTrue(any(exit_decision(prepare_trend(candles(series[:n])), "SELL")
                            for n in range(len(DOWN), len(series) + 1)))


class T102_Timeframes(unittest.TestCase):
    def test_m5_uses_5_minute_freshness_and_signals(self):
        s = strategy.evaluate(None, candles(UP, gran=300), "R_75", "EMA_TREND", tf="M5")
        self.assertIsNotNone(s); self.assertEqual(s.direction, "BUY"); self.assertTrue(s.reason.startswith("M5"))
        s1 = strategy.evaluate(None, candles(UP, gran=60), "R_75", "EMA_TREND", tf="M1")
        self.assertTrue(s1.reason.startswith("M1"))
    def test_stale_m5_candle_gives_no_signal(self):
        old = candles(UP, gran=300, end=int(time.time()) - 3000)
        self.assertIsNone(strategy.evaluate(None, old, "R_75", "EMA_TREND", tf="M5"))
    def test_switching_timeframe_reassesses(self):
        s = tb.make(strategy="EMA_TREND", timeframe="M1")
        s._last_eval_epoch = 123
        s.update_settings(timeframe="M5")
        self.assertEqual(s.timeframe, "M5"); self.assertIsNone(s._last_eval_epoch)


def _open(s, direction="BUY", n=0):
    async def go():
        await tb.trade(s, direction, n)
    tb.run(s, go)
    return get_open_trades(s.user_id, "demo")[-1]


class T103_EngineExits(unittest.TestCase):
    def setUp(self):
        self.s = tb.make(lot=10.0, maxpos=3, strategy="EMA_TREND")
        self.conv = candles(UP + ramp(UP[-1], 0.06, 4) + [UP[-1] + 0.24] * 8)
        self.cross = candles(UP + ramp(UP[-1], -0.9, 25))
    def _trend_exit(self, df):
        async def go():
            await self.s._trend_exits(df)
        tb.run(self.s, go)
    def test_profit_is_protected_on_convergence(self):
        t = _open(self.s); self.s.broker.contracts[str(t["contract_id"])]["profit"] = 3.1
        self._trend_exit(candles(UP))                                     # strong trend: kept
        self.assertEqual(len(get_open_trades(self.s.user_id, "demo")), 1)
        d = None
        for n in range(len(UP), len(self.conv) + 1):
            if exit_decision(prepare_trend(self.conv.iloc[:n]), "BUY"):
                d = self.conv.iloc[:n]; break
        self._trend_exit(d)
        self.assertEqual(len(get_open_trades(self.s.user_id, "demo")), 0)
        row = [r for r in get_recent_trades(self.s.user_id, 20) if r["id"] == t["id"]][0]
        self.assertEqual(row["status"], "WIN"); self.assertAlmostEqual(row["pnl"], 3.1); self.assertIn("converging", row["exit_reason"])
        ok, why = self.s._entry_allowed(tb.sig("BUY"), 7000)              # cooldown after an exit
        self.assertFalse(ok); self.assertIn("Cooling down", why)
    def test_trade_is_not_held_against_the_opposite_trend_even_at_a_loss(self):
        self.s.loss_policy = "hold"
        t = _open(self.s); self.s.broker.contracts[str(t["contract_id"])]["profit"] = -4.0
        self._trend_exit(self.cross)
        self.assertEqual(len(get_open_trades(self.s.user_id, "demo")), 0)
    def test_no_duplicate_positions_in_one_trend(self):
        _open(self.s, "BUY", 0)
        ok, why = self.s._entry_allowed(tb.sig("BUY"), 5001)
        self.assertFalse(ok); self.assertIn("already open", why)
        self.assertEqual(len(get_open_trades(self.s.user_id, "demo")), 1)
    def test_evaluate_runs_exit_even_without_a_signal(self):
        t = _open(self.s); self.s.broker.contracts[str(t["contract_id"])]["profit"] = 1.0
        self.s.candles["M1"] = [dict(epoch=int(r.epoch), open=r.open, high=r.high, low=r.low, close=r.close)
                                for r in self.cross.itertuples()] + [dict(epoch=int(time.time()), open=1, high=1, low=1, close=1)]
        async def go():
            await self.s._evaluate_locked()
        tb.run(self.s, go)
        self.assertEqual(len(get_open_trades(self.s.user_id, "demo")), 0)


class T104_Session(unittest.TestCase):
    def test_choices_and_default(self):
        self.assertEqual(list(config.SESSION_CHOICES), ["1", "2", "4", "8", "12", "24"])
        self.assertEqual(config.DEFAULT_SESSION_HOURS, "24")
    def test_active_session_keeps_trading(self):
        s = tb.make(strategy="EMA_TREND", session_hours="2"); s.session_start = bot_engine.time.time() - 3600
        async def go():
            await s._check_session()
        tb.run(s, go)
        self.assertFalse(s._session_over); self.assertFalse(s._stop_requested)
    def test_session_end_stops_entries_closes_winners_and_stops(self):
        s = tb.make(lot=10.0, maxpos=3, strategy="EMA_TREND", session_hours="1")
        win = _open(s, "BUY", 0); s.broker.contracts[str(win["contract_id"])]["profit"] = 2.5
        s.session_start = bot_engine.time.time() - 3700
        async def go():
            await s._check_session()
        tb.run(s, go)
        self.assertTrue(s._session_over); self.assertTrue(s._stop_requested)
        self.assertEqual(len(get_open_trades(s.user_id, "demo")), 0)      # the winner was banked
        ok, why = s._entry_allowed(tb.sig("SELL"), 9000)
        self.assertFalse(ok); self.assertIn("session", why)
        self.assertIn("1 hour", s._stop_note)
    def test_losing_trade_follows_loss_policy_at_session_end(self):
        hold = tb.make(lot=10.0, maxpos=3, strategy="EMA_TREND", session_hours="1", loss_policy="hold")
        close = tb.make(lot=10.0, maxpos=3, strategy="EMA_TREND", session_hours="1", loss_policy="close")
        for s in (hold, close):
            t = _open(s, "BUY", 0); s.broker.contracts[str(t["contract_id"])]["profit"] = -3.0
            s.session_start = bot_engine.time.time() - 3700
            async def go(s=s):
                await s._check_session()
            tb.run(s, go)
        self.assertEqual(len(get_open_trades(hold.user_id, "demo")), 1)
        self.assertEqual(len(get_open_trades(close.user_id, "demo")), 0)
    def test_close_on_stop_closes_everything(self):
        s = tb.make(lot=10.0, maxpos=3, strategy="EMA_TREND", session_hours="1", close_on_stop=True)
        t = _open(s, "BUY", 0); s.broker.contracts[str(t["contract_id"])]["profit"] = -3.0
        s.session_start = bot_engine.time.time() - 3700
        async def go():
            await s._check_session()
        tb.run(s, go)
        self.assertEqual(len(get_open_trades(s.user_id, "demo")), 0)
    def test_session_end_removes_resume_state(self):
        s = tb.make(strategy="EMA_TREND", session_hours="1")
        save_bot_state(s.user_id, dict(symbol="R_50", mode="demo", lot_size=1.0, max_positions=1, strategy="EMA_TREND",
                                       loss_policy="hold", close_on_stop=False, timeframe="M1", session_hours="1"))
        self.assertIsNotNone(get_bot_state(s.user_id))
        s.session_start = bot_engine.time.time() - 3700
        async def go():
            await s._check_session()
        tb.run(s, go)
        self.assertIsNone(get_bot_state(s.user_id))
    def test_changing_length_moves_the_end(self):
        s = tb.make(strategy="EMA_TREND", session_hours="1"); s.session_start = 1000.0
        self.assertAlmostEqual(s._session_end(), 1000 + 3600)
        s.update_settings(session_hours="4"); self.assertAlmostEqual(s._session_end(), 1000 + 4 * 3600)
        s.update_settings(session_hours="7"); self.assertEqual(s.session_hours, "4")   # not a listed choice: ignored


class T105_Wiring(unittest.TestCase):
    def test_candle_follow_is_gone_from_the_dashboard(self):
        self.assertNotIn("CANDLE_FOLLOW", config.STRATEGIES)
        self.assertEqual(config.DEFAULT_STRATEGY, "EMA_TREND")
        self.assertIn("REVERSAL_SCALPER", config.STRATEGIES); self.assertIn("EMA_CROSS", config.STRATEGIES)
    def test_saved_candle_follow_becomes_ema_trend(self):
        import database
        uid = tb.make().user_id + 500
        con = database._conn()
        con.execute("INSERT OR REPLACE INTO user_settings (user_id, symbol, mode, lot_size, max_positions, strategy, loss_policy) VALUES (?,?,?,?,?,?,?)",
                    (uid, "R_50", "demo", 1.0, 1, "CANDLE_FOLLOW", "hold")); con.commit(); con.close()
        self.assertEqual(database.get_settings(uid)["strategy"], "EMA_TREND")
        self.assertEqual(database.get_settings(uid)["session_hours"], "24")
    def test_dashboard_and_api_carry_the_trading_duration(self):
        import app, database, token_vault
        c = app.app.test_client()
        c.post("/register", data={"username": "durtest", "password": "password123", "confirm_password": "password123"})
        c.post("/login", data={"username": "durtest", "password": "password123"})
        database.set_user_token(database.get_user("durtest")["id"], "demo", token_vault.encrypt("pat_test_token_1234"))
        page = c.get("/dashboard").get_data(as_text=True)
        self.assertIn('id="session-hours-select"', page)
        for h in ("1", "2", "4", "8", "12", "24"):
            self.assertIn('<option value="{}"'.format(h), page)
            r = c.post("/api/settings", json={"session_hours": h, "strategy": "EMA_TREND", "timeframe": "M5"})
            self.assertTrue(r.get_json()["ok"], h)
            self.assertEqual(c.get("/api/settings").get_json()["saved"]["session_hours"], h)
        self.assertFalse(c.post("/api/settings", json={"session_hours": "36"}).get_json()["ok"])
        self.assertEqual(c.get("/api/settings").get_json()["saved"]["session_hours"], "24")
        r = c.post("/api/settings", json={"strategy": "CANDLE_FOLLOW"}).get_json()     # an old client still works
        self.assertEqual(r["saved"]["strategy"], "EMA_TREND")
    def test_strategy_info(self):
        i = strategy.strategy_info("EMA_TREND"); self.assertEqual((i["fast"], i["slow"]), (10, 20))


if __name__ == "__main__":
    unittest.main()