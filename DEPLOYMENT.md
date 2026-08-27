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
