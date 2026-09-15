"""
indicators.py — ClaudeFX Trend Following v2.0 indicator engine

Trend timeframe (M5): EMA10 / EMA20 + swing structure (HH/HL or LH/LL)
Entry timeframe (M1):  EMA10 pullback confirmation (Setup A)
Optional filters:      ATR(14) adaptive stop-loss, ADX(14) > 20 chop filter

`market_direction` / `is_trending` are kept as the public names bot_engine.py
already imports (backward-compatible signature) — they now return the
ClaudeFX M5 trend instead of the old triple-EMA reading.
"""
import numpy as np
import pandas as pd

from config import (
    EMA_TREND_FAST, EMA_TREND_SLOW, EMA_ENTRY,
    EMA_SLOPE_LOOKBACK, SWING_ORDER, SWING_STRUCTURE_LOOKBACK,
    CROSSOVER_WHIPSAW_LOOKBACK, CROSSOVER_WHIPSAW_MAX,
    FLAT_EMA_THRESHOLD_PCT, PULLBACK_BODY_MIN_PCT,
    ATR_PERIOD, ADX_PERIOD, ADX_MIN, USE_ADX_FILTER,
    SPIKE_ATR_MULTIPLIER,
)


# ── EMAs ─────────────────────────────────────────────────────
def compute_emas(df, fast=EMA_TREND_FAST, slow=EMA_TREND_SLOW):
    """Adds ema_fast/ema_slow columns. Default spans are the M5 trend
    EMAs (10/20). Call with fast=slow=EMA_ENTRY for the M1 entry timeframe."""
    df = df.copy()
    df["ema_fast"] = df["close"].ewm(span=fast, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=slow, adjust=False).mean()
    return df


# ── ATR / ADX (optional filters + spike detection) ───────────
def _true_range(df):
    prev_close = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)


def compute_atr(df, period=ATR_PERIOD):
    df = df.copy()
    df["atr"] = _true_range(df).ewm(alpha=1 / period, adjust=False).mean()
    return df


def compute_adx(df, period=ADX_PERIOD):
    df = df.copy()
    up_move   = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm  = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    atr = _true_range(df).ewm(alpha=1 / period, adjust=False).mean()
    plus_di  = 100 * pd.Series(plus_dm,  index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)) * 100
    df["adx"] = dx.ewm(alpha=1 / period, adjust=False).mean().fillna(0)
    return df


def adx_ok(df, minimum=ADX_MIN):
    """Optional ADX>20 ranging-market filter — only enforced if USE_ADX_FILTER=True."""
    if not USE_ADX_FILTER:
        return True
    if len(df) < ADX_PERIOD + 2:
        return False
    d = compute_adx(df)
    return float(d["adx"].iloc[-1]) > minimum


# ── Trend condition helpers ───────────────────────────────────
def ema_slope_up(df, lookback=EMA_SLOPE_LOOKBACK):
    if len(df) < lookback + 1:
        return False
    return float(df["ema_slow"].iloc[-1]) > float(df["ema_slow"].iloc[-1 - lookback])


def ema_slope_down(df, lookback=EMA_SLOPE_LOOKBACK):
    if len(df) < lookback + 1:
        return False
    return float(df["ema_slow"].iloc[-1]) < float(df["ema_slow"].iloc[-1 - lookback])


def is_flat_ema(df, lookback=EMA_SLOPE_LOOKBACK, threshold_pct=FLAT_EMA_THRESHOLD_PCT):
    """EMA20 barely moved over `lookback` candles -> flat/ranging market."""
    if len(df) < lookback + 1:
        return True
    now, then = float(df["ema_slow"].iloc[-1]), float(df["ema_slow"].iloc[-1 - lookback])
    if then == 0:
        return True
    return abs(now - then) / abs(then) * 100 < threshold_pct


def whipsaw_crossovers(df, lookback=CROSSOVER_WHIPSAW_LOOKBACK):
    """Count EMA10/EMA20 crossovers within the last `lookback` candles
    (catches 'EMA10 crosses EMA20 repeatedly' chop)."""
    if len(df) < lookback + 1:
        return 0
    window = df.iloc[-(lookback + 1):]
    fast_above = window["ema_fast"] > window["ema_slow"]
    return int((fast_above != fast_above.shift(1)).iloc[1:].sum())


