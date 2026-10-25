"""Offline tests (mock broker, temporary database). They never contact Deriv.
Run from the project folder:   python -m unittest discover -s tests -v
"""
import asyncio, os, re, sys, tempfile, time, types, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_tmp = tempfile.mkdtemp()
os.environ.update(DB_PATH=os.path.join(_tmp, "t.db"), LOG_FILE=os.path.join(_tmp, "t.log"),
                  DERIV_DEMO_TOKEN="pat_demo_secret_token_value_123456", DERIV_LIVE_TOKEN="pat_live_secret_token_value_654321",
                  SECRET_KEY="test-secret", ALLOW_REGISTRATION="true")

# Stub packages that are only needed for real network / socket use.
for name in ("aiohttp", "websockets"):
    try:
        __import__(name)
    except ImportError:
        sys.modules[name] = types.ModuleType(name)
try:
    import flask_socketio  # noqa
except ImportError:
    fs = types.ModuleType("flask_socketio")
    class _SIO:
        def __init__(self, app=None, **kw): self.sent = []
        def emit(self, *a, **k): self.sent.append(a)
        def on(self, *a, **k): return lambda f: f
        def run(self, *a, **k): pass
    fs.SocketIO = _SIO; fs.emit = lambda *a, **k: None; fs.join_room = lambda *a, **k: None
    sys.modules["flask_socketio"] = fs

import config, database, strategy, bot_engine, risk_manager
from strategy import Signal
database.init_db()


class FakeBroker:
    """Stands in for DerivBroker. Behaviour is controlled per test."""
    placed = []
    seq = 0
    def __init__(self, mode="demo", account_id=""):
        self.mode = mode; self.balance = 1000.0; self.currency = "USD"; self.account_id = "ACC1"
        self.account_type = "real" if mode == "live" else "demo"
        self.connected = True; self.auth_failed = False; self.balance_ok = True; self.last_error = ""
        self.contracts = {}; self.mode_behavior = "ok"; self.portfolio = []
    async def connect(self, cb): pass
    async def disconnect(self): self.connected = False
    async def subscribe_candles(self, s, tf): pass
    async def get_candles(self, s, tf, count=None): return []
    async def get_balance(self): return self.balance
    async def get_portfolio(self): return self.portfolio
    async def place_trade(self, symbol, direction, stake):
        FakeBroker.placed.append((symbol, direction, stake))
        if self.mode_behavior == "unknown":
            raise bot_engine.OrderStatusUnknown("connection dropped after sending")
        if self.mode_behavior == "reject":
            self.last_error = "Market is closed"; return {}
        FakeBroker.seq += 1; cid = 9000 + FakeBroker.seq
        self.contracts[str(cid)] = {"is_sold": False, "profit": 0.0, "buy_price": stake}
        self.balance -= stake
        return {"contract_id": cid, "buy_price": stake}
    async def get_contract(self, cid): return self.contracts.get(str(cid))
    async def close_trade(self, cid):
        c = self.contracts[str(cid)]; c["is_sold"] = True
        return {"sold_for": c["buy_price"] + c["profit"]}

bot_engine.DerivBroker = FakeBroker
_uid = [100]


def make(mode="demo", lot=1.0, maxpos=1, **kw):
    _uid[0] += 1
    FakeBroker.placed = []
    events = []
    s = bot_engine.BotSession(_uid[0], "R_50", mode, lambda e, p: events.append((e, p)), lot, maxpos, **kw)
    s.events = events
    s.broker = FakeBroker(mode)
    s.running = True
    s.risk = risk_manager.RiskManager(1000.0, s.user_id, mode, lot, maxpos)
    return s


def run(s, coro_fn):
    async def _go():
        s._lock = asyncio.Lock()
        s.loop = asyncio.get_event_loop()
        return await coro_fn()
    return asyncio.run(_go())


def sig(direction="BUY", price=100.0):
    return Signal(direction, "R_50", price, 1.0, 0.9, "test", "REVERSAL_SCALPER", 60.0)


async def trade(s, direction="BUY", n=0):
    ok, why = s._entry_allowed(sig(direction), 1000 + n)
    if not ok:
        return False, why
    stake, err = s.risk.stake_checked()
    if err:
        return False, err
    s._last_order_epoch = 1000 + n
    return await s._open_position(sig(direction), stake), ""


