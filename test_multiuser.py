"""T70-T78: many users, each with their OWN Deriv tokens; admin page; activate / deactivate."""
import asyncio, json, os, sys, threading, time, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_bot  # noqa (env + stubs; ADMIN_USERNAMES=alice,boss)
import test_cloud as TC
import config, database, token_vault, cloud_store
import app as appmod

DEMO_A = "pat_demo_token_of_user_A_1111"
DEMO_B = "pat_demo_token_of_user_B_2222"
LIVE_A = "pat_live_token_of_user_A_3333"


async def fake_fetch_accounts(token, app_id=None):
    if "bad" in token:
        raise ConnectionError("Invalid or expired API token.")
    if token.startswith("pat_live"):
        return [{"account_id": "CR100", "account_type": "real", "balance": 50.0, "currency": "USD"}]
    return [{"account_id": "VRTC100", "account_type": "demo", "balance": 10000.0, "currency": "USD"}]


appmod.fetch_accounts = fake_fetch_accounts
ORIG_MAKE = appmod._make_session


def client(name, register=True):
    c = appmod.app.test_client()
    if register:
        c.post("/register", data={"username": name, "password": "password123", "confirm_password": "password123"})
    c.post("/login", data={"username": name, "password": "password123"})
    return c


def uid_of(name):
    return database.get_user(name)["id"]


class Dummy:
    """A running worker stand-in."""
    def __init__(self, mode="demo"):
        self.alive = True; self._stop_requested = False; self.mode = mode; self.symbol = "R_50"
        self.strategy = "CANDLE_FOLLOW"; self.loop = None; self.stopped = False
    def stop(self, wait=False): self.stopped = True; self.alive = False
    def status_dict(self): return {"state": "running"}
    def applied_config(self): return {}


class T70_Vault(unittest.TestCase):
    def test_roundtrip_and_hint(self):
        b = token_vault.encrypt("pat_secret_abcdef123456")
        self.assertNotIn("secret", b)
        self.assertEqual(token_vault.decrypt(b), "pat_secret_abcdef123456")
        self.assertEqual(token_vault.hint("pat_secret_abcdef123456"), "••••3456")
    def test_garbage_and_empty_decrypt_to_nothing(self):
        self.assertEqual(token_vault.decrypt("not-a-real-blob"), "")
        self.assertEqual(token_vault.decrypt(""), "")


