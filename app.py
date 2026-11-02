"""
app.py — Flask web server + bot worker supervisor
Run with: python app.py
Open: http://localhost:5000

Trading workers run on background threads of THIS server process, independent of
any browser session: logging out / closing the tab never stops them. The running
configuration is stored in the database (bot_state) so the supervisor restarts
every bot that should be running after a crash or server restart.
"""
import csv
import io
import json
import math
import threading
import time
from datetime import timedelta
from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, flash, Response)
from flask_socketio import SocketIO, emit, join_room
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

from database import (init_db, create_user, get_user, get_user_by_id,
                      get_recent_trades, get_stats,
                      get_settings, save_settings, set_live_confirmed,
                      save_bot_state, get_bot_state, clear_bot_state, get_all_bot_states,
                      set_entries_hold,
                      log_activity, get_activity, get_history, get_history_summary,
                      get_history_symbols, cloud_ready, get_user_token_blobs, set_user_token,
                      set_user_active, touch_login, user_is_active, get_first_user_id, list_users_admin)
import pwa
import token_vault
from cloud_store import store as cloud
from bot_engine import BotSession
from broker import fetch_accounts
from config import (SECRET_KEY, SECRET_KEY_IS_DEFAULT, FLASK_PORT, SYMBOLS, MIN_STAKE, MAX_STAKE,
                    MAX_OPEN_TRADES, ALLOW_REGISTRATION, STRATEGIES, LOSS_POLICIES,
                    DERIV_DEMO_TOKEN, DERIV_LIVE_TOKEN, TIMEFRAME_CHOICES, SESSION_DAYS,
                    ADMIN_USERNAMES, MAX_CONCURRENT_BOTS, ADMIN_PATH, ADMIN_PATH_IS_DEFAULT,
                    ADMIN_SESSION_HOURS, ADMIN_LOGIN_MAX_FAILS, ADMIN_LOGIN_LOCK_SEC)
from logger import log

app = Flask(__name__)
app.secret_key = SECRET_KEY
# Logins are permanent cookies: a user stays signed in until they press Logout (SESSION_DAYS, default ~10 years).
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_DAYS),
                  SESSION_REFRESH_EACH_REQUEST=True)
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

APPLIED_LIVE_KEYS    = ("lot_size", "max_positions", "loss_policy", "close_on_stop", "timeframe")  # can change while running
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
        if "timeframe" in data:
            if data["timeframe"] not in TIMEFRAME_CHOICES:
                return None, "Timeframe must be 1 minute (M1) or 5 minutes (M5)"
            cfg["timeframe"] = data["timeframe"]
        if "account_id" in data:
            v = str(data["account_id"] or "").strip()
            if len(v) > 64 or not all(ch.isalnum() or ch in "-_" for ch in v):
                return None, "Invalid account id"
            cfg["account_id"] = v
    except (TypeError, ValueError):
        return None, "Invalid value in settings"
    return cfg, ""


# ── Who is who, and whose Deriv token a bot uses ─────────────
def is_admin_user(user):
    """Admins: usernames listed in ADMIN_USERNAMES; if that is empty, the first account ever created."""
    if not user:
        return False
    if ADMIN_USERNAMES:
        return str(user["username"]).strip().lower() in ADMIN_USERNAMES
    return user["id"] == get_first_user_id()


def _own_token(uid, mode):
    return token_vault.decrypt(get_user_token_blobs(uid)["live" if mode == "live" else "demo"])


def _user_token(uid, mode):
    """The token a bot for this user must use: their own; only an admin may fall back to the
    server's DERIV_*_TOKEN. Everyone else without a token gets "" (and cannot trade)."""
    own = _own_token(uid, mode)
    if own:
        return own
    if is_admin_user(get_user_by_id(uid)):
        return DERIV_LIVE_TOKEN if mode == "live" else DERIV_DEMO_TOKEN
    return ""


def _token_source(uid, mode):
    if _own_token(uid, mode):
        return "own"
    if is_admin_user(get_user_by_id(uid)) and (DERIV_LIVE_TOKEN if mode == "live" else DERIV_DEMO_TOKEN):
        return "server"
    return "none"