class T01_Stake(unittest.TestCase):
    def test_exact_stake_is_used(self):
        s = make(lot=2.35)
        ok, _ = run(s, lambda: trade(s))
        self.assertTrue(ok)
        self.assertEqual(FakeBroker.placed[-1][2], 2.35)

    def test_stake_not_silently_overridden_by_balance(self):
        s = make(lot=50.0)              # 5% of balance: allowed, not shrunk
        run(s, lambda: trade(s))
        self.assertEqual(FakeBroker.placed[-1][2], 50.0)

    def test_unaffordable_stake_refused_not_changed(self):
        s = make(lot=2000.0)
        ok, why = run(s, lambda: trade(s))
        self.assertFalse(ok); self.assertEqual(FakeBroker.placed, [])

    def test_update_applies_to_next_order(self):
        s = make(lot=1.0, maxpos=3)
        run(s, lambda: trade(s, n=1))
        s.update_settings(lot_size=4.5)
        run(s, lambda: trade(s, n=2))
        self.assertEqual([p[2] for p in FakeBroker.placed], [1.0, 4.5])


class T02_PositionCap(unittest.TestCase):
    def test_cap_never_exceeded(self):
        s = make(maxpos=2)
        res = [run(s, lambda i=i: trade(s, n=i))[0] for i in range(6)]
        self.assertEqual(res.count(True), 2)
        self.assertEqual(len(FakeBroker.placed), 2)

    def test_cap_counts_unconfirmed_orders(self):
        s = make(maxpos=1); s.broker.mode_behavior = "unknown"
        run(s, lambda: trade(s, n=1))
        s.broker.mode_behavior = "ok"
        ok, why = run(s, lambda: trade(s, n=2))
        self.assertFalse(ok)
        self.assertEqual(len(FakeBroker.placed), 1)      # no second order

    def test_slot_freed_after_close(self):
        s = make(maxpos=1)
        run(s, lambda: trade(s, n=1))
        t = database.get_open_trades(s.user_id, "demo")[0]
        s._record_close(t, 0.9, 1, "x")
        ok, _ = run(s, lambda: trade(s, n=2))
        self.assertTrue(ok)

    def test_validate_rejects_bad_caps(self):
        import app
        base = database.get_settings(1)
        for bad in (0, 11, -1, 2.5, "x"):
            cfg, err = app.validate_config({"max_positions": bad}, base)
            self.assertTrue(err, bad)
        cfg, err = app.validate_config({"max_positions": 3}, base)
        self.assertEqual(cfg["max_positions"], 3)


class T03_Accounting(unittest.TestCase):
    def test_win_loss_pnl_and_no_double_credit(self):
        s = make(maxpos=3)
        for i in range(2): run(s, lambda i=i: trade(s, n=i))
        a, b = database.get_open_trades(s.user_id, "demo")
        self.assertTrue(s._record_close(a, 0.95, 1, "won"))
        self.assertFalse(s._record_close(a, 0.95, 1, "duplicate"))     # replayed event
        self.assertFalse(s._record_close(a, -1.0, 1, "conflicting"))    # cannot flip result
        self.assertTrue(s._record_close(b, -1.0, 1, "lost"))
        st = database.get_stats(s.user_id, "demo")
        self.assertEqual((st["wins"], st["losses"]), (1, 1))
        self.assertAlmostEqual(database.get_daily_pnl(s.user_id, "demo"), -0.05, 2)

    def test_floating_not_in_realized(self):
        s = make()
        run(s, lambda: trade(s))
        cid = list(s.broker.contracts)[0]; s.broker.contracts[cid]["profit"] = 5.0
        run(s, s._reconcile_trades)
        self.assertEqual(s.floating_pnl, 5.0)
        self.assertEqual(database.get_daily_pnl(s.user_id, "demo"), 0)

    def test_reconcile_records_settlement_once(self):
        s = make()
        run(s, lambda: trade(s))
        cid = list(s.broker.contracts)[0]
        s.broker.contracts[cid].update(is_sold=True, profit=-1.0, sell_price=0)
        run(s, s._reconcile_trades); run(s, s._reconcile_trades)
        self.assertEqual(database.get_stats(s.user_id, "demo")["losses"], 1)
        self.assertAlmostEqual(database.get_daily_pnl(s.user_id, "demo"), -1.0)

    def test_modes_are_separate(self):
        d, l = make("demo"), make("live")
        run(d, lambda: trade(d)); 
        self.assertEqual(len(database.get_active_trades(l.user_id, "live")), 0)


