"""
candle_follow.py — "Candle Follow" direction logic (pure functions, no I/O).

The strategy follows the direction of the candle that is FORMING right now on the
selected timeframe (M1 or M5):

    price above the candle open by more than a small band  -> BULLISH zone
    price below the candle open by more than a small band  -> BEARISH zone
    price inside the band                                  -> NEUTRAL (no direction)

* ENTRY needs a CONFIRMED zone: the price must stay in the same zone, without a break,
  for CONFIRM_SEC seconds. Neutral or an opposite zone resets the timer.
* EXIT is IMMEDIATE: an open BUY is closed the moment the zone turns BEARISH, an open
  SELL the moment it turns BULLISH (no waiting). Neutral keeps positions open.
* After an exit the opposite trade is only opened after the opposite zone has itself
  been confirmed for CONFIRM_SEC — there is no instant BUY -> SELL flip.

The band is a fraction of the average recent candle range, so it scales with the
instrument and the timeframe. All numbers are configurable starting points
(config.py / environment), not tuned or proven settings.
"""
from typing import List, Optional


def band_size(closed_candles: List[dict], frac: float, lookback: int = 20) -> float:
    """Dead-band around the candle open = frac * average high-low range of recent
    CLOSED candles. Never zero (a flat market still needs a positive band)."""
    rng = [float(c["high"]) - float(c["low"]) for c in closed_candles[-lookback:]]
    rng = [r for r in rng if r > 0]
    if not rng:
        return 1e-9
    return max(frac * sum(rng) / len(rng), 1e-9)


def zone_of(price: float, candle_open: float, band: float) -> str:
    """'BUY' (bullish), 'SELL' (bearish) or 'NONE' (inside the band)."""
    if price > candle_open + band:
        return "BUY"
    if price < candle_open - band:
        return "SELL"
    return "NONE"


class DirectionTracker:
    """Remembers how long the current zone has lasted, to confirm entries."""

    def __init__(self, confirm_sec: float):
        self.confirm_sec = float(confirm_sec)
        self.zone = "NONE"
        self.since: Optional[float] = None

    def reset(self):
        self.zone, self.since = "NONE", None

    def update(self, now: float, zone: str):
        """Feed the current zone. Returns (zone, confirmed, seconds_in_zone)."""
        if zone != self.zone:
            self.zone, self.since = zone, now
        held = 0.0 if self.since is None else max(0.0, now - self.since)
        confirmed = zone in ("BUY", "SELL") and held >= self.confirm_sec
        return zone, confirmed, held


def exits_for(zone: str, trades: List[dict]) -> List[dict]:
    """Open trades that must be closed right now: BUYs in a bearish zone, SELLs in a
    bullish zone. Every trade is judged on its own."""
    if zone == "SELL":
        return [t for t in trades if t["direction"] == "BUY"]
    if zone == "BUY":
        return [t for t in trades if t["direction"] == "SELL"]
    return []


def profit_turned_negative(peak_profit: float, profit: float) -> bool:
    """A trade that was in profit must not be left to become a loss."""
    return peak_profit > 0 and profit <= 0