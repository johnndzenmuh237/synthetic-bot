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