def _make_session(uid, cfg):
    sid = "user_{}".format(uid)

    def emit_to_user(event, payload, _sid=sid):
        socketio.emit(event, payload, room=_sid)

    return BotSession(
        user_id=uid, symbol=cfg["symbol"], mode=cfg["mode"], emit_fn=emit_to_user,
        lot_size=cfg["lot_size"], max_positions=cfg["max_positions"],
        strategy=cfg["strategy"], loss_policy=cfg["loss_policy"],
        close_on_stop=cfg["close_on_stop"], account_id=cfg.get("account_id", ""),
        timeframe=cfg.get("timeframe", "M1"), token=_user_token(uid, cfg["mode"]))


def _start_worker(uid, cfg):
    """Start (or replace) the single worker for this user. Returns (ok, error)."""
    with _workers_lock:
        old = active_sessions.get(uid)
        if old is not None:
            old.stop(wait=True)
            if old.alive:
                return False, "The previous worker is still stopping — try again in a few seconds"
            active_sessions.pop(uid, None)
        running = sum(1 for u, s in active_sessions.items() if u != uid and s.alive)
        if running >= MAX_CONCURRENT_BOTS:
            return False, "The server is at its limit of {} running bots. Please try again later.".format(MAX_CONCURRENT_BOTS)
        sess = _make_session(uid, cfg)
        active_sessions[uid] = sess
        sess.start()
        save_bot_state(uid, cfg)      # persist the APPLIED config so a restart resumes this bot
        set_entries_hold(uid, False)  # a fresh Activate always starts without a manual hold
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
            if not user_is_active(get_user_by_id(uid)):
                clear_bot_state(uid)               # deactivated (or deleted) accounts never run
                continue
            if not _user_token(uid, state["mode"]):
                log.warning("Not resuming bot for user=%s: no %s token connected", uid, state["mode"])
                continue
            cfg = {k: state[k] for k in ("symbol", "mode", "lot_size", "max_positions",
                                         "strategy", "loss_policy", "close_on_stop", "account_id", "timeframe")}
            new = _make_session(uid, cfg)
            new.entries_hold = bool(state.get("entries_hold"))
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
        user = get_user_by_id(uid) if uid else None
        if not user:
            session.clear()
            return redirect(url_for("login"))
        if not user_is_active(user):
            session.clear()
            flash("This account has been deactivated. Please contact the administrator.", "error")
            return redirect(url_for("login"))
        session.permanent = True        # also upgrades logins made before this update
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
        if user and check_password_hash(user["password_hash"], password) and not user_is_active(user):
            flash("This account has been deactivated. Please contact the administrator.", "error")
            return render_template("login.html")
        if user and check_password_hash(user["password_hash"], password):
            touch_login(user["id"])
            session.permanent = True
            session["user_id"]  = user["id"]
            session["username"] = user["username"]
            log_activity(user["id"], "login", "Signed in")
            return redirect(url_for("dashboard"))
        if user:
            log_activity(user["id"], "login_failed", "Wrong password entered")
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    # Every user connects THEIR OWN Deriv tokens on the dashboard; the server's DERIV_*_TOKEN is only used
    # for admin accounts. Set ALLOW_REGISTRATION=false to close sign-ups.
    if not ALLOW_REGISTRATION:
        flash("Registration is currently closed.", "error")
        return redirect(url_for("login"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")
        if not cloud_ready():
            flash("The account service is still starting up. Please try again in a minute.", "error")
        elif not username or not password:
            flash("Username and password are required.", "error")
        elif password != confirm:
            flash("Passwords do not match.", "error")
        elif len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
        else:
            if create_user(username, generate_password_hash(password)):
                new_user = get_user(username)
                if new_user:
                    log_activity(new_user["id"], "register", "Account created")
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
    if session.get("user_id"):
        log_activity(session["user_id"], "logout", "Signed out (bot keeps running)")
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
                           timeframes=TIMEFRAME_CHOICES,
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
    log_activity(uid, "settings", _cfg_text(cfg))
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

    if not _user_token(uid, cfg["mode"]):
        return jsonify({"ok": False, "code": "token_required",
                        "error": "Connect your {} Deriv API token first (sidebar → Deriv connection).".format(
                            cfg["mode"].upper())}), 400

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
    log_activity(uid, "bot_start", _cfg_text(cfg))
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
    log_activity(uid, "bot_stop", "Bot stopped by the user")
    sess = active_sessions.get(uid)
    return jsonify({"ok": True, "stopped": sess is None or not sess.alive,
                    "close_on_stop": bool(sess.close_on_stop) if sess else None})


def _live_session(uid):
    sess = active_sessions.get(uid)
    if not sess or not sess.alive or sess.loop is None or not sess.loop.is_running():
        return None
    return sess


@app.route("/api/bot/close_all", methods=["POST"])
@login_required
def api_close_all():
    """Close every open trade now. hold=true (default) also keeps new trades from opening
    until the user presses Resume Trading."""
    import asyncio
    uid = session["user_id"]
    sess = _live_session(uid)
    if sess is None:
        return jsonify({"ok": False, "error": "The bot is not running. Activate it first - it needs its "
                        "Deriv connection to close trades."}), 400
    hold = (request.get_json(silent=True) or {}).get("hold", True) is not False
    try:
        result = asyncio.run_coroutine_threadsafe(sess.close_all_manual(hold), sess.loop).result(timeout=45)
    except Exception as e:
        log.error("close_all error: %s", e)
        return jsonify({"ok": False, "error": "Could not close the trades: {}".format(e)}), 500
    log_activity(uid, "close_all", "Closed all open trades manually{}".format(
        " and put new trades on hold" if hold else ""))
    result["ok"] = True
    return jsonify(result)


@app.route("/api/bot/resume", methods=["POST"])
@login_required
def api_resume():
    uid = session["user_id"]
    sess = _live_session(uid)
    if sess is None:
        return jsonify({"ok": False, "error": "The bot is not running."}), 400
    sess.set_hold(False)
    log_activity(uid, "resume", "Resumed trading")
    return jsonify({"ok": True, "hold": False})


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
                         loss_policy=cfg["loss_policy"], close_on_stop=cfg["close_on_stop"],
                         timeframe=cfg["timeframe"])
    save_bot_state(uid, sess.applied_config())
    save_settings(uid, sess.applied_config())
    log_activity(uid, "bot_update", _cfg_text(sess.applied_config()))
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
    token = _user_token(session["user_id"], mode)
    if not token:
        return jsonify({"ok": False, "error": "No {} API token connected yet".format(mode.upper())})
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
    if not is_admin_user(get_user_by_id(session["user_id"])):
        return jsonify({"ok": False, "error": "Administrators only"}), 403
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


def _cfg_text(cfg):
    return "{} · {} · {} · {} · stake {} · max {} trades".format(
        cfg.get("symbol"), cfg.get("mode"), cfg.get("strategy"), cfg.get("timeframe"),
        cfg.get("lot_size"), cfg.get("max_positions"))


# ── Each user's own Deriv tokens ─────────────────────────────
def _token_status(uid):
    out = {}
    for mode in ("demo", "live"):
        own = _own_token(uid, mode)
        out[mode] = {"source": _token_source(uid, mode), "hint": token_vault.hint(own) if own else ""}
        out[mode]["connected"] = out[mode]["source"] != "none"
    return out


@app.route("/api/tokens")
@login_required
def api_tokens():
    """Which tokens are connected (never the tokens themselves)."""
    return jsonify({"ok": True, "tokens": _token_status(session["user_id"])})


def _check_token(token, mode):
    """Ask Deriv what this token can access. Returns (ok, message, account)."""
    import asyncio
    want = "real" if mode == "live" else "demo"
    try:
        accounts = asyncio.run(fetch_accounts(token))
    except ConnectionError as e:
        msg = str(e)
        if "invalid or expired" in msg.lower():
            msg += " Make sure it was created for the same App ID this site uses, and that it is not expired."
        return False, msg, None
    except Exception as e:
        log.error("token check error: %s", e)
        return False, "Could not reach Deriv to check the token. Try again in a moment.", None
    match = [a for a in accounts if a.get("account_type") == want]
    if not match:
        kinds = sorted({a.get("account_type") or "?" for a in accounts}) or ["none"]
        return False, ("This token has no {} account (it gives access to: {}). "
                       "Use a {} token here.".format(mode.upper(), ", ".join(kinds), mode.upper())), None
    a = match[0]
    return True, "", {"account_id": a.get("account_id", ""), "balance": a.get("balance"),
                      "currency": a.get("currency", "")}


@app.route("/api/tokens", methods=["POST"])
@login_required
def api_save_token():
    uid = session["user_id"]
    data = request.get_json(silent=True) or {}
    mode = data.get("mode")
    token = str(data.get("token") or "").strip().strip('"\'').strip()
    if mode not in ("demo", "live"):
        return jsonify({"ok": False, "error": "Mode must be demo or live"}), 400
    if not (10 <= len(token) <= 200) or not all(ch.isalnum() or ch in "_-." for ch in token):
        return jsonify({"ok": False, "error": "That does not look like a Deriv API token. Paste it exactly as Deriv shows it."}), 400
    sess = active_sessions.get(uid)
    if sess is not None and sess.alive and sess.mode == mode:
        return jsonify({"ok": False, "error": "Stop your bot before changing its {} token.".format(mode.upper())}), 409
    ok, msg, account = _check_token(token, mode)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    set_user_token(uid, mode, token_vault.encrypt(token))
    log_activity(uid, "token_saved", "{} token connected (account {})".format(mode.upper(), account["account_id"]))
    return jsonify({"ok": True, "account": account, "tokens": _token_status(uid)})


@app.route("/api/tokens/remove", methods=["POST"])
@login_required
def api_remove_token():
    uid = session["user_id"]
    mode = (request.get_json(silent=True) or {}).get("mode")
    if mode not in ("demo", "live"):
        return jsonify({"ok": False, "error": "Mode must be demo or live"}), 400
    sess = active_sessions.get(uid)
    if sess is not None and sess.alive and sess.mode == mode:
        return jsonify({"ok": False, "error": "Stop your bot before removing its {} token.".format(mode.upper())}), 409
    set_user_token(uid, mode, "")
    log_activity(uid, "token_removed", "{} token removed".format(mode.upper()))
    return jsonify({"ok": True, "tokens": _token_status(uid)})


# ── Admin console: a separate app at a SECRET path with its own login ──
# Nothing in the normal app links to it, and every admin URL answers 404 (not "forbidden") to anyone who is not
# signed in here, so its existence is not revealed. Signing in here does NOT sign you in to the trading app and
# vice versa: the admin session is its own (short-lived, throttled) session.
ADMIN_BASE = ADMIN_PATH.rstrip("/")
_admin_fails = {}          # client address -> (failures, locked_until)
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() or request.remote_addr or "?")


