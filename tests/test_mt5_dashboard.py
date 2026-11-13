"""MT5 dashboard (mt5_dashboard.py) tests. Offline: a fake MT5 stands in for the terminal."""
import os, sys, unittest, tempfile
from types import SimpleNamespace as NS
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import test_mt5 as t5
import mt5_dashboard as md


class Fake(t5.FakeMT5):
    ACCOUNT_TRADE_MODE_REAL = 2

    def __init__(self, real=False, login_ok=True):
        t5.FakeMT5.__init__(self)
        self.real, self.login_ok, self.inited, self.logins = real, login_ok, False, []
        self.add("Volatility 75 Index", t5.BOUNCE[:t5.first_buy_index()], "Derived\\Synthetic")

    def initialize(self): self.inited = True; return True
    def shutdown(self): self.inited = False
    def login(self, login, password=None, server=None):
        self.logins.append((login, password, server)); return self.login_ok
    def account_info(self):
        return NS(equity=10000.0, balance=10000.0, login=777, server="Srv", currency="USD", trade_mode=2 if self.real else 0)


class Base(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd(); self._tmp = tempfile.mkdtemp(); os.chdir(self._tmp)
        md._setup_logging(); md._lines.clear()
        self.fake = Fake()
        self.ctl = md.Controller(self.fake, poll=0.05)
        self.app = md.create_app(self.ctl); self.c = self.app.test_client()
    def tearDown(self):
        self.ctl.shutdown(); self.ctl.thread and self.ctl.thread.join(3); os.chdir(self._cwd)
    def post(self, path, body=None):
        return self.c.post(path, json=body or {}, headers={"Host": "127.0.0.1:5050"}).get_json()
    def get(self, path):
        return self.c.get(path, headers={"Host": "127.0.0.1:5050"})
    def connect(self):
        return self.post("/api/connect", {"login": "12345678", "server": "Deriv-Demo", "password": "secret-pw"})


class T300_Connect(Base):
    def test_page_loads_and_status_before_connect(self):
        self.assertEqual(self.get("/").status_code, 200)
        s = self.get("/api/status").get_json(); self.assertFalse(s["connected"])
    def test_connect_demo(self):
        r = self.connect(); self.assertTrue(r["ok"]); self.assertEqual(r["account"]["mode"], "DEMO")
        self.assertEqual(self.fake.logins[-1], (12345678, "secret-pw", "Deriv-Demo"))
        s = self.get("/api/status").get_json(); self.assertTrue(s["connected"])
        self.assertEqual(s["saved"], {"login": "12345678", "server": "Deriv-Demo"})
    def test_password_is_never_saved_or_logged_or_returned(self):
        self.connect()
        with open(md.SAVED_FILE) as f:
            blob = str(self.get("/api/status").get_json()) + f.read()
        self.assertNotIn("secret-pw", blob)
    def test_bad_login_and_bad_input(self):
        self.fake.login_ok = False
        self.assertFalse(self.connect()["ok"])
        self.fake.login_ok = True
        self.assertFalse(self.post("/api/connect", {"login": "abc", "server": "x", "password": "y"})["ok"])
        self.assertFalse(self.post("/api/connect", {"login": "1", "server": "", "password": ""})["ok"])
    def test_switch_to_another_account(self):
        self.connect(); self.fake.real = True
        r = self.post("/api/connect", {"login": "999", "server": "Deriv-Live", "password": "p2"})
        self.assertEqual(r["account"]["mode"], "LIVE")


class T301_StartStop(Base):
    def test_demo_start_places_the_trade_and_shows_it(self):
        self.connect()
        self.assertTrue(self.post("/api/start", {"tf": "M5", "hours": "24"})["ok"])
        import time
        for _ in range(60):
            s = self.get("/api/status").get_json()
            if s["positions"]: break
            time.sleep(0.05)
        self.assertEqual(len(s["positions"]), 1); self.assertEqual(s["positions"][0]["side"], "BUY")
        self.assertTrue(s["running"]); self.assertEqual(s["symbols"], 1)
        self.assertTrue(any("Bot STARTED" in l for l in s["log"]))
        self.assertTrue(self.post("/api/stop")["ok"])
        self.assertFalse(self.get("/api/status").get_json()["running"])
        self.assertEqual(len(self.fake.pos), 1)                      # stopping never closes trades
    def test_live_needs_confirmation_then_runs_the_same(self):
        self.fake.real = True; self.connect()
        r = self.post("/api/start", {"tf": "M5"}); self.assertFalse(r["ok"]); self.assertIn("REAL", r["error"])
        self.assertEqual(self.fake.sent, [])
        self.assertTrue(self.post("/api/start", {"tf": "M5", "confirm_real": True})["ok"])
    def test_dry_run_on_live_needs_no_confirmation_and_places_nothing(self):
        self.fake.real = True; self.connect()
        self.assertTrue(self.post("/api/start", {"tf": "M5", "dry_run": True})["ok"])
        import time; time.sleep(0.4); self.assertEqual(self.fake.sent, [])
    def test_start_needs_connection_and_valid_settings(self):
        self.assertFalse(self.post("/api/start", {})["ok"])
        self.connect()
        for bad in ({"tf": "M2"}, {"hours": "36"}, {"risk_pct": "50"}, {"rr": "0"}, {"max_positions": "0"},
                    {"groups": []}, {"risk_pct": "abc"}):
            self.assertFalse(self.post("/api/start", bad)["ok"], bad)
        self.assertTrue(self.post("/api/start", {"groups": ["forex"]})["ok"] is False)    # no forex symbol on this fake
    def test_disconnect(self):
        self.connect(); self.post("/api/start", {})
        self.assertTrue(self.post("/api/disconnect")["ok"])
        s = self.get("/api/status").get_json(); self.assertFalse(s["connected"]); self.assertFalse(s["running"])
        self.assertFalse(self.fake.inited)


class T302_Safety(Base):
    def test_only_local_host_and_json_posts(self):
        self.assertEqual(self.c.get("/api/status", headers={"Host": "evil.example.com"}).status_code, 403)
        self.assertEqual(self.c.post("/api/stop", data="x=1", headers={"Host": "127.0.0.1:5050"}).status_code, 400)
    def test_parse_settings_defaults(self):
        s, err = md.parse_settings({}); self.assertEqual(err, "")
        self.assertEqual((s.tf, s.risk_pct, s.rr, s.max_positions, s.hours, s.dry_run), ("M5", 1.0, 2.0, 5, "24", False))
        self.assertEqual(s.groups, ("synthetic", "crypto", "forex"))


if __name__ == "__main__":
    unittest.main()