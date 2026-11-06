"""EMA 21/50 Trend strategy (pullback entries) + trading-duration (session) tests. Offline: no Deriv contact."""
import os, sys, time, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
import test_bot as tb
import bot_engine, strategy, config
from ema_trend import prepare_trend, trend_state, exit_decision, pullback_entry
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


FLAT = [100.0 + (0.05 if i % 2 else -0.05) for i in range(80)]
RUN = FLAT + ramp(FLAT[-1], 0.3, 40)                                   # a strong up-trend, price far above EMA21
PULL = RUN + ramp(RUN[-1], -0.35, 8)                                   # pull back to EMA21 ...
BOUNCE = PULL + ramp(PULL[-1], 0.3, 15)                                # ... then up again
UP = RUN
DOWN = [200.0 - x for x in RUN]                                        # mirror image
PULL_DN = [200.0 - x for x in PULL]
BOUNCE_DN = [200.0 - x for x in BOUNCE]
CONV = RUN + [RUN[-1]] * 45                                            # trend stalls: the averages come together


def walk(series, start=100, gran=60, strat="EMA_TREND", tf="M1"):
    """Evaluate every closed candle in turn, like the live bot. -> [(n, direction or None, reason)]"""
    out = []
    for n in range(start, len(series) + 1):
        sg = strategy.evaluate(None, candles(series[:n], gran=gran), "R_75", strat, tf=tf)
        out.append((n, sg.direction if sg else None, sg.reason if sg else ""))
    return out


