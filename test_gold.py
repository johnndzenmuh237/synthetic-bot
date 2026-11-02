"""T60: Gold (frxXAUUSD) trades through the same engine as every other market."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_follow as F
import test_bot as T
import database


class T60_Gold(unittest.TestCase):
    def test_candle_follow_places_and_closes_a_gold_trade(self):
        s = F.make_follow(maxpos=1)
        s.symbol = "frxXAUUSD"
        async def go():
            F.at(s, 0, 100.5); await s._follow_step(); F.at(s, 6, 100.5); await s._follow_step()
        F.run(s, go)
        self.assertEqual([p[0] for p in T.FakeBroker.placed], ["frxXAUUSD"])
        t = database.get_open_trades(s.user_id, "demo")[0]
        self.assertEqual(t["symbol"], "frxXAUUSD")
        async def close():
            return await s._sell_position(t, "test", fast=True)
        self.assertTrue(F.run(s, close))
    def test_market_closed_is_a_clean_refusal_not_a_crash(self):
        s = F.make_follow(maxpos=1)
        s.symbol = "frxXAUUSD"; s.broker.mode_behavior = "reject"      # fake broker: "Market is closed"
        async def go():
            F.at(s, 0, 100.5); await s._follow_step(); F.at(s, 6, 100.5); await s._follow_step()
        F.run(s, go)
        self.assertEqual(database.get_active_trades(s.user_id, "demo"), [])
        self.assertTrue(s.running)


if __name__ == "__main__":
    unittest.main()