"""Trade guard: $2 loss cap, profit lock, cooldown (all strategies)."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_bot as tb
import bot_engine
from database import get_open_trades, get_recent_trades

def get_trade(uid, tid):
    return [r for r in get_recent_trades(uid, 50) if r["id"] == tid][0]
D = bot_engine.BotSession.guard_decision


class T80_Decision(unittest.TestCase):
    def test_loss_cap(self):
        self.assertIsNone(D(10, 0.0, -1.99))
        self.assertIn("Loss cap", D(10, 0.0, -2.0))
        self.assertIn("Loss cap", D(10, 0.0, -5.0))
    def test_never_runs_from_plus6_to_minus5(self):
        self.assertIsNone(D(10, 6.0, 5.0))            # small wobble: keep running
        self.assertIsNone(D(10, 6.0, 4.01))
        self.assertIn("Profit locked", D(10, 6.0, 4.0))   # fell $2 from the peak: bank it
        self.assertIn("Profit locked", D(10, 6.0, 3.0))
    def test_keeps_running_while_climbing(self):
        peak = 0.0
        for p in (0.5, 1.2, 2.0, 3.5, 5.0, 7.5, 9.0):
            peak = max(peak, p); self.assertIsNone(D(10, peak, p))
    def test_small_profit_is_not_turned_into_a_loss(self):
        self.assertIn("Profit locked", D(10, 1.0, 0.5))   # peak 1.0 -> exit at +0.5, never negative
    def test_tiny_noise_before_arming_is_ignored(self):
        self.assertIsNone(D(10, 0.4, 0.0)); self.assertIsNone(D(10, 0.0, -1.0))
    def test_small_stake_scales(self):
        self.assertIn("Loss cap", D(1, 0.0, -0.5))        # $1 stake: cap is half the stake
        self.assertIsNone(D(1, 0.0, -0.4))


class T81_Engine(unittest.TestCase):
    def setUp(self):
        bot_engine.GUARD_ENABLED = True
        self.s = tb.make(lot=10.0, maxpos=3, strategy="REVERSAL_SCALPER")
    def _open(self, n=0):
        async def go():
            await tb.trade(self.s, "BUY", n)
        tb.run(self.s, go)
        t = get_open_trades(self.s.user_id, "demo")[-1]
        return t
    def _tick(self, t, profit):
        async def go():
            self.s.broker.contracts[str(t["contract_id"])]["profit"] = profit
            await self.s._reconcile_trades()
            await self.s._guard_exits()
        tb.run(self.s, go)
    def test_loss_cap_closes_and_cools_down(self):
        t = self._open()
        self._tick(t, -1.0); self.assertEqual(len(get_open_trades(self.s.user_id, "demo")), 1)
        self._tick(t, -2.3); self.assertEqual(len(get_open_trades(self.s.user_id, "demo")), 0)
        row = get_trade(self.s.user_id, t["id"]); self.assertEqual(row["status"], "LOSS"); self.assertAlmostEqual(row["pnl"], -2.3); self.assertIn("Loss cap", row["exit_reason"])
        ok, why = self.s._entry_allowed(tb.sig("BUY"), 5000)
        self.assertFalse(ok); self.assertIn("Cooling down", why)
    def test_profit_lock(self):
        t = self._open()
        for p in (2.0, 4.0, 6.0, 5.0):
            self._tick(t, p); self.assertEqual(len(get_open_trades(self.s.user_id, "demo")), 1, p)
        self._tick(t, 3.9)
        row = get_trade(self.s.user_id, t["id"]); self.assertEqual(row["status"], "WIN"); self.assertAlmostEqual(row["pnl"], 3.9)
        self.assertIn("Profit locked", row["exit_reason"])
    def test_winner_keeps_running(self):
        t = self._open()
        for p in (1, 2, 3, 4.5, 6, 8, 9.5):
            self._tick(t, p)
        self.assertEqual(len(get_open_trades(self.s.user_id, "demo")), 1)
    def tearDown(self):
        bot_engine.GUARD_ENABLED = False
    def test_off_by_default(self):
        import config; self.assertFalse(config.GUARD_ENABLED)
    def test_disabled(self):
        old = bot_engine.GUARD_ENABLED; bot_engine.GUARD_ENABLED = False
        try:
            t = self._open(); self._tick(t, -9.0)
            self.assertEqual(len(get_open_trades(self.s.user_id, "demo")), 1)
        finally:
            bot_engine.GUARD_ENABLED = old


if __name__ == "__main__":
    unittest.main()


P = bot_engine.BotSession.profit_decision


class T82_ProfitRules(unittest.TestCase):
    def test_runs_until_the_lock_level(self):
        for peak, p in ((0.0, -4.0), (0.0, 1.0), (1.5, 0.2), (2.39, 2.39), (2.39, 1.0)):
            self.assertIsNone(P(10, peak, p), (peak, p))          # below $2.40 nothing closes the trade
    def test_locked_trade_never_goes_red_and_exits_on_pullback(self):
        self.assertIsNone(P(10, 2.4, 2.4)); self.assertIsNone(P(10, 2.4, 2.2))
        self.assertIn("Profit locked", P(10, 2.4, 2.0))            # floor $2.00
        self.assertIn("Profit locked", P(10, 3.4, 2.4))            # peak 3.4 -> exit at 2.4
        self.assertIsNone(P(10, 3.4, 2.9))
        self.assertIn("Profit locked", P(10, 4.5, 3.5))
    def test_keeps_running_while_climbing_then_exits_at_target(self):
        peak = 0
        for p in (0.5, 2.4, 3.0, 3.8, 4.4, 4.9):
            peak = max(peak, p); self.assertIsNone(P(10, peak, p), p)
        self.assertIn("Profit target", P(10, 4.9, 5.0)); self.assertIn("Profit target", P(10, 5.3, 6.1))
    def test_amounts_scale_with_the_stake(self):
        self.assertIn("Profit target", P(5, 2.5, 2.5))             # $5 stake: target 2.50
        self.assertIsNone(P(5, 1.0, 1.0)); self.assertIn("Profit locked", P(5, 1.2, 1.0))


class T83_ProfitEngine(unittest.TestCase):
    def tearDown(self):
        bot_engine.PROFIT_RULES = False
    def setUp(self):
        bot_engine.PROFIT_RULES = True
        self.s = tb.make(lot=10.0, maxpos=3, strategy="REVERSAL_SCALPER")
    def _open(self):
        tb.run(self.s, lambda: tb.trade(self.s, "BUY", 0))
        return get_open_trades(self.s.user_id, "demo")[-1]
    def _tick(self, t, profit):
        async def go():
            self.s.broker.contracts[str(t["contract_id"])]["profit"] = profit
            await self.s._reconcile_trades()
            await self.s._profit_exits()
        tb.run(self.s, go)
    def _open_count(self):
        return len(get_open_trades(self.s.user_id, "demo"))
    def test_trade_runs_through_losses_and_small_profits(self):
        t = self._open()
        for p in (-6.0, -2.0, 0.5, 1.9, 2.3):
            self._tick(t, p); self.assertEqual(self._open_count(), 1, p)       # no loss cap, no early exit
    def test_lock_then_pullback_exits_in_profit_and_waits(self):
        t = self._open()
        for p in (1.0, 2.5, 3.4, 3.0): self._tick(t, p); self.assertEqual(self._open_count(), 1, p)
        self._tick(t, 2.4)
        self.assertEqual(self._open_count(), 0)
        row = get_trade(self.s.user_id, t["id"])
        self.assertEqual(row["status"], "WIN"); self.assertGreaterEqual(row["pnl"], 2.0)
        ok, why = self.s._entry_allowed(tb.sig("BUY"), 6000)
        self.assertFalse(ok); self.assertIn("Cooling down", why)
    def test_exit_at_five(self):
        t = self._open()
        for p in (2.5, 3.5, 4.5): self._tick(t, p)
        self.assertEqual(self._open_count(), 1)
        self._tick(t, 5.1)
        self.assertEqual(self._open_count(), 0); self.assertEqual(get_trade(self.s.user_id, t["id"])["status"], "WIN")
    def test_can_be_switched_off(self):
        bot_engine.PROFIT_RULES = False
        t = self._open(); self._tick(t, 7.0); self.assertEqual(self._open_count(), 1)
    def test_off_by_default(self):
        import config; self.assertFalse(config.PROFIT_RULES); self.assertFalse(config.LONG_TRADES); self.assertFalse(config.GUARD_ENABLED)


class T84_LiveAndDemoBehaveTheSame(unittest.TestCase):
    """Every rule must act identically on a Demo and a Live account."""
    def tearDown(self):
        bot_engine.PROFIT_RULES = False
    def _run(self, mode):
        bot_engine.PROFIT_RULES = True
        s = tb.make(mode=mode, lot=10.0, maxpos=3, strategy="REVERSAL_SCALPER")
        tb.run(s, lambda: tb.trade(s, "BUY", 0))
        t = get_open_trades(s.user_id, mode)[-1]
        def tick(p):
            async def go():
                s.broker.contracts[str(t["contract_id"])]["profit"] = p
                await s._reconcile_trades(); await s._profit_exits()
            tb.run(s, go)
        seen = []
        for p in (1.0, 2.5, 3.4, 3.0, 2.4):
            tick(p); seen.append(len(get_open_trades(s.user_id, mode)))
        row = get_trade(s.user_id, t["id"])
        ok, why = s._entry_allowed(tb.sig("BUY"), 7000)
        return seen, row["status"], row["pnl"], row["exit_reason"][:13], ok, why[:12]
    def test_demo_and_live_give_the_same_result(self):
        demo, live = self._run("demo"), self._run("live")
        self.assertEqual(demo, live)
        self.assertEqual(live[:4], ([1, 1, 1, 1, 0], "WIN", 2.4, "Profit locked"))