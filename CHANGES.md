# What was fixed
1. Candles were keyed by tick `epoch` instead of `open_time` -> every tick became a fake candle (EMAs corrupted, strategy ran per tick, charts garbage).
2. A signal needed 2 free slots but dashboard default is 1 position -> bot returned before ever opening a trade.
3. Strategy needed ~7 conditions at once (EMA stack, slope, HH/HL swings, whipsaw, M1 pullback + 50% body). Now: ONE rule, EMA10/EMA20 cross on closed M1 candles (+ optional M5 filter, config.USE_M5_FILTER).
4. Dashboard: charts only sent once at start (missed if page opened later / server restarted); M1 and M5 charts overlapped in one div; symbol mismatch dropped data. Now a snapshot is sent whenever the browser connects.
5. Demo mode now places real contracts on your Deriv DEMO account (config.SIMULATE_DEMO=False). Results are settled from Deriv after the 15 min contract expires.
6. Trade rejections from Deriv now show on the dashboard (red banner) and in logs/bot.log.
7. Fallback polling if the live candle stream goes quiet; fresh broker on every auto-restart.
