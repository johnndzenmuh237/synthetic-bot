"""T40-T49: permanent accounts + history (Firebase mirror), history pages, permanent login, symbols.
A tiny fake Firebase Realtime Database runs on localhost; nothing real is contacted."""
import base64, json, os, sys, tempfile, threading, time, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_bot  # noqa  (sets env, stubs, temp DB)
import config, database, cloud_store


class FakeFirebase:
    def __init__(self, secret="SECRET"):
        self.data, self.secret, self.writes = {}, secret, 0
        fb = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def _node(self, path, create=False):
                cur = fb.data
                for part in [p for p in path.strip("/").replace(".json", "").split("/") if p]:
                    if not isinstance(cur, dict) or part not in cur:
                        if create and isinstance(cur, dict):
                            cur[part] = {}
                        else:
                            return None
                    cur = cur[part]
                return cur
            def _reply(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            def _ok(self, u):
                return parse_qs(u.query).get("auth", [""])[0] == fb.secret
            def do_GET(self):
                u = urlparse(self.path)
                if not self._ok(u): return self._reply(401, {"error": "Permission denied"})
                n = self._node(u.path)
                if parse_qs(u.query).get("shallow") and isinstance(n, dict):
                    n = {k: True for k in n}
                self._reply(200, n)
            def do_PUT(self):
                u = urlparse(self.path)
                if not self._ok(u): return self._reply(401, {"error": "Permission denied"})
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                parts = [p for p in u.path.replace(".json", "").strip("/").split("/") if p]
                parent = self._node("/".join(parts[:-1]), create=True)
                parent[parts[-1]] = body; fb.writes += 1
                self._reply(200, body)
            def do_DELETE(self):
                u = urlparse(self.path)
                if not self._ok(u): return self._reply(401, {"error": "Permission denied"})
                parts = [p for p in u.path.replace(".json", "").strip("/").split("/") if p]
                parent = self._node("/".join(parts[:-1]))
                if isinstance(parent, dict): parent.pop(parts[-1], None)
                self._reply(200, None)
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def fresh_db():
    database.DB_PATH = os.path.join(tempfile.mkdtemp(), "x.db")
    database.init_db.__globals__["DB_PATH"] = database.DB_PATH


class Base(unittest.TestCase):
    def setUp(self):
        self._old_db, self._old_cloud = database.DB_PATH, database.cloud
        self.fb = FakeFirebase()
        self.cloud = cloud_store.CloudStore(self.fb.url, "", "SECRET", root="")
        self.cloud.set_reader(database._cloud_read)
        database.cloud = self.cloud
        fresh_db()
    def tearDown(self):
        self.fb.close()
        database.cloud = self._old_cloud
        database.DB_PATH = self._old_db
        database.init_db.__globals__["DB_PATH"] = self._old_db
    def seed(self):
        database.init_db()
        self.assertTrue(database.create_user("bob", "HASH_BOB"))
        uid = database.get_user("bob")["id"]
        database.save_settings(uid, {"lot_size": 7.5, "symbol": "1HZ150V"})
        t1 = database.open_trade(uid, "R_75", "BUY", 1, 5.0, 100.0, 0, 0, contract_id="C1", mode="demo")
        database.close_trade(t1, 101.0, 4.5, "WIN", "expiry")
        t2 = database.open_trade(uid, "R_75", "SELL", 1, 5.0, 100.0, 0, 0, contract_id="C2", mode="live")
        database.close_trade(t2, 101.0, -5.0, "LOSS", "reversal")
        database.log_activity(uid, "login", "Signed in")
        self.assertTrue(self.cloud.flush(10))
        return uid, t1, t2


class T40_Mirror(Base):
    def test_every_change_reaches_firebase(self):
        uid, t1, t2 = self.seed()
        d = self.fb.data
        self.assertEqual(d["users"]["u%d" % uid]["username"], "bob")
        self.assertEqual(d["settings"]["u%d" % uid]["lot_size"], 7.5)
        self.assertEqual(d["trades"]["u%d" % uid]["t%d" % t1]["status"], "WIN")
        self.assertEqual(d["trades"]["u%d" % uid]["t%d" % t2]["pnl"], -5.0)
        self.assertEqual(len(d["activity"]["u%d" % uid]), 1)

    def test_bot_state_set_and_cleared(self):
        uid, _, _ = self.seed()
        database.save_bot_state(uid, {"symbol": "R_75", "mode": "demo", "lot_size": 1, "max_positions": 2,
                                      "strategy": "CANDLE_FOLLOW", "loss_policy": "hold", "close_on_stop": False})
        self.cloud.flush(10)
        self.assertIn("u%d" % uid, self.fb.data["bot_state"])
        database.set_entries_hold(uid, True); self.cloud.flush(10)
        self.assertEqual(self.fb.data["bot_state"]["u%d" % uid]["entries_hold"], 1)
        database.clear_bot_state(uid); self.cloud.flush(10)
        self.assertNotIn("u%d" % uid, self.fb.data.get("bot_state", {}))

    def test_sync_is_retried_when_firebase_is_down(self):
        self.seed()
        self.fb.secret = "CHANGED"                 # every write now fails with 401
        uid = database.get_user("bob")["id"]
        database.log_activity(uid, "settings", "x")
        time.sleep(0.5)
        self.assertGreater(self.cloud.failed, 0)
        self.fb.secret = "SECRET"
        self.assertTrue(self.cloud.flush(15))      # delivered after it recovers
        self.assertEqual(len(self.fb.data["activity"]["u%d" % uid]), 2)


class T40b_Namespace(Base):
    def test_default_root_keeps_bot_data_in_its_own_node_and_ignores_foreign_data(self):
        self.assertEqual(config.FIREBASE_ROOT, "synthbot")
        c = cloud_store.CloudStore(self.fb.url, "", "SECRET")           # default root
        c.set_reader(database._cloud_read); database.cloud = c
        # another app's data already in the same Firebase project
        self.fb.data["users"] = {"Zk3FirebaseAuthUid": {"id": 5, "username": "hotel_guest", "role": "student"}}
        self.fb.data["settings"] = {"site": {"user_id": 1, "symbol": "X"}}
        database.init_db()
        database.create_user("carol", "H"); c.flush(10)
        self.assertEqual(set(self.fb.data), {"users", "settings", "synthbot"})
        self.assertEqual(list(self.fb.data["users"]), ["Zk3FirebaseAuthUid"])      # foreign data untouched
        self.assertEqual(self.fb.data["settings"], {"site": {"user_id": 1, "symbol": "X"}})
        self.assertIn("u%d" % database.get_user("carol")["id"], self.fb.data["synthbot"]["users"])
        self.assertIsNone(database.get_user("hotel_guest"))                         # never imported


class T45_ClearHistory(Base):
    def _more(self, uid):
        t3 = database.open_trade(uid, "R_10", "BUY", 1, 5.0, 100.0, 0, 0, contract_id="C3", mode="demo")   # still OPEN
        t4 = database.open_trade(uid, "R_10", "BUY", 1, 5.0, 100.0, 0, 0, contract_id="C4", mode="demo")
        database.close_trade(t4, 101.0, 1.0, "WIN", "expiry")
        self.assertTrue(self.cloud.flush(10))
        return t3, t4
    def test_clear_all_removes_local_and_cloud_but_keeps_open_trades(self):
        uid, t1, t2 = self.seed(); t3, t4 = self._more(uid)
        other = database.create_user("amy", "H"); amy = database.get_user("amy")["id"]
        database.log_activity(amy, "login", "x"); at = database.open_trade(amy, "R_10", "BUY", 1, 1.0, 1, 0, 0, contract_id="A1")
        database.close_trade(at, 1, 1.0, "WIN", "x"); self.assertTrue(self.cloud.flush(10))
        res = database.clear_history(uid)
        self.assertEqual((res["trades"], res["kept_open"]), (3, 1))
        self.assertTrue(self.cloud.flush(10))
        rows, total, _ = database.get_history(uid)
        self.assertEqual([r["id"] for r in rows], [t3])                       # only the open trade is left
        self.assertEqual(database.get_activity(uid)[1], 0)
        d = self.fb.data
        self.assertEqual(list(d["trades"]["u%d" % uid]), ["t%d" % t3])         # cloud copy gone too (open one put back)
        self.assertNotIn("u%d" % uid, d.get("activity", {}))
        self.assertIn("u%d" % amy, d["trades"]); self.assertIn("u%d" % amy, d["activity"])   # other users untouched
        self.assertEqual(database.get_user("bob")["password_hash"], "HASH_BOB")               # account untouched
        self.assertEqual(database.get_history(amy)[1], 1)
    def test_clear_by_account_type(self):
        uid, t1, t2 = self.seed()                                 # t1 demo WIN, t2 live LOSS
        res = database.clear_history(uid, "demo", include_activity=False)
        self.assertEqual(res["trades"], 1); self.assertTrue(self.cloud.flush(10))
        self.assertEqual([r["id"] for r in database.get_history(uid)[0]], [t2])
        self.assertEqual(list(self.fb.data["trades"]["u%d" % uid]), ["t%d" % t2])
        self.assertEqual(database.get_activity(uid)[1], 1)       # activity kept
    def test_cleared_history_does_not_come_back_after_a_wipe_and_restore(self):
        uid, t1, t2 = self.seed()
        database.clear_history(uid); self.assertTrue(self.cloud.flush(10))
        fresh_db(); self.cloud.restored = False; database.init_db()
        self.assertEqual(database.get_history(uid)[1], 0); self.assertEqual(database.get_activity(uid)[1], 0)
        self.assertEqual(database.get_user("bob")["id"], uid)


class T41_Restore(Base):
    def test_wiped_database_is_rebuilt_from_firebase(self):
        uid, t1, t2 = self.seed()
        fresh_db()                                  # simulates Render wiping the disk on redeploy
        self.cloud.restored = False
        database.init_db()
        u = database.get_user("bob")
        self.assertEqual((u["id"], u["password_hash"]), (uid, "HASH_BOB"))
        self.assertEqual(database.get_settings(uid)["lot_size"], 7.5)
        self.assertEqual(database.get_settings(uid)["symbol"], "1HZ150V")
        rows, total, totals = database.get_history(uid)
        self.assertEqual(total, 2)
        self.assertEqual(totals["pnl"], -0.5)
        self.assertEqual(database.get_activity(uid)[1], 1)
        self.assertTrue(database.cloud_ready())
        # ids keep counting up, so new rows never collide with restored history
        t3 = database.open_trade(uid, "R_10", "BUY", 1, 1.0, 1, 0, 0, contract_id="C3")
        self.assertGreater(t3, max(t1, t2))

    def test_year_old_history_survives(self):
        uid, t1, _ = self.seed()
        old = database._conn()
        old.execute("UPDATE trades SET opened_at='2025-03-04 10:00:00', closed_at='2025-03-04 10:15:00' WHERE id=?", (t1,))
        old.commit(); old.close()
        database.cloud.push("trade", t1); self.cloud.flush(10)
        fresh_db(); self.cloud.restored = False
        database.init_db()
        yearly = database.get_history_summary(uid, "year")
        self.assertIn("2025", [r["period"] for r in yearly])

    def test_existing_local_history_is_uploaded_first_time(self):
        database.cloud = cloud_store.CloudStore("", "", "")      # cloud off while data is created
        database.init_db()
        database.create_user("old", "H")
        uid = database.get_user("old")["id"]
        database.open_trade(uid, "R_10", "BUY", 1, 1.0, 1, 0, 0, contract_id="Z1")
        database.cloud = self.cloud
        database.init_db()                                       # cloud switched on later
        self.cloud.flush(10)
        self.assertEqual(self.fb.data["users"]["u%d" % uid]["username"], "old")
        self.assertEqual(len(self.fb.data["trades"]["u%d" % uid]), 1)

    def test_unreachable_cloud_blocks_signup_and_never_overwrites(self):
        uid, _, _ = self.seed()
        fresh_db()
        dead = cloud_store.CloudStore("http://127.0.0.1:9", "", "SECRET", root="")
        dead.set_reader(database._cloud_read)
        database.cloud = dead
        old_sleep, database.time.sleep = database.time.sleep, lambda s: None
        try:
            database.init_db()
        finally:
            database.time.sleep = old_sleep
        self.assertFalse(database.cloud_ready())
        database.create_user("mallory", "H2")                   # would get id 1 = bob's id
        time.sleep(1.2)
        self.assertEqual(self.fb.data["users"]["u%d" % uid]["username"], "bob")  # untouched
        import app as appmod
        appmod.cloud = dead
        r = appmod.app.test_client().post("/register", data={"username": "zed", "password": "password1",
                                                              "confirm_password": "password1"})
        self.assertIn(b"still starting", r.data)
        appmod.cloud = self._old_cloud

    def test_clashing_user_id_is_not_overwritten(self):
        uid, _, _ = self.seed()
        self.fb.data["users"]["u%d" % uid]["username"] = "someone_else"
        database.log_activity(uid, "x", "y")
        database.cloud.push("user", uid); self.cloud.flush(10)
        self.assertEqual(self.fb.data["users"]["u%d" % uid]["username"], "someone_else")


class T42_Credentials(unittest.TestCase):
    def _sa(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        return key.public_key(), {"client_email": "svc@x.iam.gserviceaccount.com", "private_key": pem,
                                  "token_uri": "https://oauth2.googleapis.com/token"}
    def test_service_account_jwt_is_valid_and_signed(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        pub, sa = self._sa()
        a = cloud_store._Auth(json.dumps(sa), "")
        self.assertTrue(a.configured)
        jwt = a.make_jwt(now=1000)
        h, c, s = jwt.split(".")
        pad = lambda x: x + "=" * (-len(x) % 4)
        claims = json.loads(base64.urlsafe_b64decode(pad(c)))
        self.assertEqual(claims["iss"], sa["client_email"])
        self.assertIn("firebase.database", claims["scope"])
        self.assertEqual(claims["exp"] - claims["iat"], 3600)
        pub.verify(base64.urlsafe_b64decode(pad(s)), (h + "." + c).encode(), padding.PKCS1v15(), hashes.SHA256())
    def test_base64_service_account_accepted(self):
        _, sa = self._sa()
        a = cloud_store._Auth(base64.b64encode(json.dumps(sa).encode()).decode(), "")
        self.assertTrue(a.configured)
    def test_bad_credential_disables_cloud_without_crashing(self):
        s = cloud_store.CloudStore("https://x.firebaseio.com", "{not json", "")
        self.assertFalse(s.enabled); self.assertTrue(s.ready())
    def test_no_credential_means_cloud_off(self):
        s = cloud_store.CloudStore("https://x.firebaseio.com", "", "")
        self.assertFalse(s.enabled); self.assertTrue(s.ready())
        s.push("trade", 1)                                       # harmless no-op


class T43_HistoryAndLogin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app
        cls.app = app
        app.app.config["TESTING"] = True
        cls.c = app.app.test_client()
        cls.c.post("/register", data={"username": "hist_user", "password": "password123", "confirm_password": "password123"})
        r = cls.c.post("/login", data={"username": "hist_user", "password": "password123"})
        cls.login_headers = r.headers.getlist("Set-Cookie")
        cls.uid = database.get_user("hist_user")["id"]
        rows = [("2025-01-15 10:00:00", "WIN", 4.0, "demo", "R_10"), ("2025-01-20 11:00:00", "LOSS", -5.0, "demo", "R_10"),
                ("2025-06-02 09:00:00", "WIN", 8.0, "live", "R_75"), ("2026-02-03 09:00:00", "LOSS", -2.0, "demo", "1HZ150V"),
                ("2026-02-03 18:30:00", "WIN", 3.0, "demo", "1HZ150V")]
        for ts, st, pnl, mode, sym in rows:
            tid = database.open_trade(cls.uid, sym, "BUY", 1, 5.0, 1, 0, 0, mode=mode)
            con = database._conn()
            con.execute("UPDATE trades SET status=?, pnl=?, opened_at=?, closed_at=? WHERE id=?", (st, pnl, ts, ts, tid))
            con.commit(); con.close()
    def test_cookie_is_permanent_not_session_only(self):
        self.assertTrue(any("Expires=" in h for h in self.login_headers), self.login_headers)
    def test_login_survives_a_new_page_load_and_refreshes_lifetime(self):
        r = self.c.get("/dashboard")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any("Expires=" in h for h in r.headers.getlist("Set-Cookie")))
    def test_history_page_and_all_totals(self):
        self.assertEqual(self.c.get("/history").status_code, 200)
        j = self.c.get("/api/history").get_json()
        self.assertEqual(j["total"], 5)
        self.assertEqual(j["totals"]["pnl"], 8.0)
        self.assertEqual((j["totals"]["wins"], j["totals"]["losses"]), (3, 2))
    def test_summary_by_year_month_day(self):
        y = {r["period"]: r for r in self.c.get("/api/history/summary?period=year").get_json()["rows"]}
        self.assertEqual(y["2025"]["pnl"], 7.0); self.assertEqual(y["2026"]["pnl"], 1.0)
        m = {r["period"]: r["pnl"] for r in self.c.get("/api/history/summary?period=month").get_json()["rows"]}
        self.assertEqual(m["2025-01"], -1.0); self.assertEqual(m["2026-02"], 1.0)
        d = {r["period"]: r["trades"] for r in self.c.get("/api/history/summary?period=day").get_json()["rows"]}
        self.assertEqual(d["2026-02-03"], 2)
    def test_filters(self):
        self.assertEqual(self.c.get("/api/history?mode=live").get_json()["total"], 1)
        self.assertEqual(self.c.get("/api/history?symbol=R_10").get_json()["total"], 2)
        self.assertEqual(self.c.get("/api/history?status=LOSS").get_json()["total"], 2)
        self.assertEqual(self.c.get("/api/history?from=2026-01-01").get_json()["total"], 2)
        self.assertEqual(self.c.get("/api/history?from=2025-01-16&to=2025-12-31").get_json()["total"], 2)
    def test_pagination(self):
        j = self.c.get("/api/history?per=2&page=3").get_json()
        self.assertEqual((len(j["trades"]), j["pages"]), (1, 3))
    def test_csv_export(self):
        r = self.c.get("/history.csv")
        self.assertIn("text/csv", r.headers["Content-Type"])
        lines = r.data.decode().strip().splitlines()
        self.assertEqual(len(lines), 6); self.assertIn("pnl", lines[0])
    def test_users_only_see_their_own_history(self):
        c2 = self.app.app.test_client()
        c2.post("/register", data={"username": "other", "password": "password123", "confirm_password": "password123"})
        c2.post("/login", data={"username": "other", "password": "password123"})
        self.assertEqual(c2.get("/api/history").get_json()["total"], 0)
        self.assertEqual(c2.get("/api/history/summary?period=year").get_json()["rows"], [])
    def test_activity_log_records_actions(self):
        own = self.app.app.test_client()
        own.post("/login", data={"username": "hist_user", "password": "password123"})
        own.post("/api/settings", json={"lot_size": 2.0})
        own.get("/logout")
        c = self.app.app.test_client()
        c.post("/login", data={"username": "hist_user", "password": "wrong"})
        c.post("/login", data={"username": "hist_user", "password": "password123"})
        kinds = [a["kind"] for a in c.get("/api/activity?per=100").get_json()["activity"]]
        for k in ("register", "login", "settings", "logout", "login_failed"):
            self.assertIn(k, kinds)
    def test_history_requires_login(self):
        c = self.app.app.test_client()
        for u in ("/history", "/api/history", "/api/activity", "/history.csv", "/api/history/summary"):
            self.assertIn(c.get(u).status_code, (302, 401))


class T44_Symbols(unittest.TestCase):
    def test_all_volatility_indices_present(self):
        want = ["R_10", "R_25", "R_50", "R_75", "R_100", "1HZ10V", "1HZ15V", "1HZ25V", "1HZ30V",
                "1HZ50V", "1HZ75V", "1HZ90V", "1HZ100V", "1HZ150V", "1HZ250V"]
        for s in want:
            self.assertIn(s, config.SYMBOLS)
        self.assertIn("BOOM500", config.SYMBOLS)              # earlier symbols untouched
    def test_gold_xauusd_selectable_and_tradable(self):
        import app
        self.assertIn("frxXAUUSD", config.SYMBOLS)
        c = app.app.test_client()
        c.post("/register", data={"username": "gold_user", "password": "password123", "confirm_password": "password123"})
        c.post("/login", data={"username": "gold_user", "password": "password123"})
        r = c.post("/api/settings", json={"symbol": "frxXAUUSD"}).get_json()
        self.assertTrue(r["ok"]); self.assertEqual(r["saved"]["symbol"], "frxXAUUSD")
        self.assertIn("frxXAUUSD", c.get("/dashboard").data.decode())
        self.assertIn("frxXAUUSD", open(os.path.join(config.__file__ and os.path.dirname(config.__file__), "static/js/dashboard.js")).read())
    def test_every_symbol_can_be_selected_and_saved(self):
        import app
        c = app.app.test_client()
        c.post("/register", data={"username": "sym_user", "password": "password123", "confirm_password": "password123"})
        c.post("/login", data={"username": "sym_user", "password": "password123"})
        for s in ("1HZ15V", "1HZ30V", "1HZ90V", "1HZ150V", "1HZ250V"):
            r = c.post("/api/settings", json={"symbol": s}).get_json()
            self.assertTrue(r["ok"], (s, r))
            self.assertEqual(r["saved"]["symbol"], s)
        html = c.get("/dashboard").data.decode()
        self.assertIn("1HZ250V", html)


if __name__ == "__main__":
    unittest.main()