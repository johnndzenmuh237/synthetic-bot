"""
mt5_bot.py - the EMA 21/50 pullback strategy, trading on MetaTrader 5 (Windows PC or Windows VPS).

SEPARATE from the Deriv/Render bot: it does not touch app.py, the database or your Deriv bot.
It uses the SAME decision code as the Deriv bot (ema_trend.py), so the entries are identical:

  BUY : EMA21 above EMA50 and rising, price pulls back to EMA21, the retest candle closes back above it -> buy
  SELL: the mirror image
  A trend that has already run far is NOT chased - the bot waits for the next pullback.

Unlike Rise/Fall, an MT5 trade is a normal position, so this bot manages it like you do by hand:
  * stop-loss behind EMA50 (clamped to 1.5-6 average candle ranges), take-profit at RR x the risk
  * at +1R the stop moves to break-even
  * a profitable trade is closed when the averages cross against it or converge (profit protected);
    a losing trade is never dumped early - the stop-loss is its protection
  * lot size from a % of equity risked per trade (or a fixed lot)
  * trades synthetic indices, crypto and forex (every tradable symbol your account offers, by group)
  * "trading duration" in hours like the Deriv bot: after it ends no new trades, winners are banked

Setup (Windows only; MetaTrader5's Python link does not exist for Linux, so NOT on Render):
  1. Install the MT5 terminal, log in to your Deriv MT5 (demo first), keep it open.
     In MT5: Tools > Options > Expert Advisors > tick "Allow algorithmic trading".
  2. pip install MetaTrader5
  3. python mt5_bot.py --login 12345678 --server "Deriv-Demo" --groups synthetic,crypto,forex --tf M5
     (the password is asked for; or set MT5_PASSWORD)
  Try first:  python mt5_bot.py --login ... --server ... --dry-run     (shows signals, places nothing)
"""
import argparse
import getpass
import logging
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

from ema_trend import prepare_trend, pullback_entry, exit_decision, MIN_ROWS

log = logging.getLogger("mt5bot")

TIMEFRAMES = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400}
SESSION_CHOICES = ("1", "2", "4", "8", "12", "24")
MAGIC = 21050
BARS = 300

FOREX_CCY = {"EUR", "USD", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "SEK", "NOK", "MXN", "ZAR", "SGD",
             "HKD", "TRY", "PLN", "CNH", "XAU", "XAG"}
CRYPTO_WORDS = ("BTC", "ETH", "LTC", "XRP", "BCH", "DOGE", "SOL", "ADA", "DOT", "BNB", "TRX", "XLM", "LINK",
                "AVAX", "MATIC", "EOS", "IOTA", "DSH", "DASH", "NEO", "ETC", "UNI", "SHIB")
SYNTH_WORDS = ("volatility", "boom", "crash", "step", "jump", "range break", "drift", "dex ", "vol ", "hybrid",
               "skewed", "multi step", "spot volatility", "gain x", "pain x", "bull market", "bear market",
               "daily reset")


def classify(name, path=""):
    """-> 'synthetic' | 'crypto' | 'forex' | None (stocks, indices and others are not traded)."""
    n, p = (name or "").lower(), (path or "").lower()
    if any(w in n for w in SYNTH_WORDS) or "derived" in p or "synthetic" in p:
        return "synthetic"
    up = (name or "").upper()
    if "crypto" in p or any(up.startswith(w) for w in CRYPTO_WORDS):
        return "crypto"
    if "forex" in p:
        return "forex"
    letters = "".join(ch for ch in up if ch.isalpha())
    if len(letters) >= 6 and letters[:3] in FOREX_CCY and letters[3:6] in FOREX_CCY:
        return "forex"
    return None


class Settings(object):
    def __init__(self, **kw):
        self.tf = "M5"
        self.groups = ("synthetic", "crypto", "forex")
        self.only = ()                    # explicit symbol names (overrides groups)
        self.risk_pct = 1.0               # % of equity risked per trade
        self.lot = 0.0                    # > 0 = fixed lot instead of % risk
        self.rr = 2.0                     # take-profit = rr x risk
        self.sl_buffer = 0.5              # stop = EMA50 -/+ this many candle ranges
        self.sl_min = 1.5                 # stop distance limits, in average candle ranges
        self.sl_max = 6.0
        self.be_r = 1.0                   # move the stop to break-even at this many R of profit
        self.max_positions = 5            # open trades across all symbols
        self.hours = "24"
        self.dry_run = False
        self.deviation = 30
        self.max_risk_factor = 3.0        # skip if the smallest lot already risks > 3x the budget
        for k, v in kw.items():
            setattr(self, k, v)


