# Change summary (v2)

## What was wrong originally
Charts never loaded and no trades were placed because: candles were keyed by tick time (so
thousands of fake "candles" appeared and the strategy never saw closed candles), the strategy
required two strict rules to line up at once, demo mode could simulate trades, and
rejections were hidden. The Deriv tokens must also be valid (a 401 means expired/invalid).

## Backend
* **Strategy:** Reversal Hedging Scalper (EMA5 / EMA13 / RSI7, level 50 - configurable starting
  points, not verified-profitable settings). Signals fire only on a *fresh* trigger on a closed
  M1 candle. EMA10/20 cross kept as an alternative. Stale data never produces a signal.
* **Stake:** the stake you enter is the stake sent. It is never shrunk silently; if it is invalid
  or unaffordable the order is refused with a message.
* **Position cap:** counts every order occupying a slot (pending, unconfirmed, open).
* **Risk:** daily-loss limit, consecutive-loss limit, zero-balance guard, aggregate exposure check.
  Paused state is shown as "Paused for risk protection" with the reason.
* **Accounting:** Deriv's balance is authoritative; no simulated prices/results. Realized and
  floating P&L are separate. Results are written once (idempotent) and only when Deriv confirms.
  Unknown results are shown as UNRECONCILED rather than guessed.
* **Order safety:** order is saved as PENDING *before* sending. If the connection drops mid-order
  it becomes UNCONFIRMED, no new orders are sent until it is matched with Deriv's open contracts,
  and it is never re-sent.
* **Accounts:** the account type reported by Deriv is verified (DEMO vs LIVE) before trading;
  a mismatch aborts. Optional account picker. First live activation needs explicit confirmation.
* **Worker:** runs on a server thread independent of any browser; logout/closing the tab does not
  stop it. States: Starting, Running, Reconnecting, Paused, Stopping, Stopped, Error.
  The applied config is stored in `bot_state`; a supervisor restarts it after a server restart
  and never starts a duplicate. Stop leaves open contracts to expire unless "close on stop" is on.
* **Security:** tokens stay on the server; log output redacts `pat_...`, `otp=`, bearer tokens;
  per-user settings/trades; HttpOnly cookies; server-side validation of every setting.

## Dashboard
Stake and max-positions controls, mode, account, strategy, loss policy, close-on-stop,
Saved / Applied / Pending settings display, live-confirmation dialog, realized / floating P&L,
verified account label, error reasons. All emojis replaced with a bundled SVG icon sprite.

## Database changes (automatic migration on start)
New table `user_settings`; new columns on `bot_state` and `trades`; unique index on contract id.
Old simulated `DEMO_` trades are marked VOID.

## Tests
`python -m unittest discover -s tests -v` (39 offline tests, mock broker, temp database).
They do NOT contact Deriv.

## Known limitations
See DEPLOYMENT.md, "What has not been verified".

---

# v3 - Candle Follow strategy, timeframe, live positions list, chart hardening

## Strategy "Candle Follow" (new default for new users; pick it in the dashboard)
* Follows the candle that is forming right now on the chosen timeframe (1 minute or 5 minutes).
  Bullish = price above the candle open by more than a small band; bearish = below; inside the
  band = neutral (no direction, nothing opens, open trades are kept).
* Entry: the direction must hold without a break for `CF_CONFIRM_SEC` (default 5 s), then BUY/SELL
  opens. If it flips back before that, nothing opens.
* While the candle keeps going the trade's way the position stays open.
* Exit: the moment the candle turns against a position it is sold at once (no waiting, no
  hoping for recovery, profit or loss does not matter). Each position is judged and sold on its own;
  a position that cannot be sold does not stop the others (it is retried with a growing pause).
* After an exit the opposite trade opens only after its own confirmation (no instant flip).
* Profit protection (`CF_PROTECT_PROFIT`, default on): a position that was in profit and drops to
  zero or below is closed (checked every `POSITION_POLL_SEC`, default 3 s).
* Multiple positions: when a direction is confirmed, every free slot up to "Max open positions"
  is filled, so 7 positions = 7 simultaneous trades, each with its own slot number and live P&L.
  A slot freed by an expiring contract is refilled only while the direction is still confirmed.
* Band = `CF_BAND_FRAC` (default 0.10) x average high-low range of the last 20 closed candles.

## Timeframe
New "Timeframe the strategy follows" selector (1 minute / 5 minutes), saved per user, shown in
Saved/Applied/Pending, and can be applied to a running bot with "Apply Changes". It drives Candle
Follow. The two older EMA strategies still work on the 1-minute candle.

## Dashboard
* "Open Positions" panel: Position 1..N, direction, Running / Opening / Verifying, stake and live
  profit/loss, refreshed every few seconds; empty slots read "Waiting for entry".
* Charts: libraries load with fallbacks (local copy in `static/vendor/`, cdnjs, jsDelivr, unpkg);
  a lost first data batch is re-requested automatically; bad candle data is cleaned instead of
  breaking the chart; if a chart cannot be shown the box says why instead of staying blank.

## New settings (environment variables, all optional)
`CF_CONFIRM_SEC`, `CF_BAND_FRAC`, `CF_PROTECT_PROFIT`, `CF_ENTRY_RETRY_SEC`, `CF_MAX_TICK_AGE`,
`POSITION_POLL_SEC`.

