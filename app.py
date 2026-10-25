"""
app.py — Flask web server + bot worker supervisor
Run with: python app.py
Open: http://localhost:5000

Trading workers run on background threads of THIS server process, independent of
any browser session: logging out / closing the tab never stops them. The running
configuration is stored in the database (bot_state) so the supervisor restarts
every bot that should be running after a crash or server restart.
"""
import math
import threading
import time
from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, flash)
from flask_socketio import SocketIO, emit, join_room
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

from database import (init_db, create_user, get_user, get_user_by_id,
                      get_recent_trades, get_stats,
                      get_settings, save_settings, set_live_confirmed,
                      save_bot_state, get_bot_state, clear_bot_state, get_all_bot_states)
from bot_engine import BotSession
from broker import fetch_accounts
from config import (SECRET_KEY, SECRET_KEY_IS_DEFAULT, FLASK_PORT, SYMBOLS, MIN_STAKE, MAX_STAKE,
                    MAX_OPEN_TRADES, ALLOW_REGISTRATION, STRATEGIES, LOSS_POLICIES,
                    DERIV_DEMO_TOKEN, DERIV_LIVE_TOKEN)
from logger import log

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
if SECRET_KEY_IS_DEFAULT:
    log.warning("SECRET_KEY is not set — using an insecure default. Set SECRET_KEY in your "
                "environment (Render: Environment tab) before exposing this app.")
# async_mode="threading": the trading bots run on plain OS threads that emit
# socket events from outside any request context; eventlet green threads are
# not safe for that. Requires the "simple-websocket" package.
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

# Active sessions: { user_id: BotSession }  (guarded by _workers_lock)
active_sessions = {}
_workers_lock = threading.RLock()

APPLIED_LIVE_KEYS    = ("lot_size", "max_positions", "loss_policy", "close_on_stop")  # can change while running
APPLIED_RESTART_KEYS = ("symbol", "mode", "strategy", "account_id")                  # need Stop -> Start


# ── Configuration validation (server side — never trust the client) ──
def validate_config(data, base):
    """Merge `data` over `base` and validate. Returns (cfg, error)."""
    cfg = dict(base)
    data = data or {}
    try:
        if "symbol" in data:
            if data["symbol"] not in SYMBOLS:
                return None, "Invalid symbol"
            cfg["symbol"] = data["symbol"]
        if "mode" in data:
            if data["mode"] not in ("demo", "live"):
                return None, "Mode must be demo or live"
            cfg["mode"] = data["mode"]
        if "lot_size" in data:
            v = float(data["lot_size"])
            if not math.isfinite(v):
                return None, "Stake must be a number"
            if v < MIN_STAKE:
                return None, "Minimum stake is {}".format(MIN_STAKE)
            if v > MAX_STAKE:
                return None, "Maximum stake is {}".format(MAX_STAKE)
            if abs(v - round(v, 2)) > 1e-9:
                return None, "Stake can have at most 2 decimal places"
            cfg["lot_size"] = round(v, 2)
        if "max_positions" in data:
            raw = data["max_positions"]
            v = int(raw)
            if isinstance(raw, float) and raw != v:
                return None, "Max positions must be a whole number"
            if v < 1 or v > MAX_OPEN_TRADES:
                return None, "Max positions must be between 1 and {}".format(MAX_OPEN_TRADES)
            cfg["max_positions"] = v
        if "strategy" in data:
            if data["strategy"] not in STRATEGIES:
                return None, "Unknown strategy"
            cfg["strategy"] = data["strategy"]
        if "loss_policy" in data:
            if data["loss_policy"] not in LOSS_POLICIES:
                return None, "Unknown loss policy"
            cfg["loss_policy"] = data["loss_policy"]
        if "close_on_stop" in data:
            v = data["close_on_stop"]
            if isinstance(v, str):
                v = v.strip().lower() in ("1", "true", "yes", "on")
            cfg["close_on_stop"] = bool(v)
        if "account_id" in data:
            v = str(data["account_id"] or "").strip()
            if len(v) > 64 or not all(ch.isalnum() or ch in "-_" for ch in v):
                return None, "Invalid account id"
            cfg["account_id"] = v
    except (TypeError, ValueError):
        return None, "Invalid value in settings"
    return cfg, ""


def _token_for(mode):
    return DERIV_LIVE_TOKEN if mode == "live" else DERIV_DEMO_TOKEN


