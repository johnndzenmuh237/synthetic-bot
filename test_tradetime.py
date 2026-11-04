"""Trade time (how long each trade runs) selectable on the dashboard, Demo and Live alike."""
import asyncio, json, os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_bot as tb
import bot_engine, broker as B, config, database
import token_vault


class T90_TradeTime(unittest.TestCase):
    def _client(self, name):
        import app
        c = app.app.test_client()
        c.post("/register", data={"username": name, "password": "password123", "confirm_password": "password123"})
        c.post("/login", data={"username": name, "password": "password123"})
        database.set_user_token(database.get_user(name)["id"], "demo", token_vault.encrypt("pat_tt_1234"))
        return c
    def test_validation_save_and_dashboard_dropdown(self):
        c = self._client("tt_user")
        page = c.get("/dashboard").get_data(as_text=True)
        self.assertIn('id="trade-time-select"', page)
        for label in ("1 minute", "15 minutes", "4 hours", "1 day"): self.assertIn(label, page)
        self.assertEqual(c.get("/api/settings").get_json()["saved"]["trade_time"], config.DEFAULT_TRADE_TIME)
        self.assertEqual(c.post("/api/settings", json={"trade_time": "99y"}).status_code, 400)
        r = c.post("/api/settings", json={"trade_time": "4h"}).get_json()
        self.assertTrue(r["ok"]); self.assertEqual(r["saved"]["trade_time"], "4h")
        self.assertEqual(c.get("/api/settings").get_json()["saved"]["trade_time"], "4h")     # persisted
        self.assertIn("trade time 4h", [a["detail"] for a in c.get("/api/activity").get_json()["activity"]][0])
    def test_parse(self):
        P = bot_engine.parse_trade_time
        self.assertEqual((P("15m"), P("4h"), P("1d"), P("1m")), ((15, "m"), (4, "h"), (1, "d"), (1, "m")))
        self.assertEqual(P("junk"), P(config.DEFAULT_TRADE_TIME)); self.assertEqual(bot_engine._duration_seconds("2h"), 7200)
    def _order(self, mode, tt):
        s = tb.make(mode=mode, lot=1.0, trade_time=tt)
        ok, _ = tb.run(s, lambda: tb.trade(s, "BUY", 0))
        self.assertTrue(ok)
        return s.broker.trade_duration
    def test_the_chosen_time_is_used_for_demo_and_live_orders(self):
        for mode in ("demo", "live"):
            self.assertEqual(self._order(mode, "4h"), (4, "h"), mode)
            self.assertEqual(self._order(mode, "1d"), (1, "d"), mode)
            self.assertEqual(self._order(mode, "5m"), (5, "m"), mode)
    def test_can_be_changed_while_running_for_new_trades(self):
        s = tb.make(lot=1.0, trade_time="15m"); s.update_settings(trade_time="2h")
        self.assertEqual(s.trade_time, "2h"); self.assertEqual(s.applied_config()["trade_time"], "2h")
        s.update_settings(trade_time="bogus"); self.assertEqual(s.trade_time, "2h")
    def test_saved_with_the_running_bot_and_restored(self):
        c = self._client("tt_state"); uid = database.get_user("tt_state")["id"]
        cfg = dict(database.DEFAULT_SETTINGS); cfg["trade_time"] = "12h"
        database.save_bot_state(uid, cfg)
        self.assertEqual(database.get_bot_state(uid)["trade_time"], "12h")
        self.assertEqual([b for b in database.get_all_bot_states() if b["user_id"] == uid][0]["trade_time"], "12h")
        database.clear_bot_state(uid)


class T91_BrokerUsesChosenTime(unittest.TestCase):
    def _run(self, duration, reject=()):
        sent = []
        class WS(__import__("test_ratelimit_broker").FakeWS):
            async def send(self, raw):
                p = json.loads(raw)
                if "proposal" in p and "duration" in p:
                    sent.append((p["duration"], p["duration_unit"]))
                    if (p["duration"], p["duration_unit"]) in reject:
                        fut = self.br._pending.get(p["req_id"])
                        asyncio.get_event_loop().call_soon(lambda: fut.set_exception(
                            B.DerivError("Trading is not offered for this duration.", "ContractBuyValidationError")))
                        return
                return await super().send(raw)
        async def go():
            b = B.DerivBroker("demo"); b.connected = True; b.ws = WS(b); b.trade_duration = duration
            return await b.place_trade("R_75", "BUY", 1.0), b
        g = dict(B._GAPS); B._GAPS.update(proposal=0.01, buy=0.01, sell=0.01, proposal_open_contract=0.01)
        try:
            r, b = asyncio.run(go())
        finally:
            B._GAPS.clear(); B._GAPS.update(g)
        return r, b, sent
    def test_exact_time_is_sent(self):
        r, b, sent = self._run((4, "h"))
        self.assertTrue(r); self.assertEqual(sent, [(4, "h")])
    def test_refused_time_is_reported_with_deriv_reason_and_never_shortened(self):
        r, b, sent = self._run((1, "d"), reject=((1, "d"),))
        self.assertEqual(r, {}); self.assertEqual(sent, [(1, "d")])                   # no silent fallback to another length
        self.assertIn("does not accept a 1d trade time", b.last_error); self.assertIn("not offered", b.last_error)


if __name__ == "__main__":
    unittest.main()