# ── Swing structure (HH/HL, LH/LL) ────────────────────────────
def find_swing_points(df, order=SWING_ORDER):
    """Fractal swing-high/low detection: a bar is a swing high if its high
    is the max of the `order` bars on each side of it (mirrored for lows).
    Returns (swing_highs, swing_lows) as [(bar_index, price), ...], oldest→newest."""
    highs, lows = [], []
    h, l = df["high"].values, df["low"].values
    n = len(df)
    for i in range(order, n - order):
        wh = h[i - order:i + order + 1]
        wl = l[i - order:i + order + 1]
        if h[i] == wh.max():
            highs.append((i, float(h[i])))
        if l[i] == wl.min():
            lows.append((i, float(l[i])))
    return highs, lows


def structure_is_bullish(df, lookback=SWING_STRUCTURE_LOOKBACK):
    """Last `lookback`+1 swing highs are higher highs AND swing lows are higher lows."""
    highs, lows = find_swing_points(df)
    if len(highs) < lookback + 1 or len(lows) < lookback + 1:
        return False
    rh = [p for _, p in highs[-(lookback + 1):]]
    rl = [p for _, p in lows[-(lookback + 1):]]
    return (all(rh[i] < rh[i + 1] for i in range(len(rh) - 1))
            and all(rl[i] < rl[i + 1] for i in range(len(rl) - 1)))


def structure_is_bearish(df, lookback=SWING_STRUCTURE_LOOKBACK):
    """Last `lookback`+1 swing highs are lower highs AND swing lows are lower lows."""
    highs, lows = find_swing_points(df)
    if len(highs) < lookback + 1 or len(lows) < lookback + 1:
        return False
    rh = [p for _, p in highs[-(lookback + 1):]]
    rl = [p for _, p in lows[-(lookback + 1):]]
    return (all(rh[i] > rh[i + 1] for i in range(len(rh) - 1))
            and all(rl[i] > rl[i + 1] for i in range(len(rl) - 1)))


def get_swing_low(df, lookback=20):
    return float(df["low"].iloc[-lookback:].min())


def get_swing_high(df, lookback=20):
    return float(df["high"].iloc[-lookback:].max())


# ── M5 trend (public entry points used by bot_engine.py) ─────
def m5_trend(df):
    """Returns 'BUY', 'SELL', or 'NONE' per the ClaudeFX M5 trend rules:
    EMA10/EMA20 stack + close beyond EMA20 + EMA20 sloping + HH/HL (or LH/LL)
    structure. 'If any condition fails: NO TRADE'."""
    direction, _ = m5_trend_debug(df)
    return direction


def m5_trend_debug(df):
    """Same logic as m5_trend, but also returns a short string explaining
    exactly which condition blocked a signal — used for diagnostic logging
    so 'why didn't it trade' is answerable instead of a silent black box."""
    min_len = EMA_TREND_SLOW + SWING_ORDER * 2 + SWING_STRUCTURE_LOOKBACK + 2
    if len(df) < min_len:
        return "NONE", "not enough M5 candles yet ({}/{})".format(len(df), min_len)
    if whipsaw_crossovers(df) > CROSSOVER_WHIPSAW_MAX:
        return "NONE", "too much EMA10/20 chop ({} crossovers)".format(whipsaw_crossovers(df))
    if is_flat_ema(df):
        return "NONE", "EMA20 is flat (ranging market)"
    if not adx_ok(df):
        return "NONE", "ADX filter failed (weak trend strength)"

    last = df.iloc[-1]
    ema_stack_up   = last["ema_fast"] > last["ema_slow"]
    ema_stack_down = last["ema_fast"] < last["ema_slow"]
    close_above    = last["close"] > last["ema_slow"]
    close_below    = last["close"] < last["ema_slow"]
    slope_up       = ema_slope_up(df)
    slope_down     = ema_slope_down(df)
    struct_bull    = structure_is_bullish(df)
    struct_bear    = structure_is_bearish(df)

    if ema_stack_up and close_above and slope_up and struct_bull:
        return "BUY", "all M5 BUY conditions met"
    if ema_stack_down and close_below and slope_down and struct_bear:
        return "SELL", "all M5 SELL conditions met"

    # Nothing matched — report which side was closer and what failed, so
    # logs show something actionable instead of just "NONE".
    if ema_stack_up:
        missing = []
        if not close_above: missing.append("close not above EMA20")
        if not slope_up: missing.append("EMA20 not sloping up")
        if not struct_bull: missing.append("no confirmed HH/HL structure")
        return "NONE", "leaning BUY but: " + ", ".join(missing)
    if ema_stack_down:
        missing = []
        if not close_below: missing.append("close not below EMA20")
        if not slope_down: missing.append("EMA20 not sloping down")
        if not struct_bear: missing.append("no confirmed LH/LL structure")
        return "NONE", "leaning SELL but: " + ", ".join(missing)
    return "NONE", "EMA10/EMA20 not clearly stacked either way"


