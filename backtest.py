"""
backtest.py - replay the bot's REAL strategies on past candles and report the honest numbers.

It does NOT touch the running bot, the database or your Deriv account. It only reads market data.

What it does
  * downloads past candles for any Deriv symbol (R_75, R_50, frxEURUSD, cryBTCUSD, ...) or reads a CSV
  * walks forward one closed candle at a time, calling the same strategy.evaluate() the live bot calls
  * places a simulated Rise/Fall trade for each signal (BUY = Rise, SELL = Fall), one trade at a time
  * a trade is settled N candles later: win if price moved your way, a tie counts as a loss (like Deriv)
  * compares the win rate with the BREAK-EVEN win rate for the payout, and with a coin-flip control

Examples (run from the bot folder):
  python backtest.py                                  (EMA_TREND, R_75, M1, 10000 candles)
  python backtest.py --strategy ALL --symbol R_50
  python backtest.py --symbol frxEURUSD --tf M5 --candles 20000 --hold 3,6,12
  python backtest.py --csv mydata.csv --tf M1         (columns: epoch,open,high,low,close)
"""
import argparse
import asyncio
import csv
import json
import logging
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

import config
import strategy

logging.disable(logging.WARNING)            # the strategies log every signal; keep the report clean

STRATEGIES = ["EMA_TREND", "REVERSAL_SCALPER", "EMA_CROSS"]
WINDOW = config.CANDLE_COUNT                # the live bot analyses the last 300 closed candles
CACHE_DIR = "backtest_cache"
PAGE = 5000                                 # Deriv returns at most 5000 candles per request


# ── data ─────────────────────────────────────────────────────
async def _fetch_all(symbol, gran, total):
    import websockets
    urls = ["wss://ws.derivws.com/websockets/v3?app_id={}".format(config.DERIV_APP_ID),
            config.DERIV_WS_PUBLIC]
    last_err = None
    for url in urls:
        try:
            rows, end, req = [], "latest", 0
            async with websockets.connect(url, open_timeout=20, ping_interval=None) as ws:
                while len(rows) < total:
                    req += 1
                    await ws.send(json.dumps({
                        "ticks_history": symbol, "style": "candles", "granularity": gran,
                        "count": min(PAGE, total - len(rows)), "end": end, "req_id": req}))
                    resp = json.loads(await asyncio.wait_for(ws.recv(), 60))
                    if "error" in resp:
                        raise RuntimeError(resp["error"].get("message", "Deriv error"))
                    got = resp.get("candles") or []
                    if not got:
                        break
                    rows = got + rows
                    end = int(got[0]["epoch"]) - 1
                    print("  downloaded {} candles...".format(len(rows)), flush=True)
                    if len(got) < 2:
                        break
            if rows:
                return rows
        except Exception as e:                                   # try the next address
            last_err = e
    raise RuntimeError("Could not download candles from Deriv ({}). "
                       "Use --csv with your own data instead.".format(last_err))


def load_candles(args, gran):
    if args.csv:
        df = pd.read_csv(args.csv)
    else:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = os.path.join(CACHE_DIR, "{}_{}.csv".format(args.symbol, gran))
        df = None
        if os.path.exists(path) and not args.refresh:
            cached = pd.read_csv(path)
            if len(cached) >= args.candles:
                df = cached.tail(args.candles)
                print("Using saved data: {} ({} candles). Use --refresh to download again.".format(path, len(df)))
        if df is None:
            print("Downloading {} candles of {} ({}s) from Deriv...".format(args.candles, args.symbol, gran))
            rows = asyncio.run(_fetch_all(args.symbol, gran, args.candles))
            df = pd.DataFrame(rows)[["epoch", "open", "high", "low", "close"]]
            df.to_csv(path, index=False)
    for c in ("epoch", "open", "high", "low", "close"):
        if c not in df.columns:
            raise SystemExit("CSV needs the columns: epoch,open,high,low,close")
    df = df[["epoch", "open", "high", "low", "close"]].drop_duplicates("epoch").sort_values("epoch")
    df = df.astype(float).reset_index(drop=True)
    df["epoch"] = df["epoch"].astype("int64")
    return df