def _admin_session_user():
    """The signed-in admin (dict) or None. Checks age, that the account still exists, is active and is an admin."""
    uid, at = session.get("admin_uid"), session.get("admin_at")
    if not uid or not at or time.time() - float(at) > ADMIN_SESSION_HOURS * 3600:
        return None
    user = get_user_by_id(uid)
    if not user or not user_is_active(user) or not is_admin_user(user):
        return None
    return user


def _admin_clear():
    for k in ("admin_uid", "admin_at", "admin_name"):
        session.pop(k, None)


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = _admin_session_user()
        if user is None:
            _admin_clear()
            if request.path.startswith(ADMIN_BASE + "/api/"):
                return ("", 404)
            return redirect(ADMIN_BASE + "/login")
        return f(*args, **kwargs)
    return decorated


def _stop_user_bot(uid, close_trades=False):
    """Stop one user's bot (persisting the stop first so the supervisor never restarts it)."""
    import asyncio
    clear_bot_state(uid)
    closed = None
    sess = _live_session(uid)
    if sess is not None and close_trades:
        try:
            closed = asyncio.run_coroutine_threadsafe(sess.close_all_manual(True), sess.loop).result(timeout=60)
        except Exception as e:
            log.error("admin close-all for user %s failed: %s", uid, e)
    with _workers_lock:
        sess = active_sessions.get(uid)
        if sess is not None:
            sess.stop(wait=True)
            if not sess.alive:
                active_sessions.pop(uid, None)
    return closed


