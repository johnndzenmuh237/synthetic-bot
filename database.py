"""database.py — SQLite persistence: users, candles, signals, trades"""
import sqlite3, os
from typing import List, Dict
from config import DB_PATH, DEFAULT_STRATEGY, DEFAULT_TIMEFRAME, DURATION_CHOICES, DEFAULT_TRADE_TIME
from logger import log
import functools, json, threading, time
from cloud_store import store as cloud

_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)


def _conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    con = _conn()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at    TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS candles (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            tf     TEXT NOT NULL,
            epoch  INTEGER NOT NULL,
            open   REAL, high REAL, low REAL, close REAL,
            UNIQUE(symbol, tf, epoch)
        );
        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT, tf TEXT, direction TEXT,
            ema21 REAL, ema50 REAL, ema100 REAL, close_price REAL,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS bot_state (
            user_id       INTEGER PRIMARY KEY,
            symbol        TEXT,
            mode          TEXT,
            lot_size      REAL,
            max_positions INTEGER,
            updated_at    TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id        INTEGER PRIMARY KEY,
            symbol         TEXT,
            mode           TEXT,
            lot_size       REAL,
            max_positions  INTEGER,
            strategy       TEXT,
            loss_policy    TEXT,
            close_on_stop  INTEGER DEFAULT 0,
            account_id     TEXT DEFAULT '',
            timeframe      TEXT DEFAULT 'M1',
            trade_time     TEXT DEFAULT '',
            live_confirmed INTEGER DEFAULT 0,
            updated_at     TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS trades (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER,
            symbol       TEXT,
            direction    TEXT,
            lot_size     REAL DEFAULT 1.0,
            stake        REAL,
            entry_price  REAL,
            stop_loss    REAL,
            take_profit  REAL,
            exit_price   REAL,
            pnl          REAL,
            status       TEXT DEFAULT 'OPEN',
            contract_id  TEXT DEFAULT '',
            mode         TEXT DEFAULT 'demo',
            position_num INTEGER DEFAULT 1,
            opened_at    TEXT DEFAULT (datetime('now')),
            closed_at    TEXT
        );
        CREATE TABLE IF NOT EXISTS user_tokens (
            user_id    INTEGER PRIMARY KEY,
            demo_token TEXT DEFAULT '',
            live_token TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS activity (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            kind       TEXT,
            detail     TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS ix_activity_user ON activity(user_id, id);
        CREATE INDEX IF NOT EXISTS ix_trades_user_closed ON trades(user_id, closed_at);
    """)
    con.commit()

    # ── Migration for ClaudeFX v2.0 TP1/TP2 partial-close tracking ──
    # ALTER TABLE ADD COLUMN has no "IF NOT EXISTS" in older SQLite, so
    # check PRAGMA table_info first — safe to run against an existing
    # database file created by an earlier version of this bot.
    existing_cols = {r[1] for r in con.execute("PRAGMA table_info(trades)").fetchall()}
    migrations = {
        "trade_group":  "TEXT DEFAULT ''",   # links the TP1 (70%) + TP2 (30%) legs of one signal
        "target_price": "REAL DEFAULT 0",    # this leg's TP price
        "r_distance":   "REAL DEFAULT 0",    # 1R in price units, for the 3R hard cap
        "sl_moved_be":  "INTEGER DEFAULT 0", # 1 once SL has been moved to breakeven
    }
    migrations.update({
        "account_id":   "TEXT DEFAULT ''",   # the verified Deriv account the order was placed on
        "exit_reason":  "TEXT DEFAULT ''",   # why/how the trade closed (expiry, reversal, rejected...)
        "strategy":     "TEXT DEFAULT ''",
        "buy_price":    "REAL",              # confirmed purchase price reported by Deriv
        "sell_price":   "REAL",              # confirmed payout/sell price reported by Deriv
    })
    for col, coltype in migrations.items():
        if col not in existing_cols:
            con.execute("ALTER TABLE trades ADD COLUMN {} {}".format(col, coltype))

    # bot_state = the configuration CURRENTLY APPLIED to the running bot
    bs_cols = {r[1] for r in con.execute("PRAGMA table_info(bot_state)").fetchall()}
    for col, coltype in {
        "strategy":      "TEXT DEFAULT 'EMA_CROSS'",
        "loss_policy":   "TEXT DEFAULT 'hold'",
        "close_on_stop": "INTEGER DEFAULT 0",
        "account_id":    "TEXT DEFAULT ''",
        "timeframe":     "TEXT DEFAULT 'M1'",
        "trade_time":    "TEXT DEFAULT ''",
        "entries_hold":  "INTEGER DEFAULT 0",   # 1 = user pressed "Close All Trades" and chose to hold new entries
    }.items():
        if col not in bs_cols:
            con.execute("ALTER TABLE bot_state ADD COLUMN {} {}".format(col, coltype))
    u_cols = {r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()}
    for col, coltype in {"is_active": "INTEGER DEFAULT 1", "last_login": "TEXT"}.items():
        if col not in u_cols:
            con.execute("ALTER TABLE users ADD COLUMN {} {}".format(col, coltype))
    us_cols = {r[1] for r in con.execute("PRAGMA table_info(user_settings)").fetchall()}
    if "timeframe" not in us_cols:
        con.execute("ALTER TABLE user_settings ADD COLUMN timeframe TEXT DEFAULT 'M1'")
    if "trade_time" not in us_cols:
        con.execute("ALTER TABLE user_settings ADD COLUMN trade_time TEXT DEFAULT ''")

    # One DB row per Deriv contract: duplicate events can never create a duplicate record.
    try:
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_contract "
                    "ON trades(contract_id) WHERE contract_id != ''")
    except sqlite3.DatabaseError as e:
        log.warning("Could not create unique contract index (old duplicate rows?): %s", e)
    con.commit()
    con.close()
    log.info("Database ready → %s", DB_PATH)
    _cloud_boot()


# ── Users ────────────────────────────────────────────────────
def create_user(username, password_hash):
    con = _conn()
    try:
        con.execute("INSERT INTO users (username, password_hash) VALUES (?,?)",
                    (username, password_hash))
        con.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        con.close()


def get_user(username):
    con = _conn()
    row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    con.close()
    return dict(row) if row else None


def get_user_by_id(user_id):
    con = _conn()
    row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    con.close()
    return dict(row) if row else None


# ── Candles ──────────────────────────────────────────────────
def save_candle(symbol, tf, epoch, o, h, l, c):
    con = _conn()
    try:
        con.execute(
            "INSERT OR IGNORE INTO candles (symbol,tf,epoch,open,high,low,close) VALUES (?,?,?,?,?,?,?)",
            (symbol, tf, epoch, o, h, l, c))
        con.commit()
    finally:
        con.close()


def save_signal(symbol, tf, direction, ema21, ema50, ema100, close):
    con = _conn()
    con.execute(
        "INSERT INTO signals (symbol,tf,direction,ema21,ema50,ema100,close_price) VALUES (?,?,?,?,?,?,?)",
        (symbol, tf, direction, ema21, ema50, ema100, close))
    con.commit()
    con.close()


# ── User settings (what the user SAVED) ──────────────────────
DEFAULT_SETTINGS = {
    "symbol":        "R_50",
    "mode":          "demo",
    "lot_size":      1.0,
    "max_positions": 1,
    "strategy":      DEFAULT_STRATEGY,
    "loss_policy":   "hold",
    "close_on_stop": False,
    "account_id":    "",
    "timeframe":     DEFAULT_TIMEFRAME,
    "trade_time":    DEFAULT_TRADE_TIME,
}
_SETTING_KEYS = tuple(DEFAULT_SETTINGS.keys())


def _row_to_settings(row):
    d = dict(DEFAULT_SETTINGS)
    if row:
        r = dict(row)
        for k in _SETTING_KEYS:
            if r.get(k) is not None:
                d[k] = r[k]
        d["close_on_stop"] = bool(d["close_on_stop"])
        d["lot_size"] = float(d["lot_size"])
        d["max_positions"] = int(d["max_positions"])
        d["account_id"] = d["account_id"] or ""
        d["timeframe"] = d["timeframe"] if d["timeframe"] in ("M1", "M5") else "M1"
        d["trade_time"] = d["trade_time"] if d["trade_time"] in DURATION_CHOICES else DEFAULT_TRADE_TIME
    return d


def get_settings(user_id):
    """Saved settings for a user (defaults for anything never saved)."""
    con = _conn()
    row = con.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
    con.close()
    s = _row_to_settings(row)
    s["live_confirmed"] = bool(dict(row).get("live_confirmed")) if row else False
    return s


def save_settings(user_id, settings):
    """Persist the user's saved settings (only known keys are stored)."""
    cur = get_settings(user_id)
    cur.update({k: v for k, v in settings.items() if k in _SETTING_KEYS})
    con = _conn()
    con.execute("""
        INSERT INTO user_settings
            (user_id, symbol, mode, lot_size, max_positions, strategy, loss_policy,
             close_on_stop, account_id, live_confirmed, timeframe, trade_time, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
        ON CONFLICT(user_id) DO UPDATE SET
            symbol=excluded.symbol, mode=excluded.mode, lot_size=excluded.lot_size,
            max_positions=excluded.max_positions, strategy=excluded.strategy,
            loss_policy=excluded.loss_policy, close_on_stop=excluded.close_on_stop,
            account_id=excluded.account_id, timeframe=excluded.timeframe, trade_time=excluded.trade_time,
            updated_at=excluded.updated_at
    """, (user_id, cur["symbol"], cur["mode"], cur["lot_size"], cur["max_positions"],
          cur["strategy"], cur["loss_policy"], 1 if cur["close_on_stop"] else 0,
          cur["account_id"], 1 if cur.get("live_confirmed") else 0, cur["timeframe"], cur["trade_time"]))
    con.commit()
    con.close()
    return get_settings(user_id)


def set_live_confirmed(user_id):
    """Remember that this user explicitly confirmed live trading."""
    if not get_user_by_id(user_id):
        return
    save_settings(user_id, {})        # make sure the row exists
    con = _conn()
    con.execute("UPDATE user_settings SET live_confirmed=1 WHERE user_id=?", (user_id,))
    con.commit()
    con.close()


# ── Bot running-state persistence (the APPLIED config; survives restarts) ──
def save_bot_state(user_id, cfg):
    """Records that this user's bot should be running, with the configuration
    that was applied to it. Read back on server startup so the bot resumes."""
    con = _conn()
    con.execute("""
        INSERT INTO bot_state (user_id, symbol, mode, lot_size, max_positions,
                               strategy, loss_policy, close_on_stop, account_id, timeframe, trade_time, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
        ON CONFLICT(user_id) DO UPDATE SET
            symbol=excluded.symbol, mode=excluded.mode,
            lot_size=excluded.lot_size, max_positions=excluded.max_positions,
            strategy=excluded.strategy, loss_policy=excluded.loss_policy,
            close_on_stop=excluded.close_on_stop, account_id=excluded.account_id,
            timeframe=excluded.timeframe, trade_time=excluded.trade_time, updated_at=excluded.updated_at
    """, (user_id, cfg["symbol"], cfg["mode"], cfg["lot_size"], cfg["max_positions"],
          cfg["strategy"], cfg["loss_policy"], 1 if cfg["close_on_stop"] else 0,
          cfg.get("account_id", ""), cfg.get("timeframe", "M1"), cfg.get("trade_time") or DEFAULT_TRADE_TIME))
    con.commit()
    con.close()


def get_bot_state(user_id):
    con = _conn()
    row = con.execute("SELECT * FROM bot_state WHERE user_id=?", (user_id,)).fetchone()
    con.close()
    if not row:
        return None
    d = _row_to_settings(row)
    d["entries_hold"] = bool(dict(row).get("entries_hold"))
    return d


def set_entries_hold(user_id, hold):
    """Remember (across restarts) that new entries are on hold because the user closed all trades."""
    con = _conn()
    con.execute("UPDATE bot_state SET entries_hold=? WHERE user_id=?", (1 if hold else 0, user_id))
    con.commit()
    con.close()


def clear_bot_state(user_id):
    con = _conn()
    con.execute("DELETE FROM bot_state WHERE user_id=?", (user_id,))
    con.commit()
    con.close()


def get_all_bot_states():
    """All users whose bot should currently be running."""
    con = _conn()
    rows = con.execute("SELECT * FROM bot_state").fetchall()
    con.close()
    out = []
    for r in rows:
        d = _row_to_settings(r)
        d["user_id"] = dict(r)["user_id"]
        d["entries_hold"] = bool(dict(r).get("entries_hold"))
        out.append(d)
    return out


# ── Trades ───────────────────────────────────────────────────
# Status lifecycle:
#   PENDING      order about to be / being submitted (counts as a position)
#   UNCONFIRMED  order submitted but Deriv's answer was lost (counts as a position,
#                blocks new orders until reconciled)
#   OPEN         confirmed open contract on Deriv
#   WIN/LOSS/EVEN  closed with a confirmed result from Deriv
#   REJECTED     Deriv refused the order (no money moved)
#   UNRECONCILED could not be matched with Deriv - verify in the back office
ACTIVE_STATUSES      = ("PENDING", "UNCONFIRMED", "OPEN")
RECONCILABLE_STATUSES = ("PENDING", "UNCONFIRMED", "OPEN", "UNRECONCILED")


def _in(statuses):
    return "(" + ",".join("'{}'".format(x) for x in statuses) + ")"


def open_trade(user_id, symbol, direction, lot_size, stake,
               entry, sl, tp, contract_id="", mode="demo", position_num=1,
               trade_group="", target_price=0, r_distance=0,
               status="OPEN", account_id="", strategy=""):
    con = _conn()
    cur = con.cursor()
    cur.execute(
        """INSERT INTO trades
           (user_id,symbol,direction,lot_size,stake,entry_price,
            stop_loss,take_profit,contract_id,mode,position_num,
            trade_group,target_price,r_distance,status,account_id,strategy)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (user_id, symbol, direction, lot_size, stake,
         entry, sl, tp, contract_id or "", mode, position_num,
         trade_group, target_price, r_distance, status, account_id, strategy))
    tid = cur.lastrowid
    con.commit()
    con.close()
    return tid


def update_trade(trade_id, **fields):
    """Update selected columns of a trade row (whitelisted)."""
    allowed = {"status", "contract_id", "entry_price", "buy_price", "stake",
               "exit_reason", "stop_loss", "take_profit"}
    cols = [k for k in fields if k in allowed]
    if not cols:
        return False
    con = _conn()
    try:
        cur = con.execute(
            "UPDATE trades SET {} WHERE id=?".format(",".join(c + "=?" for c in cols)),
            [fields[c] for c in cols] + [trade_id])
        con.commit()
        return cur.rowcount > 0
    except sqlite3.IntegrityError:
        return False          # contract already recorded on another row
    finally:
        con.close()


def update_trade_sl(trade_id, new_sl, mark_breakeven=False):
    con = _conn()
    con.execute(
        "UPDATE trades SET stop_loss=?, sl_moved_be=? WHERE id=?",
        (new_sl, 1 if mark_breakeven else 0, trade_id))
    con.commit()
    con.close()


def get_trade_group(user_id, trade_group):
    con = _conn()
    rows = con.execute(
        "SELECT * FROM trades WHERE user_id=? AND trade_group=?",
        (user_id, trade_group)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def close_trade(trade_id, exit_price, pnl, status, exit_reason="", sell_price=None):
    """Record the CONFIRMED result of a trade. Idempotent: only a trade that is
    still unresolved can be closed, so a duplicate event or repeated API response
    can never credit/deduct the same result twice. Returns True only the first time."""
    con = _conn()
    try:
        cur = con.execute(
            "UPDATE trades SET exit_price=?,pnl=?,status=?,exit_reason=?,sell_price=?,"
            "closed_at=datetime('now') WHERE id=? AND status IN " + _in(RECONCILABLE_STATUSES),
            (exit_price, pnl, status, exit_reason, sell_price, trade_id))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def mark_trade(trade_id, status, exit_reason=""):
    """Move an unresolved trade to a non-result status (REJECTED / UNRECONCILED / UNCONFIRMED...)."""
    con = _conn()
    try:
        cur = con.execute(
            "UPDATE trades SET status=?, exit_reason=? WHERE id=? AND status IN " + _in(RECONCILABLE_STATUSES),
            (status, exit_reason, trade_id))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def _trade_filter(user_id, mode, statuses):
    q = "SELECT * FROM trades WHERE status IN " + _in(statuses)
    params = []
    if user_id:
        q += " AND user_id=?"
        params.append(user_id)
    if mode:
        q += " AND mode=?"
        params.append(mode)
    return q, params


def get_open_trades(user_id=None, mode=None):
    """Confirmed OPEN contracts only."""
    q, params = _trade_filter(user_id, mode, ("OPEN",))
    con = _conn()
    rows = con.execute(q, params).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_active_trades(user_id=None, mode=None):
    """Everything that occupies a position slot (PENDING + UNCONFIRMED + OPEN)."""
    q, params = _trade_filter(user_id, mode, ACTIVE_STATUSES)
    con = _conn()
    rows = con.execute(q, params).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_reconcilable_trades(user_id=None, mode=None):
    q, params = _trade_filter(user_id, mode, RECONCILABLE_STATUSES)
    con = _conn()
    rows = con.execute(q, params).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_unresolved_orders(user_id=None, mode=None):
    """Orders whose submission outcome is not yet known."""
    q, params = _trade_filter(user_id, mode, ("PENDING", "UNCONFIRMED"))
    con = _conn()
    rows = con.execute(q, params).fetchall()
    con.close()
    return [dict(r) for r in rows]


def contract_exists(contract_id):
    if not contract_id:
        return False
    con = _conn()
    row = con.execute("SELECT 1 FROM trades WHERE contract_id=?", (str(contract_id),)).fetchone()
    con.close()
    return bool(row)


def get_recent_trades(user_id, limit=20, mode=None):
    con = _conn()
    q = "SELECT * FROM trades WHERE user_id=?"
    params = [user_id]
    if mode:
        q += " AND mode=?"
        params.append(mode)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = con.execute(q, params).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_daily_pnl(user_id=None, mode=None):
    """Realized P&L closed today (UTC). Only confirmed WIN/LOSS/EVEN results count."""
    con = _conn()
    q = ("SELECT COALESCE(SUM(pnl),0) total FROM trades "
         "WHERE status IN ('WIN','LOSS','EVEN') AND date(closed_at)=date('now')")
    params = []
    if user_id:
        q += " AND user_id=?"
        params.append(user_id)
    if mode:
        q += " AND mode=?"
        params.append(mode)
    row = con.execute(q, params).fetchone()
    con.close()
    return float(row["total"])


def get_stats(user_id, mode=None):
    con = _conn()
    q = """
        SELECT COUNT(*) total,
               COALESCE(SUM(CASE WHEN status='WIN'  THEN 1 ELSE 0 END),0) wins,
               COALESCE(SUM(CASE WHEN status='LOSS' THEN 1 ELSE 0 END),0) losses,
               COALESCE(SUM(pnl),0) total_pnl
        FROM trades WHERE user_id=? AND status IN ('WIN','LOSS','EVEN')
    """
    params = [user_id]
    if mode:
        q += " AND mode=?"
        params.append(mode)
    row = con.execute(q, params).fetchone()
    con.close()
    return dict(row)


def get_exposure(user_id, mode):
    """Open positions count and total stake currently at risk."""
    con = _conn()
    row = con.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(stake),0) s FROM trades "
        "WHERE user_id=? AND mode=? AND status IN " + _in(ACTIVE_STATUSES),
        (user_id, mode)).fetchone()
    con.close()
    return {"open_positions": int(row["n"]), "open_stake": float(row["s"])}


def get_consecutive_losses(user_id, mode=None):
    """How many LOSS trades in a row, most-recent first, closed today —
    resets naturally once a WIN trade appears or the day rolls over."""
    con = _conn()
    q = """
        SELECT status FROM trades
        WHERE user_id=? AND status IN ('WIN','LOSS','EVEN') AND date(closed_at)=date('now')
    """
    params = [user_id]
    if mode:
        q += " AND mode=?"
        params.append(mode)
    q += " ORDER BY closed_at DESC, id DESC"
    rows = con.execute(q, params).fetchall()
    con.close()
    count = 0
    for r in rows:
        if r["status"] == "LOSS":
            count += 1
        else:
            break
    return count


# ═════════════════════════════════════════════════════════════
#  Permanent history: activity log, full-history queries
#  (Nothing deletes history except clear_history(), which the user must confirm.)
# ═════════════════════════════════════════════════════════════
def log_activity(user_id, kind, detail=""):
    """Append one line to the user's permanent activity log. Never raises."""
    try:
        con = _conn()
        cur = con.execute("INSERT INTO activity (user_id, kind, detail) VALUES (?,?,?)",
                          (user_id, kind, str(detail)[:500]))
        con.commit()
        aid = cur.lastrowid
        con.close()
        return aid
    except Exception as e:           # logging an activity must never break a request
        log.warning("activity log failed: %s", e)
        return None


def clear_history(user_id, mode=None, include_activity=True):
    """The user's own "Clear history". Deletes FINISHED trades only (wins, losses, rejected, void...) - a trade that is
    still open or being confirmed is never touched - and, when include_activity, the account-activity log.
    mode: None = Demo + Live, or "demo" / "live". Returns what was removed (the cloud copy is removed too)."""
    con = _conn()
    try:
        q = "FROM trades WHERE user_id=? AND status NOT IN " + _in(RECONCILABLE_STATUSES)
        params = [user_id]
        if mode in ("demo", "live"):
            q += " AND mode=?"
            params.append(mode)
        ids = [r["id"] for r in con.execute("SELECT id " + q, params).fetchall()]
        remaining = con.execute("SELECT COUNT(*) n FROM trades WHERE user_id=?", (user_id,)).fetchone()["n"] - len(ids)
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            con.execute("DELETE FROM trades WHERE id IN (%s)" % ",".join("?" * len(chunk)), chunk)
        acts = 0
        if include_activity:
            acts = con.execute("SELECT COUNT(*) n FROM activity WHERE user_id=?", (user_id,)).fetchone()["n"]
            con.execute("DELETE FROM activity WHERE user_id=?", (user_id,))
        con.commit()
    finally:
        con.close()
    return {"trades": len(ids), "activity": acts, "kept_open": remaining, "trade_ids": ids,
            "scope": mode or "all", "activity_cleared": bool(include_activity)}


def get_activity(user_id, limit=100, offset=0):
    con = _conn()
    rows = con.execute("SELECT * FROM activity WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                       (user_id, int(limit), int(offset))).fetchall()
    total = con.execute("SELECT COUNT(*) n FROM activity WHERE user_id=?", (user_id,)).fetchone()["n"]
    con.close()
    return [dict(r) for r in rows], int(total)


_HIST_STATUSES = ("WIN", "LOSS", "EVEN", "OPEN", "PENDING", "UNCONFIRMED", "REJECTED", "UNRECONCILED", "VOID")


def _history_where(user_id, mode=None, status=None, symbol=None, date_from=None, date_to=None):
    """WHERE clause for the history views. Dates are YYYY-MM-DD (UTC), inclusive; a trade
    is dated by when it closed, or when it opened if it has not closed yet."""
    w, p = ["user_id=?"], [user_id]
    if mode in ("demo", "live"):
        w.append("mode=?"); p.append(mode)
    if status == "CLOSED":
        w.append("status IN ('WIN','LOSS','EVEN')")
    elif status in _HIST_STATUSES:
        w.append("status=?"); p.append(status)
    if symbol:
        w.append("symbol=?"); p.append(symbol)
    if date_from:
        w.append("date(COALESCE(closed_at, opened_at)) >= date(?)"); p.append(date_from)
    if date_to:
        w.append("date(COALESCE(closed_at, opened_at)) <= date(?)"); p.append(date_to)
    return " AND ".join(w), p


def get_history(user_id, mode=None, status=None, symbol=None, date_from=None, date_to=None,
                limit=50, offset=0):
    """(rows, total_count, totals) for the full history, newest first."""
    where, p = _history_where(user_id, mode, status, symbol, date_from, date_to)
    con = _conn()
    rows = con.execute("SELECT * FROM trades WHERE " + where +
                       " ORDER BY COALESCE(closed_at, opened_at) DESC, id DESC LIMIT ? OFFSET ?",
                       p + [int(limit), int(offset)]).fetchall()
    t = con.execute(
        "SELECT COUNT(*) n, "
        "COALESCE(SUM(CASE WHEN status IN ('WIN','LOSS','EVEN') THEN pnl END),0) pnl, "
        "COALESCE(SUM(CASE WHEN status='WIN' THEN 1 ELSE 0 END),0) wins, "
        "COALESCE(SUM(CASE WHEN status='LOSS' THEN 1 ELSE 0 END),0) losses, "
        "COALESCE(SUM(CASE WHEN status IN ('WIN','LOSS','EVEN') THEN 1 ELSE 0 END),0) closed "
        "FROM trades WHERE " + where, p).fetchone()
    con.close()
    totals = {"count": int(t["n"]), "pnl": round(float(t["pnl"]), 2), "wins": int(t["wins"]),
              "losses": int(t["losses"]), "closed": int(t["closed"])}
    return [dict(r) for r in rows], totals["count"], totals


def get_history_summary(user_id, period="day", mode=None, symbol=None, date_from=None, date_to=None):
    """Profit/loss per day, month or year over the whole history (closed trades only)."""
    fmt = {"day": "%Y-%m-%d", "month": "%Y-%m", "year": "%Y"}.get(period, "%Y-%m-%d")
    where, p = _history_where(user_id, mode, "CLOSED", symbol, date_from, date_to)
    con = _conn()
    rows = con.execute(
        "SELECT strftime('{f}', closed_at) period, COUNT(*) trades, "
        "SUM(CASE WHEN status='WIN' THEN 1 ELSE 0 END) wins, "
        "SUM(CASE WHEN status='LOSS' THEN 1 ELSE 0 END) losses, "
        "COALESCE(SUM(pnl),0) pnl, COALESCE(SUM(stake),0) staked "
        "FROM trades WHERE {w} AND closed_at IS NOT NULL GROUP BY period ORDER BY period DESC".format(f=fmt, w=where),
        p).fetchall()
    con.close()
    return [{"period": r["period"], "trades": int(r["trades"]), "wins": int(r["wins"]),
             "losses": int(r["losses"]), "pnl": round(float(r["pnl"]), 2),
             "staked": round(float(r["staked"]), 2)} for r in rows]


def get_history_symbols(user_id):
    con = _conn()
    rows = con.execute("SELECT DISTINCT symbol FROM trades WHERE user_id=? ORDER BY symbol", (user_id,)).fetchall()
    con.close()
    return [r["symbol"] for r in rows if r["symbol"]]


# ═════════════════════════════════════════════════════════════
#  Firebase mirror (see cloud_store.py). Local SQLite stays the working database;
#  every change below is copied to Firebase, and a wiped local file is rebuilt from it.
# ═════════════════════════════════════════════════════════════
_TABLE_OF = {"user": "users", "settings": "user_settings", "bot_state": "bot_state",
             "trade": "trades", "activity": "activity", "tokens": "user_tokens"}
_PK_OF = {"users": "id", "user_settings": "user_id", "bot_state": "user_id", "trades": "id",
          "activity": "id", "user_tokens": "user_id"}
_KINDS = ("user", "settings", "bot_state", "tokens", "trade", "activity")


def _clean(d):
    return {k: v for k, v in dict(d).items() if v is not None}


def _cloud_path(kind, row):
    if kind == "user":
        return "users/u{}".format(row["id"])
    if kind == "settings":
        return "settings/u{}".format(row["user_id"])
    if kind == "bot_state":
        return "bot_state/u{}".format(row["user_id"])
    if kind == "tokens":
        return "tokens/u{}".format(row["user_id"])
    if kind == "trade":
        return "trades/u{}/t{}".format(row["user_id"], row["id"])
    return "activity/u{}/a{}".format(row["user_id"], row["id"])


def _cloud_read(kind, key):
    """Current local copy of one row -> (firebase path, value). value None = delete it there."""
    if kind == "node":                                # remove this whole Firebase path (used by Clear history)
        return key, None
    table = _TABLE_OF[kind]
    con = _conn()
    row = con.execute("SELECT * FROM {} WHERE {}=?".format(table, _PK_OF[table]), (key,)).fetchone()
    con.close()
    if row is None:
        if kind in ("bot_state", "tokens"):           # a stopped bot / removed tokens: remove the record
            return "{}/u{}".format(kind, key), None
        return None, None
    d = dict(row)
    return _cloud_path(kind, d), _clean(d)


cloud.set_reader(_cloud_read)


def cloud_ready():
    """False only while Firebase is configured but the account restore has not finished."""
    return cloud.ready()


def _cloud_snapshot():
    """Everything in Firebase as {kind: {key: row}} (keys are the local primary keys)."""
    snap = {k: {} for k in _KINDS}
    for kind, node in (("user", "users"), ("settings", "settings"), ("bot_state", "bot_state"), ("tokens", "tokens")):
        for _, row in (cloud.get(node) or {}).items():
            if isinstance(row, dict):
                key = row.get("id") if kind == "user" else row.get("user_id")
                if key is not None:
                    snap[kind][int(key)] = row
    for kind, node in (("trade", "trades"), ("activity", "activity")):
        for uk in (cloud.get(node, "shallow=true") or {}):
            for _, row in (cloud.get("{}/{}".format(node, uk)) or {}).items():
                if isinstance(row, dict) and row.get("id") is not None:
                    snap[kind][int(row["id"])] = row
    return snap


def _cloud_restore_once():
    """Rebuild missing local rows from Firebase, then push any local row Firebase lacks or has stale."""
    snap = _cloud_snapshot()
    con = _conn()
    restored = 0
    for kind in _KINDS:
        table = _TABLE_OF[kind]
        cols = {r[1] for r in con.execute("PRAGMA table_info({})".format(table)).fetchall()}
        for key, row in snap[kind].items():
            keys = [k for k in row if k in cols]
            cur = con.execute("INSERT OR IGNORE INTO {} ({}) VALUES ({})".format(
                table, ",".join(keys), ",".join("?" * len(keys))), [row[k] for k in keys])
            restored += max(cur.rowcount, 0)
    con.commit()
    # Local rows the cloud does not have (or has an older version of) go up now.
    queued = 0
    for kind in _KINDS:
        table = _TABLE_OF[kind]
        for r in con.execute("SELECT * FROM {}".format(table)).fetchall():
            d = dict(r)
            key = d[_PK_OF[table]]
            if _clean(d) != _clean(snap[kind].get(key, {})):
                cloud.push(kind, key)
                queued += 1
    con.close()
    cloud.restored = True
    log.info("Firebase restore done: %d rows restored from the cloud, %d local rows queued to upload.",
             restored, queued)


def _cloud_boot():
    if not cloud.enabled:
        if cloud.error:
            log.warning("Cloud sync OFF: %s", cloud.error)
        else:
            log.warning("Cloud sync is OFF (no FIREBASE_SERVICE_ACCOUNT set): accounts and history "
                        "live only in the local database file.")
        return
    # One quick attempt while starting; if it fails the server still starts at once (Render must see
    # the web port open quickly) and the restore keeps retrying in the background.
    try:
        _cloud_restore_once()
        return
    except Exception as e:
        cloud.error = str(e)
        log.warning("Firebase restore attempt failed: %s", e)

    def retry():
        while not cloud.restored:
            time.sleep(15)
            try:
                _cloud_restore_once()
                cloud.error = ""
            except Exception as e:
                cloud.error = str(e)
                log.warning("Firebase restore still failing: %s", e)
    log.error("Firebase is unreachable — starting with the local database; new sign-ups are paused "
              "until the cloud copy has been restored (retrying every 15 s).")
    threading.Thread(target=retry, name="cloud-restore", daemon=True).start()


def _mirror(fn, items):
    """Wrap a write function so each call also queues the touched rows for Firebase."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        result = fn(*a, **k)
        try:
            for item in items(a, k, result):
                if item and item[1] is not None:
                    cloud.push(*item)
        except Exception as e:
            log.warning("cloud queue failed: %s", e)
        return result
    return wrapper


def _first(a, k, name):
    return a[0] if a else k.get(name)


create_user         = _mirror(create_user, lambda a, k, r: [("user", (get_user(_first(a, k, "username")) or {}).get("id"))] if r else [])
save_settings       = _mirror(save_settings, lambda a, k, r: [("settings", _first(a, k, "user_id"))])
set_live_confirmed  = _mirror(set_live_confirmed, lambda a, k, r: [("settings", _first(a, k, "user_id"))])
save_bot_state      = _mirror(save_bot_state, lambda a, k, r: [("bot_state", _first(a, k, "user_id"))])
set_entries_hold    = _mirror(set_entries_hold, lambda a, k, r: [("bot_state", _first(a, k, "user_id"))])
clear_bot_state     = _mirror(clear_bot_state, lambda a, k, r: [("bot_state", _first(a, k, "user_id"))])
open_trade          = _mirror(open_trade, lambda a, k, r: [("trade", r)])
update_trade        = _mirror(update_trade, lambda a, k, r: [("trade", _first(a, k, "trade_id"))])
update_trade_sl     = _mirror(update_trade_sl, lambda a, k, r: [("trade", _first(a, k, "trade_id"))])
close_trade         = _mirror(close_trade, lambda a, k, r: [("trade", _first(a, k, "trade_id"))])
mark_trade          = _mirror(mark_trade, lambda a, k, r: [("trade", _first(a, k, "trade_id"))])
log_activity        = _mirror(log_activity, lambda a, k, r: [("activity", r)])


def _clear_cloud_items(a, k, r):
    uid = _first(a, k, "user_id")
    items = []
    if not r["trade_ids"] and not r["activity"]:
        return items
    if r["scope"] == "all":                           # one delete for the whole node, then put back any open trades
        items.append(("node", "trades/u{}".format(uid)))
        con = _conn()
        keep = [x["id"] for x in con.execute("SELECT id FROM trades WHERE user_id=?", (uid,)).fetchall()]
        con.close()
        items += [("trade", i) for i in keep]
    else:
        items += [("node", "trades/u{}/t{}".format(uid, i)) for i in r["trade_ids"]]
    if r["activity_cleared"]:
        items.append(("node", "activity/u{}".format(uid)))
    return items


clear_history       = _mirror(clear_history, _clear_cloud_items)


# ═════════════════════════════════════════════════════════════
#  Multi-user: per-user Deriv tokens (stored encrypted), account on/off, admin overview
# ═════════════════════════════════════════════════════════════
def get_user_token_blobs(user_id):
    """{'demo': <encrypted or ''>, 'live': <encrypted or ''>} — still encrypted."""
    con = _conn()
    row = con.execute("SELECT demo_token, live_token FROM user_tokens WHERE user_id=?", (user_id,)).fetchone()
    con.close()
    return {"demo": (row["demo_token"] if row else "") or "", "live": (row["live_token"] if row else "") or ""}


def set_user_token(user_id, mode, encrypted):
    col = "live_token" if mode == "live" else "demo_token"
    con = _conn()
    con.execute("INSERT OR IGNORE INTO user_tokens (user_id) VALUES (?)", (user_id,))
    con.execute("UPDATE user_tokens SET {}=?, updated_at=datetime('now') WHERE user_id=?".format(col),
                (encrypted or "", user_id))
    con.commit()
    con.close()


def set_user_active(user_id, active):
    con = _conn()
    con.execute("UPDATE users SET is_active=? WHERE id=?", (1 if active else 0, user_id))
    con.commit()
    con.close()


def touch_login(user_id):
    con = _conn()
    con.execute("UPDATE users SET last_login=datetime('now') WHERE id=?", (user_id,))
    con.commit()
    con.close()


def user_is_active(user):
    return bool(user) and (user.get("is_active") is None or int(user.get("is_active")) == 1)


def get_first_user_id():
    con = _conn()
    row = con.execute("SELECT MIN(id) m FROM users").fetchone()
    con.close()
    return row["m"] if row and row["m"] is not None else None


def list_users_admin():
    """Every account with its tokens-connected flags and trading totals (no secrets)."""
    con = _conn()
    users = con.execute("SELECT id, username, created_at, last_login, is_active FROM users ORDER BY id").fetchall()
    tok = {r["user_id"]: r for r in con.execute("SELECT user_id, demo_token, live_token FROM user_tokens").fetchall()}
    stats = {}
    for r in con.execute(
            "SELECT user_id, mode, COUNT(*) n, COALESCE(SUM(CASE WHEN status IN ('WIN','LOSS','EVEN') THEN pnl END),0) pnl, "
            "SUM(CASE WHEN status IN ('PENDING','UNCONFIRMED','OPEN') THEN 1 ELSE 0 END) open_n "
            "FROM trades GROUP BY user_id, mode").fetchall():
        stats.setdefault(r["user_id"], {})[r["mode"]] = r
    last = {r["user_id"]: r["m"] for r in con.execute(
        "SELECT user_id, MAX(created_at) m FROM activity GROUP BY user_id").fetchall()}
    con.close()
    out = []
    for u in users:
        t = tok.get(u["id"])
        st = stats.get(u["id"], {})
        def pick(mode, key):
            return (st.get(mode)[key] if st.get(mode) is not None else 0) or 0
        out.append({
            "id": u["id"], "username": u["username"], "created_at": u["created_at"],
            "last_login": u["last_login"], "is_active": u["is_active"] is None or int(u["is_active"]) == 1,
            "has_demo_token": bool(t and t["demo_token"]), "has_live_token": bool(t and t["live_token"]),
            "trades": int(pick("demo", "n")) + int(pick("live", "n")),
            "pnl_demo": round(float(pick("demo", "pnl")), 2), "pnl_live": round(float(pick("live", "pnl")), 2),
            "open_trades": int(pick("demo", "open_n")) + int(pick("live", "open_n")),
            "last_activity": last.get(u["id"]),
        })
    return out


set_user_token      = _mirror(set_user_token, lambda a, k, r: [("tokens", _first(a, k, "user_id"))])
set_user_active     = _mirror(set_user_active, lambda a, k, r: [("user", _first(a, k, "user_id"))])
touch_login         = _mirror(touch_login, lambda a, k, r: [("user", _first(a, k, "user_id"))])