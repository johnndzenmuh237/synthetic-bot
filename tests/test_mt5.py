"""MetaTrader 5 bot (mt5_bot.py) tests. Offline: a fake MT5 stands in for the terminal."""
import os, sys, unittest
from types import SimpleNamespace as NS
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import mt5_bot
from mt5_bot import Mt5Bot, Settings, classify
from ema_trend import prepare_trend, pullback_entry


def ramp(start, step, n):
    return [start + step * i for i in range(1, n + 1)]


FLAT = [100.0 + (0.05 if i % 2 else -0.05) for i in range(80)]
RUN = FLAT + ramp(FLAT[-1], 0.3, 40)
PULL = RUN + ramp(RUN[-1], -0.35, 8)
BOUNCE = PULL + ramp(PULL[-1], 0.3, 15)
CROSS = RUN + ramp(RUN[-1], -0.9, 40)
GRAN = 300
T0 = 1_800_000_000 // GRAN * GRAN


def frame(closes, end_bucket):
    n = len(closes)
    return pd.DataFrame({"epoch": [end_bucket - GRAN * (n - i) for i in range(n)], "open": closes,
                         "high": [c + 0.5 for c in closes], "low": [c - 0.5 for c in closes], "close": closes})


class FakeMT5(object):
    SYMBOL_TRADE_MODE_FULL = 4
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    ORDER_TIME_GTC = 0
    TRADE_RETCODE_DONE, TRADE_RETCODE_PLACED = 10009, 10008
    TIMEFRAME_M5 = 5

    def __init__(self):
        self.syms = {}            # name -> (path, mode)
        self.closes = {}          # name -> closes list
        self.bucket = T0 + 1000 * GRAN
        self.tick_time = self.bucket + 5
        self.price = {}           # optional override of the live price
        self.pos, self.sent, self.next = [], [], 100
        self.equity = 10000.0
        self.vol_min = 0.01

    def add(self, name, closes, path="", mode=4):
        self.syms[name] = (path, mode); self.closes[name] = list(closes)

    def advance(self):
        self.bucket += GRAN; self.tick_time = self.bucket + 5

    # --- api
    def last_error(self): return (0, "ok")
    def account_info(self): return NS(equity=self.equity, balance=self.equity, login=1, server="x", currency="USD", trade_mode=0)
    def symbols_get(self): return [NS(name=n, path=p, trade_mode=m) for n, (p, m) in self.syms.items()]
    def symbol_select(self, name, on): return True
    def symbol_info(self, name):
        return NS(name=name, point=0.01, digits=2, trade_tick_size=0.01, trade_tick_value=0.01, volume_min=self.vol_min,
                  volume_max=100.0, volume_step=0.01, trade_stops_level=0, spread=2, filling_mode=2)
    def symbol_info_tick(self, name):
        c = self.price.get(name, self.closes[name][-1])
        return NS(time=self.tick_time, bid=c, ask=c + 0.02)
    def copy_rates_from_pos(self, name, tf, start, count):
        df = frame(self.closes[name], self.bucket)
        rec = [(int(r.epoch), r.open, r.high, r.low, r.close, 1, 2, 0) for r in df.itertuples()]
        return np.array(rec, dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"),
                                    ("tick_volume", "i8"), ("spread", "i4"), ("real_volume", "i8")])
    def positions_get(self, symbol=None):
        return [p for p in self.pos if symbol is None or p.symbol == symbol]
    def order_send(self, req):
        self.sent.append(req)
        if req["action"] == self.TRADE_ACTION_DEAL and "position" not in req:
            self.next += 1
            self.pos.append(NS(ticket=self.next, symbol=req["symbol"], type=req["type"], volume=req["volume"],
                               price_open=req["price"], sl=req["sl"], tp=req["tp"], profit=0.0, magic=req["magic"]))
            return NS(retcode=10009, order=self.next, comment="")
        if req["action"] == self.TRADE_ACTION_DEAL:
            self.pos = [p for p in self.pos if p.ticket != req["position"]]
            return NS(retcode=10009, order=0, comment="")
        for p in self.pos:
            if p.ticket == req["position"]:
                p.sl = req["sl"]
        return NS(retcode=10009, order=0, comment="")


def first_buy_index():
    for n in range(100, len(BOUNCE) + 1):
        if pullback_entry(prepare_trend(frame(BOUNCE[:n], T0)))[0] == "BUY":
            return n
    raise AssertionError("no pullback BUY in the test series")