class Mt5Bot(object):
    def __init__(self, mt5, settings, clock=time.time):
        self.mt5 = mt5
        self.s = settings
        self.clock = clock
        self.gran = TIMEFRAMES[settings.tf]
        self.symbols = []
        self.bucket = {}                  # symbol -> last candle bucket evaluated
        self.risk = {}                    # ticket -> [initial risk distance, break-even done]
        self.start = clock()
        self.session_over = False
        self.done = False
        self.orders_sent = 0

    # ── connection / symbols ─────────────────────────────────
    def discover(self):
        m = self.mt5
        found = []
        for si in (m.symbols_get() or []):
            if self.s.only:
                if si.name not in self.s.only:
                    continue
            else:
                if getattr(si, "trade_mode", m.SYMBOL_TRADE_MODE_FULL) != m.SYMBOL_TRADE_MODE_FULL:
                    continue
                if classify(si.name, getattr(si, "path", "")) not in self.s.groups:
                    continue
            if m.symbol_select(si.name, True):
                found.append(si.name)
        self.symbols = sorted(found)
        return self.symbols

    # ── data ─────────────────────────────────────────────────
    def candles(self, symbol):
        tf = getattr(self.mt5, "TIMEFRAME_" + self.s.tf)
        rates = self.mt5.copy_rates_from_pos(symbol, tf, 1, BARS)      # start=1: closed candles only
        if rates is None or len(rates) < MIN_ROWS:
            return None
        df = pd.DataFrame(rates)
        df = df.rename(columns={"time": "epoch"})[["epoch", "open", "high", "low", "close"]].astype(float)
        return df.reset_index(drop=True)

    def server_now(self):
        """Newest tick time across the watched symbols = the broker's current time (24/7 symbols keep it live)."""
        best = 0
        for sym in self.symbols:
            t = self.mt5.symbol_info_tick(sym)
            if t is not None:
                best = max(best, int(t.time))
        return best

    # ── positions ────────────────────────────────────────────
    def positions(self, symbol=None):
        res = self.mt5.positions_get(symbol=symbol) if symbol else self.mt5.positions_get()
        return [p for p in (res or []) if getattr(p, "magic", MAGIC) == MAGIC]

    def _is_buy(self, pos):
        return pos.type == self.mt5.POSITION_TYPE_BUY

    # ── trade sizing ─────────────────────────────────────────
    def volume_for(self, info, dist):
        if self.s.lot > 0:
            lots = self.s.lot
        else:
            acct = self.mt5.account_info()
            budget = float(acct.equity) * self.s.risk_pct / 100.0
            per_lot = dist / float(info.trade_tick_size) * float(info.trade_tick_value)
            if per_lot <= 0:
                return None, "cannot size the trade"
            lots = budget / per_lot
            if info.volume_min * per_lot > budget * self.s.max_risk_factor:
                return None, "smallest lot ({}) already risks {:.2f}, over the {:.2f} budget - use --lot to override".format(
                    info.volume_min, info.volume_min * per_lot, budget)
        step = float(info.volume_step)
        lots = np.floor(lots / step + 1e-9) * step
        lots = min(max(lots, float(info.volume_min)), float(info.volume_max))
        return round(lots, 8), ""

    def stops(self, direction, entry, d, info):
        last = d.iloc[-1]
        rng = float(last["rng"])
        slow = float(last["ema_slow"])
        if direction == "BUY":
            dist = entry - (slow - self.s.sl_buffer * rng)
        else:
            dist = (slow + self.s.sl_buffer * rng) - entry
        dist = min(max(dist, self.s.sl_min * rng), self.s.sl_max * rng)
        floor = float(getattr(info, "trade_stops_level", 0)) * float(info.point) * 1.1
        dist = max(dist, floor)
        sign = 1 if direction == "BUY" else -1
        digits = int(info.digits)
        return round(entry - sign * dist, digits), round(entry + sign * self.s.rr * dist, digits), dist

    # ── entries ──────────────────────────────────────────────
    def _filling(self, info):
        m, mode = self.mt5, int(getattr(info, "filling_mode", 0))
        if mode & 2:
            return m.ORDER_FILLING_IOC
        if mode & 1:
            return m.ORDER_FILLING_FOK
        return m.ORDER_FILLING_RETURN

    def open_trade(self, symbol, direction, d, why):
        m = self.mt5
        info, tick = m.symbol_info(symbol), m.symbol_info_tick(symbol)
        if info is None or tick is None:
            return False
        entry = float(tick.ask if direction == "BUY" else tick.bid)
        sl, tp, dist = self.stops(direction, entry, d, info)
        dist = abs(entry - sl)                                   # the real (rounded) stop distance
        vol, note = self.volume_for(info, dist)
        if vol is None:
            log.warning("%s %s skipped: %s", symbol, direction, note)
            return False
        log.info("SIGNAL %s %s | %s | entry %.5f SL %.5f TP %.5f lots %s", direction, symbol, why, entry, sl, tp, vol)
        if self.s.dry_run:
            log.info("  (dry run - no order sent)")
            return False
        req = {"action": m.TRADE_ACTION_DEAL, "symbol": symbol, "volume": vol,
               "type": m.ORDER_TYPE_BUY if direction == "BUY" else m.ORDER_TYPE_SELL,
               "price": entry, "sl": sl, "tp": tp, "deviation": self.s.deviation, "magic": MAGIC,
               "comment": "EMA21/50 pullback", "type_time": m.ORDER_TIME_GTC,
               "type_filling": self._filling(info)}
        res = m.order_send(req)
        ok = res is not None and res.retcode in (m.TRADE_RETCODE_DONE, m.TRADE_RETCODE_PLACED)
        if ok:
            self.orders_sent += 1
            log.info("  ORDER FILLED %s %s lots %s (ticket %s)", direction, symbol, vol, getattr(res, "order", "?"))
        else:
            log.error("  order rejected for %s: %s", symbol, getattr(res, "comment", m.last_error()))
        return ok

    def entry_allowed(self, symbol):
        if self.session_over:
            return False
        if self.positions(symbol):
            return False                                        # one position per symbol
        return len(self.positions()) < self.s.max_positions

    # ── exits / management ───────────────────────────────────
    def close_position(self, pos, reason):
        m = self.mt5
        tick = m.symbol_info_tick(pos.symbol)
        buy = self._is_buy(pos)
        req = {"action": m.TRADE_ACTION_DEAL, "symbol": pos.symbol, "volume": pos.volume,
               "type": m.ORDER_TYPE_SELL if buy else m.ORDER_TYPE_BUY, "position": pos.ticket,
               "price": float(tick.bid if buy else tick.ask), "deviation": self.s.deviation, "magic": MAGIC,
               "comment": "EMA21/50 exit", "type_time": m.ORDER_TIME_GTC,
               "type_filling": self._filling(m.symbol_info(pos.symbol))}
        if self.s.dry_run:
            log.info("(dry run) would close %s %s: %s", pos.symbol, pos.ticket, reason)
            return False
        res = m.order_send(req)
        ok = res is not None and res.retcode in (m.TRADE_RETCODE_DONE, m.TRADE_RETCODE_PLACED)
        log.info("CLOSE %s %s (profit %.2f): %s -> %s", pos.symbol, pos.ticket, pos.profit, reason, "done" if ok else "FAILED")
        return ok

    def manage_break_even(self, pos):
        m = self.mt5
        rec = self.risk.get(pos.ticket)
        if rec is None:
            dist = abs(pos.price_open - pos.sl) if pos.sl else 0.0
            rec = self.risk[pos.ticket] = [dist, dist <= 0]
        if rec[1]:
            return
        tick, info = m.symbol_info_tick(pos.symbol), m.symbol_info(pos.symbol)
        buy = self._is_buy(pos)
        price = float(tick.bid if buy else tick.ask)
        moved = (price - pos.price_open) if buy else (pos.price_open - price)
        if moved < self.s.be_r * rec[0]:
            return
        pad = float(info.point) * (float(getattr(info, "spread", 0)) + 1)
        new_sl = round(pos.price_open + (pad if buy else -pad), int(info.digits))
        res = m.order_send({"action": m.TRADE_ACTION_SLTP, "symbol": pos.symbol, "position": pos.ticket,
                            "sl": new_sl, "tp": pos.tp, "magic": MAGIC})
        if res is not None and res.retcode in (m.TRADE_RETCODE_DONE, m.TRADE_RETCODE_PLACED):
            rec[1] = True
            log.info("BREAK-EVEN %s %s: stop moved to %.5f", pos.symbol, pos.ticket, new_sl)

    def manage_exits(self, symbol, d):
        for pos in self.positions(symbol):
            direction = "BUY" if self._is_buy(pos) else "SELL"
            why = exit_decision(d, direction)
            if why and pos.profit > 0:                           # profit is protected; a loser keeps its stop-loss
                self.close_position(pos, why)

    # ── session ──────────────────────────────────────────────
    def check_session(self):
        if self.session_over:
            return
        if self.clock() - self.start >= int(self.s.hours) * 3600:
            self.session_over = True
            log.info("Trading duration (%s h) is over: no new trades; profitable trades are being closed.", self.s.hours)
        if self.session_over:
            for pos in self.positions():
                if pos.profit > 0:
                    self.close_position(pos, "trading duration ended")
            if not self.positions():
                log.info("Trading duration ended and no trades are open - bot stopped.")
                self.done = True

    # ── one polling pass ─────────────────────────────────────
    def step(self):
        self.check_session()
        if self.done:
            return
        now = self.server_now()
        for sym in self.symbols:
            tick = self.mt5.symbol_info_tick(sym)
            if tick is None:
                continue
            for pos in self.positions(sym):
                self.manage_break_even(pos)
            b = int(tick.time) // self.gran
            if self.bucket.get(sym) == b:
                continue                                         # no new closed candle yet
            first_look = sym not in self.bucket
            self.bucket[sym] = b
            if now and int(tick.time) < now - 600:               # this market is not ticking (closed)
                continue
            df = self.candles(sym)
            if df is None:
                continue
            if int(df["epoch"].iloc[-1]) + self.gran < int(tick.time) - 2 * self.gran:
                continue                                         # candles are stale
            d = prepare_trend(df)
            self.manage_exits(sym, d)
            if self.session_over or not self.entry_allowed(sym):
                continue
            state, why = pullback_entry(d)
            if state in ("BUY", "SELL"):
                self.open_trade(sym, state, d, "{} | {}".format(self.s.tf, why))

    def run(self, poll=2.0):
        log.info("Watching %d symbols on %s: %s", len(self.symbols), self.s.tf, ", ".join(self.symbols[:12]) +
                 (" ..." if len(self.symbols) > 12 else ""))
        while not self.done:
            try:
                self.step()
            except Exception as e:                               # never die on one bad pass
                log.exception("pass failed: %s", e)
            time.sleep(poll)


