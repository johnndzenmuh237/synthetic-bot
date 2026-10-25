"""
bot_engine.py — one trading worker per user (runs on a background thread of the
server process, completely independent of any browser/dashboard session).

Lifecycle states reported to the dashboard (always the REAL state of the worker):
    starting | running | reconnecting | paused | stopping | stopped | error

Safety rules implemented here
  * Strategy runs only on CLOSED M1 candles, once per candle (duplicate events ignored).
  * Stake = exactly what the user configured; position cap counts every order that
    occupies a slot (PENDING / UNCONFIRMED / OPEN) so repeated signals, races or a
    retry can never exceed it.
  * An order is written to the database BEFORE it is sent. If Deriv's answer is lost
    the order is marked UNCONFIRMED and NO new order is sent until it is reconciled
    against Deriv (never blindly repeated).
  * Results (win/loss/profit) come ONLY from Deriv's confirmed contract data; the
    balance shown is the balance Deriv reports. Floating profit is shown separately
    and never added to the balance. Closing a trade is idempotent in the database.
  * On every (re)start and reconnect, open trades are reconciled against Deriv
    before any new trade is considered.
  * If Deriv rejects the token, no orders are submitted and the real reason is shown.
"""
import asyncio
import calendar
import threading
import time
import uuid
import pandas as pd
from collections import defaultdict
from typing import Dict, List

from broker import DerivBroker, OrderStatusUnknown
from strategy import evaluate, ema_direction, prepare, strategy_info, _in_daily_reset_window
from candle_follow import band_size, zone_of, DirectionTracker, exits_for, profit_turned_negative
from strategy import Signal
from risk_manager import RiskManager
from database import (
    save_candle, save_signal,
    open_trade, update_trade, close_trade, mark_trade,
    get_open_trades, get_active_trades, get_reconcilable_trades,
    get_unresolved_orders, contract_exists,
    get_recent_trades, get_stats, get_daily_pnl, get_exposure,
)
from config import (
    EXECUTION_TF, ANALYSIS_TF, TIMEFRAMES, DURATION, DURATION_UNIT,
    DEFAULT_STRATEGY, DEFAULT_LOSS_POLICY, ALLOW_HEDGE, MAX_OPEN_TRADES,
    CF_CONFIRM_SEC, CF_BAND_FRAC, CF_PROTECT_PROFIT, CF_ENTRY_RETRY_SEC, CF_MAX_TICK_AGE,
    POSITION_POLL_SEC, DEFAULT_TIMEFRAME,
)
from logger import log

MAX_CANDLES_KEPT = 600
MIN_CANDLES      = 30        # closed candles needed before any analysis
STREAM_STALE_SEC = 30        # no live candle for this long -> poll as fallback
SLOW_EVERY_SEC   = 30        # reconcile + balance refresh interval
UNRESOLVED_GRACE = 120       # seconds before an unmatched order is flagged UNRECONCILED


def _duration_seconds():
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(DURATION_UNIT, 60)
    return DURATION * mult


