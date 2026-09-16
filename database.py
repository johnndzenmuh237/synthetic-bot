"""database.py — SQLite persistence: users, candles, signals, trades"""
import sqlite3, os
from typing import List, Dict
from config import DB_PATH
from logger import log

os.makedirs("data", exist_ok=True)


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
        "buy_price":    "REAL DEFAULT 0",    # Deriv's real buy_price — exact amount paid to open
    }
    for col, coltype in migrations.items():
        if col not in existing_cols:
            con.execute("ALTER TABLE trades ADD COLUMN {} {}".format(col, coltype))
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
    """A transient 'database is locked' error here previously propagated
    all the way up and crashed the entire history-load / bot session.
    Retry briefly, then give up on just this one row rather than taking
    the whole bot down over one write."""
    import time as _time
    for attempt in range(3):
        con = _conn()
        try:
            con.execute(
                "INSERT OR IGNORE INTO candles (symbol,tf,epoch,open,high,low,close) "
                "VALUES (?,?,?,?,?,?,?)",
                (symbol, tf, epoch, o, h, l, c))
            con.commit()
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 2:
                _time.sleep(0.05 * (attempt + 1))
                continue
            log.error("save_candle failed for %s %s epoch=%s: %s", symbol, tf, epoch, e)
            return
        finally:
            con.close()


def save_signal(symbol, tf, direction, ema21, ema50, ema100, close):
    con = _conn()
    con.execute(
        "INSERT INTO signals (symbol,tf,direction,ema21,ema50,ema100,close_price) VALUES (?,?,?,?,?,?,?)",
        (symbol, tf, direction, ema21, ema50, ema100, close))
    con.commit()
    con.close()


# ── Bot running-state persistence (survives server restarts) ──
def save_bot_state(user_id, symbol, mode, lot_size, max_positions):
    """Records that this user's bot should be running. Read back on
    server startup so the bot resumes automatically after any restart
    (deploy, crash, Render waking back up) instead of staying off until
    someone manually clicks Start again."""
    con = _conn()
    con.execute("""
        INSERT INTO bot_state (user_id, symbol, mode, lot_size, max_positions, updated_at)
        VALUES (?,?,?,?,?, datetime('now'))
        ON CONFLICT(user_id) DO UPDATE SET
            symbol=excluded.symbol, mode=excluded.mode,
            lot_size=excluded.lot_size, max_positions=excluded.max_positions,
            updated_at=excluded.updated_at
    """, (user_id, symbol, mode, lot_size, max_positions))
    con.commit()
    con.close()


def clear_bot_state(user_id):
    con = _conn()
    con.execute("DELETE FROM bot_state WHERE user_id=?", (user_id,))
    con.commit()
    con.close()


def get_all_bot_states():
    """All users whose bot should currently be running — used on server
    startup to auto-resume them."""
    con = _conn()
    rows = con.execute("SELECT * FROM bot_state").fetchall()
    con.close()
    return [dict(r) for r in rows]


# ── Trades ───────────────────────────────────────────────────
def open_trade(user_id, symbol, direction, lot_size, stake,
               entry, sl, tp, contract_id="", mode="demo", position_num=1,
               trade_group="", target_price=0, r_distance=0, buy_price=0):
    con = _conn()
    cur = con.cursor()
    cur.execute(
        """INSERT INTO trades
           (user_id,symbol,direction,lot_size,stake,entry_price,
            stop_loss,take_profit,contract_id,mode,position_num,
            trade_group,target_price,r_distance,buy_price)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (user_id, symbol, direction, lot_size, stake,
         entry, sl, tp, contract_id, mode, position_num,
         trade_group, target_price, r_distance, buy_price))
    tid = cur.lastrowid
    con.commit()
    con.close()
    return tid


def update_trade_sl(trade_id, new_sl, mark_breakeven=False):
    """Used to move a trade's stop-loss to breakeven after TP1 fills."""
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


def close_trade(trade_id, exit_price, pnl, status):
    con = _conn()
    con.execute(
        "UPDATE trades SET exit_price=?,pnl=?,status=?,closed_at=datetime('now') WHERE id=?",
        (exit_price, pnl, status, trade_id))
    con.commit()
    con.close()


def get_open_trades(user_id=None, mode=None):
    con = _conn()
    q = "SELECT * FROM trades WHERE status='OPEN'"
    params = []
    if user_id:
        q += " AND user_id=?"
        params.append(user_id)
    if mode:
        q += " AND mode=?"
        params.append(mode)
    rows = con.execute(q, params).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_recent_trades(user_id, limit=20):
    con = _conn()
    rows = con.execute(
        "SELECT * FROM trades WHERE user_id=? ORDER BY opened_at DESC LIMIT ?",
        (user_id, limit)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_daily_pnl(user_id=None, mode=None):
    con = _conn()
    q = "SELECT COALESCE(SUM(pnl),0) total FROM trades WHERE date(closed_at)=date('now')"
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
               SUM(CASE WHEN status='WIN'  THEN 1 ELSE 0 END) wins,
               SUM(CASE WHEN status='LOSS' THEN 1 ELSE 0 END) losses,
               COALESCE(SUM(pnl),0) total_pnl
        FROM trades WHERE user_id=? AND status IN ('WIN','LOSS')
    """
    params = [user_id]
    if mode:
        q += " AND mode=?"
        params.append(mode)
    row = con.execute(q, params).fetchone()
    con.close()
    return dict(row)


def get_consecutive_losses(user_id, mode=None):
    """How many LOSS trades in a row, most-recent first, closed today —
    resets naturally once a WIN trade appears or the day rolls over."""
    con = _conn()
    q = """
        SELECT status FROM trades
        WHERE user_id=? AND status IN ('WIN','LOSS') AND date(closed_at)=date('now')
    """
    params = [user_id]
    if mode:
        q += " AND mode=?"
        params.append(mode)
    q += " ORDER BY closed_at DESC"
    rows = con.execute(q, params).fetchall()
    con.close()
    count = 0
    for r in rows:
        if r["status"] == "LOSS":
            count += 1
        else:
            break
    return count
