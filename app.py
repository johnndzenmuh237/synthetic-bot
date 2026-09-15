"""
app.py — Flask web server
Run with: python app.py
Open: http://localhost:5000
"""
from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, flash)
from flask_socketio import SocketIO, emit, join_room
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

from database import (init_db, create_user, get_user, get_user_by_id,
                      get_recent_trades, get_stats,
                      save_bot_state, clear_bot_state, get_all_bot_states)
from bot_engine import BotSession
from config import SECRET_KEY, FLASK_PORT, SYMBOLS, MIN_STAKE, MAX_STAKE, ALLOW_REGISTRATION
from logger import log

app = Flask(__name__)
app.secret_key = SECRET_KEY
# async_mode="threading" instead of "eventlet": the trading bot runs on a
# plain OS thread (threading.Thread in bot_engine.py) that calls
# socketio.emit() from outside any Flask/SocketIO request context.
# eventlet uses cooperative "green threads" and isn't safe to call into
# from a real OS thread it doesn't control — that mismatch is what was
# causing balance/candle updates to silently stop reaching the browser
# after a while, with no error on the bot side. "threading" mode uses
# real OS threads with proper locking throughout, so it's safe to emit
# from the bot's thread at any time. Requires the "simple-websocket"
# package for WebSocket transport support (pip install simple-websocket).
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

# Active sessions: { user_id: BotSession }
active_sessions = {}


def _resume_bots_on_startup():
    """Runs once when the server process boots (after init_db(), see the
    bottom of this file). Restarts any bot that was running before this
    restart (a Render redeploy, a crash, waking back up from being asleep)
    — without this, a server restart would silently leave the bot off
    until someone manually opened the dashboard and clicked Start again,
    which defeats 'keep running unless I stop it'."""
    for state in get_all_bot_states():
        uid = state["user_id"]
        sid = "user_{}".format(uid)

        def emit_to_user(event, payload, _sid=sid):
            socketio.emit(event, payload, room=_sid)

        sess = BotSession(
            user_id       = uid,
            symbol        = state["symbol"],
            mode          = state["mode"],
            emit_fn       = emit_to_user,
            lot_size      = state["lot_size"],
            max_positions = state["max_positions"],
        )
        active_sessions[uid] = sess
        sess.start()
        log.info("Auto-resumed bot for user=%s symbol=%s mode=%s "
                 "(was running before this server restart)",
                 uid, state["symbol"], state["mode"])


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
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


@app.route("/dashboard")
@login_required
def dashboard():
    uid    = session["user_id"]
    user   = get_user_by_id(uid)
    trades = get_recent_trades(uid, 20)
    stats  = get_stats(uid)
    return render_template("dashboard.html",
                           username=user["username"],
                           symbols=SYMBOLS,
                           trades=trades,
                           stats=stats,
                           min_stake=MIN_STAKE,
                           max_stake=MAX_STAKE)


# ── Bot API ──────────────────────────────────────────────────
@app.route("/api/bot/start", methods=["POST"])
@login_required
def api_start_bot():
    uid  = session["user_id"]
    data = request.json or {}

    symbol        = data.get("symbol", "R_50")
    mode          = data.get("mode", "demo")
    lot_size      = float(data.get("lot_size", 1.0))
    # Default is 2, not 1: every ClaudeFX signal opens 2 legs simultaneously
    # (TP1 at 70%, TP2 at 30%) — with max_positions=1 the bot can never
    # actually place a trade, and would fail completely silently.
    max_positions = int(data.get("max_positions", 2))

    if symbol not in SYMBOLS:
        return jsonify({"ok": False, "error": "Invalid symbol"}), 400
    if mode not in ("demo", "live"):
        return jsonify({"ok": False, "error": "Mode must be demo or live"}), 400
    if lot_size < MIN_STAKE:
        return jsonify({"ok": False,
                        "error": "Minimum lot size is {}".format(MIN_STAKE)}), 400
    if max_positions < 2 or max_positions > 10:
        return jsonify({"ok": False,
                        "error": "Max positions must be at least 2 (each signal "
                                 "opens a TP1 leg and a TP2 leg simultaneously), "
                                 "up to 10"}), 400

    # Stop existing session
    if uid in active_sessions:
        active_sessions[uid].stop()
        del active_sessions[uid]

    sid = "user_{}".format(uid)

    def emit_to_user(event, payload):
        socketio.emit(event, payload, room=sid)

    sess = BotSession(
        user_id       = uid,
        symbol        = symbol,
        mode          = mode,
        emit_fn       = emit_to_user,
        lot_size      = lot_size,
        max_positions = max_positions,
    )
    active_sessions[uid] = sess
    sess.start()
    # Persist so the server can auto-resume this bot after any restart
    # (deploy, crash, Render waking back up) without needing anyone to
    # click Start again — the strategy requires the bot to keep running
    # until manually stopped.
    save_bot_state(uid, symbol, mode, lot_size, max_positions)

    return jsonify({
        "ok":           True,
        "symbol":       symbol,
        "mode":         mode,
        "lot_size":     lot_size,
        "max_positions": max_positions,
    })