def _make_session(uid, cfg):
    sid = "user_{}".format(uid)

    def emit_to_user(event, payload, _sid=sid):
        socketio.emit(event, payload, room=_sid)

    return BotSession(
        user_id=uid, symbol=cfg["symbol"], mode=cfg["mode"], emit_fn=emit_to_user,
        lot_size=cfg["lot_size"], max_positions=cfg["max_positions"],
        strategy=cfg["strategy"], loss_policy=cfg["loss_policy"],
        close_on_stop=cfg["close_on_stop"], account_id=cfg.get("account_id", ""))


def _start_worker(uid, cfg):
    """Start (or replace) the single worker for this user. Returns (ok, error)."""
    with _workers_lock:
        old = active_sessions.get(uid)
        if old is not None:
            old.stop(wait=True)
            if old.alive:
                return False, "The previous worker is still stopping — try again in a few seconds"
            active_sessions.pop(uid, None)
        sess = _make_session(uid, cfg)
        active_sessions[uid] = sess
        sess.start()
        save_bot_state(uid, cfg)      # persist the APPLIED config so a restart resumes this bot
    return True, ""


def _supervise_once():
    """Restart any bot that SHOULD be running (bot_state row) but has no live worker.
    Covers server restarts and a worker thread that died."""
    for state in get_all_bot_states():
        uid = state["user_id"]
        with _workers_lock:
            sess = active_sessions.get(uid)
            if sess is not None and (sess.alive or sess._stop_requested):
                continue
            cfg = {k: state[k] for k in ("symbol", "mode", "lot_size", "max_positions",
                                         "strategy", "loss_policy", "close_on_stop", "account_id")}
            new = _make_session(uid, cfg)
            active_sessions[uid] = new
            new.start()
            log.info("Supervisor (re)started bot for user=%s symbol=%s mode=%s", uid, cfg["symbol"], cfg["mode"])


def _supervisor_loop():
    while True:
        time.sleep(30)
        try:
            _supervise_once()
        except Exception as e:
            log.error("Supervisor error: %s", e)


_supervisor_started = False


