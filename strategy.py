"""
strategy.py — strategies the user can pick on the dashboard

REVERSAL_SCALPER (default)  — "Reversal Hedging Scalper", sequential version
    Indicators (config.py, all starting points for testing): EMA5, EMA13, RSI7, level 50
    BUY  state : EMA5 > EMA13  AND  RSI7 > 50  AND  close > EMA5
    SELL state : EMA5 < EMA13  AND  RSI7 < 50  AND  close < EMA5
    A signal fires only when the state CHANGES into BUY/SELL on a CLOSED M1 candle
    (a fresh turn, or the price resuming the trend after a pullback through EMA5) —
    never simply because a candle changed colour.
    Exit: when the opposite signal appears, an open position that is IN PROFIT is
    closed (sold at market), its confirmed result is recorded, and only then may
    the opposite trade be opened. A LOSING position follows the user's loss policy
    (hold to expiry, or close) — an opposite trade is never assumed to fix a loss.

EMA_CROSS — EMA10 crosses EMA20 on a closed M1 candle (optional M5 agreement).

Every function works on CLOSED candles only (last row = last closed candle).
"""
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from indicators import compute_emas, compute_rsi
from config import (
    REV_FAST_EMA, REV_SLOW_EMA, REV_RSI_PERIOD, REV_RSI_LEVEL, REV_USE_M5_FILTER,
    MAX_SIGNAL_AGE_SEC, EMA_TREND_FAST, EMA_TREND_SLOW, USE_M5_FILTER,
    DAILY_RESET_START_UTC, DAILY_RESET_END_UTC, DAILY_RESET_PAUSE, TIMEFRAMES, STRATEGIES, CF_CONFIRM_SEC,
)
from logger import log


@dataclass
class Signal:
    direction:   str
    symbol:      str
    close_price: float
    ema_fast:    float
    ema_slow:    float
    reason:      str
    strategy:    str = ""
    rsi:         Optional[float] = None


# ── Strategy metadata (drives chart lines, dashboard labels) ──
def strategy_info(name):
    if name == "CANDLE_FOLLOW":
        return {"name": name, "title": STRATEGIES.get(name, name),
                "fast": REV_FAST_EMA, "slow": REV_SLOW_EMA, "rsi": None,   # chart guide lines only
                "labels": {"a": "Open", "b": "Price", "c": "Band"},
                "rules": ["Follows the live candle of the selected timeframe",
                          "Entry only after the direction holds {:g}s".format(CF_CONFIRM_SEC),
                          "Exit immediately when the candle turns against the trade",
                          "Opposite entry only after its own confirmation"]}
    if name == "EMA_CROSS":
        return {"name": name, "title": STRATEGIES.get(name, name),
                "fast": EMA_TREND_FAST, "slow": EMA_TREND_SLOW, "rsi": None,
                "labels": {"a": "EMA{}".format(EMA_TREND_FAST),
                           "b": "EMA{}".format(EMA_TREND_SLOW), "c": "Close"},
                "rules": ["Entries on closed 1-minute candles",
                          "EMA{} crossing EMA{} triggers a trade".format(EMA_TREND_FAST, EMA_TREND_SLOW),
                          "Contracts run until expiry"]}
    return {"name": "REVERSAL_SCALPER", "title": STRATEGIES.get("REVERSAL_SCALPER", "Reversal Scalper"),
            "fast": REV_FAST_EMA, "slow": REV_SLOW_EMA, "rsi": REV_RSI_PERIOD,
            "labels": {"a": "EMA{}".format(REV_FAST_EMA), "b": "EMA{}".format(REV_SLOW_EMA),
                       "c": "RSI{}".format(REV_RSI_PERIOD)},
            "rules": ["Entries on closed 1-minute candles",
                      "EMA{}/EMA{} momentum + RSI{} vs {:g}".format(
                          REV_FAST_EMA, REV_SLOW_EMA, REV_RSI_PERIOD, REV_RSI_LEVEL),
                      "Profit-first exit on reversal, then switch"]}


def prepare(df, name):
    """Add the indicator columns this strategy needs (ema_fast, ema_slow, rsi)."""
    info = strategy_info(name)
    d = compute_emas(df, fast=info["fast"], slow=info["slow"])
    if info["rsi"]:
        d = compute_rsi(d, info["rsi"])
    else:
        d["rsi"] = float("nan")
    return d


def _in_daily_reset_window(now=None):
    if not DAILY_RESET_PAUSE:
        return False                     # no automatic pause around the daily reset (opt-in only)
    now = now or datetime.now(timezone.utc)
    hm = now.strftime("%H:%M")
    return hm >= DAILY_RESET_START_UTC or hm <= DAILY_RESET_END_UTC


def candle_is_fresh(m1_df, now=None, max_age=MAX_SIGNAL_AGE_SEC, gran=None):
    """The last CLOSED candle must have closed recently (market data is current).
    `gran` = candle length in seconds (default: 1 minute)."""
    if m1_df is None or len(m1_df) == 0:
        return False
    now = time.time() if now is None else now
    closed_at = int(m1_df.iloc[-1]["epoch"]) + (gran or TIMEFRAMES["M1"])
    return (now - closed_at) <= max_age


