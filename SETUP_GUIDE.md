# ⚡ SyntheticBot Pro — Complete Setup Guide
**Multi-Timeframe Triple EMA Bot | Deriv Volatility Indices | Web Dashboard**

---

## HOW THE BOT WORKS

| Timeframe | Role |
|-----------|------|
| **H4** | Sets the macro direction (BUY / SELL / consolidating) |
| **H1** | Confirms the direction + checks EMAs are spread (not tangled) |
| **M15** | Waits for price to touch the 21 EMA and reverse — this is where trades are placed |

**Rule:** If H1 or H4 shows consolidation (EMAs tangled, no clear direction) → bot does NOT trade. It waits until the trend is clear again.

**Exit:** When the 21 EMA crosses back through the 50 EMA on M15, OR when H1 starts consolidating → trade is closed automatically.

---

## STEP 1 — Get Your Deriv API Tokens

You need **two tokens** — one for Demo, one for Live.

1. Go to **https://app.deriv.com** and log in
2. Click your **profile icon** (top-right) → **Security & Limits → API Token**
3. Click **Create Token**
4. Name it `"Bot Demo"` → tick ✅ **Read** + ✅ **Trade** → Create
5. Copy the token — this is your **DEMO token**
6. Repeat for `"Bot Live"` → this is your **LIVE token**

---

## STEP 2 — Get Your Deriv App ID

1. Go to **https://developers.deriv.com**
2. Log in → click **Register Application**
3. Fill in any name (e.g. `SyntheticBot`) → Submit
4. Copy your **App ID**

> 💡 You can use the public App ID `1089` for testing, but use your own for any real usage.

---

## STEP 3 — Set Up Your .env File

Open the `.env` file in VS Code and fill in your details:

```
DERIV_APP_ID=your_app_id_here

DERIV_DEMO_TOKEN=your_demo_token_here
DERIV_LIVE_TOKEN=your_live_token_here

SECRET_KEY=pick-any-random-string-here-like-abc123xyz
FLASK_PORT=5000

MAX_RISK_PER_TRADE=1.0
MAX_DAILY_LOSS=5.0

LOG_LEVEL=INFO
```

---

## STEP 4 — Open VS Code Terminal

Press **Ctrl + `** (backtick) to open the VS Code terminal.

Navigate to your project folder:
```bash
cd synthetic-bot
```

---

## STEP 5 — Create Python Virtual Environment

```bash
# Windows
python -m venv venv
venv\Scripts\activate

# Mac / Linux
python3 -m venv venv
source venv/bin/activate
```

You'll see **(venv)** in your terminal — that means it's active.

---

## STEP 6 — Install Dependencies

```bash
pip install -r requirements.txt
```

This installs Flask, WebSockets, Pandas, and everything else. Takes about 1–2 minutes.

---

## STEP 7 — Create Required Folders

```bash
# Windows
mkdir data
mkdir logs

# Mac / Linux
mkdir -p data logs
```

---

## STEP 8 — Run the Web App

```bash
python app.py
```

You should see:
```
  🤖 Synthetic Bot Web App
  Open your browser → http://localhost:5000
```

---

## STEP 9 — Open in Your Browser

Go to: **http://localhost:5000**

You'll see the **login page**. Since this is your first time, click **"Create one"** to register an account with any username and password you like.

After registering, log back in.

---

## STEP 10 — Using the Dashboard

Once logged in you'll see the full trading dashboard:

### LEFT SIDEBAR
| Element | What to do |
|---------|-----------|
| **Account Mode** | Choose DEMO or LIVE — always start with DEMO |
| **Volatility Pair** | Click any pair to select it (R_50, R_75, etc.) |
| **Start Bot** | Click to activate the bot on the selected pair |

### MAIN AREA
| Element | Description |
|---------|-------------|
| **Live Chart** | Candlestick chart with EMA 21 (red), 50 (purple), 100 (green) |
| **M15 / H1 / H4 tabs** | Switch between timeframes on the chart |
| **H4 Analysis** | Shows H4 direction and EMA values |
| **H1 Analysis** | Shows H1 direction + whether market is trending or consolidating |
| **M15 Execution** | Shows bot entry status |
| **Signal Feed** | Live signals as they are generated |
| **Trade History** | All your trades with P&L |

---

## STEP 11 — Go Live (Only After Demo Testing!)

1. Test in **DEMO mode for at least 2 weeks**
2. When satisfied, open the sidebar → switch to **LIVE**
3. Make sure your Deriv live account has real funds
4. Select your pair → click **Start Bot**

---

## HOW TO STOP

- Click **Stop Bot** in the dashboard, OR
- Press **Ctrl + C** in the terminal

---

## HOW TO VIEW LOGS

In a second terminal:
```bash
# Windows PowerShell
Get-Content logs/bot.log -Wait

# Mac/Linux
tail -f logs/bot.log
```

---

## HOW TO CHECK TRADES IN DATABASE

```bash
# All trades
sqlite3 data/market_data.db "SELECT * FROM trades ORDER BY opened_at DESC LIMIT 20;"

# Today's P&L
sqlite3 data/market_data.db "SELECT SUM(pnl) FROM trades WHERE date(closed_at)=date('now');"
```

---

## TROUBLESHOOTING

| Problem | Fix |
|---------|-----|
| `ModuleNotFoundError` | Run `pip install -r requirements.txt` |
| `Auth failed` | Check your token in `.env` — no spaces, no quotes |
| Browser can't connect | Check `http://localhost:5000` — make sure `python app.py` is running |
| Charts not loading | Start the bot first — charts load from live Deriv data |
| No signals appearing | Normal. The bot only takes HIGH quality setups. H1 + H4 must both agree. |
| Port already in use | Change `FLASK_PORT=5001` in `.env` |

---

## PROJECT FILE OVERVIEW

```
synthetic-bot/
├── app.py                    ← Web server (Flask + SocketIO) — RUN THIS
├── bot_engine.py             ← Core trading engine per user session
├── strategy.py               ← Multi-TF Triple EMA logic
├── indicators.py             ← EMA calc, trend detection, entry filters
├── broker.py                 ← Deriv WebSocket API client
├── risk_manager.py           ← Position sizing + daily loss gate
├── database.py               ← SQLite: users, candles, signals, trades
├── config.py                 ← All settings in one place
├── logger.py                 ← Coloured logging
├── requirements.txt          ← Python packages
├── .env                      ← Your API credentials (keep private!)
├── templates/                ← HTML pages
│   ├── base.html
│   ├── login.html
│   ├── register.html
│   └── dashboard.html        ← Main trading interface
├── static/
│   ├── css/style.css         ← Dark trading theme
│   └── js/dashboard.js       ← Charts, SocketIO, bot controls
├── strategies/
│   ├── volatility_strategy.py
│   ├── breakout_strategy.py  ← Placeholder (not active)
│   └── martingale.py         ← Educational only — NOT active
├── data/
│   └── market_data.db        ← Auto-created on first run
└── logs/
    └── bot.log               ← Auto-created on first run
```

---

## RISK REMINDER ⚠️

- **Always demo test for at least 2 weeks** before going live
- **1% per trade max** is the default — do not increase this
- The **5% daily loss limit** will automatically pause the bot
- Synthetic indices trade 24/7 — set daily limits and stick to them
- Past performance does not guarantee future results
```