@app.route("/api/bot/stop", methods=["POST"])
@login_required
def api_stop_bot():
    uid = session["user_id"]
    if uid in active_sessions:
        active_sessions[uid].stop()
        del active_sessions[uid]
    clear_bot_state(uid)   # explicit stop — do NOT auto-resume this one on restart
    return jsonify({"ok": True})


@app.route("/api/bot/update", methods=["POST"])
@login_required
def api_update_bot():
    """Update lot size / max positions while bot is running."""
    uid  = session["user_id"]
    data = request.json or {}
    sess = active_sessions.get(uid)
    if not sess:
        return jsonify({"ok": False, "error": "Bot not running"}), 400

    lot_size      = data.get("lot_size")
    max_positions = data.get("max_positions")

    if lot_size is not None and float(lot_size) < MIN_STAKE:
        return jsonify({"ok": False,
                        "error": "Minimum lot size is {}".format(MIN_STAKE)}), 400

    sess.update_settings(
        lot_size      = float(lot_size) if lot_size else None,
        max_positions = int(max_positions) if max_positions else None,
    )
    return jsonify({"ok": True})


@app.route("/api/bot/status")
@login_required
def api_bot_status():
    uid  = session["user_id"]
    sess = active_sessions.get(uid)
    if sess:
        return jsonify({
            "running":       sess.running,
            "symbol":        sess.symbol,
            "mode":          sess.mode,
            "balance":       sess.broker.balance,
            "currency":      sess.broker.currency,
            "account_id":    sess.broker.account_id,
            "lot_size":      sess.lot_size,
            "max_positions": sess.max_positions,
        })
    return jsonify({"running": False})


@app.route("/api/trades")
@login_required
def api_trades():
    uid = session["user_id"]
    return jsonify({
        "trades": get_recent_trades(uid, 50),
        "stats":  get_stats(uid),
    })


# ── SocketIO ─────────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    uid = session.get("user_id")
    if not uid:
        return
    join_room("user_{}".format(uid))
    emit("connected", {"message": "WebSocket connected"})

    # If this user's bot is already running (auto-resumed on server boot,
    # or just running from before this browser tab opened/reopened), the
    # one-time initial candle history was already sent into the room back
    # when the bot started — possibly before this socket ever connected,
    # so it was missed entirely. Replay it now, directly to this socket,
    # so the chart always has something to draw regardless of when the
    # browser joined relative to when the bot started.
    sess = active_sessions.get(uid)
    if sess and sess.candles:
        # Snapshot with dict()/list() first: the bot's background thread
        # may be appending new candles concurrently, and iterating the
        # live dict/list directly here could race with that.
        for tf, candles in dict(sess.candles).items():
            emit("candles_init", {
                "tf": tf, "symbol": sess.symbol, "candles": list(candles)[-200:]})


@socketio.on("disconnect")
def on_disconnect():
    pass


# ── Entry point ──────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    _resume_bots_on_startup()
    print("")
    print("  ==========================================")
    print("   SyntheticBot Pro — Starting...")
    print("   Open browser → http://localhost:{}".format(FLASK_PORT))
    print("  ==========================================")
    print("")
    # allow_unsafe_werkzeug=True: newer Flask-SocketIO refuses to run its
    # built-in dev server unless explicitly confirmed. Since async_mode is
    # "threading" (not eventlet/gevent — see the comment above), Werkzeug's
    # dev server is what actually serves requests here even in production
    # on Render. This is a reasonable tradeoff for a personal-use dashboard
    # behind Render's HTTPS proxy; it is not meant for high-traffic public use.
    socketio.run(app, host="0.0.0.0", port=FLASK_PORT, debug=False,
                 allow_unsafe_werkzeug=True)