# Deploying SyntheticBot Pro to the web (Render + GitHub)

This gets you a public HTTPS link (e.g. `https://synthetic-bot.onrender.com`)
that opens the same dashboard on your phone, tablet, or desktop — instead
of running it locally.

**Why not Vercel?** Vercel hosts static sites and short-lived serverless
functions. This app is a single Flask + Socket.IO process that keeps a
persistent WebSocket connection to Deriv open and runs a continuous
background trading loop — Vercel kills functions after seconds and can't
keep that connection or background loop alive. Render (or Railway/Fly.io)
runs your app as one continuously-running process instead, which is what
this needs.

---

## 1. Push the code to GitHub

```bash
git init
git add .
git commit -m "Initial commit"
```

Create a new **empty** repo on GitHub (don't add a README/license there —
avoids a merge conflict), then:

```bash
git remote add origin https://github.com/<your-username>/<your-repo>.git
git branch -M main
git push -u origin main
```

Your `.env` file and `data/` folder are excluded automatically by
`.gitignore` — your Deriv tokens never touch GitHub.

## 2. Create a Render account and connect the repo

1. Go to [render.com](https://render.com) and sign up (GitHub login is easiest).
2. Click **New +** → **Blueprint**.
3. Select your GitHub repo. Render reads `render.yaml` in this project
   automatically and sets up the web service for you, including generating
   a random `SECRET_KEY`.

## 3. Set your Deriv credentials as environment variables

In the Render dashboard → your service → **Environment**, fill in:

- `DERIV_APP_ID` — your own registered app_id from developers.deriv.com
  (not the shared `1089`)
- `DERIV_DEMO_TOKEN`
- `DERIV_LIVE_TOKEN`

These stay private to Render and are never committed to GitHub.

## 4. Deploy and lock the signup page

Render builds (`pip install -r requirements.txt`) and starts
(`python app.py`) automatically. Once live, you'll get a public URL.

Open it, register your one account, then go back to Render's Environment
tab and set `ALLOW_REGISTRATION` to `false`. **This step matters**: every
account that registers on this app shares the same Deriv token — anyone
who finds your link and signs up before you lock it can trade on your
Deriv account.

## 5. Open the link from any device

Same URL works on phone, tablet, or desktop — the dashboard is already
responsive (breakpoints at 1024px/768px/380px).

---

## Known limitation: no persistent disk on the free tier

Render's free plan has an ephemeral filesystem — `data/market_data.db`
(your local trade history) resets on redeploy or after the service sleeps
from inactivity and restarts. Your actual Deriv account balance and trades
are unaffected (Deriv is the source of truth), but the dashboard's local
history/stats would reset. Render's paid tier ($7/mo) adds a persistent
disk you can mount at `/data` if this matters to you.


---

## v2 notes: keeping the bot running, and being honest about limits

**Always-on hosting.** The worker lives inside the web process. It keeps trading when you log
out or close the browser, but only while that process is alive.
* Render **free** plan: sleeps after ~15 min idle and wipes its disk on every deploy, so the
  bot stops and users/settings/history disappear. Fine for a look around, not for trading.
* For continuous running use `render.always-on.yaml` (rename to `render.yaml`): paid plan,
  one instance, persistent disk at `/var/data` (`DB_PATH` points there). After a restart the
  supervisor resumes any bot that was running and reconciles open contracts before trading.
* Keep exactly **one** instance. Two instances would run two workers for the same user.
* Health check: `GET /healthz`.

**Configuring / operating**
1. Set `DERIV_APP_ID`, `DERIV_DEMO_TOKEN`, `DERIV_LIVE_TOKEN` (Read + Trade scopes) as environment variables.
2. Log in, choose DEMO, pick a pair, set *Stake per trade* and *Max open positions*, press **Save Settings**.
3. Press **Activate Bot**. Status shows Starting -> Running. Changes to stake / max positions /
   loss policy can be applied to the running bot with **Apply Changes**; pair, mode, strategy and
   account need Stop -> Activate.
4. **Stop Bot** stops new entries. Open contracts run to expiry unless *Close open trades on stop* is ticked.
5. LIVE: switch to LIVE and press Activate; tick the confirmation box the first time.

**What has not been verified**
* Nothing here was run against the real Deriv API (the test sandbox cannot reach it). Offline
  tests use a mock broker. Start on DEMO and watch the first trades.
* Deriv new-API details assumed from the legacy API: `portfolio` fields, early `sell` of
  Rise/Fall contracts, `proposal_open_contract` fields. If Deriv refuses an early sell, the
  trade just runs to expiry.
* All registered users share the tokens in the server environment. Set `ALLOW_REGISTRATION=false`
  after creating your login.
* No strategy is guaranteed profitable; EMA5/13/RSI7/50 are starting parameters.