def build_m5(df):
    """5-minute candles built from 1-minute candles (only complete buckets) - for the M1 strategies' M5 filter."""
    b = (df["epoch"] // 300) * 300
    g = df.groupby(b)
    m5 = pd.DataFrame({"epoch": g["epoch"].first().index.values, "open": g["open"].first().values,
                       "high": g["high"].max().values, "low": g["low"].min().values,
                       "close": g["close"].last().values, "n": g["epoch"].count().values})
    return m5[m5["n"] == 5].drop(columns="n").reset_index(drop=True)


# ── signals ──────────────────────────────────────────────────
def collect_signals(df, name, tf, gran):
    """-> list of (candle_index, 'BUY'/'SELL') the live strategy would have fired on each closed candle."""
    m5 = build_m5(df) if (tf == "M1") else None
    m5_end = (m5["epoch"].to_numpy() + 300) if m5 is not None else None
    out = []
    first = strategy_min_rows(name)
    epochs = df["epoch"].to_numpy()
    for i in range(first, len(df)):
        win = df.iloc[max(0, i - WINDOW + 1): i + 1]
        now = float(epochs[i] + gran + 1)                       # right after this candle closed: data is fresh
        m5w = None
        if m5 is not None:
            k = int(np.searchsorted(m5_end, epochs[i] + gran, side="right"))   # M5 candles closed by now
            m5w = m5.iloc[max(0, k - WINDOW): k]
        try:
            sg = strategy.evaluate(m5w, win, "BT", name, now=now, tf=tf)
        except Exception:
            sg = None
        if sg is not None and sg.direction in ("BUY", "SELL"):
            out.append((i, sg.direction))
    return out


def strategy_min_rows(name):
    if name == "EMA_TREND":
        from ema_trend import MIN_ROWS
        return MIN_ROWS + 5
    return 120


# ── simulation ───────────────────────────────────────────────
def settle(df, signals, hold):
    """One trade at a time. -> list of (entry_index, direction, won)."""
    close = df["close"].to_numpy()
    trades, busy_until = [], -1
    for i, d in signals:
        if i <= busy_until or i + hold >= len(close):
            continue
        entry, exit_ = close[i], close[i + hold]
        won = (exit_ > entry) if d == "BUY" else (exit_ < entry)           # a tie loses, like Deriv
        trades.append((i, d, bool(won)))
        busy_until = i + hold
    return trades


def wilson(w, n, z=1.96):
    if n == 0:
        return 0.0, 1.0
    p = w / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return c - h, c + h


def coin_flip_rate(df, trades, hold, runs=300, seed=7):
    """What a random Rise/Fall on the SAME entry candles would have won - the control."""
    if not trades:
        return float("nan")
    close = df["close"].to_numpy()
    idx = np.array([t[0] for t in trades])
    up = close[idx + hold] > close[idx]
    down = close[idx + hold] < close[idx]
    rng = np.random.RandomState(seed)
    rates = []
    for _ in range(runs):
        pick_up = rng.rand(len(idx)) < 0.5
        rates.append(float(np.where(pick_up, up, down).mean()))
    return float(np.mean(rates))


def longest_losing_streak(trades):
    best = cur = 0
    for _, _, won in trades:
        cur = 0 if won else cur + 1
        best = max(best, cur)
    return best


def verdict(n, lo, hi, be):
    if n < 100:
        return "TOO FEW TRADES ({}) to say anything - need 100+, ideally 300+".format(n)
    if lo > be:
        return "possible edge (win rate is above break-even even at the low end) - re-test on different data before trusting it"
    if hi < be:
        return "LOSES MONEY: the win rate is below break-even"
    return "NO PROVEN EDGE: the win rate is indistinguishable from break-even/coin-flip"


# ── report ───────────────────────────────────────────────────
def report(df, name, tf, gran, holds, payout):
    be = 1.0 / (1.0 + payout)
    print("\n" + "=" * 78)
    print("{}  |  {} candles  |  break-even win rate {:.1f}% (payout {:.0f}%)".format(
        name, len(df), be * 100, payout * 100))
    print("=" * 78)
    sigs = collect_signals(df, name, tf, gran)
    print("signals found: {}".format(len(sigs)))
    if not sigs:
        print("  no signals - nothing to test on this data")
        return
    print("{:>6} {:>7} {:>8} {:>16} {:>9} {:>10} {:>9}".format(
        "hold", "trades", "win %", "95% range", "coin %", "ROI/trade", "worst run"))
    best = None
    for h in holds:
        tr = settle(df, sigs, h)
        n = len(tr)
        w = sum(1 for t in tr if t[2])
        if n == 0:
            print("{:>5}c {:>7}".format(h, 0))
            continue
        lo, hi = wilson(w, n)
        roi = (w * payout - (n - w)) / n
        coin = coin_flip_rate(df, tr, h)
        print("{:>5}c {:>7} {:>7.1f}% {:>7.1f}-{:<6.1f}% {:>8.1f}% {:>+9.1f}% {:>9}".format(
            h, n, 100.0 * w / n, lo * 100, hi * 100, coin * 100, roi * 100, longest_losing_streak(tr)))
        best = (h, n, lo, hi) if best is None or n > best[1] else best
    if best:
        print("verdict ({}c hold): {}".format(best[0], verdict(best[1], best[2], best[3], be)))


def main():
    ap = argparse.ArgumentParser(description="Backtest the bot's strategies on past candles (no real or demo trades).")
    ap.add_argument("--strategy", default="EMA_TREND", help="EMA_TREND, REVERSAL_SCALPER, EMA_CROSS or ALL")
    ap.add_argument("--symbol", default="R_75", help="Deriv symbol, e.g. R_75, R_50, frxEURUSD")
    ap.add_argument("--tf", default="M1", choices=sorted(config.TIMEFRAME_CHOICES), help="candle size: M1, M5 or M15")
    ap.add_argument("--candles", type=int, default=10000, help="how many past candles to test (default 10000)")
    ap.add_argument("--hold", default="1,3,5,10", help="trade length in candles; comma list to compare, e.g. 1,3,5,10")
    ap.add_argument("--payout", type=float, default=0.95, help="payout on a win as a fraction of the stake (default 0.95)")
    ap.add_argument("--csv", help="use your own candle file instead of downloading")
    ap.add_argument("--refresh", action="store_true", help="download again instead of using the saved file")
    args = ap.parse_args()

    gran = config.TIMEFRAMES[args.tf]
    holds = sorted({int(x) for x in args.hold.split(",") if x.strip()})
    names = STRATEGIES if args.strategy.upper() == "ALL" else [args.strategy.upper()]
    for n in names:
        if n not in STRATEGIES:
            raise SystemExit("Unknown strategy {}. Choose: {} or ALL".format(n, ", ".join(STRATEGIES)))

    df = load_candles(args, gran)
    span_h = (df["epoch"].iloc[-1] - df["epoch"].iloc[0]) / 3600.0
    print("Data: {} {} candles, about {:.0f} hours of market".format(len(df), args.tf, span_h))
    for n in names:
        report(df, n, args.tf, gran, holds, args.payout)
    print("\nHow to read this: 'win %' must be clearly above the break-even rate AND above the coin-flip column,")
    print("on 300+ trades, and the 95% range must sit fully above break-even. Otherwise the strategy has no edge")
    print("on this data. Past results never guarantee future ones. This tool places no trades.")


if __name__ == "__main__":
    main()