@app.route(ADMIN_BASE + "/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        ip = _client_ip()
        fails, locked_until = _admin_fails.get(ip, (0, 0))
        if locked_until > time.time():
            flash("Too many attempts. Try again in a few minutes.", "error")
            return render_template("admin_login.html", admin_base=ADMIN_BASE), 429
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_user(username)
        ok = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)
        if user and ok and user_is_active(user) and is_admin_user(user):
            _admin_fails.pop(ip, None)
            session["admin_uid"], session["admin_at"], session["admin_name"] = user["id"], time.time(), user["username"]
            session.permanent = True
            log_activity(user["id"], "admin_login", "Signed in to the admin console")
            return redirect(ADMIN_BASE + "/")
        fails += 1
        _admin_fails[ip] = (fails, time.time() + ADMIN_LOGIN_LOCK_SEC if fails >= ADMIN_LOGIN_MAX_FAILS else 0)
        if fails >= ADMIN_LOGIN_MAX_FAILS:
            _admin_fails[ip] = (0, time.time() + ADMIN_LOGIN_LOCK_SEC)
        log.warning("Admin console: failed sign-in for '%s' from %s", username[:40], ip)
        flash("Invalid credentials.", "error")
    return render_template("admin_login.html", admin_base=ADMIN_BASE)


