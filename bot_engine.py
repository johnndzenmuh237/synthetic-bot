"""
bot_engine.py — Core trading engine per user session

New features:
  - lot_size: user-controlled stake size (MetaTrader style)
  - max_positions: how many trades bot places concurrently
  - Proper demo vs live account separation
  - Better error reporting to frontend
"""
import asyncio
import threading
import time
import uuid
import pandas as pd
from collections import defaultdict
from typing import Callable, Dict, List

from broker import DerivBroker
from strategy import evaluate, should_exit, max_r_price
from risk_manager import RiskManager
from database import (
    save_candle, save_signal,
    open_trade, close_trade, get_open_trades,
    get_recent_trades, get_stats,
    update_trade_sl, get_trade_group,
)
from indicators import compute_emas, market_direction, is_trending
from config import (
    ANALYSIS_TF, EXECUTION_TF, TIMEFRAMES, EMA_SLOW,
    TP1_CLOSE_PCT, MAX_HOLD_MINUTES,
)
from logger import log


class BotSession:
    def __init__(self, user_id, symbol, mode, emit_fn,
                 lot_size=1.0, max_positions=2):
        self.user_id       = user_id
        self.symbol        = symbol
        self.mode          = mode           # "demo" or "live"
        self.emit          = emit_fn
        self.lot_size      = float(lot_size)
        self.max_positions = int(max_positions)
        self.running       = False
        self._stop_requested = False
        self.broker        = DerivBroker(mode)
        self.risk          = None
        self.loop          = None
        self.candles: Dict[str, List[dict]] = defaultdict(list)
        self._lock         = None

    def start(self):
        self.running = True
        self._stop_requested = False
        t = threading.Thread(target=self._run_loop, daemon=True)
        t.start()
        log.info("Bot started | user=%s symbol=%s mode=%s lot=%.2f maxpos=%d",
                 self.user_id, self.symbol, self.mode,
                 self.lot_size, self.max_positions)

    def stop(self):
        self._stop_requested = True
        self.running = False
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self.broker.disconnect(), self.loop)
        log.info("Bot stopped | user=%s symbol=%s", self.user_id, self.symbol)

    def update_settings(self, lot_size=None, max_positions=None):
        """Update lot size / max positions while bot is running."""
        if lot_size is not None:
            self.lot_size = float(lot_size)
        if max_positions is not None:
            self.max_positions = int(max_positions)
        if self.risk:
            self.risk.update_settings(lot_size, max_positions)
        log.info("Settings updated | lot=%.2f maxpos=%d",
                 self.lot_size, self.max_positions)

    def _run_loop(self):
        """
        Self-healing loop: the strategy doc requires the bot to keep running
        unless the user explicitly stops it. Previously, any unhandled
        exception here (a dropped connection that couldn't recover, a bad
        API response, etc.) would silently end the thread forever while the
        dashboard still showed 'running'. Now it retries with backoff
        instead, and only stops for real when stop() was actually called.
        """
        backoff = 5
        while not self._stop_requested:
            # Create and register the event loop for this thread FIRST.
            # Nothing that touches asyncio primitives (Lock, Event, etc.)
            # may be constructed before set_event_loop() runs, or it will
            # fail with "There is no current event loop in thread ...".
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_until_complete(self._main())
            except ConnectionError as e:
                log.error("Connection error: %s", e)
                self._emit_status("error", str(e))
            except Exception as e:
                # Some exceptions (e.g. certain websockets close errors) have
                # an empty str(e); fall back to type name + repr so the log
                # line is never just "Bot session error:" with nothing after it.
                detail = str(e) or repr(e) or type(e).__name__
                log.error("Bot session error: %s", detail)
                self._emit_status("error", "Bot error: {}".format(detail))
            finally:
                try:
                    self.loop.close()
                except Exception:
                    pass

            if self._stop_requested:
                break

            log.warning("Bot loop ended unexpectedly — auto-restarting in %ds "
                        "(strategy requires the bot to keep running until "
                        "manually stopped)...", backoff)
            self._emit_status("reconnecting",
                "Bot hit an error — retrying in {}s...".format(backoff))
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)   # cap retry gap at 60s
            self.running = True              # mark running again for the retry

        self.running = False
        self._emit_status("stopped", "Bot stopped")

    async def _main(self):
        # The lock is created here, inside the running loop, so it
        # binds to the correct loop automatically (safe on 3.8+ and
        # on 3.10+ where Lock() no longer accepts a loop argument at all).
        self._lock = asyncio.Lock()

        # Connect and authenticate
        self._emit_status("connecting",
            "Connecting to Deriv ({})...".format(self.mode.upper()))

        await self.broker.connect(self._on_candle_tick)

        # Initialise risk manager with real balance and user settings
        self.risk = RiskManager(
            balance       = self.broker.balance,
            user_id       = self.user_id,
            mode          = self.mode,
            lot_size      = self.lot_size,
            max_positions = self.max_positions,
        )

        self._emit_status("connected",
            "Connected | {} | Balance: {:.2f} {}".format(
                self.broker.account_id,
                self.broker.balance,
                self.broker.currency))
        self._emit_balance(self.broker.balance)

        # Load historical candles for all timeframes
        await self._load_history()

        # Subscribe to live streams
        for tf in [EXECUTION_TF] + ANALYSIS_TF:
            await self.broker.subscribe_candles(self.symbol, tf)

        self._emit_status("running",
            "Bot running | {} | {} | Lot: {:.2f} | Max positions: {}".format(
                self.symbol, self.mode.upper(),
                self.lot_size, self.max_positions))

        # Keep-alive: poll balance every 60s
        while self.running:
            await asyncio.sleep(60)
            if not self.running:
                break
            try:
                bal = await self.broker.get_balance()
                self.risk.update_balance(bal)
                self._emit_balance(bal)
                await self._check_exits()
            except Exception as e:
                log.error("Keep-alive error: %s", e)

    async def _load_history(self):
        self._emit_status("loading", "Loading chart data for {}...".format(self.symbol))
        for tf in [EXECUTION_TF] + ANALYSIS_TF:
            raw = await self.broker.get_candles(self.symbol, tf)
            if not raw:
                log.warning("No candles returned for %s %s", self.symbol, tf)
                continue
            self.candles[tf] = [
                {"epoch": c["epoch"],
                 "open":  float(c["open"]),
                 "high":  float(c["high"]),
                 "low":   float(c["low"]),
                 "close": float(c["close"])}
                for c in raw
            ]
            # Run the synchronous DB writes in a background thread so the
            # event loop stays free to answer Deriv's keepalive pings.
            # 300 blocking sqlite writes in a row can easily exceed the
            # ping_timeout window and get the connection killed with
            # "keepalive ping timeout; no close frame received".
            # (run_in_executor used instead of asyncio.to_thread for
            # Python 3.8 compatibility — to_thread is 3.9+ only.)
            await self.loop.run_in_executor(
                None, self._save_candles_sync, self.symbol, tf, raw)
            # Send chart data to frontend
            self._emit_candles(tf, self.candles[tf][-200:])
            log.info("Chart ready | %s %s | %d candles", self.symbol, tf, len(raw))

    def _save_candles_sync(self, symbol, tf, raw):
        for c in raw:
            save_candle(symbol, tf, c["epoch"],
                        float(c["open"]), float(c["high"]),
                        float(c["low"]),  float(c["close"]))

    def _on_candle_tick(self, ohlc):
        symbol = ohlc.get("symbol") or ohlc.get("underlying", self.symbol)
        if symbol != self.symbol:
            return

        gran  = int(ohlc.get("granularity", TIMEFRAMES[EXECUTION_TF]))
        tf    = next((k for k, v in TIMEFRAMES.items() if v == gran), EXECUTION_TF)
        epoch = int(ohlc["epoch"])
        c = {
            "epoch": epoch,
            "open":  float(ohlc["open"]),
            "high":  float(ohlc["high"]),
            "low":   float(ohlc["low"]),
            "close": float(ohlc["close"]),
        }

        store = self.candles[tf]
        if store and store[-1]["epoch"] == epoch:
            store[-1] = c  # update forming candle
        else:
            store.append(c)  # new closed candle → evaluate
            save_candle(symbol, tf, epoch,
                        c["open"], c["high"], c["low"], c["close"])
            if self.loop and self.loop.is_running():
                asyncio.run_coroutine_threadsafe(self._evaluate(), self.loop)

        self._emit_candle_update(tf, c)

    async def _evaluate(self):
        """Evaluate the ClaudeFX v2.0 strategy on each new closed M1 candle."""
        async with self._lock:
            m5 = self._df("M5")
            m1 = self._df("M1")

            if m5 is None or m1 is None:
                return

            # Push analysis to frontend
            self._emit_analysis(m5, m1)

            # Check exits for open trades (TP1/TP2/SL/trend-reversal/max-hold)
            await self._check_exits()

            # Check risk gates (daily loss %, consecutive losses, max positions)
            if not self.risk.can_trade():
                return

            # How many more position *slots* can we open? Each signal opens
            # 2 trades (TP1 leg + TP2 leg), so we need at least 2 free slots.
            slots = self.risk.positions_available()
            if slots < 2:
                log.info("Blocked: only %d position slot(s) available, need "
                         "2 (TP1+TP2 legs) — increase Max Positions to at "
                         "least 2 in the dashboard.", slots)
                return

            # Get signal
            signal = evaluate(m5, m1, self.symbol)
            if signal is None:
                return

            save_signal(self.symbol, EXECUTION_TF, signal.direction,
                        signal.ema_fast, signal.ema_slow, signal.ema_slow,
                        signal.close_price)

            total_stake = self.risk.get_stake()
            stake_tp1   = round(total_stake * TP1_CLOSE_PCT / 100, 2)
            stake_tp2   = round(total_stake - stake_tp1, 2)
            trade_group = uuid.uuid4().hex[:12]

            # Emit signal to frontend
            self.emit("signal", {
                "symbol":        self.symbol,
                "direction":     signal.direction,
                "price":         signal.close_price,
                "sl":            signal.stop_loss,
                "tp1":           signal.tp1_price,
                "tp2":           signal.tp2_price,
                "stake":         total_stake,
                "lot_size":      self.lot_size,
                "max_positions": self.max_positions,
                "slots_left":    slots,
                "reason":        signal.reason,
            })

            legs = [
                # (position_num, stake, target_price)
                (1, stake_tp1, signal.tp1_price),
                (2, stake_tp2, signal.tp2_price),
            ]

            for pos_num, stake, target in legs:
                if not self.running:
                    break
                if stake < 0.01:
                    continue

                # Both demo and live place a REAL Deriv contract — the only
                # difference is which token self.broker was constructed
                # with (DERIV_DEMO_TOKEN vs DERIV_LIVE_TOKEN), so demo
                # trades genuinely execute against your Deriv demo account
                # and its balance updates for real, exactly like live does.
                contract = await self.broker.place_trade(
                    self.symbol, signal.direction, stake)
                if contract:
                    open_trade(
                        user_id=self.user_id, symbol=self.symbol,
                        direction=signal.direction, lot_size=self.lot_size,
                        stake=stake, entry=signal.close_price, sl=signal.stop_loss,
                        tp=target, contract_id=str(contract.get("contract_id", "")),
                        mode=self.mode, position_num=pos_num,
                        trade_group=trade_group, target_price=target,
                        r_distance=signal.r_distance,
                        buy_price=float(contract.get("buy_price", stake)),
                    )
                else:
                    log.error("Failed to place TP%d leg for signal", pos_num)
                    break

            # Refresh balance after trades (demo and live both hold a real
            # Deriv account balance now)
            bal = await self.broker.get_balance()
            self.risk.update_balance(bal)
            self._emit_balance(bal)

            self._emit_trade_update()

    async def _check_exits(self):
        m5 = self._df("M5")
        m1 = self._df("M1")
        if m5 is None or m1 is None:
            return

        current_price = float(m1.iloc[-1]["close"])
        now_ts = time.time()

        for trade in get_open_trades(self.user_id, self.mode):
            if trade["symbol"] != self.symbol:
                continue

            direction = trade["direction"]
            entry     = float(trade["entry_price"])
            sl        = float(trade["stop_loss"])
            target    = float(trade["target_price"] or 0)
            r_dist    = float(trade["r_distance"] or 0)
            opened_ts = self._opened_at_ts(trade["opened_at"])

            hit_sl, hit_tp, hit_cap, trend_reversed, timed_out = (
                False, False, False, False, False)

            if direction == "BUY":
                hit_sl = current_price <= sl
                hit_tp = target > 0 and current_price >= target
                if r_dist > 0:
                    cap = max_r_price("BUY", entry, r_dist)
                    hit_cap = current_price >= cap
            else:
                hit_sl = current_price >= sl
                hit_tp = target > 0 and current_price <= target
                if r_dist > 0:
                    cap = max_r_price("SELL", entry, r_dist)
                    hit_cap = current_price <= cap

            do_exit, reason = should_exit(m5, direction)
            trend_reversed = do_exit

            if opened_ts and (now_ts - opened_ts) > MAX_HOLD_MINUTES * 60:
                timed_out = True

            if not (hit_sl or hit_tp or hit_cap or trend_reversed or timed_out):
                continue

            if hit_tp or hit_cap:
                exit_p = target if hit_tp else current_price
                exit_reason = "TP1 hit" if trade["position_num"] == 1 else "TP2/3R cap hit"
            elif hit_sl:
                exit_p = sl
                exit_reason = "Stop-loss hit"
            elif timed_out:
                exit_p = current_price
                exit_reason = "Max hold time ({} min) reached — closed manually".format(
                    MAX_HOLD_MINUTES)
            else:
                exit_p = current_price
                exit_reason = reason

            log.info("EXIT trade #%d (leg %s) | %s", trade["id"], trade["position_num"], exit_reason)

            # Every trade (demo and live) is a real Deriv contract now, so
            # always sell it for real instead of guessing an exit price.
            sold = await self.broker.close_trade(trade["contract_id"])
            if sold:
                exit_p = float(sold.get("sold_for", exit_p))
            else:
                log.error("Failed to close trade #%d on Deriv — will retry "
                          "next cycle rather than mark it closed incorrectly.",
                          trade["id"])
                continue

            # Real realized P&L, straight from Deriv's own numbers — not an
            # approximation. buy_price is what Deriv actually charged to
            # open the contract; sold_for is what it actually paid back.
            buy_price = float(trade["buy_price"]) or float(trade["stake"])
            pnl = round(exit_p - buy_price, 2)
            status = "WIN" if pnl >= 0 else "LOSS"

            close_trade(trade["id"], exit_p, pnl, status)

            # Deriv's sell response includes the account's exact balance
            # right after this trade — use it directly instead of waiting
            # for the next periodic balance refresh.
            if "balance_after" in sold:
                bal = float(sold["balance_after"])
                self.risk.update_balance(bal)
                self._emit_balance(bal)

            # TP1 leg closed as a win -> move the sibling TP2 leg's SL to breakeven
            if hit_tp and trade["position_num"] == 1 and status == "WIN" and trade["trade_group"]:
                for sib in get_trade_group(self.user_id, trade["trade_group"]):
                    if sib["id"] != trade["id"] and sib["status"] == "OPEN":
                        update_trade_sl(sib["id"], entry, mark_breakeven=True)
                        log.info("Moved TP2 leg #%d SL to breakeven (%.5f)", sib["id"], entry)

            self.emit("trade_closed", {
                "trade_id": trade["id"],
                "pnl":      pnl,
                "status":   status,
                "reason":   exit_reason,
            })

        self._emit_trade_update()

    @staticmethod
    def _opened_at_ts(opened_at_str):
        """Parse the SQLite 'YYYY-MM-DD HH:MM:SS' UTC timestamp to epoch seconds."""
        if not opened_at_str:
            return None
        try:
            import calendar
            t = time.strptime(opened_at_str, "%Y-%m-%d %H:%M:%S")
            return calendar.timegm(t)
        except ValueError:
            return None

    # ── DataFrame helper ─────────────────────────────────────
    def _df(self, tf):
        store = self.candles.get(tf, [])
        if len(store) < EMA_SLOW + 5:
            return None
        df = pd.DataFrame(store, columns=["epoch", "open", "high", "low", "close"])
        return compute_emas(df)

    # ── Emit helpers ─────────────────────────────────────────
    def _emit_status(self, state, msg):
        self.emit("bot_status", {"state": state, "message": msg})

    def _emit_balance(self, bal):
        self.emit("balance_update", {
            "balance":       round(bal, 2),
            "currency":      self.broker.currency,
            "daily_pnl":     self.risk.daily_loss_pct() if self.risk else 0,
            "account_id":    self.broker.account_id,
            "lot_size":      self.lot_size,
            "max_positions": self.max_positions,
        })

    def _emit_candles(self, tf, candles):
        self.emit("candles_init", {
            "tf": tf, "symbol": self.symbol, "candles": candles})

    def _emit_candle_update(self, tf, candle):
        self.emit("candle_update", {
            "tf": tf, "symbol": self.symbol, "candle": candle})

    def _emit_trade_update(self):
        trades = get_recent_trades(self.user_id, 15)
        stats  = get_stats(self.user_id)
        self.emit("trades_update", {"trades": trades, "stats": stats})

    def _emit_analysis(self, m5_df, m1_df):
        """Emitted keys are kept as 'h4'/'h1' (and the ema21/ema50/ema100
        field names) for wire-format compatibility with the existing
        dashboard.js, which reads exactly those keys. They now represent
        the M5 trend timeframe ('h4' slot) and M1 entry timeframe
        ('h1' slot) instead of the old H4/H1 triple-EMA reading.
        ema21->EMA10, ema50->EMA20, ema100->close price (dashboard.html
        labels these fields accordingly)."""
        m5l = m5_df.iloc[-1]
        m1l = m1_df.iloc[-1]
        self.emit("analysis_update", {
            "h4": {  # M5 trend timeframe
                "direction": market_direction(m5_df),
                "trending":  is_trending(m5_df),
                "ema21":     round(float(m5l["ema_fast"]), 5),
                "ema50":     round(float(m5l["ema_slow"]), 5),
                "ema100":    round(float(m5l["close"]), 5),
                "close":     round(float(m5l["close"]), 5),
            },
            "h1": {  # M1 entry timeframe
                "direction": market_direction(m5_df),  # entry follows the M5 trend direction
                "trending":  is_trending(m5_df),
                "ema21":     round(float(m1l["ema_fast"]), 5),
                "ema50":     round(float(m1l["ema_slow"]), 5),
                "ema100":    round(float(m1l["close"]), 5),
                "close":     round(float(m1l["close"]), 5),
            },
        })