class T04_ConnectionDrop(unittest.TestCase):
    def test_unknown_order_flagged_and_blocks_new_orders(self):
        s = make(maxpos=5); s.broker.mode_behavior = "unknown"
        ok, _ = run(s, lambda: trade(s, n=1))
        self.assertFalse(ok)
        self.assertEqual(len(database.get_unresolved_orders(s.user_id, "demo")), 1)
        s.broker.mode_behavior = "ok"
        ok, why = run(s, lambda: trade(s, n=2))
        self.assertFalse(ok); self.assertIn("reconcile", why.lower())
        self.assertEqual(len(FakeBroker.placed), 1)               # never re-sent

    def test_unknown_order_attached_when_found_on_deriv(self):
        s = make(); s.broker.mode_behavior = "unknown"
        run(s, lambda: trade(s, n=1))
        s.broker.portfolio = [{"contract_id": 777, "contract_type": "CALL", "symbol": "R_50",
                               "buy_price": 1.0, "purchase_time": time.time()}]
        s.broker.contracts["777"] = {"is_sold": False, "profit": 0.2, "buy_price": 1.0}
        run(s, s._reconcile_trades)
        self.assertEqual(database.get_unresolved_orders(s.user_id, "demo"), [])
        self.assertEqual(database.get_open_trades(s.user_id, "demo")[0]["contract_id"], "777")
        self.assertEqual(len(FakeBroker.placed), 1)

    def test_rejected_order_recorded_not_counted(self):
        s = make(); s.broker.mode_behavior = "reject"
        ok, _ = run(s, lambda: trade(s))
        self.assertFalse(ok)
        self.assertEqual(len(database.get_active_trades(s.user_id, "demo")), 0)
        self.assertTrue(any(e == "trade_error" for e, _ in s.events))


class T05_Restart(unittest.TestCase):
    def setUp(self):
        import app; self.app = app
        app.active_sessions.clear()

    def test_orphaned_pending_order_is_not_resent(self):
        s = make()
        database.open_trade(s.user_id, "R_50", "BUY", 1, 1.0, 100, 0, 0, status="PENDING", mode="demo")
        run(s, s._reconcile_trades)
        self.assertEqual(len(FakeBroker.placed), 0)
        self.assertEqual(database.get_active_trades(s.user_id, "demo")[0]["status"], "UNCONFIRMED")

    def test_supervisor_resumes_saved_bot_once(self):
        started = []
        class Dummy:
            alive = False; _stop_requested = False
            def start(self_): started.append(1); self_.alive = True
        self.app._make_session = lambda uid, cfg: Dummy()
        uid = database.create_user("restart_user", "x")
        cfg = dict(database.DEFAULT_SETTINGS); cfg["mode"] = "demo"
        database.save_bot_state(uid, cfg)
        self.app._supervise_once(); self.app._supervise_once()
        self.assertEqual(len(started), 1)                      # no duplicate worker
        database.clear_bot_state(uid)

    def test_stopped_bot_is_not_resumed(self):
        uid = database.create_user("stopped_user", "x")
        cfg = dict(database.DEFAULT_SETTINGS); database.save_bot_state(uid, cfg); database.clear_bot_state(uid)
        self.assertNotIn(uid, [b["user_id"] for b in database.get_all_bot_states()])

    def test_duplicate_worker_refused(self):
        s = make()
        s._thread = types.SimpleNamespace(is_alive=lambda: True)
        with self.assertRaises(RuntimeError): s.start()


class T06_Stop(unittest.TestCase):
    def test_stop_leaves_positions_by_default(self):
        s = make(); run(s, lambda: trade(s))
        s.loop = None; s.stop(wait=False)
        self.assertEqual(len(database.get_open_trades(s.user_id, "demo")), 1)

    def test_close_on_stop_closes_with_confirmed_result(self):
        s = make(close_on_stop=True); run(s, lambda: trade(s))
        cid = list(s.broker.contracts)[0]; s.broker.contracts[cid]["profit"] = 0.4
        s.broker.contracts[cid]["sell_price"] = 1.4
        run(s, lambda: s._close_all_open("stopped"))
        self.assertEqual(database.get_open_trades(s.user_id, "demo"), [])
        self.assertAlmostEqual(database.get_daily_pnl(s.user_id, "demo"), 0.4, 2)