@app.route(ADMIN_BASE + "/logout")
def admin_logout():
    user = _admin_session_user()
    if user:
        log_activity(user["id"], "admin_logout", "Signed out of the admin console")
    _admin_clear()
    return redirect(ADMIN_BASE + "/login")


@app.route(ADMIN_BASE + "/")
@admin_required
def admin_page():
    return render_template("admin.html", admin_base=ADMIN_BASE, username=session.get("admin_name", ""),
                           admin_setting=bool(ADMIN_USERNAMES))


@app.route(ADMIN_BASE + "/api/users")
@admin_required
def api_admin_users():
    rows = list_users_admin()
    for u in rows:
        sess = active_sessions.get(u["id"])
        running = bool(sess is not None and sess.alive)
        u["bot_running"] = running
        u["bot"] = ({"mode": sess.mode, "symbol": sess.symbol, "strategy": sess.strategy,
                     "state": sess.status_dict().get("state")} if running else None)
        u["is_admin"] = is_admin_user({"id": u["id"], "username": u["username"]})
    return jsonify({"ok": True, "users": rows, "summary": {
        "total": len(rows), "active": sum(1 for u in rows if u["is_active"]),
        "deactivated": sum(1 for u in rows if not u["is_active"]),
        "bots_running": sum(1 for u in rows if u["bot_running"]),
        "max_bots": MAX_CONCURRENT_BOTS}})


@app.route(ADMIN_BASE + "/api/users/<int:target>")
@admin_required
def api_admin_user_detail(target):
    user = get_user_by_id(target)
    if not user:
        return jsonify({"ok": False, "error": "No such user"}), 404
    acts, total = get_activity(target, 50, 0)
    return jsonify({"ok": True, "username": user["username"], "activity": acts, "activity_total": total,
                    "trades": get_recent_trades(target, 50), "stats_demo": get_stats(target, "demo"),
                    "stats_live": get_stats(target, "live"), "settings": get_settings(target)})


@app.route(ADMIN_BASE + "/api/users/<int:target>/active", methods=["POST"])
@admin_required
def api_admin_set_active(target):
    me = _admin_session_user()
    user = get_user_by_id(target)
    if not user:
        return jsonify({"ok": False, "error": "No such user"}), 404
    data = request.get_json(silent=True) or {}
    active = bool(data.get("active"))
    if target == me["id"] and not active:
        return jsonify({"ok": False, "error": "You cannot deactivate your own account."}), 400
    if not active and is_admin_user(user):
        return jsonify({"ok": False, "error": "Administrator accounts cannot be deactivated."}), 400
    set_user_active(target, active)
    closed = None
    if active:
        log_activity(target, "activated", "Account activated by admin {}".format(me["username"]))
    else:
        closed = _stop_user_bot(target, bool(data.get("close_trades")))
        log_activity(target, "deactivated", "Account deactivated by admin {} (bot stopped{})".format(
            me["username"], ", open trades closed" if data.get("close_trades") else ""))
    return jsonify({"ok": True, "id": target, "is_active": active, "closed": closed})


# ── Install-as-app (PWA): the trading app and the admin console are two SEPARATE installable apps ──
@app.route("/manifest.webmanifest")
def user_manifest_route():
    return Response(json.dumps(pwa.user_manifest()), mimetype="application/manifest+json",
                    headers={"Cache-Control": "no-cache"})