def _opened_at_ts(opened_at_str):
    """Parse SQLite 'YYYY-MM-DD HH:MM:SS' (UTC) into epoch seconds."""
    if not opened_at_str:
        return None
    try:
        return calendar.timegm(time.strptime(opened_at_str, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class BotSession:
    def __init__(self, user_id, symbol, mode, emit_fn,
                 lot_size=1.0, max_positions=1,
                 strategy=DEFAULT_STRATEGY, loss_policy=DEFAULT_LOSS_POLICY,
                 close_on_stop=False, account_id="", timeframe=DEFAULT_TIMEFRAME):
        self.user_id       = user_id
        self.symbol        = symbol
        self.mode          = mode           # "demo" or "live"
        self.emit          = emit_fn
        self.lot_size      = float(lot_size)
        self.max_positions = int(max_positions)
        self.strategy      = strategy
        self.loss_policy   = loss_policy
        self.close_on_stop = bool(close_on_stop)
        self.account_pref  = account_id or ""
        self.info          = strategy_info(strategy)
        self.timeframe     = timeframe if timeframe in ("M1", "M5") else "M1"
        # Candle Follow state (all per-bot, per-position)
        self.tracker       = DirectionTracker(CF_CONFIRM_SEC)
        self.pnl_now       = {}      # trade id -> live profit reported by Deriv
        self.pnl_peak      = {}      # trade id -> best profit seen while open
        self._selling      = set()   # trade ids with a sell in flight (no double sells)
        self._sell_block   = {}      # trade id -> (retry_at, failures)
        self._entry_block_until = 0.0
        self._follow_busy  = False
        self._follow_dirty = False
        self._last_follow_emit = 0.0
        self._pos_task     = None

        self.running       = False
        self._stop_requested = False
        self._thread       = None
        self.broker        = DerivBroker(mode, self.account_pref)
        self.risk          = None
        self.loop          = None
        self.candles: Dict[str, List[dict]] = defaultdict(list)
        self._lock         = None
        self._last_tick    = time.time()
        self._last_eval_epoch  = None
        self._last_order_epoch = None
        self._was_disconnected = False

        self.state         = "stopped"
        self.state_msg     = "Bot not started"
        self.state_detail  = ""
        self.floating_pnl  = 0.0
        self.balance_pending = False

    # ── status ───────────────────────────────────────────────
    @property
    def alive(self):
        return self._thread is not None and self._thread.is_alive()

    def _set_state(self, state, msg, detail=""):
        changed = (state, msg, detail) != (self.state, self.state_msg, self.state_detail)
        self.state, self.state_msg, self.state_detail = state, msg, detail
        if changed:
            self._emit("bot_status", self._status_payload())

    def _status_payload(self):
        return {"state": self.state, "message": self.state_msg, "detail": self.state_detail,
                "mode": self.mode, "account_id": self.broker.account_id,
                "account_type": self.broker.account_type}

    def applied_config(self):
        return {"symbol": self.symbol, "mode": self.mode, "lot_size": self.lot_size,
                "max_positions": self.max_positions, "strategy": self.strategy,
                "loss_policy": self.loss_policy, "close_on_stop": self.close_on_stop,
                "account_id": self.account_pref, "timeframe": self.timeframe}

    def status_dict(self):
        """The REAL state of the worker (never claims 'running' unless it is)."""
        state = self.state
        if not self.alive:
            state = state if state in ("stopped", "error") else ("stopped" if self._stop_requested else "error")
        elif state in ("running", "paused") and not self._stop_requested and not self.broker.connected:
            state = "reconnecting"
        d = {"state": state, "message": self.state_msg, "detail": self.state_detail,
             "worker_alive": self.alive, "connected": bool(self.broker.connected),
             "account_id": self.broker.account_id, "account_type": self.broker.account_type,
             "currency": self.broker.currency, "balance": self.broker.balance,
             "balance_pending": self.balance_pending,
             "paused_reason": self.risk.pause_reason if self.risk else ""}
        d.update(self.applied_config())
        return d

    # ── lifecycle ────────────────────────────────────────────
    def start(self):
        if self.alive:
            raise RuntimeError("A worker is already running for this bot — refusing to start a duplicate")
        self.running = True
        self._stop_requested = False
        self._set_state("starting", "Starting bot...")
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="bot-user-{}".format(self.user_id))
        self._thread.start()
        log.info("Bot started | user=%s symbol=%s mode=%s strategy=%s stake=%.2f maxpos=%d",
                 self.user_id, self.symbol, self.mode, self.strategy,
                 self.lot_size, self.max_positions)

    def stop(self, wait=True, timeout=25):
        """Stop policy: no new entries immediately; open contracts are left to run to
        expiry (and are reconciled on the next start) unless the user enabled
        'close open trades on stop'."""
        self._stop_requested = True
        self._set_state("stopping", "Stopping bot...")
        loop = self.loop
        if loop is not None and loop.is_running():
            try:
                if self.close_on_stop:
                    asyncio.run_coroutine_threadsafe(
                        self._close_all_open("Closed because the bot was stopped"), loop).result(timeout=20)
                asyncio.run_coroutine_threadsafe(self._final_reconcile(), loop).result(timeout=15)
            except Exception as e:
                log.error("Stop sequence error: %s", e)
            self.running = False
            try:
                asyncio.run_coroutine_threadsafe(self.broker.disconnect(), loop)
            except Exception:
                pass
        else:
            self.running = False
        if wait and self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)
        if not self.alive:
            self._set_state("stopped", "Bot stopped")
        log.info("Bot stopped | user=%s symbol=%s", self.user_id, self.symbol)

    def update_settings(self, lot_size=None, max_positions=None, loss_policy=None, close_on_stop=None,
                        timeframe=None):
        """Apply validated changes to the RUNNING bot; used for the next order onwards."""
        if lot_size is not None:
            self.lot_size = float(lot_size)
        if max_positions is not None:
            self.max_positions = max(1, min(int(max_positions), MAX_OPEN_TRADES))
        if loss_policy is not None:
            self.loss_policy = loss_policy
        if close_on_stop is not None:
            self.close_on_stop = bool(close_on_stop)
        if timeframe in ("M1", "M5") and timeframe != self.timeframe:
            self.timeframe = timeframe
            self.tracker.reset()               # a new timeframe needs its own confirmation
            log.info("Timeframe changed to %s", timeframe)
        if self.risk:
            self.risk.update_settings(lot_size, max_positions)
        log.info("Settings applied | stake=%.2f maxpos=%d loss_policy=%s close_on_stop=%s",
                 self.lot_size, self.max_positions, self.loss_policy, self.close_on_stop)

    def _run_loop(self):
        """Self-healing loop: retries with backoff until stop() is called."""
        backoff = 5
        while not self._stop_requested:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            detail = ""
            try:
                self.loop.run_until_complete(self._main())
                backoff = 5
            except ConnectionError as e:
                detail = str(e) or "Connection error"
                log.error("Connection error: %s", detail)
            except Exception as e:
                detail = str(e) or repr(e) or type(e).__name__
                log.error("Bot session error: %s", detail, exc_info=True)
            finally:
                try:
                    self.loop.close()
                except Exception:
                    pass

            if self._stop_requested:
                break

            if detail:
                self._set_state("error", detail, detail)
                # Keep the REAL reason visible while we wait to retry
                msg = "Retrying in {}s".format(backoff)
                self._set_state("reconnecting", msg, detail)
            else:
                self._set_state("reconnecting", "Worker restarting in {}s".format(backoff))
            log.warning("Bot loop ended — auto-restarting in %ds (reason: %s)", backoff, detail or "none")
            for _ in range(backoff):
                if self._stop_requested:
                    break
                time.sleep(1)
            backoff = min(backoff * 2, 60)
            self.running = True

        self.running = False
        self._set_state("stopped", "Bot stopped")

    async def _main(self):
        self._lock = asyncio.Lock()
        # Fresh broker + candle state on every (re)start: the old socket and
        # half-filled candle lists are dead after a crash.
        self.broker = DerivBroker(self.mode, self.account_pref)
        self.candles = defaultdict(list)
        self._last_eval_epoch = None
        self._was_disconnected = False

        self._set_state("starting", "Connecting to Deriv ({})...".format(self.mode.upper()))
        await self.broker.connect(self._on_candle_tick)

        # Verify the REAL account type reported by Deriv before anything else.
        expected = "real" if self.mode == "live" else "demo"
        if self.broker.account_type != expected:
            await self.broker.disconnect()
            raise ConnectionError(
                "Account verification failed: expected a {} account but Deriv reports '{}'. "
                "No orders will be placed.".format(self.mode.upper(), self.broker.account_type))

        self.risk = RiskManager(
            balance=self.broker.balance, user_id=self.user_id, mode=self.mode,
            lot_size=self.lot_size, max_positions=self.max_positions)
        self._emit_balance()

        await self._load_history()
        for tf in [EXECUTION_TF] + ANALYSIS_TF:
            await self.broker.subscribe_candles(self.symbol, tf)
        self._last_tick = time.time()

        # Reconcile with the platform BEFORE any new trade is considered.
        async with self._lock:
            await self._reconcile_trades()
        await self._refresh_balance()

        self._set_state("running", self._running_message())
        self._emit_trade_update()
        self._emit_positions()
        await self._evaluate()
        self._pos_task = asyncio.ensure_future(self._positions_loop())
        if self.strategy == "CANDLE_FOLLOW":
            self._schedule_follow()

        last_slow = time.time()
        try:
            while self.running and not self._stop_requested:
                for _ in range(10):
                    if not self.running or self._stop_requested:
                        break
                    await asyncio.sleep(1)
                if not self.running or self._stop_requested:
                    break
                try:
                    await self._housekeeping(last_slow)
                    if time.time() - last_slow >= SLOW_EVERY_SEC:
                        last_slow = time.time()
                except Exception as e:
                    log.error("Housekeeping error: %s", e)
        finally:
            if self._pos_task is not None:
                self._pos_task.cancel()
                self._pos_task = None

    def _running_message(self):
        return "Bot running | {} | {} {} | {} | {} | Stake {:.2f} | Max open {}".format(
            self.symbol, self.mode.upper(), self.broker.account_id,
            self.info["title"], self.timeframe, self.lot_size, self.max_positions)

    async def _housekeeping(self, last_slow):
        now = time.time()
        if self.broker.auth_failed:
            raise ConnectionError("Deriv rejected the API token (expired or revoked). "
                                  "New orders are disabled until the token is fixed.")
        if not self.broker.connected:
            self._was_disconnected = True
            self._sync_state()
            return
        if self._was_disconnected:
            log.info("Connection restored — resyncing candles and reconciling trades")
            self._was_disconnected = False
            await self._poll_candles(count=60)
            async with self._lock:
                await self._reconcile_trades()
            await self._refresh_balance()
        if now - self._last_tick > STREAM_STALE_SEC:
            log.warning("No live candles for %ds — polling candles as fallback", int(now - self._last_tick))
            await self._poll_candles()
        if now - last_slow >= SLOW_EVERY_SEC:
            async with self._lock:
                await self._reconcile_trades()
            await self._refresh_balance()
        self._sync_state()

    def _sync_state(self):
        """Keep the reported state truthful: reconnecting / paused / running."""
        if self._stop_requested:
            return
        if not self.broker.connected:
            self._set_state("reconnecting", "Connection to Deriv lost — reconnecting...", self.state_detail)
            return
        if self.risk is not None:
            ok, why = self.risk.check()
            if not ok:
                self._set_state("paused", "Paused for risk protection", why)
                return
        self._set_state("running", self._running_message())

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
            return False                       # stale / out-of-order update
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
        self._set_state("starting", "Loading market data for {}...".format(self.symbol))
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
        """Called for every live `ohlc` message from Deriv (on the bot's event loop)."""
        try:
            symbol = (ohlc.get("symbol") or ohlc.get("underlying_symbol")
                      or ohlc.get("underlying") or self.symbol)
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

            if self.strategy == "CANDLE_FOLLOW":
                if tf == self.timeframe:
                    self._schedule_follow()          # every price update, in real time
            elif is_new and tf == EXECUTION_TF:
                asyncio.ensure_future(self._evaluate())
        except Exception as e:
            log.error("Candle tick error: %s", e)

    async def _poll_candles(self, count=5):
        """Pull the latest candles directly (fallback / resync after a reconnect)."""
        new_m1 = False
        for tf in [EXECUTION_TF] + ANALYSIS_TF:
            raw = await self.broker.get_candles(self.symbol, tf, count=count)
            for r in raw or []:
                c = self._clean(r)
                if self._ingest(tf, c) and tf == EXECUTION_TF:
                    new_m1 = True
            if raw:
                self._emit_candle_update(tf, self.candles[tf][-1])
                self._last_tick = time.time()
        if self.strategy == "CANDLE_FOLLOW":
            self._schedule_follow()
        elif new_m1:
            await self._evaluate()

    def _df(self, tf):
        """CLOSED candles only (the last stored item is still forming)."""
        closed = self.candles.get(tf, [])[:-1]
        if len(closed) < MIN_CANDLES:
            return None
        return pd.DataFrame(closed, columns=["epoch", "open", "high", "low", "close"])

    # ── Strategy / entries ───────────────────────────────────
    async def _evaluate(self):
        try:
            async with self._lock:
                await self._evaluate_locked()
        except Exception as e:
            log.error("Evaluate error: %s", e, exc_info=True)

    async def _evaluate_locked(self):
        if self.strategy == "CANDLE_FOLLOW":
            return                                   # handled live by _follow_step
        m1 = self._df("M1")
        m5 = self._df("M5")
        if m1 is None:
            return
        closed_epoch = int(m1.iloc[-1]["epoch"])
        if closed_epoch == self._last_eval_epoch:
            return                               # this closed candle was already handled
        self._last_eval_epoch = closed_epoch

        self._emit_analysis(m5, m1)
        await self._reconcile_trades()
        self._sync_state()
        if self._stop_requested or not self.broker.connected or self.broker.auth_failed:
            return

        signal = evaluate(m5, m1, self.symbol, self.strategy)
        if signal is None:
            return
        try:
            save_signal(self.symbol, EXECUTION_TF, signal.direction,
                        signal.ema_fast, signal.ema_slow, signal.rsi or 0.0, signal.close_price)
        except Exception as e:
            log.error("save_signal error: %s", e)

        # Profit-first sequential reversal: manage the opposite position first.
        if self.strategy == "REVERSAL_SCALPER":
            await self._reversal_exits(signal)

        ok, why = self._entry_allowed(signal, closed_epoch)
        if not ok:
            log.info("Signal %s not traded: %s", signal.direction, why)
            self._emit_signal(signal, False, why)
            return

        stake, err = self.risk.stake_checked()
        if err:
            log.error("Order refused: %s", err)
            self._emit("trade_error", {"message": "Order refused: " + err})
            self._emit_signal(signal, False, err)
            return

        self._last_order_epoch = closed_epoch
        opened = await self._open_position(signal, stake)
        self._emit_signal(signal, opened, "" if opened else "Order not placed")
        await self._refresh_balance()
        self._emit_trade_update()

    def _entry_allowed(self, signal, closed_epoch):
        """Every gate that must pass before an order may be sent. -> (ok, reason)"""
        if self._stop_requested or not self.running:
            return False, "Bot is stopping"
        if not self.broker.connected or self.broker.auth_failed:
            return False, "Not connected to Deriv"
        if get_unresolved_orders(self.user_id, self.mode):
            return False, "Waiting to reconcile an earlier order of unknown status"
        ok, why = self.risk.check()
        if not ok:
            return False, why
        if self._last_order_epoch == closed_epoch:
            return False, "An order was already sent for this candle"
        if self.risk.positions_available() < 1:
            return False, "Position limit reached ({}/{})".format(
                self.risk.positions_open(), self.max_positions)
        if not ALLOW_HEDGE:
            for t in get_active_trades(self.user_id, self.mode):
                if t["symbol"] == self.symbol and t["direction"] != signal.direction:
                    return False, "An opposite {} position is still open".format(t["direction"])
        return True, ""

    async def _open_position(self, signal, stake):
        """Write the order to the DB first, then send it. Returns True if confirmed open."""
        tid = open_trade(
            user_id=self.user_id, symbol=self.symbol, direction=signal.direction,
            lot_size=self.lot_size, stake=stake, entry=signal.close_price, sl=0, tp=0,
            contract_id="", mode=self.mode, position_num=self._free_slot(),
            trade_group=uuid.uuid4().hex[:12], target_price=0, r_distance=0,
            status="PENDING", account_id=self.broker.account_id, strategy=signal.strategy)
        try:
            contract = await self.broker.place_trade(self.symbol, signal.direction, stake)
        except OrderStatusUnknown as e:
            mark_trade(tid, "UNCONFIRMED", str(e))
            msg = ("Order status unknown ({}). New orders are paused until it is reconciled "
                   "with Deriv.".format(e))
            log.error(msg)
            self._emit("trade_error", {"message": msg})
            return False
        except Exception as e:          # anything unexpected: treat as unknown, never retry blindly
            mark_trade(tid, "UNCONFIRMED", "Unexpected error: {}".format(e))
            log.error("Unexpected error while ordering: %s", e, exc_info=True)
            self._emit("trade_error", {"message": "Unexpected error while placing the order — "
                                                   "it will be reconciled before any new order."})
            return False

        if not contract:
            reason = self.broker.last_error or "unknown error"
            mark_trade(tid, "REJECTED", reason)
            msg = "Trade NOT placed: {}".format(reason)
            log.error(msg)
            self._emit("trade_error", {"message": msg})
            return False

        cid = str(contract.get("contract_id", ""))
        buy_price = _num(contract.get("buy_price"), stake)
        if not update_trade(tid, contract_id=cid, status="OPEN", buy_price=buy_price):
            mark_trade(tid, "UNCONFIRMED", "Contract {} could not be recorded".format(cid))
            log.error("Contract %s opened on Deriv but could not be recorded — flagged UNCONFIRMED", cid)
            return False
        if contract.get("balance_after") is not None:
            self.broker.balance = _num(contract["balance_after"], self.broker.balance)
            self.risk.update_balance(self.broker.balance)
        return True

    # ── Reversal exits ───────────────────────────────────────
    async def _reversal_exits(self, signal):
        for t in get_open_trades(self.user_id, self.mode):
            if t["symbol"] != self.symbol or t["direction"] == signal.direction:
                continue
            info = await self.broker.get_contract(t["contract_id"])
            if not info or info.get("is_sold"):
                continue                                   # reconcile will settle it
            profit = _num(info.get("profit"))
            if profit > 0:
                await self._sell_position(t, "Profit taken on {} reversal signal".format(signal.direction))
            elif self.loss_policy == "close":
                await self._sell_position(t, "Loss closed on {} reversal (policy: close)".format(signal.direction))
            else:
                log.info("Reversal signal while trade #%s is losing (%.2f): holding to expiry (policy: hold)",
                         t["id"], profit)

    async def _sell_position(self, t, reason, fast=False):
        """Sell at market, then record ONLY Deriv's confirmed result.
        fast=True skips the pre-check so a reversal exit is sent without delay."""
        cid = str(t["contract_id"])
        if not fast:
            info = await self.broker.get_contract(cid)
            if info and "is_valid_to_sell" in info and not info.get("is_valid_to_sell"):
                log.info("Trade #%s cannot be sold early — it will run to expiry", t["id"])
                return False
        sold = await self.broker.close_trade(cid)
        if not sold:
            self._emit("trade_error", {"message": "Could not close trade #{}: {}".format(
                t["id"], self.broker.last_error or "refused by Deriv")})
            return False

        verify = await self.broker.get_contract(cid)
        buy_price = _num((verify or {}).get("buy_price"), _num(t.get("buy_price"), _num(t["stake"])))
        if verify and verify.get("is_sold"):
            pnl = _num(verify.get("profit"))
            sell_price = _num(verify.get("sell_price"), _num(sold.get("sold_for"), buy_price + pnl))
        elif sold.get("sold_for") is not None:
            sell_price = _num(sold.get("sold_for"))
            pnl = sell_price - buy_price
        else:
            log.warning("Trade #%s sell sent but no confirmed result yet — reconcile will confirm", t["id"])
            return False
        exit_p = _num((verify or {}).get("sell_spot") or (verify or {}).get("exit_tick"), _num(t["entry_price"]))
        recorded = self._record_close(t, pnl, exit_p, reason, sell_price)
        if sold.get("balance_after") is not None:
            self.broker.balance = _num(sold["balance_after"], self.broker.balance)
            self.broker.balance_ok = True
            if self.risk:
                self.risk.update_balance(self.broker.balance)
        return recorded

    async def _close_all_open(self, reason):
        async with self._lock:
            for t in get_open_trades(self.user_id, self.mode):
                if t["symbol"] == self.symbol:
                    try:
                        await self._sell_position(t, reason)
                    except Exception as e:
                        log.error("Could not close trade #%s on stop: %s", t["id"], e)

    async def _final_reconcile(self):
        async with self._lock:
            await self._reconcile_trades()

    # ── Candle Follow: live direction, immediate exits, confirmed entries ──
    def _schedule_follow(self):
        """Run one evaluation now; if prices arrive meanwhile, run once more afterwards."""
        if self._follow_busy:
            self._follow_dirty = True
            return
        self._follow_busy = True
        asyncio.ensure_future(self._follow_runner())

    async def _follow_runner(self):
        try:
            while True:
                self._follow_dirty = False
                try:
                    await self._follow_step()
                except Exception as e:
                    log.error("Candle Follow error: %s", e, exc_info=True)
                if not self._follow_dirty:
                    break
        finally:
            self._follow_busy = False

    def _follow_view(self, tf):
        """(zone, open, price, band) of the forming candle on `tf`, or None."""
        store = self.candles.get(tf, [])
        if len(store) < 6:
            return None
        cur, closed = store[-1], store[:-1]
        band = band_size(closed, CF_BAND_FRAC)
        return zone_of(cur["close"], cur["open"], band), cur["open"], cur["close"], band

    async def _follow_step(self):
        if self._stop_requested or not self.running:
            return
        view = self._follow_view(self.timeframe)
        if view is None:
            return
        zone, opn, price, band = view
        now = time.time()
        zone, confirmed, held = self.tracker.update(now, zone)
        self._emit_follow_analysis(now)

        # 1) EXIT FIRST, immediately, every position on its own.
        await self._follow_exits(zone)

        # 2) ENTRY only after the direction has been confirmed.
        if confirmed:
            await self._follow_entries(zone, price, opn, band, held)

    async def _follow_exits(self, zone):
        now = time.time()
        open_trades = [t for t in get_open_trades(self.user_id, self.mode) if t["symbol"] == self.symbol]
        todo = [(t, "Closed at once: candle turned {} against this {}".format(
            "bearish" if t["direction"] == "BUY" else "bullish", t["direction"]))
            for t in exits_for(zone, open_trades)]
        if CF_PROTECT_PROFIT:
            ids = {t["id"] for t, _ in todo}
            for t in open_trades:
                if t["id"] in ids:
                    continue
                if profit_turned_negative(self.pnl_peak.get(t["id"], 0.0), self.pnl_now.get(t["id"], 0.0)):
                    todo.append((t, "Profit protection: this {} was in profit and was turning into a loss".format(t["direction"])))
        jobs = []
        for t, reason in todo:
            if t["id"] in self._selling or now < self._sell_block.get(t["id"], (0, 0))[0]:
                continue
            self._selling.add(t["id"])
            jobs.append(self._follow_close(t, reason))
        if not jobs:
            return
        results = await asyncio.gather(*jobs, return_exceptions=True)
        if any(r is True for r in results):
            await self._refresh_balance()
            self._emit_trade_update()
            self._emit_positions()

    async def _follow_close(self, t, reason):
        """Close ONE position. A failure here never affects any other position."""
        try:
            ok = await self._sell_position(t, reason, fast=True)
            if ok:
                self._sell_block.pop(t["id"], None)
            else:
                fails = self._sell_block.get(t["id"], (0, 0))[1] + 1
                self._sell_block[t["id"]] = (time.time() + min(5 * fails, 60), fails)
            return ok
        except Exception as e:
            log.error("Close of trade #%s failed: %s", t["id"], e)
            fails = self._sell_block.get(t["id"], (0, 0))[1] + 1
            self._sell_block[t["id"]] = (time.time() + min(5 * fails, 60), fails)
            return False
        finally:
            self._selling.discard(t["id"])

    def _follow_entry_allowed(self, zone):
        now = time.time()
        if self._stop_requested or not self.running:
            return False, "Bot is stopping"
        if not self.broker.connected or self.broker.auth_failed:
            return False, "Not connected to Deriv"
        if now - self._last_tick > CF_MAX_TICK_AGE:
            return False, "Price feed is not live"
        if _in_daily_reset_window():
            return False, "Daily reset window"
        if now < self._entry_block_until:
            return False, "Waiting after a refused order"
        if get_unresolved_orders(self.user_id, self.mode):
            return False, "Waiting to reconcile an earlier order of unknown status"
        ok, why = self.risk.check()
        if not ok:
            return False, why
        if self.risk.positions_available() < 1:
            return False, "Position limit reached ({}/{})".format(self.risk.positions_open(), self.max_positions)
        if not ALLOW_HEDGE:
            for t in get_active_trades(self.user_id, self.mode):
                if t["symbol"] == self.symbol and t["direction"] != zone:
                    return False, "Opposite position still closing"
        return True, ""

    async def _follow_entries(self, zone, price, opn, band, held):
        async with self._lock:
            if self.tracker.zone != zone:
                return
            ok, why = self._follow_entry_allowed(zone)
            if not ok:
                return
            reason = ("Candle {} confirmed for {:.0f}s on {} (price {:.5f} vs candle open {:.5f}, band {:.5f})"
                      .format("bullish" if zone == "BUY" else "bearish", held, self.timeframe, price, opn, band))
            signal = Signal(zone, self.symbol, price, opn, price, reason, "CANDLE_FOLLOW", None)
            opened = 0
            while self.risk.positions_available() >= 1 and not self._stop_requested:
                if self.tracker.zone != zone:           # direction changed meanwhile -> stop opening
                    break
                stake, err = self.risk.stake_checked()
                if err:
                    self._entry_block_until = time.time() + CF_ENTRY_RETRY_SEC
                    self._emit("trade_error", {"message": "Order refused: " + err})
                    break
                placed = await self._open_position(signal, stake)
                if not placed:
                    self._entry_block_until = time.time() + CF_ENTRY_RETRY_SEC
                    break
                opened += 1
                await asyncio.sleep(0.2)
            if opened:
                log.info("SIGNAL %s | %s | opened %d position(s) | %s", zone, self.symbol, opened, reason)
                try:
                    save_signal(self.symbol, self.timeframe, zone, opn, price, 0.0, price)
                except Exception as e:
                    log.error("save_signal error: %s", e)
                self._emit_signal(signal, True, "{} position(s) opened".format(opened))
                await self._refresh_balance()
                self._emit_trade_update()
                self._emit_positions()

    def _emit_follow_analysis(self, now, force=False):
        if not force and now - self._last_follow_emit < 1.0:
            return
        self._last_follow_emit = now
        def card(tf):
            v = self._follow_view(tf)
            if v is None:
                return {"direction": "NONE", "a": 0, "b": 0, "c": 0}
            zone, opn, price, band = v
            return {"direction": zone, "a": round(opn, 5), "b": round(price, 5), "c": round(band, 5)}
        self._emit("analysis_update", {"m5": card("M5"), "m1": card("M1"),
                                       "labels": self.info["labels"], "rules": self.info["rules"],
                                       "strategy": self.info["title"], "kind": "follow",
                                       "timeframe": self.timeframe})

    # ── Per-position live monitoring ─────────────────────────
    async def _positions_loop(self):
        """Every few seconds: re-price ALL open positions together, record settled ones,
        apply profit protection, and refresh the dashboard's position list."""
        while self.running and not self._stop_requested:
            await asyncio.sleep(POSITION_POLL_SEC)
            if not self.running or self._stop_requested or not self.broker.connected:
                continue
            try:
                async with self._lock:
                    await self._reconcile_trades()
                if self.strategy == "CANDLE_FOLLOW" and CF_PROTECT_PROFIT:
                    await self._follow_exits(self.tracker.zone)
                self._emit_positions()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("Position monitor error: %s", e)

    def _free_slot(self):
        used = {int(t.get("position_num") or 0) for t in get_active_trades(self.user_id, self.mode)}
        for n in range(1, max(self.max_positions, 1) + 20):
            if n not in used:
                return n
        return 1

    def _emit_positions(self):
        slots = {}
        for t in get_active_trades(self.user_id, self.mode):
            n = int(t.get("position_num") or 0) or 1
            while n in slots:
                n += 1
            slots[n] = t
        rows = []
        for n in range(1, max(self.max_positions, max(slots) if slots else 0) + 1):
            t = slots.get(n)
            if t is None:
                rows.append({"slot": n, "empty": True})
                continue
            rows.append({"slot": n, "empty": False, "id": t["id"], "direction": t["direction"],
                         "symbol": t["symbol"], "stake": t["stake"], "status": t["status"],
                         "profit": self.pnl_now.get(t["id"]), "peak": self.pnl_peak.get(t["id"]),
                         "opened_at": t["opened_at"]})
        self._emit("positions_update", {"positions": rows, "max_positions": self.max_positions,
                                        "floating_pnl": self.floating_pnl})

    # ── Reconciliation / accounting ──────────────────────────
    def _record_close(self, t, pnl, exit_price, reason, sell_price=None):
        """Persist a CONFIRMED result exactly once. Returns True only the first time."""
        pnl = round(float(pnl), 2)
        status = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "EVEN")
        if not close_trade(t["id"], exit_price, pnl, status, reason, sell_price):
            return False                                   # duplicate event: already recorded
        self.pnl_now.pop(t["id"], None)
        self.pnl_peak.pop(t["id"], None)
        log.info("TRADE CLOSED #%s | %s | P&L %.2f | %s", t["id"], status, pnl, reason)
        self._emit("trade_closed", {"trade_id": t["id"], "pnl": pnl, "status": status, "reason": reason})
        return True

    async def _reconcile_trades(self):
        """Match every unresolved trade with what Deriv says. Results are recorded
        only when Deriv confirms the contract is finished."""
        now, dur = time.time(), _duration_seconds()
        floating, changed = 0.0, False

        if await self._resolve_unconfirmed():
            changed = True

        todo = []
        for t in get_reconcilable_trades(self.user_id, self.mode):
            if t["status"] in ("PENDING", "UNCONFIRMED"):
                continue
            cid = str(t["contract_id"] or "")
            if cid.startswith("DEMO_"):
                mark_trade(t["id"], "VOID", "Legacy simulated trade from an old version")
                changed = True
                continue
            if not cid:
                mark_trade(t["id"], "UNRECONCILED", "No contract id recorded")
                changed = True
                continue
            if not self.broker.connected:
                continue
            todo.append((t, cid))

        # Ask Deriv about every open position AT THE SAME TIME (independent, not one by one).
        infos = await asyncio.gather(*[self.broker.get_contract(cid) for _, cid in todo],
                                     return_exceptions=True) if todo else []
        for (t, cid), info in zip(todo, infos):
            if isinstance(info, Exception):
                info = {}
            if info and (info.get("is_sold") or info.get("status") in ("won", "lost")):
                pnl = _num(info.get("profit"))
                exit_p = _num(info.get("sell_spot") or info.get("exit_tick"), _num(t["entry_price"]))
                if self._record_close(t, pnl, exit_p, "Contract expired / settled by Deriv",
                                      _num(info.get("sell_price"), None) if info.get("sell_price") is not None else None):
                    changed = True
            elif info:
                if t["status"] == "UNRECONCILED":
                    update_trade(t["id"], status="OPEN")
                live = _num(info.get("profit"))
                floating += live
                self.pnl_now[t["id"]] = live
                self.pnl_peak[t["id"]] = max(self.pnl_peak.get(t["id"], live), live)
            else:
                opened = _opened_at_ts(t["opened_at"]) or now
                if t["status"] == "OPEN" and now - opened > dur * 3 + 300:
                    mark_trade(t["id"], "UNRECONCILED",
                               "Deriv returned no data for this contract — verify in the Deriv back office")
                    log.warning("Trade #%s could not be reconciled with Deriv", t["id"])
                    changed = True

        self.floating_pnl = round(floating, 2)
        if changed:
            await self._refresh_balance()
            self._emit_trade_update()

    async def _resolve_unconfirmed(self):
        """Orders whose submission outcome is unknown: look for them in Deriv's open
        contracts. They are attached if found, flagged UNRECONCILED if not, and never re-sent."""
        orders = get_unresolved_orders(self.user_id, self.mode)
        if not orders or not self.broker.connected:
            return False
        for t in orders:                                    # orphaned PENDING (crash mid-order)
            if t["status"] == "PENDING":
                mark_trade(t["id"], "UNCONFIRMED", "Bot restarted while this order was being sent")
        portfolio = await self.broker.get_portfolio()
        now, changed = time.time(), False
        for t in get_unresolved_orders(self.user_id, self.mode):
            created = _opened_at_ts(t["opened_at"]) or now
            age = now - created
            match = None
            for c in portfolio or []:
                cid = str(c.get("contract_id", ""))
                if not cid or contract_exists(cid):
                    continue
                want = "CALL" if t["direction"] == "BUY" else "PUT"
                sym = c.get("symbol") or c.get("underlying_symbol") or c.get("underlying")
                if (c.get("contract_type") == want
                        and (sym is None or sym == t["symbol"])
                        and abs(_num(c.get("buy_price")) - _num(t["stake"])) < 0.02
                        and _num(c.get("purchase_time"), created) >= created - 10):
                    match = c
                    break
            if match:
                if update_trade(t["id"], contract_id=str(match["contract_id"]), status="OPEN",
                                buy_price=_num(match.get("buy_price"))):
                    log.info("Order #%s reconciled: found Deriv contract %s", t["id"], match["contract_id"])
                    changed = True
            elif (portfolio is not None and age > UNRESOLVED_GRACE) or \
                 (portfolio is None and age > _duration_seconds() + UNRESOLVED_GRACE):
                mark_trade(t["id"], "UNRECONCILED",
                           "Order not found among Deriv's open contracts — verify in the Deriv back office")
                log.warning("Order #%s could not be matched with Deriv — flagged UNRECONCILED", t["id"])
                changed = True
        return changed

    async def _refresh_balance(self):
        """Authoritative balance from Deriv. If the refresh fails, say so — never invent one."""
        if self.risk is None:
            return
        bal = await self.broker.get_balance()
        self.balance_pending = not self.broker.balance_ok
        if not self.balance_pending:
            self.risk.update_balance(bal)
        self._emit_balance()

    # ── Dashboard events ─────────────────────────────────────
    def _emit(self, event, payload):
        try:
            self.emit(event, payload)
        except Exception as e:
            log.error("emit(%s) failed: %s", event, e)

    def send_snapshot(self):
        """Re-send everything the dashboard needs (called when a browser connects)."""
        try:
            self._emit("bot_status", self._status_payload() if self.alive else
                       {"state": self.status_dict()["state"], "message": self.state_msg,
                        "detail": self.state_detail, "mode": self.mode,
                        "account_id": self.broker.account_id, "account_type": self.broker.account_type})
            if self.risk:
                self._emit_balance()
            for tf in [EXECUTION_TF] + ANALYSIS_TF:
                c = list(self.candles.get(tf, []))
                if c:
                    self._emit_candles(tf, c[-200:])
            if self.strategy == "CANDLE_FOLLOW":
                self._emit_follow_analysis(time.time(), force=True)
            else:
                m1, m5 = self._df("M1"), self._df("M5")
                if m1 is not None:
                    self._emit_analysis(m5, m1)
            self._emit_trade_update()
            self._emit_positions()
        except Exception as e:
            log.error("Snapshot error: %s", e)

    def _emit_balance(self):
        exp = get_exposure(self.user_id, self.mode)
        bal = self.broker.balance
        self._emit("balance_update", {
            "balance":         round(bal, 2),
            "currency":        self.broker.currency,
            "balance_pending": self.balance_pending,
            "daily_pnl":       self.risk.daily_loss_pct() if self.risk else 0,
            "realized_today":  round(get_daily_pnl(self.user_id, self.mode), 2),
            "realized_total":  round(float(get_stats(self.user_id, self.mode)["total_pnl"] or 0), 2),
            "floating_pnl":    self.floating_pnl,
            "open_positions":  exp["open_positions"],
            "open_stake":      round(exp["open_stake"], 2),
            "account_id":      self.broker.account_id,
            "account_type":    self.broker.account_type,
            "mode":            self.mode,
            "lot_size":        self.lot_size,
            "max_positions":   self.max_positions,
        })

    def _emit_candles(self, tf, candles):
        self._emit("candles_init", {"tf": tf, "symbol": self.symbol, "candles": candles,
                                    "ema": {"fast": self.info["fast"], "slow": self.info["slow"]}})

    def _emit_candle_update(self, tf, candle):
        self._emit("candle_update", {"tf": tf, "symbol": self.symbol, "candle": candle})

    def _emit_trade_update(self):
        self._emit("trades_update", {
            "trades": get_recent_trades(self.user_id, 15, self.mode),
            "stats":  get_stats(self.user_id, self.mode),
        })

    def _emit_signal(self, signal, taken, note=""):
        self._emit("signal", {
            "symbol": self.symbol, "direction": signal.direction, "price": signal.close_price,
            "stake": self.lot_size, "lot_size": self.lot_size, "max_positions": self.max_positions,
            "reason": signal.reason, "taken": bool(taken), "note": note, "strategy": signal.strategy,
        })

    def _emit_analysis(self, m5_df, m1_df):
        def card(df):
            if df is None:
                return {"direction": "NONE", "a": 0, "b": 0, "c": 0}
            d = prepare(df, self.strategy)
            last = d.iloc[-1]
            third = float(last["rsi"]) if self.info["rsi"] else float(last["close"])
            return {"direction": ema_direction(d),
                    "a": round(float(last["ema_fast"]), 5),
                    "b": round(float(last["ema_slow"]), 5),
                    "c": round(third, 2 if self.info["rsi"] else 5)}
        self._emit("analysis_update", {"m5": card(m5_df), "m1": card(m1_df),
                                       "labels": self.info["labels"], "rules": self.info["rules"],
                                       "strategy": self.info["title"]})