# ── start-up ─────────────────────────────────────────────────
def connect(mt5, login, password, server):
    if not mt5.initialize():
        raise SystemExit("Cannot reach the MT5 terminal: {}. Open MT5 and log in first.".format(mt5.last_error()))
    if login:
        if not mt5.login(int(login), password=password, server=server):
            raise SystemExit("MT5 login failed: {}".format(mt5.last_error()))
    acct = mt5.account_info()
    if acct is None:
        raise SystemExit("No MT5 account is logged in.")
    return acct


def main(argv=None):
    ap = argparse.ArgumentParser(description="EMA 21/50 pullback bot for MetaTrader 5 (Windows).")
    ap.add_argument("--login", default=os.getenv("MT5_LOGIN"), help="MT5 account number (or MT5_LOGIN)")
    ap.add_argument("--server", default=os.getenv("MT5_SERVER"), help="MT5 server, e.g. Deriv-Demo (or MT5_SERVER)")
    ap.add_argument("--password", default=os.getenv("MT5_PASSWORD"), help="asked for if not given (or MT5_PASSWORD)")
    ap.add_argument("--groups", default="synthetic,crypto,forex", help="comma list: synthetic, crypto, forex")
    ap.add_argument("--symbols", default="", help="trade only these exact symbol names (comma list)")
    ap.add_argument("--tf", default="M5", choices=sorted(TIMEFRAMES, key=TIMEFRAMES.get))
    ap.add_argument("--risk", type=float, default=1.0, help="%% of equity risked per trade (default 1)")
    ap.add_argument("--lot", type=float, default=0.0, help="fixed lot size instead of %% risk")
    ap.add_argument("--rr", type=float, default=2.0, help="take-profit = this x the risk (default 2)")
    ap.add_argument("--max-positions", type=int, default=5)
    ap.add_argument("--hours", default="24", choices=SESSION_CHOICES, help="trading duration in hours")
    ap.add_argument("--dry-run", action="store_true", help="show signals only, place no orders")
    ap.add_argument("--live-ok", action="store_true", help="required to run on a REAL-money account")
    a = ap.parse_args(argv)

    try:
        import MetaTrader5 as mt5
    except ImportError:
        raise SystemExit("Install it first:  pip install MetaTrader5   (Windows only)")
    password = a.password or getpass.getpass("MT5 password: ")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("mt5_bot.log", encoding="utf-8")])
    acct = connect(mt5, a.login, password, a.server)
    real = acct.trade_mode == mt5.ACCOUNT_TRADE_MODE_REAL
    log.info("Connected: account %s on %s | %s | balance %.2f %s", acct.login, acct.server,
             "REAL" if real else "DEMO", acct.balance, acct.currency)
    if real and not a.live_ok and not a.dry_run:
        raise SystemExit("This is a REAL-money account. Add --live-ok to confirm. (Behaviour is identical to demo.)")

    s = Settings(tf=a.tf, groups=tuple(g.strip() for g in a.groups.split(",") if g.strip()),
                 only=tuple(x.strip() for x in a.symbols.split(",") if x.strip()), risk_pct=a.risk, lot=a.lot,
                 rr=a.rr, max_positions=a.max_positions, hours=a.hours, dry_run=a.dry_run)
    bot = Mt5Bot(mt5, s)
    if not bot.discover():
        raise SystemExit("No tradable symbols found for those groups. Check --groups / --symbols and Market Watch.")
    try:
        bot.run()
    except KeyboardInterrupt:
        log.info("Stopped by you. Open trades keep their stop-loss and take-profit in MT5.")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()