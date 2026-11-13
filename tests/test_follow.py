"""Candle Follow tests: confirmed entries, immediate exits, reversals, 1M/5M, 7 positions.
Mock broker + fake clock; no network."""
import asyncio, time, types, unittest
import test_bot as T                      # sets up env, stubs, FakeBroker, helpers
import bot_engine, database, candle_follow

clock = [1_000_000.0]
_real_time = time


class _Clock(types.SimpleNamespace):
    pass


bot_engine.time = _Clock(time=lambda: clock[0], sleep=_real_time.sleep,
                         strptime=_real_time.strptime)


class FollowBroker(T.FakeBroker):
    fail_sell = set()
    async def close_trade(self, cid):
        if str(cid) in FollowBroker.fail_sell:
            self.last_error = "not valid to sell"
            return {}
        c = self.contracts[str(cid)]
        c["is_sold"] = True
        c["sell_price"] = c["buy_price"] + c["profit"]
        return {"sold_for": c["sell_price"]}


bot_engine.DerivBroker = FollowBroker
T.FakeBroker.placed = []


def make_follow(maxpos=1, tf="M1", lot=1.0):
    FollowBroker.fail_sell = set()
    s = T.make("demo", lot=lot, maxpos=maxpos, strategy="CANDLE_FOLLOW", timeframe=tf)
    s.broker = FollowBroker("demo")
    gran = 60 if tf == "M1" else 300
    base = 1_000_000 - (1_000_000 % gran) - gran * 40
    closed = [{"epoch": base + i * gran, "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0}
              for i in range(40)]
    forming = {"epoch": base + 40 * gran, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0}
    s.candles[tf] = closed + [forming]
    s.pnl_now.clear(); s.pnl_peak.clear()
    clock[0] = 1_000_000.0
    s._last_tick = clock[0]
    return s


def at(s, t, price, tf=None):
    """Move the clock to t seconds and set the live price of the forming candle."""
    tf = tf or s.timeframe
    clock[0] = 1_000_000.0 + t
    s._last_tick = clock[0]
    c = s.candles[tf][-1]
    c["close"] = price; c["high"] = max(c["high"], price); c["low"] = min(c["low"], price)


async def step(s):
    await s._follow_step()


def run(s, fn):
    return T.run(s, fn)


def active(s):
    return database.get_active_trades(s.user_id, "demo")


def open_dirs(s):
    return sorted(t["direction"] for t in database.get_open_trades(s.user_id, "demo"))


class T20_Logic(unittest.TestCase):
    def test_zone_and_band(self):
        b = candle_follow.band_size([{"high": 101, "low": 100}] * 20, 0.1)
        self.assertAlmostEqual(b, 0.1)
        self.assertEqual(candle_follow.zone_of(100.2, 100, b), "BUY")
        self.assertEqual(candle_follow.zone_of(99.8, 100, b), "SELL")
        self.assertEqual(candle_follow.zone_of(100.05, 100, b), "NONE")

    def test_confirmation_needs_continuous_hold(self):
        tr = candle_follow.DirectionTracker(5)
        self.assertEqual(tr.update(0, "BUY")[1], False)
        self.assertEqual(tr.update(4, "BUY")[1], False)
        self.assertEqual(tr.update(5, "BUY")[1], True)
        tr.update(6, "NONE")                                # a break resets it
        self.assertEqual(tr.update(10, "BUY")[1], False)

    def test_exits_for(self):
        ts = [{"direction": "BUY"}, {"direction": "SELL"}]
        self.assertEqual(candle_follow.exits_for("SELL", ts), [ts[0]])
        self.assertEqual(candle_follow.exits_for("BUY", ts), [ts[1]])
        self.assertEqual(candle_follow.exits_for("NONE", ts), [])


class T21_BuySellFlow(unittest.TestCase):
    def test_full_flow_buy_then_sell_then_buy(self):
        s = make_follow()
        async def go():
            at(s, 0, 100.5);  await step(s)                       # bullish seen
            self.assertEqual(active(s), [])                        # not confirmed yet
            at(s, 3, 100.6);  await step(s)
            self.assertEqual(active(s), [])                        # still waiting
            at(s, 5.5, 100.7); await step(s)                       # confirmed -> BUY
            self.assertEqual(open_dirs(s), ["BUY"])
            at(s, 20, 101.0); await step(s)                        # keeps running while bullish
            self.assertEqual(open_dirs(s), ["BUY"])
            # candle turns bearish: BUY must close AT ONCE, SELL must NOT open yet
            at(s, 21, 99.7);  await step(s)
            self.assertEqual(open_dirs(s), [])
            self.assertEqual([p[1] for p in T.FakeBroker.placed], ["BUY"])
            at(s, 24, 99.6);  await step(s)
            self.assertEqual(open_dirs(s), [])                     # 3s bearish: not confirmed
            at(s, 26.5, 99.6); await step(s)                       # 5.5s bearish: confirmed -> SELL
            self.assertEqual(open_dirs(s), ["SELL"])
            # candle turns bullish: SELL closes at once, BUY waits for confirmation
            at(s, 40, 100.4); await step(s)
            self.assertEqual(open_dirs(s), [])
            at(s, 42, 100.5); await step(s)
            self.assertEqual(open_dirs(s), [])
            at(s, 45.5, 100.5); await step(s)
            self.assertEqual(open_dirs(s), ["BUY"])
            self.assertEqual([p[1] for p in T.FakeBroker.placed], ["BUY", "SELL", "BUY"])
        run(s, go)

    def test_unconfirmed_direction_never_enters(self):
        s = make_follow()
        async def go():
            for t, p in [(0, 100.5), (2, 100.5), (3, 100.0), (4, 100.5), (7, 100.5), (8, 100.0), (9, 99.5)]:
                at(s, t, p); await step(s)                         # flips before 5s each time
            self.assertEqual(T.FakeBroker.placed, [])
        run(s, go)

    def test_neutral_keeps_position_open(self):
        s = make_follow()
        async def go():
            at(s, 0, 100.5); await step(s); at(s, 6, 100.5); await step(s)
            self.assertEqual(open_dirs(s), ["BUY"])
            at(s, 10, 100.02); await step(s)                       # back inside the band
            self.assertEqual(open_dirs(s), ["BUY"])
        run(s, go)

    def test_exit_does_not_depend_on_profit(self):
        s = make_follow()
        async def go():
            at(s, 0, 100.5); await step(s); at(s, 6, 100.5); await step(s)
            cid = list(s.broker.contracts)[0]
            s.broker.contracts[cid]["profit"] = -0.4               # losing trade
            at(s, 8, 99.6); await step(s)
            self.assertEqual(open_dirs(s), [])                     # closed anyway, not held for recovery
            st = database.get_stats(s.user_id, "demo")
            self.assertEqual(st["losses"], 1)
        run(s, go)

    def test_profit_protection(self):
        s = make_follow()
        async def go():
            at(s, 0, 100.5); await step(s); at(s, 6, 100.5); await step(s)
            cid = list(s.broker.contracts)[0]
            s.broker.contracts[cid]["profit"] = 0.6
            await s._reconcile_trades(); self.assertEqual(open_dirs(s), ["BUY"])
            s.broker.contracts[cid]["profit"] = -0.05              # was winning, now turning negative
            await s._reconcile_trades()
            at(s, 9, 100.5)                                        # direction still bullish
            await s._follow_exits(s.tracker.zone)
            self.assertEqual(open_dirs(s), [])
        run(s, go)


class T22_Timeframes(unittest.TestCase):
    def _feed(self, s, tf):
        gran = 60 if tf == "M1" else 300
        s._last_tick = clock[0]
        s._on_candle_tick({"underlying_symbol": "R_50", "granularity": gran,
                           "open_time": s.candles[tf][-1]["epoch"], "epoch": clock[0],
                           "open": 100, "high": 101, "low": 99, "close": 100.4})

    def test_only_selected_timeframe_drives_strategy(self):
        for tf, other in (("M1", "M5"), ("M5", "M1")):
            s = make_follow(tf=tf)
            for o in ("M1", "M5"):
                if o != tf and not s.candles.get(o):
                    s.candles[o] = [dict(s.candles[tf][0])] * 8
            calls = []
            s._schedule_follow = lambda c=calls: c.append(1)
            async def go():
                self._feed(s, other); self.assertEqual(calls, [])
                self._feed(s, tf);    self.assertEqual(calls, [1])
            run(s, go)

    def test_m5_flow_uses_m5_candle(self):
        s = make_follow(tf="M5")
        s.candles["M1"] = [{"epoch": 1, "open": 100, "high": 101, "low": 99, "close": 100}] * 10
        async def go():
            s.candles["M1"][-1] = dict(s.candles["M1"][-1], close=90.0)    # M1 screams bearish
            at(s, 0, 100.5); await step(s); at(s, 6, 100.5); await step(s)
            self.assertEqual(open_dirs(s), ["BUY"])                       # M5 decides
        run(s, go)

    def test_timeframe_can_change_while_running(self):
        s = make_follow(tf="M1")
        s.update_settings(timeframe="M5")
        self.assertEqual(s.timeframe, "M5"); self.assertEqual(s.tracker.zone, "NONE")
        s.update_settings(timeframe="H4")                                  # invalid -> ignored
        self.assertEqual(s.timeframe, "M5")


class T23_MultiPositions(unittest.TestCase):
    def test_seven_positions_open_together_with_own_slots(self):
        s = make_follow(maxpos=7)
        async def go():
            at(s, 0, 100.5); await step(s); at(s, 6, 100.5); await step(s)
            self.assertEqual(open_dirs(s), ["BUY"] * 7)
            self.assertEqual(sorted(t["position_num"] for t in active(s)), [1, 2, 3, 4, 5, 6, 7])
            at(s, 7, 100.6); await step(s)
            self.assertEqual(len(T.FakeBroker.placed), 7)                   # never more than the cap
        run(s, go)

    def test_all_seven_close_on_reversal(self):
        s = make_follow(maxpos=7)
        async def go():
            at(s, 0, 100.5); await step(s); at(s, 6, 100.5); await step(s)
            at(s, 8, 99.6); await step(s)
            self.assertEqual(open_dirs(s), [])
            self.assertEqual(database.get_stats(s.user_id, "demo")["total"], 7)
        run(s, go)

    def test_one_failed_close_does_not_block_the_others(self):
        s = make_follow(maxpos=7)
        async def go():
            at(s, 0, 100.5); await step(s); at(s, 6, 100.5); await step(s)
            stuck = list(s.broker.contracts)[2]
            FollowBroker.fail_sell = {stuck}
            at(s, 8, 99.6); await step(s)
            self.assertEqual(len(database.get_open_trades(s.user_id, "demo")), 1)   # only the stuck one
            at(s, 9, 99.5); await step(s)                                          # backoff: not hammered
            self.assertEqual(len(database.get_open_trades(s.user_id, "demo")), 1)
            FollowBroker.fail_sell = set()
            clock[0] += 30; s._last_tick = clock[0]
            await s._follow_exits("SELL")
            self.assertEqual(len(database.get_open_trades(s.user_id, "demo")), 0)
        run(s, go)

    def test_freed_slot_is_refilled_only_while_direction_confirmed(self):
        s = make_follow(maxpos=3)
        async def go():
            at(s, 0, 100.5); await step(s); at(s, 6, 100.5); await step(s)
            t = database.get_open_trades(s.user_id, "demo")[1]
            s._record_close(t, 0.5, 1, "expired")                                   # one expires
            at(s, 7, 100.5); await step(s)
            self.assertEqual(open_dirs(s), ["BUY"] * 3)                             # refilled
            self.assertEqual(sorted(x["position_num"] for x in active(s)), [1, 2, 3])
        run(s, go)

    def test_positions_update_payload(self):
        s = make_follow(maxpos=4)
        async def go():
            at(s, 0, 100.5); await step(s); at(s, 6, 100.5); await step(s)
            cid = list(s.broker.contracts)[0]; s.broker.contracts[cid]["profit"] = 0.42
            await s._reconcile_trades(); s.events.clear(); s._emit_positions()
            ev = [p for e, p in s.events if e == "positions_update"][-1]
            self.assertEqual(len(ev["positions"]), 4)
            self.assertEqual([p["slot"] for p in ev["positions"]], [1, 2, 3, 4])
            first = ev["positions"][0]
            self.assertFalse(first["empty"]); self.assertEqual(first["profit"], 0.42)
        run(s, go)


class T24_Safety(unittest.TestCase):
    def test_no_entry_when_price_feed_stale(self):
        s = make_follow()
        async def go():
            at(s, 0, 100.5); await step(s)
            at(s, 6, 100.5); s._last_tick = clock[0] - 60; await step(s)
            self.assertEqual(T.FakeBroker.placed, [])
        run(s, go)

    def test_refused_order_pauses_retries(self):
        s = make_follow()
        s.broker.mode_behavior = "reject"
        async def go():
            at(s, 0, 100.5); await step(s); at(s, 6, 100.5); await step(s)
            at(s, 7, 100.5); await step(s); at(s, 8, 100.5); await step(s)
            self.assertEqual(len(T.FakeBroker.placed), 1)                           # not hammering Deriv
        run(s, go)

    def test_opposite_not_opened_while_old_position_still_open(self):
        s = make_follow()
        async def go():
            at(s, 0, 100.5); await step(s); at(s, 6, 100.5); await step(s)
            FollowBroker.fail_sell = set(s.broker.contracts)                        # cannot be closed
            at(s, 8, 99.6); await step(s); at(s, 14, 99.6); await step(s)
            self.assertEqual([p[1] for p in T.FakeBroker.placed], ["BUY"])          # no hedge
        run(s, go)

    def test_validate_timeframe(self):
        import app
        base = database.get_settings(1)
        for good in ("M1", "M5"):
            cfg, err = app.validate_config({"timeframe": good}, base); self.assertEqual(err, "")
        cfg, err = app.validate_config({"timeframe": "M15"}, base); self.assertTrue(err)


class T25_CloseAll(unittest.TestCase):
    def test_close_all_closes_every_trade_and_holds_new_ones(self):
        s = make_follow(maxpos=7)
        async def go():
            at(s, 0, 100.5); await step(s); at(s, 6, 100.5); await step(s)
            self.assertEqual(len(open_dirs(s)), 7)
            res = await s.close_all_manual(hold=True)
            self.assertEqual((res["attempted"], res["closed"], res["remaining"]), (7, 7, 0))
            self.assertTrue(s.entries_hold)
            placed = len(T.FakeBroker.placed)
            at(s, 10, 100.6); await step(s); at(s, 20, 100.6); await step(s)   # still bullish and confirmed
            self.assertEqual(len(T.FakeBroker.placed), placed)                 # nothing new while on hold
            s.set_hold(False)                                                  # Resume Trading
            at(s, 21, 100.6); await step(s)
            self.assertEqual(len(open_dirs(s)), 7)                             # trades again
        run(s, go)

    def test_close_all_without_hold_keeps_trading(self):
        s = make_follow(maxpos=2)
        async def go():
            at(s, 0, 100.5); await step(s); at(s, 6, 100.5); await step(s)
            res = await s.close_all_manual(hold=False)
            self.assertFalse(res["hold"]); self.assertEqual(res["closed"], 2)
            at(s, 7, 100.5); await step(s)
            self.assertEqual(len(open_dirs(s)), 2)                             # bot reopened (no hold)
        run(s, go)

    def test_one_trade_that_cannot_be_closed_does_not_stop_the_others(self):
        s = make_follow(maxpos=4)
        async def go():
            at(s, 0, 100.5); await step(s); at(s, 6, 100.5); await step(s)
            FollowBroker.fail_sell = {list(s.broker.contracts)[1]}
            res = await s.close_all_manual(hold=True)
            self.assertEqual((res["closed"], res["remaining"]), (3, 1))
            self.assertTrue(res["error"])
        run(s, go)

    def test_hold_survives_restart_and_fresh_start_clears_it(self):
        s = make_follow()
        cfg = dict(database.DEFAULT_SETTINGS)
        database.save_bot_state(s.user_id, cfg)
        s.set_hold(True)
        row = [b for b in database.get_all_bot_states() if b["user_id"] == s.user_id][0]
        self.assertTrue(row["entries_hold"])
        database.set_entries_hold(s.user_id, False)
        self.assertFalse(database.get_bot_state(s.user_id)["entries_hold"])
        database.clear_bot_state(s.user_id)

    def test_routes(self):
        import app, threading
        c = app.app.test_client()
        c.post("/register", data={"username": "closer", "password": "password123", "confirm_password": "password123"})
        c.post("/login", data={"username": "closer", "password": "password123"})
        uid = database.get_user("closer")["id"]
        self.assertEqual(c.post("/api/bot/close_all", json={}).status_code, 400)       # no bot running
        self.assertEqual(c.post("/api/bot/resume").status_code, 400)
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        calls = []
        class Dummy:
            alive = True; _stop_requested = False
            def __init__(d): d.loop = loop
            async def close_all_manual(d, hold): calls.append(hold); return {"attempted": 3, "closed": 3, "remaining": 0, "hold": hold, "unconfirmed_orders": 0, "error": ""}
            def set_hold(d, hold): calls.append(("resume", hold))
        app.active_sessions[uid] = Dummy()
        try:
            j = c.post("/api/bot/close_all", json={"hold": True}).get_json()
            self.assertTrue(j["ok"]); self.assertEqual(j["closed"], 3)
            c.post("/api/bot/close_all", json={"hold": False})
            self.assertTrue(c.post("/api/bot/resume").get_json()["ok"])
            self.assertEqual(calls, [True, False, ("resume", False)])
        finally:
            app.active_sessions.pop(uid, None); loop.call_soon_threadsafe(loop.stop)


if __name__ == "__main__":
    unittest.main(verbosity=2)