def ema_direction(df) -> str:
    """'BUY' if fast EMA > slow EMA, 'SELL' if below, else 'NONE' (needs ema_fast/ema_slow)."""
    last = df.iloc[-1]
    if last["ema_fast"] > last["ema_slow"]:
        return "BUY"
    if last["ema_fast"] < last["ema_slow"]:
        return "SELL"
    return "NONE"


# ── Reversal scalper ──────────────────────────────────────────
def reversal_state(row, level=REV_RSI_LEVEL):
    if row["ema_fast"] > row["ema_slow"] and row["rsi"] > level and row["close"] > row["ema_fast"]:
        return "BUY"
    if row["ema_fast"] < row["ema_slow"] and row["rsi"] < level and row["close"] < row["ema_fast"]:
        return "SELL"
    return "NONE"


def _evaluate_reversal(m5_df, m1_df, symbol, tf="M1"):
    need = max(REV_SLOW_EMA, REV_RSI_PERIOD) + 5
    if m1_df is None or len(m1_df) < need:
        return None
    d = prepare(m1_df, "REVERSAL_SCALPER")
    last, prev = d.iloc[-1], d.iloc[-2]
    now_state, before = reversal_state(last), reversal_state(prev)
    if now_state == "NONE" or now_state == before:
        return None                      # no fresh trigger on this candle

    if REV_USE_M5_FILTER and tf == "M1" and m5_df is not None and len(m5_df) >= need:
        m5dir = ema_direction(prepare(m5_df, "REVERSAL_SCALPER"))
        if m5dir != now_state:
            log.info("Reversal %s signal skipped — M5 trend is %s", now_state, m5dir)
            return None

    why = ("EMA{}{}EMA{} + RSI{}={:.1f} {} {:g} + close {} EMA{} (fresh {} trigger)".format(
        REV_FAST_EMA, ">" if now_state == "BUY" else "<", REV_SLOW_EMA,
        REV_RSI_PERIOD, float(last["rsi"]), ">" if now_state == "BUY" else "<", REV_RSI_LEVEL,
        "above" if now_state == "BUY" else "below", REV_FAST_EMA, now_state))
    log.info("SIGNAL %s | %s | Close=%.5f | %s", now_state, symbol, float(last["close"]), why)
    return Signal(now_state, symbol, float(last["close"]), float(last["ema_fast"]),
                  float(last["ema_slow"]), why, "REVERSAL_SCALPER", float(last["rsi"]))


# ── EMA cross ─────────────────────────────────────────────────
def _evaluate_cross(m5_df, m1_df, symbol, tf="M1"):
    if m1_df is None or len(m1_df) < EMA_TREND_SLOW + 3:
        return None
    m1 = prepare(m1_df, "EMA_CROSS")
    prev, last = m1.iloc[-2], m1.iloc[-1]
    up   = prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]
    down = prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]
    if not (up or down):
        return None
    direction = "BUY" if up else "SELL"

    if USE_M5_FILTER and tf == "M1":          # the M5 agreement filter only makes sense on M1
        if m5_df is None or len(m5_df) < EMA_TREND_SLOW + 3:
            log.warning("M5 filter ON but M5 data not ready — taking the M1 cross without it")
        else:
            m5dir = ema_direction(prepare(m5_df, "EMA_CROSS"))
            if m5dir != direction:
                log.info("M1 %s cross SKIPPED — M5 trend is %s", direction, m5dir)
                return None

    reason = "{} EMA{} crossed {} EMA{}{}".format(
        tf, EMA_TREND_FAST, "above" if up else "below", EMA_TREND_SLOW,
        " | M5 trend agrees" if (USE_M5_FILTER and tf == "M1") else "")
    log.info("SIGNAL %s | %s | Close=%.5f | %s", direction, symbol, float(last["close"]), reason)
    return Signal(direction, symbol, float(last["close"]), float(last["ema_fast"]),
                  float(last["ema_slow"]), reason, "EMA_CROSS", None)


def evaluate(m5_df, m1_df, symbol, strategy="REVERSAL_SCALPER", now=None, tf="M1") -> Optional[Signal]:
    """Return a Signal for the last closed M1 candle, or None.
    Stale data and the daily-reset window never produce a signal."""
    if strategy == "CANDLE_FOLLOW":
        return None            # decided live, tick by tick, in bot_engine (candle_follow.py)
    if _in_daily_reset_window():
        return None
    if not candle_is_fresh(m1_df, now, gran=TIMEFRAMES.get(tf, 60)):
        log.warning("Market data is stale — no signal evaluated")
        return None
    if strategy == "EMA_CROSS":
        return _evaluate_cross(m5_df, m1_df, symbol, tf)
    return _evaluate_reversal(m5_df, m1_df, symbol, tf)