"""database.py — SQLite persistence: users, candles, signals, trades"""
import sqlite3, os
from typing import List, Dict
from config import DB_PATH, DEFAULT_STRATEGY, DEFAULT_TIMEFRAME
from logger import log

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
        "entries_hold":  "INTEGER DEFAULT 0",   # 1 = user pressed "Close All Trades" and chose to hold new entries
    }.items():
        if col not in bs_cols:
            con.execute("ALTER TABLE bot_state ADD COLUMN {} {}".format(col, coltype))
    us_cols = {r[1] for r in con.execute("PRAGMA table_info(user_settings)").fetchall()}
    if "timeframe" not in us_cols:
        con.execute("ALTER TABLE user_settings ADD COLUMN timeframe TEXT DEFAULT 'M1'")

    # One DB row per Deriv contract: duplicate events can never create a duplicate record.
    try:
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_contract "
                    "ON trades(contract_id) WHERE contract_id != ''")
    except sqlite3.DatabaseError as e:
        log.warning("Could not create unique contract index (old duplicate rows?): %s", e)
    con.commit()
    con.close()
    log.info("Database ready → %s", DB_PATH)


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
             close_on_stop, account_id, live_confirmed, timeframe, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
        ON CONFLICT(user_id) DO UPDATE SET
            symbol=excluded.symbol, mode=excluded.mode, lot_size=excluded.lot_size,
            max_positions=excluded.max_positions, strategy=excluded.strategy,
            loss_policy=excluded.loss_policy, close_on_stop=excluded.close_on_stop,
            account_id=excluded.account_id, timeframe=excluded.timeframe,
            updated_at=excluded.updated_at
    """, (user_id, cur["symbol"], cur["mode"], cur["lot_size"], cur["max_positions"],
          cur["strategy"], cur["loss_policy"], 1 if cur["close_on_stop"] else 0,
          cur["account_id"], 1 if cur.get("live_confirmed") else 0, cur["timeframe"]))
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
                               strategy, loss_policy, close_on_stop, account_id, timeframe, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?, datetime('now'))
        ON CONFLICT(user_id) DO UPDATE SET
            symbol=excluded.symbol, mode=excluded.mode,
            lot_size=excluded.lot_size, max_positions=excluded.max_positions,
            strategy=excluded.strategy, loss_policy=excluded.loss_policy,
            close_on_stop=excluded.close_on_stop, account_id=excluded.account_id,
            timeframe=excluded.timeframe, updated_at=excluded.updated_at
    """, (user_id, cfg["symbol"], cfg["mode"], cfg["lot_size"], cfg["max_positions"],
          cfg["strategy"], cfg["loss_policy"], 1 if cfg["close_on_stop"] else 0,
          cfg.get("account_id", ""), cfg.get("timeframe", "M1")))
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