def start_supervisor():
    """Resume saved bots now and keep watching them. Safe to call more than once."""
    global _supervisor_started
    if _supervisor_started:
        return
    _supervisor_started = True
    try:
        _supervise_once()
    except Exception as e:
        log.error("Could not resume bots on startup: %s", e)
    threading.Thread(target=_supervisor_loop, daemon=True, name="bot-supervisor").start()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = session.get("user_id")
        # The cookie can outlive the user (e.g. the database was reset by a redeploy).
        if not uid or not get_user_by_id(uid):
            session.clear()
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ── Pages ────────────────────────────────────────────────────
@app.route("/")
def index():
    return redirect(url_for("dashboard") if session.get("user_id") else url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_user(username)
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"]  = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    # Every account that registers here shares the SAME Deriv token
    # (set once in the environment, not per-user) — anyone who signs up
    # can trade on your Deriv account. Set ALLOW_REGISTRATION=false once
    # you've created your own login to lock the signup page.
    if not ALLOW_REGISTRATION:
        flash("Registration is currently closed.", "error")
        return redirect(url_for("login"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")
        if not username or not password:
            flash("Username and password are required.", "error")
        elif password != confirm:
            flash("Passwords do not match.", "error")
        elif len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
        else:
            if create_user(username, generate_password_hash(password)):
                flash("Account created! Please log in.", "success")
                return redirect(url_for("login"))
            else:
                flash("Username already taken.", "error")
    return render_template("register.html")


@app.route("/logout")
def logout():
    # Logging out only ends the browser session — it must NOT stop the
    # trading bot. The bot keeps running server-side, exactly as it did
    # before you logged in, until you explicitly click Stop.
    session.clear()
    return redirect(url_for("login"))


@app.route("/healthz")
def healthz():
    """Unauthenticated liveness probe (for hosting health checks / uptime pings)."""
    return jsonify({"ok": True})


@app.route("/dashboard")
@login_required
def dashboard():
    uid      = session["user_id"]
    user     = get_user_by_id(uid)
    settings = get_settings(uid)
    return render_template("dashboard.html",
                           username=user["username"],
                           symbols=SYMBOLS,
                           settings=settings,
                           strategies=STRATEGIES,
                           loss_policies=LOSS_POLICIES,
                           min_stake=MIN_STAKE,
                           max_stake=MAX_STAKE,
                           max_positions_limit=MAX_OPEN_TRADES)


def _settings_payload(uid):
    saved = get_settings(uid)
    sess = active_sessions.get(uid)
    applied = None
    if sess is not None:
        applied = sess.applied_config()
    elif get_bot_state(uid):
        applied = get_bot_state(uid)
    pending = []
    if applied:
        pending = [k for k in APPLIED_LIVE_KEYS + APPLIED_RESTART_KEYS
                   if saved.get(k) != applied.get(k)]
    status = sess.status_dict() if sess is not None else (
        {"state": "starting", "message": "Worker is being restarted..."} if applied else {"state": "stopped"})
    return {"saved": saved, "applied": applied, "pending": pending, "status": status,
            "live_confirmed": saved.get("live_confirmed", False)}


# ── Bot API ──────────────────────────────────────────────────
@app.route("/api/settings", methods=["GET"])
@login_required
def api_get_settings():
    return jsonify(_settings_payload(session["user_id"]))


@app.route("/api/settings", methods=["POST"])
@login_required
def api_save_settings():
    """Save the user's preferred configuration (does NOT change the running bot)."""
    uid = session["user_id"]
    cfg, err = validate_config(request.get_json(silent=True), get_settings(uid))
    if err:
        return jsonify({"ok": False, "error": err}), 400
    save_settings(uid, cfg)
    out = _settings_payload(uid)
    out["ok"] = True
    return jsonify(out)


@app.route("/api/bot/start", methods=["POST"])
@login_required
def api_start_bot():
    uid  = session["user_id"]
    data = request.get_json(silent=True) or {}
    saved = get_settings(uid)
    cfg, err = validate_config(data, saved)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    if not _token_for(cfg["mode"]):
        return jsonify({"ok": False, "error": "No {} API token is configured on the server "
                        "(set DERIV_{}_TOKEN).".format(cfg["mode"].upper(), "LIVE" if cfg["mode"] == "live" else "DEMO")}), 400

    if cfg["mode"] == "live":
        if data.get("confirm_live") is True:
            set_live_confirmed(uid)
        elif not saved.get("live_confirmed"):
            return jsonify({"ok": False, "code": "live_confirmation_required",
                            "error": "Live trading uses REAL money. Confirm to activate it."}), 400

    save_settings(uid, cfg)                      # saved = what the user selected
    ok, err = _start_worker(uid, cfg)            # applied = what the worker runs with
    if not ok:
        return jsonify({"ok": False, "error": err}), 409
    out = _settings_payload(uid)
    out["ok"] = True
    return jsonify(out)


@app.route("/api/bot/stop", methods=["POST"])
@login_required
def api_stop_bot():
    uid = session["user_id"]
    clear_bot_state(uid)       # persist the stop FIRST so the supervisor never restarts it
    with _workers_lock:
        sess = active_sessions.get(uid)
        if sess is not None:
            sess.stop(wait=True)
            if not sess.alive:
                active_sessions.pop(uid, None)
    sess = active_sessions.get(uid)
    return jsonify({"ok": True, "stopped": sess is None or not sess.alive,
                    "close_on_stop": bool(sess.close_on_stop) if sess else None})


@app.route("/api/bot/update", methods=["POST"])
@login_required
def api_update_bot():
    """Apply changes to the RUNNING bot (stake, max open trades, loss policy,
    close-on-stop). Symbol / mode / strategy / account need Stop -> Start."""
    uid  = session["user_id"]
    data = request.get_json(silent=True) or {}
    sess = active_sessions.get(uid)
    if not sess or not sess.alive:
        return jsonify({"ok": False, "error": "Bot not running"}), 400

    applied = sess.applied_config()
    cfg, err = validate_config(data, applied)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    for k in APPLIED_RESTART_KEYS:
        if cfg.get(k) != applied.get(k):
            return jsonify({"ok": False, "error": "Changing {} requires stopping the bot first.".format(
                k.replace("_", " "))}), 400

    sess.update_settings(lot_size=cfg["lot_size"], max_positions=cfg["max_positions"],
                         loss_policy=cfg["loss_policy"], close_on_stop=cfg["close_on_stop"])
    save_bot_state(uid, sess.applied_config())
    save_settings(uid, sess.applied_config())
    out = _settings_payload(uid)
    out["ok"] = True
    return jsonify(out)


@app.route("/api/bot/status")
@login_required
def api_bot_status():
    uid = session["user_id"]
    out = _settings_payload(uid)
    st = out["status"]
    return jsonify({"running": st.get("state") in ("starting", "running", "reconnecting", "paused", "stopping"),
                    "status": st, "applied": out["applied"], "saved": out["saved"],
                    "pending": out["pending"]})


@app.route("/api/accounts")
@login_required
def api_accounts():
    """Accounts the configured token can access (ids, type, balance, currency only)."""
    mode = request.args.get("mode", "demo")
    if mode not in ("demo", "live"):
        return jsonify({"ok": False, "error": "Mode must be demo or live"}), 400
    token = _token_for(mode)
    if not token:
        return jsonify({"ok": False, "error": "No {} API token configured on the server".format(mode.upper())})
    import asyncio
    try:
        accounts = asyncio.run(fetch_accounts(token))
    except ConnectionError as e:
        return jsonify({"ok": False, "error": str(e)})
    except Exception as e:
        log.error("fetch_accounts error: %s", e)
        return jsonify({"ok": False, "error": "Could not load accounts"})
    want = "real" if mode == "live" else "demo"
    return jsonify({"ok": True, "accounts": [a for a in accounts if a["account_type"] == want]})


@app.route("/api/diagnose")
@login_required
def api_diagnose():
    """Shows what THIS server has configured and what Deriv answers. Tokens are never
    returned - only their length, first/last characters and any suspicious characters."""
    import asyncio
    import aiohttp
    from config import DERIV_APP_ID, DERIV_REST_BASE

    def describe(tok):
        info = {"set": bool(tok), "length": len(tok)}
        if tok:
            info.update({"starts_with": tok[:4], "ends_with": tok[-4:],
                         "starts_with_pat_": tok.startswith("pat_"),
                         "has_whitespace_or_quotes": any(c.isspace() or c in "\"'" for c in tok),
                         "has_non_ascii": any(ord(c) > 127 for c in tok)})
        return info

    async def probe(token):
        if not token:
            return {"result": "no token set on this server"}
        headers = {"Deriv-App-ID": str(DERIV_APP_ID), "Authorization": "Bearer {}".format(token)}
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get("{}/accounts".format(DERIV_REST_BASE), headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                    full = await r.text()
                    out = {"http_status": r.status}
                    if r.status == 200:
                        try:
                            import json as _j
                            out["accounts"] = [{"account_id": a.get("account_id"), "type": a.get("account_type"),
                                                "balance": a.get("balance")}
                                               for a in _j.loads(full).get("data", [])]
                        except Exception:
                            out["accounts"] = "reply received (not parsed)"
                    else:
                        out["deriv_reply"] = full[:400]
                    return out
        except Exception as e:
            return {"result": "could not reach Deriv: {}".format(e)}

    async def server_ip():
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get("https://api.ipify.org", timeout=aiohttp.ClientTimeout(total=8)) as r:
                    return (await r.text()).strip()
        except Exception:
            return "unknown"

    async def run():
        return {
            "app_id": DERIV_APP_ID,
            "app_id_length": len(str(DERIV_APP_ID)),
            "server_outbound_ip": await server_ip(),
            "demo_token": describe(DERIV_DEMO_TOKEN), "demo_check": await probe(DERIV_DEMO_TOKEN),
            "live_token": describe(DERIV_LIVE_TOKEN), "live_check": await probe(DERIV_LIVE_TOKEN),
        }
    try:
        return jsonify(asyncio.run(run()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trades")
@login_required
def api_trades():
    uid  = session["user_id"]
    mode = request.args.get("mode") or get_settings(uid)["mode"]
    if mode not in ("demo", "live"):
        mode = "demo"
    return jsonify({
        "trades": get_recent_trades(uid, 50, mode),
        "stats":  get_stats(uid, mode),
        "mode":   mode,
    })


# ── SocketIO ─────────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    uid = session.get("user_id")
    if uid:
        join_room("user_{}".format(uid))
        emit("connected", {"message": "WebSocket connected"})
        # Browser just (re)connected: re-send charts/status/trades so the
        # dashboard is populated even if the bot started before the page opened.
        sess = active_sessions.get(uid)
        if sess:
            sess.send_snapshot()


@socketio.on("request_snapshot")
def on_request_snapshot():
    uid = session.get("user_id")
    sess = active_sessions.get(uid) if uid else None
    if sess:
        sess.send_snapshot()


@socketio.on("disconnect")
def on_disconnect():
    pass


# ── Entry point ──────────────────────────────────────────────
def main():
    init_db()
    start_supervisor()
    print("")
    print("  ==========================================")
    print("   SyntheticBot Pro - Starting...")
    print("   Open browser -> http://localhost:{}".format(FLASK_PORT))
    print("  ==========================================")
    print("")
    # allow_unsafe_werkzeug=True: Flask-SocketIO refuses its built-in dev server
    # unless confirmed. With async_mode="threading" Werkzeug serves requests, which
    # is fine for a personal dashboard behind Render's HTTPS proxy.
    socketio.run(app, host="0.0.0.0", port=FLASK_PORT, debug=False,
                 allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()