@app.route("/sw.js")
def user_sw():
    return Response(pwa.service_worker_js("synthbot-user-v1", "/offline"), mimetype="application/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.route("/offline")
def user_offline():
    return pwa.OFFLINE_HTML % "No connection right now. Your bots keep running on the server; reconnect to see them."


@app.route(ADMIN_BASE + "/manifest.webmanifest")
def admin_manifest_route():
    return Response(json.dumps(pwa.admin_manifest(ADMIN_BASE)), mimetype="application/manifest+json",
                    headers={"Cache-Control": "no-cache"})


@app.route(ADMIN_BASE + "/sw.js")
def admin_sw():
    return Response(pwa.service_worker_js("synthbot-admin-v1", ADMIN_BASE + "/offline"),
                    mimetype="application/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": ADMIN_BASE + "/"})


@app.route(ADMIN_BASE + "/offline")
def admin_offline():
    return pwa.OFFLINE_HTML % "No connection right now. Reconnect to use the admin console."


# ── History: every trade and every action, kept forever ─────
def _hist_filters():
    a = request.args
    return dict(mode=a.get("mode") or None, status=a.get("status") or None,
                symbol=a.get("symbol") or None,
                date_from=a.get("from") or None, date_to=a.get("to") or None)


@app.route("/history")
@login_required
def history_page():
    uid = session["user_id"]
    return render_template("history.html", username=get_user_by_id(uid)["username"],
                           symbols=SYMBOLS, used_symbols=get_history_symbols(uid),
                           mode=get_settings(uid)["mode"])


@app.route("/api/history")
@login_required
def api_history():
    uid = session["user_id"]
    page = max(1, request.args.get("page", 1, type=int))
    per = min(200, max(1, request.args.get("per", 50, type=int)))
    rows, total, totals = get_history(uid, limit=per, offset=(page - 1) * per, **_hist_filters())
    return jsonify({"trades": rows, "total": total, "totals": totals, "page": page, "per": per,
                    "pages": max(1, math.ceil(total / float(per)))})


@app.route("/api/history/summary")
@login_required
def api_history_summary():
    uid = session["user_id"]
    period = request.args.get("period", "day")
    if period not in ("day", "month", "year"):
        period = "day"
    f = _hist_filters()
    rows = get_history_summary(uid, period, f["mode"], f["symbol"], f["date_from"], f["date_to"])
    return jsonify({"period": period, "rows": rows,
                    "pnl": round(sum(r["pnl"] for r in rows), 2),
                    "trades": sum(r["trades"] for r in rows)})


@app.route("/api/activity")
@login_required
def api_activity():
    uid = session["user_id"]
    page = max(1, request.args.get("page", 1, type=int))
    per = min(200, max(1, request.args.get("per", 50, type=int)))
    rows, total = get_activity(uid, per, (page - 1) * per)
    return jsonify({"activity": rows, "total": total, "page": page, "per": per,
                    "pages": max(1, math.ceil(total / float(per)))})


@app.route("/history.csv")
@login_required
def history_csv():
    """The user's complete (filtered) trade history as a spreadsheet file."""
    uid = session["user_id"]
    rows, _, _ = get_history(uid, limit=10 ** 9, offset=0, **_hist_filters())
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "opened_at_utc", "closed_at_utc", "mode", "symbol", "direction", "strategy",
                "stake", "entry_price", "exit_price", "pnl", "status", "exit_reason", "contract_id"])
    for t in rows:
        w.writerow([t["id"], t.get("opened_at") or "", t.get("closed_at") or "", t.get("mode"),
                    t.get("symbol"), t.get("direction"), t.get("strategy") or "", t.get("stake"),
                    t.get("entry_price"), t.get("exit_price"), t.get("pnl"), t.get("status"),
                    t.get("exit_reason") or "", t.get("contract_id") or ""])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=trade-history.csv"})


@app.route("/api/cloud")
@login_required
def api_cloud():
    """Is the permanent (Firebase) copy switched on and up to date?"""
    return jsonify(cloud.status())


# ── SocketIO ─────────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    uid = session.get("user_id")
    if uid and user_is_active(get_user_by_id(uid)):
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
    log.warning("Admin console address: %s%s", ADMIN_BASE + "/login",
                "   (derived from SECRET_KEY - set ADMIN_PATH in Render to choose your own)" if ADMIN_PATH_IS_DEFAULT else "")
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