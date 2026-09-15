"""
strategy.py — ClaudeFX Trend Following v2.0

M5 = trend timeframe (EMA10/EMA20 + HH/HL or LH/LL structure)
M1 = entry timeframe  (Setup A: EMA10 pullback)

Only Setup A (EMA Pullback) is implemented — see config.ENTRY_SETUP.
Setup B (Breakout Retest) needs a separate support/resistance engine
and is reserved for a future update.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from indicators import (
    compute_emas, m5_trend, m5_trend_debug, pullback_entry, exit_signal,
    candles_since_spike,
)
from config import (
    EMA_TREND_FAST, EMA_TREND_SLOW, EMA_ENTRY,
    STOP_LOSS_POINTS, STOP_LOSS_POINTS_DEFAULT,
    USE_ATR_SL, ATR_SL_MULTIPLIER, ATR_PERIOD,
    TP1_R, TP1_CLOSE_PCT, TP2_R, TP_MAX_R,
    BOOM_CRASH_SYMBOLS, SPIKE_WAIT_MIN_CANDLES, SPIKE_WAIT_MAX_CANDLES,
    DAILY_RESET_START_UTC, DAILY_RESET_END_UTC,
)
from logger import log


@dataclass
class Signal:
    direction:      str
    symbol:         str
    close_price:    float
    ema_fast:       float
    ema_slow:       float
    stop_loss:      float
    r_distance:     float   # |entry - stop_loss|, i.e. 1R in price units
    tp1_price:      float
    tp2_price:      float
    tp1_close_pct:  int
    reason:         str


def _in_daily_reset_window(now=None):
    """Skip trading during the 23:55-00:05 GMT daily reset window."""
    now = now or datetime.now(timezone.utc)
    hm = now.strftime("%H:%M")
    return hm >= DAILY_RESET_START_UTC or hm <= DAILY_RESET_END_UTC


def _stop_loss_distance(symbol, m5_df):
    if USE_ATR_SL:
        if len(m5_df) < ATR_PERIOD + 2:
            return None
        from indicators import compute_atr
        atr = float(compute_atr(m5_df, ATR_PERIOD)["atr"].iloc[-1])
        return atr * ATR_SL_MULTIPLIER if atr > 0 else None
    return STOP_LOSS_POINTS.get(symbol, STOP_LOSS_POINTS_DEFAULT)


def _boom_crash_ok(symbol, m5_df):
    """Boom/Crash rule: trade only after a spike, never immediately after —
    wait 5-10 candles for a base to form, then normal trend rules apply."""
    if symbol not in BOOM_CRASH_SYMBOLS:
        return True
    since = candles_since_spike(m5_df)
    if since is None:
        return False  # no recent spike to base off of — no trade
    return SPIKE_WAIT_MIN_CANDLES <= since <= SPIKE_WAIT_MAX_CANDLES


def evaluate(m5_df, m1_df, symbol):
    """Run the full ClaudeFX v2.0 decision chain. Returns a Signal or None.
    Logs a one-line diagnostic on every call — not just when a signal
    fires — so 'why hasn't it traded' is answerable from the logs instead
    of being a silent black box."""
    if _in_daily_reset_window():
        log.info("EVAL %s | skipped: inside daily reset window", symbol)
        return None

    m5 = compute_emas(m5_df, fast=EMA_TREND_FAST, slow=EMA_TREND_SLOW)
    m1 = compute_emas(m1_df, fast=EMA_ENTRY, slow=EMA_ENTRY)

    direction, trend_reason = m5_trend_debug(m5)
    if direction == "NONE":
        log.info("EVAL %s | M5 trend: NONE (%s)", symbol, trend_reason)
        return None

    if not _boom_crash_ok(symbol, m5):
        log.info("EVAL %s | M5 trend=%s but blocked: Boom/Crash spike-wait "
                 "filter not satisfied", symbol, direction)
        return None

    if not pullback_entry(m1, direction):
        log.info("EVAL %s | M5 trend=%s (%s) but no M1 EMA10 pullback "
                 "confirmation yet — waiting for entry setup",
                 symbol, direction, trend_reason)
        return None

    last  = m1.iloc[-1]
    close = float(last["close"])

    sl_distance = _stop_loss_distance(symbol, m5)
    if not sl_distance or sl_distance <= 0:
        log.info("EVAL %s | M5 trend=%s + M1 pullback confirmed, but "
                 "stop-loss distance is invalid (%s) — check STOP_LOSS_POINTS "
                 "for this symbol in config.py", symbol, direction, sl_distance)
        return None

    if direction == "BUY":
        stop_loss = close - sl_distance
        tp1 = close + TP1_R * sl_distance
        tp2 = close + TP2_R * sl_distance
        reason = "M5 EMA10>EMA20 + HH/HL structure | M1 EMA10 pullback confirmed"
    else:
        stop_loss = close + sl_distance
        tp1 = close - TP1_R * sl_distance
        tp2 = close - TP2_R * sl_distance
        reason = "M5 EMA10<EMA20 + LH/LL structure | M1 EMA10 pullback confirmed"

    log.info("SIGNAL %s | %s | Close=%.5f SL=%.5f TP1=%.5f TP2=%.5f (R=%.5f)",
              direction, symbol, close, stop_loss, tp1, tp2, sl_distance)

    return Signal(
        direction=direction, symbol=symbol, close_price=close,
        ema_fast=float(last["ema_fast"]), ema_slow=float(m5.iloc[-1]["ema_slow"]),
        stop_loss=stop_loss, r_distance=sl_distance,
        tp1_price=tp1, tp2_price=tp2, tp1_close_pct=TP1_CLOSE_PCT,
        reason=reason,
    )


def should_exit(m5_df, direction):
    """M5 trend-reversal exit condition (per '9. Exit Rules')."""
    m5 = compute_emas(m5_df, fast=EMA_TREND_FAST, slow=EMA_TREND_SLOW)
    if exit_signal(m5, direction):
        return True, "M5 EMA10/20 reverse cross, close beyond EMA20, or structure flipped"
    return False, ""


def max_r_price(direction, entry_price, r_distance):
    """3R hard cap price ('do not aim beyond 3R')."""
    if direction == "BUY":
        return entry_price + TP_MAX_R * r_distance
    return entry_price - TP_MAX_R * r_distance