def make(closes, **kw):
    f = FakeMT5()
    f.add("Volatility 75 Index", closes, "Derived\\Synthetic")
    clock = kw.pop("clock", None)
    bot = Mt5Bot(f, Settings(**kw), clock=clock or (lambda: 0.0))
    bot.discover()
    return f, bot


class T200_Symbols(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify("Volatility 75 Index"), "synthetic")
        self.assertEqual(classify("Boom 1000 Index"), "synthetic")
        self.assertEqual(classify("Step Index"), "synthetic")
        self.assertEqual(classify("BTCUSD"), "crypto")
        self.assertEqual(classify("ETHUSD"), "crypto")
        self.assertEqual(classify("EURUSD"), "forex")
        self.assertEqual(classify("frxGBPJPY"[3:]), "forex")
        self.assertEqual(classify("XAUUSD"), "forex")
        self.assertEqual(classify("AAPL", "Stocks\\US"), None)
    def test_discover_by_group_and_trade_mode(self):
        f = FakeMT5()
        for n in ("Volatility 75 Index", "BTCUSD", "EURUSD", "AAPL"):
            f.add(n, FLAT * 3)
        f.add("Crash 500 Index", FLAT * 3, mode=0)                     # trading disabled: skipped
        allg = Mt5Bot(f, Settings()); self.assertEqual(allg.discover(), ["BTCUSD", "EURUSD", "Volatility 75 Index"])
        fx = Mt5Bot(f, Settings(groups=("forex",))); self.assertEqual(fx.discover(), ["EURUSD"])
        only = Mt5Bot(f, Settings(only=("AAPL",))); self.assertEqual(only.discover(), ["AAPL"])


class T201_Entries(unittest.TestCase):
    def setUp(self):
        self.n = first_buy_index()
    def test_buy_on_pullback_with_stop_and_target(self):
        f, bot = make(BOUNCE[:self.n])
        bot.step()
        self.assertEqual(len(f.pos), 1)
        p = f.pos[0]
        self.assertEqual(p.type, f.POSITION_TYPE_BUY)
        self.assertLess(p.sl, p.price_open); self.assertGreater(p.tp, p.price_open)
        self.assertAlmostEqual((p.tp - p.price_open) / (p.price_open - p.sl), 2.0, places=1)       # RR 2
        d = prepare_trend(frame(BOUNCE[:self.n], T0)); rng = float(d["rng"].iloc[-1])
        risk = p.price_open - p.sl
        self.assertGreaterEqual(risk, 1.5 * rng - 0.02); self.assertLessEqual(risk, 6 * rng + 0.02)
    def test_sell_mirror(self):
        f = FakeMT5()
        f.add("Volatility 75 Index", [200 - c for c in BOUNCE[:self.n]], "Derived")
        bot = Mt5Bot(f, Settings(), clock=lambda: 0.0); bot.discover(); bot.step()
        self.assertEqual(len(f.pos), 1); p = f.pos[0]
        self.assertEqual(p.type, f.POSITION_TYPE_SELL); self.assertGreater(p.sl, p.price_open); self.assertLess(p.tp, p.price_open)
    def test_no_entry_in_the_middle_of_a_running_trend(self):
        f, bot = make(RUN); bot.step(); self.assertEqual(f.pos, [])
    def test_no_duplicate_and_one_check_per_candle(self):
        f, bot = make(BOUNCE[:self.n]); bot.step(); bot.step(); bot.step()
        self.assertEqual(len(f.pos), 1)
        f.advance(); bot.step()                                          # new candle, same trade still open: no second one
        self.assertEqual(len(f.pos), 1)
    def test_dry_run_places_nothing(self):
        f, bot = make(BOUNCE[:self.n], dry_run=True); bot.step(); self.assertEqual(f.sent, [])
    def test_max_positions(self):
        f, bot = make(BOUNCE[:self.n], max_positions=0); bot.step(); self.assertEqual(f.pos, [])
    def test_stale_or_closed_market_is_skipped(self):
        f, bot = make(BOUNCE[:self.n])
        f.add("EURUSD", BOUNCE[:self.n])
        bot.discover()
        bot.step_time = None
        f.tick_time = f.bucket + 5
        orig = f.symbol_info_tick
        f.symbol_info_tick = lambda name: NS(time=f.tick_time - 7200, bid=1, ask=1) if name == "EURUSD" else orig(name)
        bot.step()
        self.assertTrue(all(p.symbol != "EURUSD" for p in f.pos))        # EURUSD's ticks are 2h old = closed

