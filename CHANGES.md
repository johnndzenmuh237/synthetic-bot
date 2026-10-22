# What was fixed

**Why no trades were placed**
1. Candles were keyed by the tick `epoch` (changes every tick) instead of Deriv's `open_time` -> every tick became a fake candle, EMAs were corrupted and the strategy ran on garbage.
2. A signal needed 2 free slots (old TP1/TP2 split) but the dashboard default is 1 position -> the bot returned before ever checking the strategy.
3. The strategy needed ~7 conditions at once (EMA stack, slope, HH/HL swings, whipsaw, M1 pullback, 50% candle body). Now ONE rule: EMA10 crosses EMA20 on a closed M1 candle (+ optional M5 filter, `USE_M5_FILTER` in config.py).
4. Deriv's NEW API names the instrument field `underlying_symbol` (old API: `symbol`). The bot now sends the new name and automatically falls back to the old one if Deriv rejects it.
5. Trade rejections from Deriv are now shown on the dashboard (red banner) and in logs/bot.log ("Trade NOT placed: <reason>").

**Why charts didn't load**
6. Charts were sent only once when the bot started. A snapshot is now sent whenever the browser connects (page open, refresh, Render wake-up, server restart).
7. M1 and M5 charts were drawn on top of each other in one div -> one pane each.
8. Page default pair (R_10 highlighted) did not match the JS default (R_50) so incoming candles were dropped.
9. Candle messages that use `underlying_symbol` instead of `symbol` are accepted.

**Other**
10. Demo mode now places real contracts on your Deriv DEMO account (`SIMULATE_DEMO = False` in config.py; set True for the old in-bot simulation).
11. Trades are settled from Deriv's real result after the 15 min contract expires; unresolved ones are marked VOID so slots never get stuck.
12. If the live candle stream goes quiet, candles are polled. A dropped connection that can't recover restarts the session with a fresh connection.
13. MAX_CONSECUTIVE_LOSSES 2 -> 5.