def market_direction(df):
    """Kept for bot_engine.py compatibility — now returns the M5 ClaudeFX trend."""
    return m5_trend(df)


def is_trending(df):
    """Kept for bot_engine.py compatibility — True when M5 trend is BUY/SELL."""
    return m5_trend(df) != "NONE"


# ── Setup A: EMA Pullback (M1 entry) ──────────────────────────
def pullback_entry(m1_df, direction):
    """M1: price retraces to EMA10, confirmation candle closes back in the
    trend direction with a body > PULLBACK_BODY_MIN_PCT of its total range."""
    if len(m1_df) < EMA_ENTRY + 2:
        return False
    last, prev = m1_df.iloc[-1], m1_df.iloc[-2]
    e10 = float(last["ema_fast"])   # m1_df must be computed with fast=slow=EMA_ENTRY
    rng = float(last["high"] - last["low"])
    if rng <= 0:
        return False
    body_pct = abs(float(last["close"]) - float(last["open"])) / rng * 100
    if body_pct <= PULLBACK_BODY_MIN_PCT:
        return False

    if direction == "BUY":
        return (float(last["low"]) <= e10
                and last["close"] > last["open"]
                and last["close"] > e10
                and last["close"] > prev["close"])
    else:
        return (float(last["high"]) >= e10
                and last["close"] < last["open"]
                and last["close"] < e10
                and last["close"] < prev["close"])


# ── Exit condition (M5) ────────────────────────────────────────
def exit_signal(m5_df, direction):
    """Immediate-exit condition: EMA10/20 reverse cross, close beyond EMA20,
    or an opposite higher-high/lower-low forms."""
    if len(m5_df) < 3:
        return False
    prev, last = m5_df.iloc[-2], m5_df.iloc[-1]
    if direction == "BUY":
        cross        = prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]
        closed_below = last["close"] < last["ema_slow"]
        reversal     = structure_is_bearish(m5_df)
        return bool(cross or closed_below or reversal)
    else:
        cross        = prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]
        closed_above = last["close"] > last["ema_slow"]
        reversal     = structure_is_bullish(m5_df)
        return bool(cross or closed_above or reversal)


# ── Boom/Crash spike detection ────────────────────────────────
def candles_since_spike(df, atr_period=ATR_PERIOD, mult=SPIKE_ATR_MULTIPLIER, max_lookback=20):
    """How many candles ago the most recent spike candle occurred
    (0 = the spike is the current/last candle). Returns None if no
    spike found within max_lookback candles."""
    if len(df) < atr_period + max_lookback:
        return None
    d = compute_atr(df, atr_period)
    for i in range(1, max_lookback + 1):
        rng = float(d["high"].iloc[-i] - d["low"].iloc[-i])
        atr = float(d["atr"].iloc[-i - 1]) if len(d) > i else 0.0
        if atr > 0 and rng > mult * atr:
            return i - 1
    return None