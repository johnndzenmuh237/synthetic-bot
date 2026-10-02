"""
strategy.py — SIMPLIFIED single strategy: EMA10 / EMA20 crossover

Runs once per CLOSED M1 candle:
  BUY  (CALL): EMA10 crosses above EMA20 on the last closed M1 candle
  SELL (PUT) : EMA10 crosses below EMA20 on the last closed M1 candle
Optional M5 filter (config.USE_M5_FILTER): the M5 EMA10/EMA20 must point
the same way as the M1 cross.

The old multi-condition ClaudeFX pullback/structure logic is still in
indicators.py (unused) — it required ~7 conditions to line up at once,
which almost never happens on synthetic indices.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from indicators import compute_emas
from config import (
    EMA_TREND_FAST, EMA_TREND_SLOW, USE_M5_FILTER,
    DAILY_RESET_START_UTC, DAILY_RESET_END_UTC,
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


def _in_daily_reset_window(now=None):
    now = now or datetime.now(timezone.utc)
    hm = now.strftime("%H:%M")
    return hm >= DAILY_RESET_START_UTC or hm <= DAILY_RESET_END_UTC


def ema_direction(df) -> str:
    """'BUY' if EMA10 > EMA20, 'SELL' if below, else 'NONE'. df needs ema_fast/ema_slow."""
    last = df.iloc[-1]
    if last["ema_fast"] > last["ema_slow"]:
        return "BUY"
    if last["ema_fast"] < last["ema_slow"]:
        return "SELL"
    return "NONE"


def evaluate(m5_df, m1_df, symbol) -> Optional[Signal]:
    """m1_df / m5_df must contain CLOSED candles only (last row = last closed candle)."""
    if _in_daily_reset_window():
        return None
    if m1_df is None or len(m1_df) < EMA_TREND_SLOW + 3:
        return None

    m1 = compute_emas(m1_df, fast=EMA_TREND_FAST, slow=EMA_TREND_SLOW)
    prev, last = m1.iloc[-2], m1.iloc[-1]

    crossed_up   = prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]
    crossed_down = prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]
    if not (crossed_up or crossed_down):
        return None

    direction = "BUY" if crossed_up else "SELL"

    if USE_M5_FILTER:
        if m5_df is None or len(m5_df) < EMA_TREND_SLOW + 3:
            log.warning("M5 filter ON but M5 data not ready — taking the M1 cross without it")
        else:
            m5 = compute_emas(m5_df, fast=EMA_TREND_FAST, slow=EMA_TREND_SLOW)
            m5_dir = ema_direction(m5)
            if m5_dir != direction:
                log.info("M1 %s cross SKIPPED — M5 trend is %s", direction, m5_dir)
                return None

    close = float(last["close"])
    word = "above" if crossed_up else "below"
    reason = "M1 EMA{} crossed {} EMA{}{}".format(
        EMA_TREND_FAST, word, EMA_TREND_SLOW,
        " | M5 trend agrees" if USE_M5_FILTER else "")
    log.info("SIGNAL %s | %s | Close=%.5f | %s", direction, symbol, close, reason)
    return Signal(direction, symbol, close,
                  float(last["ema_fast"]), float(last["ema_slow"]), reason)