class T07_Accounts(unittest.TestCase):
    def test_wrong_account_type_blocks_trading(self):
        class Wrong(FakeBroker):
            def __init__(self, mode="demo", account_id=""):
                FakeBroker.__init__(self, mode, account_id); self.account_type = "demo"
        s = make("live"); old = bot_engine.DerivBroker; bot_engine.DerivBroker = Wrong
        try:
            with self.assertRaises(ConnectionError):
                run(s, s._main)
        finally:
            bot_engine.DerivBroker = old
        self.assertEqual(FakeBroker.placed, [])

    def test_no_simulated_trades_when_unconnected(self):
        s = make(); s.broker.connected = False
        ok, why = run(s, lambda: trade(s))
        self.assertFalse(ok); self.assertEqual(FakeBroker.placed, [])


class T08_Risk(unittest.TestCase):
    def test_daily_loss_limit_pauses(self):
        s = make(lot=10.0, maxpos=10)   # limit = MAX_DAILY_LOSS_PCT of balance
        for i in range(8):
            run(s, lambda i=i: trade(s, n=i))
            t = database.get_open_trades(s.user_id, "demo")[0]
            s._record_close(t, -10.0, 1, "loss")
            if not s.risk.check()[0]: break
        ok, why = s.risk.check()
        self.assertFalse(ok); self.assertIn("Daily loss", why)
        ok, why = run(s, lambda: trade(s, n=99)); self.assertFalse(ok)

    def test_consecutive_losses_pause(self):
        s = make(lot=0.35, maxpos=10); s.risk.update_balance(100000)
        for i in range(config.MAX_CONSECUTIVE_LOSSES):
            run(s, lambda i=i: trade(s, n=i))
            s._record_close(database.get_open_trades(s.user_id, "demo")[0], -0.35, 1, "l")
        self.assertFalse(s.risk.check()[0])

    def test_zero_balance_pauses(self):
        s = make(); s.risk.update_balance(0)
        self.assertFalse(s.risk.check()[0])


class T09_Strategy(unittest.TestCase):
    def _df(self, closes):
        import pandas as pd
        return pd.DataFrame({"epoch": [int(time.time()) - 60 * (len(closes) - i) for i in range(len(closes))],
                             "open": closes, "high": [c + .1 for c in closes], "low": [c - .1 for c in closes], "close": closes})
    def test_signal_on_fresh_trigger_only(self):
        strategy._in_daily_reset_window = lambda now=None: False
        down = [100 - i * .5 for i in range(40)]; up = [down[-1] + i * 1.5 for i in range(1, 25)]
        sigs = []
        for n in range(len(down) + 1, len(down) + len(up) + 1):
            s = strategy.evaluate(None, self._df((down + up)[:n]), "R_50")
            if s: sigs.append(s.direction)
        self.assertEqual(sigs.count("BUY"), 1)                 # one trigger, not one per candle
    def test_rsi_bounds(self):
        import indicators
        r = indicators.compute_rsi(self._df([float(i) for i in range(30)]), 7)
        self.assertEqual(float(r["rsi"].iloc[-1]), 100.0)