class T202_Sizing(unittest.TestCase):
    def test_risk_percent_sizing(self):
        n = first_buy_index()
        f, bot = make(BOUNCE[:n], risk_pct=1.0); bot.step()
        p = f.pos[0]; risk = p.price_open - p.sl
        loss = risk * p.volume                                           # tick value makes $1 per price unit per lot
        self.assertLessEqual(loss, 100.0 + 1e-6); self.assertGreater(loss, 90.0)
    def test_fixed_lot(self):
        n = first_buy_index()
        f, bot = make(BOUNCE[:n], lot=0.5); bot.step(); self.assertAlmostEqual(f.pos[0].volume, 0.5)
    def test_account_too_small_for_the_smallest_lot_is_skipped(self):
        n = first_buy_index()
        f, bot = make(BOUNCE[:n]); f.vol_min = 90.0; bot.step(); self.assertEqual(f.pos, [])


class T203_Management(unittest.TestCase):
    def _pos(self, f, buy=True, entry=110.0, sl=107.0, profit=5.0):
        f.next += 1
        f.pos.append(NS(ticket=f.next, symbol="Volatility 75 Index", type=0 if buy else 1, volume=1.0,
                        price_open=entry, sl=sl, tp=entry + 6, profit=profit, magic=mt5_bot.MAGIC))
        return f.pos[-1]
    def test_profitable_trade_closed_when_trend_turns(self):
        f, bot = make(CROSS); p = self._pos(f, profit=5.0); t = p.ticket
        bot.step(); self.assertNotIn(t, [x.ticket for x in f.pos])
    def test_losing_trade_keeps_its_stop_loss(self):
        f, bot = make(CROSS); p = self._pos(f, profit=-5.0); t = p.ticket
        bot.step(); self.assertIn(t, [x.ticket for x in f.pos])
    def test_strong_trend_keeps_a_profitable_trade(self):
        f, bot = make(BOUNCE); p = self._pos(f, profit=5.0); t = p.ticket
        bot.step(); self.assertIn(t, [x.ticket for x in f.pos])
    def test_break_even(self):
        f, bot = make(RUN); p = self._pos(f, entry=110.0, sl=107.0)
        f.price["Volatility 75 Index"] = 113.5                           # +3.5 = more than 1R (risk 3)
        bot.step()
        self.assertGreaterEqual(p.sl, 110.0)
        sl = p.sl; f.price["Volatility 75 Index"] = 116.0; f.advance(); bot.step()
        self.assertEqual(p.sl, sl)                                       # moved once only
    def test_no_break_even_before_1r(self):
        f, bot = make(RUN); p = self._pos(f, entry=110.0, sl=107.0)
        f.price["Volatility 75 Index"] = 111.0; bot.step(); self.assertEqual(p.sl, 107.0)


class T204_Session(unittest.TestCase):
    def test_session_end_banks_winners_and_stops_when_flat(self):
        t = {"now": 0.0}
        f, bot = make(RUN, hours="1", clock=lambda: t["now"])
        f.next += 1
        f.pos.append(NS(ticket=f.next, symbol="Volatility 75 Index", type=0, volume=1, price_open=110, sl=107, tp=116,
                        profit=4.0, magic=mt5_bot.MAGIC))
        bot.step(); self.assertFalse(bot.session_over)
        t["now"] = 3700.0; bot.step()
        self.assertTrue(bot.session_over); self.assertEqual(f.pos, []); self.assertTrue(bot.done)
    def test_losers_left_to_their_stop_after_session_end(self):
        t = {"now": 0.0}
        f, bot = make(RUN, hours="1", clock=lambda: t["now"])
        f.next += 1
        f.pos.append(NS(ticket=f.next, symbol="Volatility 75 Index", type=0, volume=1, price_open=110, sl=107, tp=116,
                        profit=-4.0, magic=mt5_bot.MAGIC))
        t["now"] = 3700.0; bot.step()
        self.assertEqual(len(f.pos), 1); self.assertFalse(bot.done)
    def test_no_new_trades_after_session_end(self):
        n = first_buy_index(); t = {"now": 0.0}
        f, bot = make(BOUNCE[:n], hours="1", clock=lambda: t["now"])
        t["now"] = 3700.0; bot.step(); self.assertEqual(f.pos, [])


if __name__ == "__main__":
    unittest.main()