"""
bot_engine.py — Core trading engine per user session (simplified EMA-cross version)

What changed vs the old version (these were the reasons nothing traded / no charts):
  * Candles are keyed by Deriv's `open_time` (candle start). The old code keyed them
    by the tick `epoch`, which changes on EVERY tick, so every tick was stored as a
    new "closed candle". That corrupted the EMAs and triggered a strategy run per tick.
  * Strategy runs only on CLOSED M1 candles (the forming candle is never used).
  * One trade per signal. The old code needed 2 free slots per signal while the
    dashboard default was 1 position -> it returned before even looking for a signal.
  * Demo mode now places real contracts on the Deriv DEMO account (config.SIMULATE_DEMO).
  * Trades are settled from Deriv's real contract result when the contract expires.
  * Chart snapshot can be re-sent any time (page reload, reconnect, server restart).
  * If the live candle stream goes quiet, candles are polled as a fallback.
"""
import asyncio
import calendar
import threading
import time
import uuid
import pandas as pd
from collections import defaultdict
from typing import Dict, List

from broker import DerivBroker
from strategy import evaluate, ema_direction
from risk_manager import RiskManager
from database import (
    save_candle, save_signal,
    open_trade, close_trade, get_open_trades,
    get_recent_trades, get_stats,
)
from indicators import compute_emas
from config import (
    EXECUTION_TF, ANALYSIS_TF, TIMEFRAMES, EMA_SLOW,
    DURATION, DURATION_UNIT, SIMULATE_DEMO, SIM_PAYOUT,
)
from logger import log

MAX_CANDLES_KEPT = 600
STREAM_STALE_SEC = 30      # no live candle for this long -> poll as fallback
SETTLE_EVERY_SEC = 30


def _duration_seconds():
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(DURATION_UNIT, 60)
    return DURATION * mult