## Honest limits
* Not run against the real Deriv API. Tested offline: 63 unit tests (20 for Candle Follow, incl. BUY
  and SELL reversal, 1M and 5M, 7 positions, a failed close, profit protection) and a headless-browser
  check of the dashboard script with stand-in chart/socket libraries. The real chart library could not
  be downloaded in the test environment, so real chart drawing has not been seen by me.
* "Immediately" means on the next price update from Deriv (about every 1-2 s) plus the time Deriv takes
  to accept the sell. Early selling pays the contract's current value, not the full payout, so frequent
  exits cost money; Deriv may also refuse an early sell on some contracts (then it runs to expiry).
* Filling all slots at once multiplies exposure: stake x positions in one direction at the same time.
* 7 positions re-priced every 3 s is about 140 requests a minute to Deriv; raise `POSITION_POLL_SEC`
  if Deriv ever answers with a rate-limit error.
* No strategy is guaranteed to be profitable. Test on DEMO first.

---

# v4 - no auto-pause, Close All Trades, all three strategies on both timeframes

* **No automatic pause.** The daily-loss limit, the losses-in-a-row limit, the zero-balance stop and the
  daily-reset-window block are OFF. They can be switched back on deliberately with
  `ENABLE_RISK_LIMITS=true` / `DAILY_RESET_PAUSE=true`. What can still stop an individual ORDER (not the
  bot): position cap, an order whose stake is more than the balance, a price feed older than
  `CF_MAX_TICK_AGE` seconds (Candle Follow), a short wait (`CF_ENTRY_RETRY_SEC`) after Deriv refused an
  order, and waiting for an order of unknown status to be matched with Deriv (prevents duplicates).
* **Close All Trades** button (with confirmation): closes every open trade now, each on its own. Tick
  "keep new trades on hold" to stop new entries until you press **Resume Trading** (the hold survives
  a server restart). Untick it and the bot keeps trading normally. Needs the bot to be running (it
  needs its Deriv connection).
* **All three strategies stay selectable** (Candle Follow, Reversal Scalper, EMA Cross) and all three now
  follow the 1-minute / 5-minute selector. Each is tested on BUY and SELL, on both timeframes.


## v5 — permanent accounts, full history, all Volatility indices
* **Accounts never expire.** Login cookie is now permanent (`SESSION_DAYS`, default 10 years) and renewed on every visit;
  only pressing Logout ends it. Accounts/settings/trades are mirrored to Firebase Realtime Database
  (`cloud_store.py`) and restored on start-up, so Render redeploys no longer wipe them. See `FIREBASE_SETUP.md`.
  Without `FIREBASE_SERVICE_ACCOUNT` the bot works exactly as before.
* **History kept forever.** New `/history` page: every trade with date/time, account, symbol, result and profit/loss;
  totals by day / month / year; filters (account, symbol, result, date range); CSV download; and an
  "Account activity" log (register, login, logout, settings saved, bot start/stop, close-all, resume).
  Nothing in the code ever deletes a trade or activity row.
* **More Volatility indices:** added 1HZ15V, 1HZ30V, 1HZ90V, 1HZ150V, 1HZ250V (now R_10/25/50/75/100 and
  1HZ10/15/25/30/50/75/90/100/150/250V).
* Trading logic, strategies and the existing dashboard are untouched.


## v6 — many users, one App ID, admin page
* Every user connects their OWN Deriv demo / live token on the dashboard ("Deriv connection"). The token is checked with
  Deriv, stored ENCRYPTED (`token_vault.py`, key = TOKEN_ENCRYPTION_KEY or SECRET_KEY), copied to Firebase encrypted, and
  never sent back to the browser (only the last 4 characters).
* The server's DERIV_DEMO_TOKEN / DERIV_LIVE_TOKEN now belong to the ADMIN only (ADMIN_USERNAMES; if empty, the first
  account ever created). A normal user without their own token cannot trade - they are never given the server's token.
* One App ID for everyone (DERIV_APP_ID). Each user's bot has its own Deriv connection, settings, positions and history.
* Admin console (see v7 for its hidden address): every account, token-connected flags, bot running, trade counts, demo/live P/L, last login;
  search/filter; View (recent trades + activity); Deactivate (stops their bot, optionally closes open trades, blocks login
  and all API use) and Activate. The supervisor never restarts a deactivated user's bot. Admins cannot be deactivated.
* MAX_CONCURRENT_BOTS (default 25) protects a small server. Fixed a race in the Firebase sync that could occasionally
  upload an older copy of a trade after a newer change.


## v7 — hidden admin console + installable apps
* The admin console is no longer part of the normal app. It lives at a secret address (`ADMIN_PATH` in Render, e.g.
  `/ctl-x7k29q`; if unset, an address is derived from SECRET_KEY and printed in the server log at start-up). There is no
  link to it anywhere, `/admin` no longer exists, and anyone who is not signed in to it gets a 404 on its API.
* Its own sign-in page (`<ADMIN_PATH>/login`): same username/password as your normal account, but only usernames in
  ADMIN_USERNAMES are accepted, it is a separate 12-hour session, and 5 wrong tries lock the sign-in for 5 minutes.
* Two separate installable apps (PWA): the trading app (scope `/`) and the admin console (scope `<ADMIN_PATH>/`), each with
  its own name, icon, colour and manifest. Install button on the login/register/dashboard pages (Chrome/Edge/Android),
  and the "Share -> Add to Home Screen" hint on iPhone. The service workers only cache icons/CSS/JS and an offline page -
  never pages, trading data or the API.