"""
ema_trend.py — EMA 10/20 trend-following decisions (pure functions, no network).

The strategy is TREND-driven, not crossover-event-driven:
  * every closed candle the bot asks "is there a clear, healthy trend right now?"
    — so a trend that was already running when the bot was started is traded at once;
  * a fresh crossover is just the moment such a trend first appears.

All distances and slopes are measured in AVERAGE CANDLE RANGES, so the same settings work on
any index and on both the 1-minute and the 5-minute candles.

BUY  trend : EMA10 above EMA20, both rising, gap wide enough and not shrinking.
SELL trend : EMA10 below EMA20, both falling, gap wide enough and not shrinking.
No trend   : flat averages, averages too close, or repeated crossings (consolidation).

Exit of an open trade (closed candles only):
  * the averages crossed against the trade                      -> exit
  * the gap has been shrinking from its recent peak for N candles in a row -> exit (convergence)
"""
import numpy as np

from config import (
    ET_FAST, ET_SLOW, ET_RANGE_PERIOD, ET_SLOPE_LOOKBACK, ET_MIN_SEP, ET_MIN_SLOPE,
    ET_WHIPSAW_LOOKBACK, ET_WHIPSAW_MAX, ET_ENTRY_KEEP, ET_CONVERGE_RATIO,
    ET_CONVERGE_CONFIRM, ET_PEAK_LOOKBACK,
)
from indicators import compute_emas

MIN_ROWS = ET_SLOW + ET_RANGE_PERIOD + 2


def prepare_trend(df):
    """Add ema_fast / ema_slow plus the trend measures (gap, slopes, ... in candle ranges)."""
    d = compute_emas(df, fast=ET_FAST, slow=ET_SLOW)
    rng = (d["high"] - d["low"]).rolling(ET_RANGE_PERIOD, min_periods=5).mean()
    tiny = float(d["close"].abs().mean() or 1.0) * 1e-9
    rng = rng.where(rng > tiny, tiny)
    k = max(ET_SLOPE_LOOKBACK, 1)
    d["rng"] = rng
    d["gap"] = (d["ema_fast"] - d["ema_slow"]) / rng                       # signed, in candle ranges
    d["slope_f"] = (d["ema_fast"] - d["ema_fast"].shift(k)) / k / rng
    d["slope_s"] = (d["ema_slow"] - d["ema_slow"].shift(k)) / k / rng
    d["rsi"] = float("nan")
    return d


def _crossings(d, lookback=ET_WHIPSAW_LOOKBACK):
    sign = np.sign(d["gap"].iloc[-(lookback + 1):].to_numpy())
    sign = sign[sign != 0]
    return int((np.diff(sign) != 0).sum()) if len(sign) > 1 else 0


def trend_state(d):
    """-> (state, reason). state is 'BUY', 'SELL' or 'NONE'. `d` comes from prepare_trend()."""
    if d is None or len(d) < MIN_ROWS:
        return "NONE", "Not enough candles yet"
    last = d.iloc[-1]
    gap, sf, ss = float(last["gap"]), float(last["slope_f"]), float(last["slope_s"])
    if np.isnan(gap) or np.isnan(sf) or np.isnan(ss):
        return "NONE", "Not enough candles yet"
    direction = "BUY" if gap > 0 else "SELL" if gap < 0 else "NONE"
    if direction == "NONE":
        return "NONE", "EMA10 and EMA20 are level - no direction"
    sign = 1.0 if direction == "BUY" else -1.0

    crosses = _crossings(d)
    if crosses > ET_WHIPSAW_MAX:
        return "NONE", "Consolidation: the averages crossed {} times in the last {} candles".format(
            crosses, ET_WHIPSAW_LOOKBACK)
    if abs(gap) < ET_MIN_SEP:
        return "NONE", "Averages too close together ({:.2f} candle ranges) - no clear trend".format(abs(gap))
    if sign * sf < ET_MIN_SLOPE or sign * ss < ET_MIN_SLOPE:
        return "NONE", "Averages are flat or not both pointing {} - no clear trend".format(
            "up" if direction == "BUY" else "down")

    # gaining direction / still healthy: the gap must not be shrinking
    k = max(ET_SLOPE_LOOKBACK, 1)
    ds = (sign * d["gap"]).to_numpy()
    before = ds[-1 - k]
    peak = float(np.nanmax(ds[-ET_PEAK_LOOKBACK:]))
    if ds[-1] < before * 0.98 or (peak > 0 and ds[-1] < ET_ENTRY_KEEP * peak):
        return "NONE", "{} trend is weakening (averages coming together) - not entering".format(
            "Up" if direction == "BUY" else "Down")

    why = "EMA{} {} EMA{}, gap {:.2f} candle ranges, both {} (slopes {:+.2f}/{:+.2f})".format(
        ET_FAST, "above" if direction == "BUY" else "below", ET_SLOW, abs(gap),
        "rising" if direction == "BUY" else "falling", sf, ss)
    return direction, why


def exit_decision(d, direction):
    """Should an open trade in `direction` be closed now? -> reason text, or None to keep it."""
    if d is None or len(d) < MIN_ROWS:
        return None
    sign = 1.0 if direction == "BUY" else -1.0
    ds = (sign * d["gap"]).to_numpy()
    if np.isnan(ds[-1]):
        return None
    if ds[-1] < 0:
        state, _ = trend_state(d)
        if state not in ("NONE", direction):
            return "Opposite {} trend confirmed - closing {}".format(state, direction)
        return "EMA10 crossed EMA20 against the {} - closing".format(direction)

    n = max(ET_CONVERGE_CONFIRM, 1)
    if len(ds) < n + ET_PEAK_LOOKBACK + 2:
        return None
    weak_rows = 0
    for i in range(1, n + 1):                       # the last n closed candles, newest first
        idx = len(ds) - i
        window = ds[max(0, idx - ET_PEAK_LOOKBACK + 1): idx + 1]
        peak = float(np.nanmax(window))
        shrinking = ds[idx] < ds[idx - 1]
        if peak >= ET_MIN_SEP and ds[idx] < ET_CONVERGE_RATIO * peak and shrinking:
            weak_rows += 1
    if weak_rows >= n:
        return "EMAs converging for {} candles (gap now {:.2f} from a peak of {:.2f} ranges) - protecting the trade".format(
            n, ds[-1], float(np.nanmax(ds[-ET_PEAK_LOOKBACK:])))
    return None