class BotSession:
    def __init__(self, user_id, symbol, mode, emit_fn,
                 lot_size=1.0, max_positions=1):
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
        self._last_tick    = time.time()
        self._last_eval_epoch = None
        self._last_status  = ("idle", "")

    # ── lifecycle ────────────────────────────────────────────
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
        if lot_size is not None:
            self.lot_size = float(lot_size)
        if max_positions is not None:
            self.max_positions = int(max_positions)
        if self.risk:
            self.risk.update_settings(lot_size, max_positions)
        log.info("Settings updated | lot=%.2f maxpos=%d",
                 self.lot_size, self.max_positions)

    def _run_loop(self):
        """Self-healing loop: retries with backoff until stop() is called."""
        backoff = 5
        while not self._stop_requested:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_until_complete(self._main())
            except ConnectionError as e:
                log.error("Connection error: %s", e)
                self._emit_status("error", str(e))
            except Exception as e:
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

            log.warning("Bot loop ended unexpectedly — auto-restarting in %ds...", backoff)
            self._emit_status("reconnecting",
                "Bot hit an error — retrying in {}s...".format(backoff))
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            self.running = True

        self.running = False
        self._emit_status("stopped", "Bot stopped")

    async def _main(self):
        self._lock = asyncio.Lock()
        # Fresh broker + fresh candle state on every (re)start: the old websocket,
        # its subscriptions and half-filled candle lists are dead after a crash.
        self.broker = DerivBroker(self.mode)
        self.candles = defaultdict(list)
        self._last_eval_epoch = None

        self._emit_status("connecting",
            "Connecting to Deriv ({})...".format(self.mode.upper()))
        await self.broker.connect(self._on_candle_tick)

        self.risk = RiskManager(
            balance       = self.broker.balance,
            user_id       = self.user_id,
            mode          = self.mode,
            lot_size      = self.lot_size,
            max_positions = self.max_positions,
        )
        self._emit_status("connected",
            "Connected | {} | Balance: {:.2f} {}".format(
                self.broker.account_id, self.broker.balance, self.broker.currency))
        self._emit_balance(self.broker.balance)

        await self._load_history()

        for tf in [EXECUTION_TF] + ANALYSIS_TF:
            await self.broker.subscribe_candles(self.symbol, tf)
        self._last_tick = time.time()

        self._emit_status("running",
            "Bot running | {} | {} | Stake cap: {:.2f} | Max open: {}".format(
                self.symbol, self.mode.upper(), self.lot_size, self.max_positions))
        self._emit_trade_update()

        # Evaluate right away on the history we have (logs analysis, updates panels)
        await self._evaluate()

        last_slow = 0.0
        while self.running:
            await asyncio.sleep(10)
            if not self.running:
                break
            now = time.time()
            try:
                if now - self._last_tick > STREAM_STALE_SEC:
                    log.warning("No live candles for %ds — polling candles as fallback",
                                int(now - self._last_tick))
                    await self._poll_candles()
                if now - last_slow >= SETTLE_EVERY_SEC:
                    last_slow = now
                    bal = await self.broker.get_balance()
                    self.risk.update_balance(bal)
                    self._emit_balance(bal)
                    async with self._lock:
                        await self._settle_trades()
            except Exception as e:
                log.error("Keep-alive error: %s", e)

    # ── Candle handling ──────────────────────────────────────
    @staticmethod
    def _clean(c, epoch=None):
        return {
            "epoch": int(epoch if epoch is not None else c["epoch"]),
            "open":  float(c["open"]),
            "high":  float(c["high"]),
            "low":   float(c["low"]),
            "close": float(c["close"]),
        }

    def _ingest(self, tf, c):
        """Insert/update a candle keyed by its OPEN time.
        Returns True when a NEW candle opened (=> the previous one just closed)."""
        store = self.candles[tf]
        if store and c["epoch"] == store[-1]["epoch"]:
            store[-1] = c
            return False
        if store and c["epoch"] < store[-1]["epoch"]:
            return False                       # stale/out-of-order update
        if store:                              # previous candle is now final -> persist
            p = store[-1]
            try:
                save_candle(self.symbol, tf, p["epoch"], p["open"], p["high"], p["low"], p["close"])
            except Exception as e:
                log.error("save_candle error: %s", e)
        store.append(c)
        if len(store) > MAX_CANDLES_KEPT:
            del store[:len(store) - MAX_CANDLES_KEPT]
        return True

    async def _load_history(self):
        self._emit_status("loading", "Loading chart data for {}...".format(self.symbol))
        for tf in [EXECUTION_TF] + ANALYSIS_TF:
            raw = await self.broker.get_candles(self.symbol, tf)
            if not raw:
                log.warning("No candles returned for %s %s", self.symbol, tf)
                continue
            self.candles[tf] = [self._clean(c) for c in raw]
            self._emit_candles(tf, self.candles[tf][-200:])
            log.info("Chart ready | %s %s | %d candles", self.symbol, tf, len(raw))
            await self.loop.run_in_executor(None, self._save_candles_sync, self.symbol, tf, raw)

    def _save_candles_sync(self, symbol, tf, raw):
        for c in raw[:-1]:                      # skip the still-forming last candle
            save_candle(symbol, tf, c["epoch"],
                        float(c["open"]), float(c["high"]),
                        float(c["low"]),  float(c["close"]))

    def _on_candle_tick(self, ohlc):
        """Called for every live `ohlc` message from Deriv (runs on the bot's event loop)."""
        symbol = ohlc.get("symbol") or ohlc.get("underlying") or self.symbol
        if symbol != self.symbol:
            return
        self._last_tick = time.time()

        gran = int(ohlc.get("granularity", TIMEFRAMES[EXECUTION_TF]))
        tf   = next((k for k, v in TIMEFRAMES.items() if v == gran), None)
        if tf is None:
            return
        # Candle id = candle OPEN time (NOT the tick epoch, which changes every tick)
        open_time = ohlc.get("open_time")
        if open_time is None:
            open_time = (int(ohlc["epoch"]) // gran) * gran
        c = self._clean(ohlc, epoch=open_time)

        is_new = self._ingest(tf, c)
        self._emit_candle_update(tf, c)

        if is_new and tf == EXECUTION_TF:
            asyncio.ensure_future(self._evaluate())

    async def _poll_candles(self):
        """Fallback if the live stream is silent: pull the latest candles directly."""
        new_m1 = False
        for tf in [EXECUTION_TF] + ANALYSIS_TF:
            raw = await self.broker.get_candles(self.symbol, tf, count=5)
            for r in raw or []:
                c = self._clean(r)
                is_new = self._ingest(tf, c)
                if is_new and tf == EXECUTION_TF:
                    new_m1 = True
            if raw:
                self._emit_candle_update(tf, self.candles[tf][-1])
                self._last_tick = time.time()
        if new_m1:
            await self._evaluate()

    # ── Strategy ─────────────────────────────────────────────
    async def _evaluate(self):
        try:
            async with self._lock:
                await self._evaluate_locked()
        except Exception as e:
            log.error("Evaluate error: %s", e, exc_info=True)

    async def _evaluate_locked(self):
        m1 = self._df("M1")
        m5 = self._df("M5")
        if m1 is None:
            return
        closed_epoch = int(m1.iloc[-1]["epoch"])
        if closed_epoch == self._last_eval_epoch:
            return                               # already handled this closed candle
        self._last_eval_epoch = closed_epoch

        self._emit_analysis(m5, m1)
        await self._settle_trades()

        if not self.risk.can_trade():
            return
        if self.risk.positions_available() < 1:
            return

        signal = evaluate(m5, m1, self.symbol)
        if signal is None:
            return

        save_signal(self.symbol, EXECUTION_TF, signal.direction,
                    signal.ema_fast, signal.ema_slow, 0.0, signal.close_price)

        stake = self.risk.get_stake()
        self.emit("signal", {
            "symbol":        self.symbol,
            "direction":     signal.direction,
            "price":         signal.close_price,
            "stake":         stake,
            "lot_size":      self.lot_size,
            "max_positions": self.max_positions,
            "reason":        signal.reason,
        })

        if self.mode == "demo" and SIMULATE_DEMO:
            contract_id = "DEMO_{}".format(uuid.uuid4().hex[:12])
            log.info("SIMULATED TRADE | %s %s | Stake=$%.2f", signal.direction, self.symbol, stake)
        else:
            contract = await self.broker.place_trade(self.symbol, signal.direction, stake)
            if not contract:
                msg = "Trade NOT placed: {}".format(self.broker.last_error or "unknown error")
                log.error(msg)
                self.emit("trade_error", {"message": msg})
                return
            contract_id = str(contract.get("contract_id", ""))

        open_trade(
            user_id=self.user_id, symbol=self.symbol,
            direction=signal.direction, lot_size=self.lot_size,
            stake=stake, entry=signal.close_price, sl=0, tp=0,
            contract_id=contract_id, mode=self.mode, position_num=1,
            trade_group=uuid.uuid4().hex[:12], target_price=0, r_distance=0,
        )

        bal = await self.broker.get_balance()
        self.risk.update_balance(bal)
        self._emit_balance(bal)
        self._emit_trade_update()

    # ── Settlement ───────────────────────────────────────────
    async def _settle_trades(self):
        """Close trades whose contract has expired, using Deriv's real result."""
        now = time.time()
        dur = _duration_seconds()
        changed = False

        for t in get_open_trades(self.user_id, self.mode):
            opened = self._opened_at_ts(t["opened_at"])
            if not opened:
                continue
            age = now - opened
            if age < dur:
                continue

            cid    = str(t["contract_id"] or "")
            entry  = float(t["entry_price"] or 0)
            stake  = float(t["stake"] or 0)
            exit_p, pnl = entry, None

            if cid.startswith("DEMO_"):
                if t["symbol"] != self.symbol or not self.candles["M1"]:
                    continue
                exit_p = float(self.candles["M1"][-1]["close"])
                win = exit_p > entry if t["direction"] == "BUY" else exit_p < entry
                pnl = round(stake * SIM_PAYOUT, 2) if win else -stake
            elif cid:
                info = await self.broker.get_contract(cid)
                if info and info.get("is_sold"):
                    pnl = float(info.get("profit", 0))
                    exit_p = float(info.get("sell_spot") or info.get("exit_tick") or entry)

            if pnl is None:
                if age > dur * 3 + 120:          # can't resolve it — free the slot
                    close_trade(t["id"], entry, 0.0, "VOID")
                    log.warning("Trade #%s could not be resolved — marked VOID", t["id"])
                    changed = True
                continue

            status = "WIN" if pnl > 0 else "LOSS"
            close_trade(t["id"], exit_p, round(pnl, 2), status)
            log.info("TRADE CLOSED #%s | %s | P&L %.2f", t["id"], status, pnl)
            self.emit("trade_closed", {
                "trade_id": t["id"], "pnl": round(pnl, 2), "status": status,
                "reason": "Contract expired",
            })
            changed = True

        if changed:
            self._emit_trade_update()

    @staticmethod
    def _opened_at_ts(opened_at_str):
        if not opened_at_str:
            return None
        try:
            return calendar.timegm(time.strptime(opened_at_str, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            return None

    # ── DataFrame helper (CLOSED candles only) ───────────────
    def _df(self, tf):
        store = self.candles.get(tf, [])
        closed = store[:-1]                     # last item is the forming candle
        if len(closed) < EMA_SLOW + 5:
            return None
        df = pd.DataFrame(closed, columns=["epoch", "open", "high", "low", "close"])
        return compute_emas(df)

    # ── Snapshot for (re)connecting browsers ─────────────────
    def send_snapshot(self):
        """Re-send everything the dashboard needs. Called when a browser connects,
        so charts load even if the bot was started before the page was opened."""
        try:
            state, msg = self._last_status
            self.emit("bot_status", {"state": state, "message": msg})
            if self.risk:
                self._emit_balance(self.broker.balance)
            for tf in [EXECUTION_TF] + ANALYSIS_TF:
                c = list(self.candles.get(tf, []))
                if c:
                    self._emit_candles(tf, c[-200:])
            self._emit_analysis(self._df("M5"), self._df("M1"))
            self._emit_trade_update()
        except Exception as e:
            log.error("Snapshot error: %s", e)

    # ── Emit helpers ─────────────────────────────────────────
    def _emit_status(self, state, msg):
        self._last_status = (state, msg)
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
        self.emit("candles_init", {"tf": tf, "symbol": self.symbol, "candles": candles})

    def _emit_candle_update(self, tf, candle):
        self.emit("candle_update", {"tf": tf, "symbol": self.symbol, "candle": candle})

    def _emit_trade_update(self):
        self.emit("trades_update", {
            "trades": get_recent_trades(self.user_id, 15),
            "stats":  get_stats(self.user_id),
        })

    def _emit_analysis(self, m5_df, m1_df):
        """'h4' slot = M5 trend card, 'h1' slot = M1 entry card (dashboard.js keys)."""
        def card(df):
            if df is None:
                return {"direction": "NONE", "trending": False,
                        "ema21": 0, "ema50": 0, "ema100": 0, "close": 0}
            last = df.iloc[-1]
            d = ema_direction(df)
            return {
                "direction": d, "trending": d != "NONE",
                "ema21":  round(float(last["ema_fast"]), 5),
                "ema50":  round(float(last["ema_slow"]), 5),
                "ema100": round(float(last["close"]), 5),
                "close":  round(float(last["close"]), 5),
            }
        self.emit("analysis_update", {"h4": card(m5_df), "h1": card(m1_df)})