class T100_PullbackEntries(unittest.TestCase):
    def test_no_entry_while_the_trend_is_running_far_from_ema21(self):
        for series, name in ((RUN, "up"), (DOWN, "down")):
            self.assertTrue(all(d is None for _, d, _ in walk(series, 100)), name)
    def test_buy_on_pullback_retest(self):
        hits = [(n, r) for n, d, r in walk(BOUNCE) if d == "BUY"]
        self.assertTrue(hits); self.assertTrue(all("Pullback retest of EMA21" in r for _, r in hits))
        self.assertTrue(all(n > len(RUN) + 2 for n, _ in hits))           # only after price came back to EMA21
        self.assertTrue(all(d != "SELL" for _, d, _ in walk(BOUNCE)))
    def test_sell_on_pullback_retest(self):
        res = walk(BOUNCE_DN)
        self.assertTrue([n for n, d, _ in res if d == "SELL"]); self.assertTrue(all(d != "BUY" for _, d, _ in res))
    def test_retest_that_just_happened_is_taken_at_start(self):
        n = [n for n, d, _ in walk(BOUNCE) if d == "BUY"][0]
        sg = strategy.evaluate(None, candles(BOUNCE[:n]), "R_75", "EMA_TREND")         # bot switched on right at the retest
        self.assertIsNotNone(sg); self.assertEqual((sg.direction, sg.strategy), ("BUY", "EMA_TREND"))
    def test_a_retest_one_or_two_candles_old_still_counts(self):
        ns = [n for n, d, r in walk(BOUNCE) if d == "BUY" and ("1 candle" in r or "2 candle" in r)]
        self.assertTrue(ns)
    def test_not_entered_in_the_middle_of_the_trend(self):
        last_buy = max(n for n, d, _ in walk(BOUNCE) if d == "BUY")
        late = [(n, d, r) for n, d, r in walk(BOUNCE, last_buy + 1)]
        self.assertTrue(late); self.assertTrue(all(d is None for _, d, _ in late))
        st, why = pullback_entry(prepare_trend(candles(BOUNCE)))
        self.assertEqual(st, "NONE"); self.assertTrue("already running" in why or "waiting for price to pull back" in why, why)
    def test_start_in_the_middle_of_a_long_trend_waits(self):
        long_run = FLAT + ramp(FLAT[-1], 0.3, 120)
        self.assertIsNone(strategy.evaluate(None, candles(long_run), "R_75", "EMA_TREND"))
    def test_trend_that_broke_ema21_is_not_bought(self):
        deep = RUN + ramp(RUN[-1], -0.35, 14)                              # fell through EMA21 and well below
        for n in range(len(RUN) + 1, len(deep) + 1):
            d = prepare_trend(candles(deep[:n]))
            if float(d["close"].iloc[-1]) < float(d["ema_fast"].iloc[-1]):          # closed under EMA21: never a BUY
                self.assertEqual(pullback_entry(d)[0], "NONE", n)
    def test_flat_market_is_consolidation(self):
        self.assertTrue(all(d is None for _, d, _ in walk(FLAT + FLAT, 90)))
    def test_whipsaw_is_consolidation(self):
        zig = FLAT + [100 + (0.6 if (i // 2) % 2 else -0.6) for i in range(60)]
        self.assertTrue(all(d is None for _, d, _ in walk(zig, 90)))
    def test_too_little_data(self):
        self.assertEqual(trend_state(prepare_trend(candles(FLAT[:30])))[0], "NONE")
        self.assertIsNone(strategy.evaluate(None, candles(FLAT[:30]), "R_75", "EMA_TREND"))
    def test_uses_ema_21_and_50(self):
        d = prepare_trend(candles(RUN))
        self.assertAlmostEqual(float(d["ema_fast"].iloc[-1]), float(pd.Series(RUN).ewm(span=21, adjust=False).mean().iloc[-1]), places=6)
        self.assertAlmostEqual(float(d["ema_slow"].iloc[-1]), float(pd.Series(RUN).ewm(span=50, adjust=False).mean().iloc[-1]), places=6)
    def test_trend_state_still_reads_a_healthy_trend(self):
        self.assertEqual(trend_state(prepare_trend(candles(RUN)))[0], "BUY")
        self.assertEqual(trend_state(prepare_trend(candles(DOWN)))[0], "SELL")


class T101_Exits(unittest.TestCase):
    def test_hold_while_trend_is_strong(self):
        for n in range(100, len(BOUNCE) + 1):                            # including the whole pullback
            self.assertIsNone(exit_decision(prepare_trend(candles(BOUNCE[:n])), "BUY"), n)
    def test_convergence_exits_before_the_cross(self):
        hit = None
        for n in range(len(RUN), len(CONV) + 1):
            d = prepare_trend(candles(CONV[:n])); r = exit_decision(d, "BUY")
            if r:
                hit = (n, r, float(d["gap"].iloc[-1])); break
        self.assertIsNotNone(hit, "no exit while the averages came together")
        self.assertIn("converging", hit[1]); self.assertGreater(hit[2], 0)      # still on the BUY side: exit came EARLY
    def test_cross_against_trade_exits(self):
        series = RUN + ramp(RUN[-1], -0.9, 40)
        d = prepare_trend(candles(series))
        self.assertIsNotNone(exit_decision(d, "BUY"))
        self.assertIsNone(exit_decision(d, "SELL"))
    def test_sell_side_mirror(self):
        conv_dn = [200.0 - x for x in CONV]
        self.assertTrue(any(exit_decision(prepare_trend(candles(conv_dn[:n])), "SELL") for n in range(len(RUN), len(conv_dn) + 1)))


class T102_Timeframes(unittest.TestCase):
    def test_all_three_timeframes_give_the_same_entries(self):
        base = [n for n, d, _ in walk(BOUNCE) if d == "BUY"]
        self.assertTrue(base)
        for gran, tf in ((300, "M5"), (900, "M15")):
            res = walk(BOUNCE, gran=gran, tf=tf)
            self.assertEqual([n for n, d, _ in res if d == "BUY"], base, tf)
            self.assertTrue(all(r.startswith(tf) for _, d, r in res if d), tf)
            sells = [n for n, d, _ in walk(BOUNCE_DN, gran=gran, tf=tf) if d == "SELL"]
            self.assertTrue(sells, tf)
    def test_stale_candle_gives_no_signal(self):
        n = [n for n, d, _ in walk(BOUNCE) if d == "BUY"][0]
        for gran, tf, age in ((60, "M1", 600), (300, "M5", 3000), (900, "M15", 5000)):
            old = candles(BOUNCE[:n], gran=gran, end=int(time.time()) - age)
            self.assertIsNone(strategy.evaluate(None, old, "R_75", "EMA_TREND", tf=tf), tf)
    def test_m15_selectable_and_applied_live(self):
        self.assertEqual(config.TIMEFRAMES["M15"], 900); self.assertIn("M15", config.TIMEFRAME_CHOICES)
        self.assertIn("M15", config.ANALYSIS_TF)                        # its candles are loaded and streamed
        s = tb.make(strategy="EMA_TREND", timeframe="M15")
        self.assertEqual(s.timeframe, "M15"); self.assertIn("15-minute", " ".join(s._rules()))
        s.update_settings(timeframe="M1"); s._last_eval_epoch = 5
        s.update_settings(timeframe="M15"); self.assertEqual(s.timeframe, "M15"); self.assertIsNone(s._last_eval_epoch)
        s.update_settings(timeframe="M30"); self.assertEqual(s.timeframe, "M15")     # not offered: ignored
    def test_m15_api_and_dashboard(self):
        import app, database, token_vault
        c = app.app.test_client()
        c.post("/register", data={"username": "tf15", "password": "password123", "confirm_password": "password123"})
        c.post("/login", data={"username": "tf15", "password": "password123"})
        database.set_user_token(database.get_user("tf15")["id"], "demo", token_vault.encrypt("pat_test_token_1234"))
        page = c.get("/dashboard").get_data(as_text=True)
        self.assertIn('id="chart-M15"', page); self.assertIn("switchChart('M15')", page); self.assertIn('<option value="M15"', page)
        r = c.post("/api/settings", json={"timeframe": "M15", "strategy": "EMA_TREND"}).get_json()
        self.assertTrue(r["ok"]); self.assertEqual(r["saved"]["timeframe"], "M15")
        self.assertFalse(c.post("/api/settings", json={"timeframe": "M30"}).get_json()["ok"])
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
        self.conv = candles(CONV)
        self.cross = candles(RUN + ramp(RUN[-1], -0.9, 40))
        for n in range(len(RUN), len(self.conv) + 1):
            if exit_decision(prepare_trend(self.conv.iloc[:n]), "BUY"):
                self.conv_hit = self.conv.iloc[:n]; break
    def _trend_exit(self, df):
        async def go():
            await self.s._trend_exits(df)
        tb.run(self.s, go)
    def _open_with(self, profit):
        t = _open(self.s); self.s.broker.contracts[str(t["contract_id"])]["profit"] = profit
        return t
    def _row(self, t):
        return [r for r in get_recent_trades(self.s.user_id, 20) if r["id"] == t["id"]][0]
    def test_profit_is_protected_on_convergence(self):
        t = self._open_with(3.1)
        self._trend_exit(candles(RUN))                                    # strong trend: kept
        self.assertEqual(len(get_open_trades(self.s.user_id, "demo")), 1)
        self._trend_exit(self.conv_hit)
        self.assertEqual(len(get_open_trades(self.s.user_id, "demo")), 0)
        row = self._row(t)
        self.assertEqual(row["status"], "WIN"); self.assertAlmostEqual(row["pnl"], 3.1); self.assertIn("converging", row["exit_reason"])
        ok, why = self.s._entry_allowed(tb.sig("BUY"), 7000)              # cooldown after an exit
        self.assertFalse(ok); self.assertIn("Cooling down", why)
    def test_convergence_never_sells_a_losing_trade(self):
        for policy, profit in (("hold", -2.0), ("close", -2.0), ("hold", 0.0), ("close", 0.0)):
            self.setUp(); self.s.loss_policy = policy
            t = self._open_with(profit)
            self._trend_exit(self.conv_hit)
            self.assertFalse(self.s.broker.contracts[str(t["contract_id"])]["is_sold"], (policy, profit))
            self.assertEqual(len(get_open_trades(self.s.user_id, "demo")), 1)
    def test_losing_trade_is_held_when_trend_turns_and_policy_is_hold(self):
        self.s.loss_policy = "hold"
        t = self._open_with(-4.0)
        self._trend_exit(self.cross)
        self.assertEqual(len(get_open_trades(self.s.user_id, "demo")), 1)
        self.assertFalse(self.s.broker.contracts[str(t["contract_id"])]["is_sold"])
    def test_losing_trade_closed_when_trend_turns_and_policy_is_close(self):
        self.s.loss_policy = "close"
        self._open_with(-4.0)
        self._trend_exit(self.cross)
        self.assertEqual(len(get_open_trades(self.s.user_id, "demo")), 0)
    def test_profitable_trade_closed_when_trend_turns(self):
        self.s.loss_policy = "hold"
        self._open_with(1.5)
        self._trend_exit(self.cross)
        self.assertEqual(len(get_open_trades(self.s.user_id, "demo")), 0)
    def test_no_duplicate_positions_in_one_trend(self):
        _open(self.s, "BUY", 0)
        ok, why = self.s._entry_allowed(tb.sig("BUY"), 5001)
        self.assertFalse(ok); self.assertIn("already open", why)
        self.assertEqual(len(get_open_trades(self.s.user_id, "demo")), 1)
    def test_evaluate_runs_exit_even_without_a_signal(self):
        self._open_with(1.0)
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
        i = strategy.strategy_info("EMA_TREND"); self.assertEqual((i["fast"], i["slow"]), (21, 50))
        self.assertTrue(any("pullback" in r.lower() for r in i["rules"]))


if __name__ == "__main__":
    unittest.main()