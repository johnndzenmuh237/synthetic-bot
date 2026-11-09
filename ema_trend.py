"""
ema_trend.py — EMA 21/50 trend-following decisions (periods set by ET_FAST / ET_SLOW) (pure functions, no network).

The strategy is TREND-driven, not crossover-event-driven:
  * every closed candle the bot asks "is there a clear, healthy trend right now?"
    — so a trend that was already running when the bot was started is traded at once;
  * a fresh crossover is just the moment such a trend first appears.

All distances and slopes are measured in AVERAGE CANDLE RANGES, so the same settings work on
any index and on both the 1-minute and the 5-minute candles.

ENTRY = PULLBACK RETEST: inside a clear trend the bot waits for price to pull back to the fast EMA and
hold it (the retest candle), then enters. A trend that is already far advanced is NOT chased - it waits for the
next retest. A retest that has just happened (a few candles ago) is still taken.

BUY  trend : fast EMA above slow EMA, both rising, gap wide enough and not shrinking.
SELL trend : fast EMA below slow EMA, both falling, gap wide enough and not shrinking.
No trend   : flat averages, averages too close, or repeated crossings (consolidation).

Exit of an open trade (closed candles only):
  * the averages crossed against the trade                      -> exit
  * the gap has been shrinking from its recent peak for N candles in a row -> exit (convergence)
"""
import numpy as np

from config import (
    ET_FAST, ET_SLOW, ET_RANGE_PERIOD, ET_SLOPE_LOOKBACK, ET_MIN_SEP, ET_MIN_SLOPE,
    ET_WHIPSAW_LOOKBACK, ET_WHIPSAW_MAX, ET_ENTRY_KEEP, ET_CONVERGE_RATIO,
    ET_CONVERGE_CONFIRM, ET_PEAK_LOOKBACK, ET_EXIT_PEAK_LOOKBACK,
    ET_PULLBACK_LOOKBACK, ET_AWAY, ET_TOUCH_TOL, ET_RETEST_WINDOW, ET_MAX_CHASE, ET_PULLBACK_KEEP,
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
        return "NONE", "EMA{} and EMA{} are level - no direction".format(ET_FAST, ET_SLOW)
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


def pullback_entry(d):
    """-> (state, reason). 'BUY'/'SELL' only when a retest of the fast EMA has just happened inside a healthy trend."""
    if d is None or len(d) < MIN_ROWS:
        return "NONE", "Not enough candles yet"
    n = len(d)
    gap = d["gap"].to_numpy(); sf = d["slope_f"].to_numpy(); ss = d["slope_s"].to_numpy()
    if np.isnan(gap[-1]) or np.isnan(sf[-1]) or np.isnan(ss[-1]):
        return "NONE", "Not enough candles yet"
    direction = "BUY" if gap[-1] > 0 else "SELL" if gap[-1] < 0 else "NONE"
    if direction == "NONE":
        return "NONE", "EMA{} and EMA{} are level - no direction".format(ET_FAST, ET_SLOW)
    sign = 1.0 if direction == "BUY" else -1.0
    word = "up" if direction == "BUY" else "down"

    crosses = _crossings(d)
    if crosses > ET_WHIPSAW_MAX:
        return "NONE", "Consolidation: the averages crossed {} times in the last {} candles".format(crosses, ET_WHIPSAW_LOOKBACK)
    if abs(gap[-1]) < ET_MIN_SEP:
        return "NONE", "Averages too close together ({:.2f} candle ranges) - no clear trend".format(abs(gap[-1]))
    if sign * sf[-1] < ET_MIN_SLOPE * 0.5 or sign * ss[-1] < ET_MIN_SLOPE:
        return "NONE", "Averages are flat or not both pointing {} - no clear trend".format(word)
    ds = sign * gap
    peak = float(np.nanmax(ds[-ET_PEAK_LOOKBACK:]))
    if peak > 0 and ds[-1] < ET_PULLBACK_KEEP * peak:
        return "NONE", "{} trend is fading (averages coming together) - waiting".format(word.capitalize())

    close = d["close"].to_numpy(); high = d["high"].to_numpy(); low = d["low"].to_numpy()
    ef = d["ema_fast"].to_numpy(); rng = d["rng"].to_numpy()
    dist = sign * (close - ef) / rng                              # + = price on the trend side of EMA21, in ranges
    tol = ET_TOUCH_TOL
    # price must still be holding the trend side of EMA21 on every candle since the retest
    retest = None
    for back in range(0, ET_RETEST_WINDOW + 1):                  # newest first: the most recent retest
        i = n - 1 - back
        touched = (low[i] <= ef[i] + tol * rng[i]) if direction == "BUY" else (high[i] >= ef[i] - tol * rng[i])
        held = dist[i] >= -tol
        if not (touched and held):
            continue
        lo = max(0, i - ET_PULLBACK_LOOKBACK)
        if i - lo < 3 or float(np.nanmax(dist[lo:i])) < ET_AWAY:   # no leg away from EMA21 first = hugging it, not a pullback
            continue
        retest = i
        break
    if retest is None:
        if dist[-1] > ET_MAX_CHASE:
            return "NONE", "{} trend is already running ({:.1f} candle ranges from EMA{}) - waiting for a pullback to EMA{}".format(
                word.capitalize(), dist[-1], ET_FAST, ET_FAST)
        return "NONE", "{} trend - waiting for price to pull back to EMA{} and hold it".format(word.capitalize(), ET_FAST)
    if (dist[retest:] < -tol).any():
        return "NONE", "Price lost EMA{} after the retest - no entry".format(ET_FAST)
    if dist[-1] <= 0:
        return "NONE", "Retest in progress - waiting for the candle to close back {} EMA{}".format(
            "above" if direction == "BUY" else "below", ET_FAST)
    if dist[-1] > ET_MAX_CHASE:
        return "NONE", "Retest was {} candle(s) ago and price has moved {:.1f} ranges away - too late, waiting for the next pullback".format(
            n - 1 - retest, dist[-1])
    ago = n - 1 - retest
    why = "Pullback retest of EMA{} in an {} trend ({}), EMA{} {} EMA{}, gap {:.2f} ranges".format(
        ET_FAST, "up" if direction == "BUY" else "down", "just now" if ago == 0 else "{} candle(s) ago".format(ago),
        ET_FAST, "above" if direction == "BUY" else "below", ET_SLOW, abs(gap[-1]))
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
        return "EMA{} crossed EMA{} against the {} - closing".format(ET_FAST, ET_SLOW, direction)

    n = max(ET_CONVERGE_CONFIRM, 1)
    if len(ds) < n + ET_EXIT_PEAK_LOOKBACK + 2:
        return None
    weak_rows = 0
    for i in range(1, n + 1):                       # the last n closed candles, newest first
        idx = len(ds) - i
        window = ds[max(0, idx - ET_EXIT_PEAK_LOOKBACK + 1): idx + 1]
        peak = float(np.nanmax(window))
        shrinking = ds[idx] < ds[idx - 1]
        if peak >= ET_MIN_SEP and ds[idx] < ET_CONVERGE_RATIO * peak and shrinking:
            weak_rows += 1
    if weak_rows >= n:
        return "EMAs converging for {} candles (gap now {:.2f} from a peak of {:.2f} ranges) - protecting the trade".format(
            n, ds[-1], float(np.nanmax(ds[-ET_EXIT_PEAK_LOOKBACK:])))
    return None