class T71_Tokens(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = client("mu_user_a"); cls.b = client("mu_user_b")
    def test_bad_token_rejected_and_nothing_stored(self):
        r = self.a.post("/api/tokens", json={"mode": "demo", "token": "pat_bad_token_value_000"})
        self.assertEqual(r.status_code, 400); self.assertIn("Invalid", r.get_json()["error"])
        self.assertEqual(database.get_user_token_blobs(uid_of("mu_user_a"))["demo"], "")
    def test_demo_token_in_live_slot_rejected(self):
        r = self.a.post("/api/tokens", json={"mode": "live", "token": DEMO_A})
        self.assertEqual(r.status_code, 400); self.assertIn("no LIVE account", r.get_json()["error"])
    def test_junk_input_rejected(self):
        for t in ("short", "has spaces in the middle of it!!", "x" * 300):
            self.assertEqual(self.a.post("/api/tokens", json={"mode": "demo", "token": t}).status_code, 400)
        self.assertEqual(self.a.post("/api/tokens", json={"mode": "xx", "token": DEMO_A}).status_code, 400)
    def test_save_encrypts_and_never_echoes_the_token(self):
        seen = []
        r = self.a.post("/api/tokens", json={"mode": "demo", "token": DEMO_A}); seen.append(r.data)
        self.assertTrue(r.get_json()["ok"]); self.assertEqual(r.get_json()["account"]["account_id"], "VRTC100")
        seen.append(self.a.get("/api/tokens").data); seen.append(self.a.get("/dashboard").data)
        seen.append(self.a.get("/api/settings").data); seen.append(self.a.get("/api/bot/status").data)
        for blob in seen:
            self.assertNotIn(DEMO_A.encode(), blob)
        t = self.a.get("/api/tokens").get_json()["tokens"]
        self.assertTrue(t["demo"]["connected"]); self.assertEqual(t["demo"]["source"], "own")
        self.assertTrue(t["demo"]["hint"].endswith("1111")); self.assertFalse(t["live"]["connected"])
        raw = database.get_user_token_blobs(uid_of("mu_user_a"))["demo"]
        self.assertNotIn(DEMO_A, raw); self.assertEqual(token_vault.decrypt(raw), DEMO_A)
    def test_remove_token(self):
        self.b.post("/api/tokens", json={"mode": "demo", "token": DEMO_B})
        r = self.b.post("/api/tokens/remove", json={"mode": "demo"})
        self.assertTrue(r.get_json()["ok"]); self.assertFalse(r.get_json()["tokens"]["demo"]["connected"])
    def test_cannot_change_token_while_bot_runs_in_that_mode(self):
        c = client("mu_runner"); uid = uid_of("mu_runner")
        appmod.active_sessions[uid] = Dummy("demo")
        try:
            r = c.post("/api/tokens", json={"mode": "demo", "token": DEMO_A})
            self.assertEqual(r.status_code, 409)
            self.assertEqual(c.post("/api/tokens", json={"mode": "live", "token": LIVE_A}).status_code, 200)  # other mode fine
        finally:
            appmod.active_sessions.pop(uid, None)


class T72_Isolation(unittest.TestCase):
    def setUp(self):
        appmod._make_session = ORIG_MAKE; appmod.active_sessions.clear()
    def test_each_bot_uses_only_its_owners_token_and_never_the_servers(self):
        a = client("iso_a"); b = client("iso_b"); boss = client("boss")
        a.post("/api/tokens", json={"mode": "demo", "token": DEMO_A})
        b.post("/api/tokens", json={"mode": "demo", "token": DEMO_B})
        cfg = dict(database.DEFAULT_SETTINGS); cfg["mode"] = "demo"
        sa, sb = appmod._make_session(uid_of("iso_a"), cfg), appmod._make_session(uid_of("iso_b"), cfg)
        self.assertEqual(sa.broker.token, DEMO_A); self.assertEqual(sb.broker.token, DEMO_B)
        # a user WITHOUT a token gets nothing - never the server's token
        c = client("iso_c")
        sc = appmod._make_session(uid_of("iso_c"), cfg)
        self.assertEqual(sc.broker.token, "")
        self.assertNotEqual(sc.broker.token, config.DERIV_DEMO_TOKEN)
        live = dict(cfg); live["mode"] = "live"
        self.assertEqual(appmod._make_session(uid_of("iso_a"), live).broker.token, "")     # a has no LIVE token
    def test_admin_may_fall_back_to_server_token_but_own_token_wins(self):
        boss = client("boss"); uid = uid_of("boss")
        self.assertEqual(appmod._user_token(uid, "demo") or None, config.DERIV_DEMO_TOKEN or None)
        self.assertEqual(appmod._token_source(uid, "demo"), "server")
        boss.post("/api/tokens", json={"mode": "demo", "token": DEMO_B})
        self.assertEqual(appmod._user_token(uid, "demo"), DEMO_B)
        boss.post("/api/tokens/remove", json={"mode": "demo"})
    def test_non_admin_cannot_start_without_own_token(self):
        c = client("iso_d")
        r = c.post("/api/bot/start", json={"mode": "demo"})
        self.assertEqual(r.status_code, 400); self.assertEqual(r.get_json()["code"], "token_required")
        r = c.post("/api/bot/start", json={"mode": "live", "confirm_live": True})
        self.assertEqual(r.status_code, 400); self.assertEqual(r.get_json()["code"], "token_required")
    def test_accounts_and_diagnose_use_the_callers_rights(self):
        c = client("iso_e")
        self.assertFalse(c.get("/api/accounts?mode=demo").get_json()["ok"])          # no token, no server fallback
        self.assertEqual(c.get("/api/diagnose").status_code, 403)
        c.post("/api/tokens", json={"mode": "demo", "token": DEMO_A})
        j = c.get("/api/accounts?mode=demo").get_json()
        self.assertTrue(j["ok"]); self.assertEqual(j["accounts"][0]["account_id"], "VRTC100")
    def test_histories_are_separate(self):
        a = client("iso_a"); b = client("iso_b")
        database.open_trade(uid_of("iso_a"), "R_10", "BUY", 1, 1.0, 1, 0, 0, contract_id="ISO1")
        self.assertEqual(a.get("/api/history").get_json()["total"], 1)
        self.assertEqual(b.get("/api/history").get_json()["total"], 0)


class T73_Admin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        appmod._make_session = ORIG_MAKE; appmod.active_sessions.clear()
        cls.boss = client("boss"); cls.boss.post("/ctl-test/login", data={"username": "boss", "password": "password123"}); cls.u = client("adm_user"); cls.other = client("adm_other")
        cls.u.post("/api/tokens", json={"mode": "demo", "token": DEMO_A})
    def test_admin_console_is_hidden_and_separate(self):
        B = "/ctl-test"
        # old, guessable addresses simply do not exist
        for url in ("/admin", "/api/admin/users", "/admin/login"):
            self.assertEqual(self.boss.get(url).status_code, 404)
        # a normally-logged-in admin is NOT automatically in the console: separate sign-in
        fresh = client("boss", register=False)
        self.assertEqual(fresh.get(B + "/").status_code, 302)
        self.assertIn("/ctl-test/login", fresh.get(B + "/").headers["Location"])
        self.assertEqual(fresh.get(B + "/api/users").status_code, 404)
        self.assertEqual(self.boss.get(B + "/").status_code, 200)
        self.assertEqual(self.boss.get(B + "/api/users").status_code, 200)
        # ordinary users and anonymous visitors get nothing (API: 404, no hint it exists)
        for c in (self.u, appmod.app.test_client()):
            self.assertEqual(c.get(B + "/").status_code, 302)
            self.assertEqual(c.get(B + "/api/users").status_code, 404)
            self.assertEqual(c.post(B + "/api/users/1/active", json={"active": False}).status_code, 404)
        # a non-admin cannot sign in to the console even with the right password
        c = appmod.app.test_client()
        r = c.post(B + "/login", data={"username": "adm_user", "password": "password123"})
        self.assertIn(b"Invalid", r.data); self.assertEqual(c.get(B + "/").status_code, 302)
        # sign out ends the console session only
        self.assertEqual(fresh.post(B + "/login", data={"username": "boss", "password": "password123"}).status_code, 302)
        self.assertEqual(fresh.get(B + "/").status_code, 200)
        fresh.get(B + "/logout"); self.assertEqual(fresh.get(B + "/").status_code, 302)
    def test_no_admin_link_anywhere_in_user_app(self):
        for c in (self.boss, self.u):
            for page in ("/dashboard", "/history"):
                self.assertNotIn(b"ctl-test", c.get(page).data); self.assertNotIn(b"/admin", c.get(page).data)
        self.assertNotIn(b"ctl-test", appmod.app.test_client().get("/login").data)
    def test_login_throttle(self):
        c = appmod.app.test_client()
        appmod._admin_fails.clear()
        for _ in range(5):
            c.post("/ctl-test/login", data={"username": "boss", "password": "bad"})
        r = c.post("/ctl-test/login", data={"username": "boss", "password": "password123"})
        self.assertEqual(r.status_code, 429)
        appmod._admin_fails.clear()
    def test_list_shows_everyone_without_secrets(self):
        raw = self.boss.get("/ctl-test/api/users").data
        j = json.loads(raw)
        names = {u["username"]: u for u in j["users"]}
        self.assertIn("adm_user", names); self.assertTrue(names["adm_user"]["has_demo_token"])
        self.assertFalse(names["adm_user"]["has_live_token"]); self.assertTrue(names["boss"]["is_admin"])
        self.assertNotIn(DEMO_A.encode(), raw); self.assertNotIn(b"password_hash", raw)
        self.assertEqual(j["summary"]["total"], len(j["users"]))
    def test_detail_view(self):
        j = self.boss.get("/ctl-test/api/users/%d" % uid_of("adm_user")).get_json()
        self.assertTrue(j["ok"]); self.assertEqual(j["username"], "adm_user")
        self.assertIn("token_saved", [a["kind"] for a in j["activity"]])
    def test_admin_cannot_deactivate_self_or_another_admin(self):
        r = self.boss.post("/ctl-test/api/users/%d/active" % uid_of("boss"), json={"active": False})
        self.assertEqual(r.status_code, 400)
        client("alice")
        r = self.boss.post("/ctl-test/api/users/%d/active" % uid_of("alice"), json={"active": False})
        self.assertEqual(r.status_code, 400)
    def test_deactivate_blocks_everything_and_activate_restores(self):
        c = client("adm_victim"); uid = uid_of("adm_victim")
        c.post("/api/tokens", json={"mode": "demo", "token": DEMO_A})
        sess = Dummy(); appmod.active_sessions[uid] = sess
        cfg = dict(database.DEFAULT_SETTINGS); cfg["mode"] = "demo"; database.save_bot_state(uid, cfg)
        r = self.boss.post("/ctl-test/api/users/%d/active" % uid, json={"active": False})
        self.assertTrue(r.get_json()["ok"]); self.assertFalse(r.get_json()["is_active"])
        self.assertTrue(sess.stopped); self.assertNotIn(uid, appmod.active_sessions)          # bot stopped
        self.assertNotIn(uid, [b["user_id"] for b in database.get_all_bot_states()])         # and will not resume
        # their open browser session is thrown out ...
        self.assertEqual(c.get("/dashboard").status_code, 302)
        self.assertEqual(c.post("/api/bot/start", json={"mode": "demo"}).status_code, 302)
        # ... and logging in again is refused with a clear message
        c2 = appmod.app.test_client()
        r = c2.post("/login", data={"username": "adm_victim", "password": "password123"})
        self.assertIn(b"deactivated", r.data); self.assertEqual(c2.get("/dashboard").status_code, 302)
        # the supervisor never restarts a deactivated user's bot
        database.save_bot_state(uid, cfg)
        started = []
        orig = appmod._make_session
        appmod._make_session = lambda u, c_: (started.append(u) or Dummy())
        try:
            appmod._supervise_once()
        finally:
            appmod._make_session = orig
        self.assertNotIn(uid, started)
        self.assertNotIn(uid, [b["user_id"] for b in database.get_all_bot_states()])
        # list shows them as deactivated, then activate -> they can log in and trade again
        row = [u for u in self.boss.get("/ctl-test/api/users").get_json()["users"] if u["username"] == "adm_victim"][0]
        self.assertFalse(row["is_active"])
        r = self.boss.post("/ctl-test/api/users/%d/active" % uid, json={"active": True})
        self.assertTrue(r.get_json()["is_active"])
        c3 = appmod.app.test_client()
        c3.post("/login", data={"username": "adm_victim", "password": "password123"})
        self.assertEqual(c3.get("/dashboard").status_code, 200)
        kinds = [a["kind"] for a in database.get_activity(uid, 50)[0]]
        self.assertIn("deactivated", kinds); self.assertIn("activated", kinds)
    def test_deactivate_can_close_open_trades_first(self):
        c = client("adm_closer"); uid = uid_of("adm_closer")
        loop = asyncio.new_event_loop(); threading.Thread(target=loop.run_forever, daemon=True).start()
        calls = []
        class S(Dummy):
            async def close_all_manual(self, hold): calls.append(hold); return {"closed": 2}
        s = S(); s.loop = loop; appmod.active_sessions[uid] = s
        try:
            r = self.boss.post("/ctl-test/api/users/%d/active" % uid, json={"active": False, "close_trades": True})
            self.assertEqual(r.get_json()["closed"], {"closed": 2}); self.assertEqual(calls, [True]); self.assertTrue(s.stopped)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            self.boss.post("/ctl-test/api/users/%d/active" % uid, json={"active": True})
    def test_server_bot_cap(self):
        old = appmod.MAX_CONCURRENT_BOTS; appmod.MAX_CONCURRENT_BOTS = 2
        c = client("adm_cap"); uid = uid_of("adm_cap")
        full = {9001: Dummy(), 9002: Dummy()}
        appmod.active_sessions.update(full)
        try:
            ok, err = appmod._start_worker(uid, dict(database.DEFAULT_SETTINGS))
            self.assertFalse(ok); self.assertIn("limit", err)
        finally:
            for k in full: appmod.active_sessions.pop(k, None)
            appmod.MAX_CONCURRENT_BOTS = old
    def test_first_user_is_admin_when_no_admin_list(self):
        old = appmod.ADMIN_USERNAMES; appmod.ADMIN_USERNAMES = []
        try:
            first = database.get_first_user_id()
            self.assertTrue(appmod.is_admin_user(database.get_user_by_id(first)))
            self.assertFalse(appmod.is_admin_user(database.get_user("adm_user")))
        finally:
            appmod.ADMIN_USERNAMES = old


class T74_TokensSurviveRedeploy(TC.Base):
    def test_tokens_are_mirrored_encrypted_and_restored(self):
        database.init_db()
        database.create_user("tok_u", "H"); uid = uid_of("tok_u")
        database.set_user_token(uid, "demo", token_vault.encrypt(DEMO_A))
        database.set_user_active(uid, False)
        self.assertTrue(self.cloud.flush(10))
        stored = json.dumps(self.fb.data)
        self.assertNotIn(DEMO_A, stored)                                     # never plain text in Firebase
        self.assertIn("tokens", self.fb.data)
        TC.fresh_db(); self.cloud.restored = False
        database.init_db()
        self.assertEqual(token_vault.decrypt(database.get_user_token_blobs(uid)["demo"]), DEMO_A)
        self.assertFalse(database.user_is_active(database.get_user("tok_u")))   # deactivation survives too
    def test_removed_token_is_removed_from_firebase(self):
        database.init_db(); database.create_user("tok_v", "H"); uid = uid_of("tok_v")
        database.set_user_token(uid, "demo", token_vault.encrypt(DEMO_A)); self.cloud.flush(10)
        database.set_user_token(uid, "demo", ""); self.cloud.flush(10)
        blob = self.fb.data["tokens"]["u%d" % uid].get("demo_token", "")
        self.assertEqual(blob, "")


if __name__ == "__main__":
    unittest.main()


class T75_PWA(unittest.TestCase):
    def setUp(self):
        self.c = appmod.app.test_client()
    def test_user_app_is_installable(self):
        m = self.c.get("/manifest.webmanifest")
        self.assertEqual(m.status_code, 200); j = m.get_json()
        self.assertEqual(j["scope"], "/"); self.assertEqual(j["display"], "standalone")
        self.assertTrue(any(i["sizes"] == "512x512" for i in j["icons"]))
        for i in j["icons"]:
            self.assertEqual(self.c.get(i["src"]).status_code, 200)
        sw = self.c.get("/sw.js")
        self.assertEqual(sw.status_code, 200); self.assertIn("javascript", sw.content_type)
        self.assertEqual(sw.headers.get("Service-Worker-Allowed"), "/")
        self.assertEqual(self.c.get("/offline").status_code, 200)
        self.assertIn(b"manifest.webmanifest", self.c.get("/login").data)
    def test_admin_app_is_separate_installable(self):
        B = "/ctl-test"
        u = self.c.get("/manifest.webmanifest").get_json()
        a = self.c.get(B + "/manifest.webmanifest").get_json()
        self.assertEqual(a["scope"], B + "/"); self.assertTrue(a["start_url"].startswith(B))
        self.assertNotEqual(a["id"], u["id"]); self.assertNotEqual(a["name"], u["name"])
        for i in a["icons"]:
            self.assertEqual(self.c.get(i["src"]).status_code, 200)
        self.assertEqual(self.c.get(B + "/sw.js").status_code, 200)
        self.assertEqual(self.c.get(B + "/sw.js").headers.get("Service-Worker-Allowed"), B + "/")
        page = self.c.get(B + "/login").data
        self.assertIn(b"/ctl-test/manifest.webmanifest", page)
    def test_service_worker_never_caches_pages_or_api(self):
        sw = self.c.get("/sw.js").data.decode()
        self.assertIn("/static/", sw); self.assertIn("/api/", sw); self.assertIn("/socket.io", sw)


class T76_ClearEndpoint(unittest.TestCase):
    def test_needs_confirmation_and_only_touches_own_history(self):
        appmod._make_session = ORIG_MAKE
        a = client("clr_a"); b = client("clr_b"); ua, ub = uid_of("clr_a"), uid_of("clr_b")
        for u in (ua, ub):
            t = database.open_trade(u, "R_10", "BUY", 1, 1.0, 1, 0, 0, contract_id="K%d" % u)
            database.close_trade(t, 1, 2.0, "WIN", "x")
        open_t = database.open_trade(ua, "R_10", "BUY", 1, 1.0, 1, 0, 0, contract_id="KOPEN")
        self.assertEqual(appmod.app.test_client().post("/api/history/clear", json={"confirm": "CLEAR"}).status_code, 302)
        self.assertEqual(a.post("/api/history/clear", json={"scope": "all"}).status_code, 400)          # no confirmation
        self.assertEqual(a.post("/api/history/clear", json={"scope": "all", "confirm": "yes"}).status_code, 400)
        self.assertEqual(a.post("/api/history/clear", json={"scope": "bogus", "confirm": "CLEAR"}).status_code, 400)
        self.assertEqual(a.get("/api/history").get_json()["total"], 2)
        r = a.post("/api/history/clear", json={"scope": "all", "confirm": "clear"}).get_json()
        self.assertTrue(r["ok"]); self.assertEqual(r["trades"], 1); self.assertEqual(r["kept_open"], 1)
        self.assertEqual([t["id"] for t in a.get("/api/history").get_json()["trades"]], [open_t])
        kinds = [x["kind"] for x in a.get("/api/activity").get_json()["activity"]]
        self.assertEqual(kinds, ["history_cleared"])                                                    # an audit line remains
        self.assertEqual(b.get("/api/history").get_json()["total"], 1)                                  # other user untouched
        self.assertIn(b"Clear history", a.get("/history").data)


class T77_ConnectGate(unittest.TestCase):
    def test_new_user_sees_only_the_connect_page_until_a_token_is_saved(self):
        appmod._make_session = ORIG_MAKE
        c = client("gate_new")
        page = c.get("/dashboard").get_data(as_text=True)
        self.assertIn("Connect your Deriv", page); self.assertIn('id="tok-demo"', page)
        for hidden in ("Open Positions", "SESSION STATS", "dashboard.js", "Live Signal Feed", "id=\"btn-start\""):
            self.assertNotIn(hidden, page)
        self.assertEqual(c.post("/api/bot/start", json={"mode": "demo"}).status_code, 400)           # cannot start without a token
        self.assertEqual(c.post("/api/tokens", json={"mode": "demo", "token": DEMO_A}).status_code, 200)
        page = c.get("/dashboard").get_data(as_text=True)                                              # now the full dashboard
        self.assertIn("Open Positions", page); self.assertIn("dashboard.js", page)
        c.post("/api/tokens/remove", json={"mode": "demo"})                                            # removing it gates again
        self.assertIn("Connect your Deriv", c.get("/dashboard").get_data(as_text=True))
    def test_admin_with_server_tokens_goes_straight_to_the_dashboard(self):
        boss = client("boss")
        old = (appmod.DERIV_DEMO_TOKEN, appmod.DERIV_LIVE_TOKEN)
        appmod.DERIV_DEMO_TOKEN = "pat_server_demo_token"
        try:
            self.assertIn("Open Positions", boss.get("/dashboard").get_data(as_text=True))
        finally:
            appmod.DERIV_DEMO_TOKEN, appmod.DERIV_LIVE_TOKEN = old
        self.assertIn("Connect your Deriv", client("alice").get("/dashboard").get_data(as_text=True))   # admin WITHOUT a server token is gated too