class T10_App(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app; cls.app = app
        app.app.config["TESTING"] = True
        cls.c = app.app.test_client()
        cls.c.post("/register", data={"username": "alice", "password": "password123", "confirm_password": "password123"})
        r = cls.c.post("/login", data={"username": "alice", "password": "password123"})
    def test_dashboard_renders(self):
        r = self.c.get("/dashboard"); self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"pat_", r.data)                       # no token in the page
    def test_requires_login(self):
        self.assertIn(app_status(self.app.app.test_client().get("/api/settings")), (302, 401))
    def test_save_settings_roundtrip(self):
        r = self.c.post("/api/settings", json={"lot_size": 3.25, "max_positions": 4})
        self.assertTrue(r.get_json()["ok"])
        s = self.c.get("/api/settings").get_json()["saved"]
        self.assertEqual((s["lot_size"], s["max_positions"]), (3.25, 4))
    def test_invalid_stake_rejected(self):
        for v in (0, -1, "abc", 1.234, 999999999, float("nan")):
            r = self.c.post("/api/settings", json={"lot_size": v})
            self.assertEqual(r.status_code, 400, v)
    def test_live_requires_confirmation(self):
        r = self.c.post("/api/bot/start", json={"mode": "live"})
        self.assertEqual(r.status_code, 400); self.assertEqual(r.get_json()["code"], "live_confirmation_required")
    def test_start_stop_with_fake_session(self):
        app = self.app
        class Dummy:
            alive = True; _stop_requested = False; close_on_stop = False
            def __init__(s_, cfg): s_.cfg = dict(cfg)
            def start(s_): pass
            def stop(s_, wait=True): s_.alive = False
            def applied_config(s_): return s_.cfg
            def status_dict(s_): return {"state": "running" if s_.alive else "stopped"}
            def update_settings(s_, **k): s_.cfg.update({a: b for a, b in k.items() if b is not None})
        orig = app._make_session; app._make_session = lambda uid, cfg: Dummy(cfg)
        try:
            r = self.c.post("/api/bot/start", json={"mode": "demo", "lot_size": 2.0, "max_positions": 3})
            j = r.get_json(); self.assertTrue(j["ok"]); self.assertEqual(j["applied"]["lot_size"], 2.0)
            r = self.c.post("/api/bot/update", json={"lot_size": 5.0, "max_positions": 2})
            j = r.get_json(); self.assertEqual((j["applied"]["lot_size"], j["applied"]["max_positions"]), (5.0, 2))
            r = self.c.post("/api/bot/update", json={"symbol": "R_100"})
            self.assertEqual(r.status_code, 400)                # restart-only setting
            self.assertTrue(self.c.post("/api/bot/stop").get_json()["stopped"])
            self.assertEqual(database.get_all_bot_states() and
                             [b for b in database.get_all_bot_states() if b["user_id"] == self.uid()], [])
        finally:
            app._make_session = orig
    def uid(self):
        return database.get_user("alice")["id"]
    def test_user_isolation(self):
        other = self.app.app.test_client()
        other.post("/register", data={"username": "bob", "password": "password123", "confirm_password": "password123"})
        other.post("/login", data={"username": "bob", "password": "password123"})
        self.c.post("/api/settings", json={"lot_size": 7.0})
        self.assertNotEqual(other.get("/api/settings").get_json()["saved"]["lot_size"], 7.0)


def app_status(r): return r.status_code


class T11_NoEmojiAndSecrets(unittest.TestCase):
    EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐⭕←-⇿■-◿✓✔✕✖]")
    def test_no_emoji_in_ui_files(self):
        for rel in ["templates/dashboard.html", "templates/login.html", "templates/register.html",
                    "templates/base.html", "static/js/dashboard.js"]:
            txt = open(os.path.join(ROOT, rel), encoding="utf-8").read()
            self.assertEqual(self.EMOJI.findall(txt), [], rel)
    def test_icons_used_exist_in_sprite(self):
        sprite = open(os.path.join(ROOT, "templates/_icons.html"), encoding="utf-8").read()
        have = set(re.findall(r'id="i-([\w-]+)"', sprite))
        used = set()
        for rel in ["templates/dashboard.html", "templates/login.html", "templates/register.html", "static/js/dashboard.js"]:
            txt = open(os.path.join(ROOT, rel), encoding="utf-8").read()
            used |= set(re.findall(r"#i-([\w-]+)", txt))
            used |= set(re.findall(r"ico\('([\w-]+)'", txt)) | set(re.findall(r"\bic\(\"([\w-]+)\"", txt))
        self.assertEqual(used - have, set())
    def test_dashboard_js_ids_exist_in_template(self):
        js = open(os.path.join(ROOT, "static/js/dashboard.js"), encoding="utf-8").read()
        html = open(os.path.join(ROOT, "templates/dashboard.html"), encoding="utf-8").read()
        ids = set(re.findall(r'\$\("([\w-]+)"\)', js))
        missing = [i for i in ids if 'id="%s"' % i not in html and i not in ("chart-M1", "chart-M5")]
        self.assertEqual(missing, [])
    def test_secrets_redacted_in_logs(self):
        from logger import log
        log.info("token pat_abcdefghijklmnopqrstuvwxyz0123456789 and Bearer abc.def otp=zzz123")
        for h in log.handlers:
            h.flush()
        txt = open(os.environ["LOG_FILE"], encoding="utf-8").read()
        self.assertNotIn("pat_abcdefghijklmnopqrstuvwxyz", txt)
        self.assertNotIn("otp=zzz123", txt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
