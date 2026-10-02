"""config.py — Central configuration for SyntheticBot Pro"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Deriv API ────────────────────────────────────────────────
DERIV_APP_ID     = os.getenv("DERIV_APP_ID", "1089")
DERIV_DEMO_TOKEN = os.getenv("DERIV_DEMO_TOKEN", "")
DERIV_LIVE_TOKEN = os.getenv("DERIV_LIVE_TOKEN", "")

# Deriv's new-style "pat_..." Personal Access Tokens are REST Bearer
# tokens, NOT the old WebSocket `{"authorize": token}` credential.
# Flow: GET /accounts (Bearer) -> POST /accounts/{id}/otp (Bearer)
#       -> connect the returned wss:// URL directly (OTP embeds auth,
#       no `authorize` message is sent or needed).
# The old `wss://ws.derivws.com/websockets/v3?app_id=...` + `authorize`
# message flow only works with old-style short tokens, not `pat_...`
# tokens, and is kept below only as a legacy fallback reference.
DERIV_REST_BASE      = "https://api.derivws.com/trading/v1/options"
DERIV_WS_PUBLIC      = "wss://api.derivws.com/trading/v1/options/ws/public"
DERIV_OTP_TTL_SEC    = 120  # OTP is single-use and expires fast; fetch fresh on every connect

# Legacy fallback (only valid for old-format tokens, kept for reference)
DERIV_WS_URL     = "wss://ws.derivws.com/websockets/v3?app_id={}".format(DERIV_APP_ID)

# ── Volatility pairs + Boom/Crash ────────────────────────────
SYMBOLS = {
    "R_10":    "Volatility 10 Index",
    "R_25":    "Volatility 25 Index",
    "R_50":    "Volatility 50 Index",
    "R_75":    "Volatility 75 Index",
    "R_100":   "Volatility 100 Index",
    "1HZ10V":  "Volatility 10 (1s) Index",
    "1HZ25V":  "Volatility 25 (1s) Index",
    "1HZ50V":  "Volatility 50 (1s) Index",
    "1HZ75V":  "Volatility 75 (1s) Index",
    "1HZ100V": "Volatility 100 (1s) Index",
    "BOOM500":  "Boom 500 Index",
    "CRASH500": "Crash 500 Index",
}

# ── Timeframes ───────────────────────────────────────────────
# ClaudeFX Trend Following v2.0: M5 = trend timeframe, M1 = entry timeframe.
# M15/H1/H4 kept in the table (harmless, unused by the strategy) in case
# the dashboard chart lets a user pick a different display timeframe.
TIMEFRAMES = {
    "M1":  60,
    "M5":  300,
    "M15": 900,
    "H1":  3600,
    "H4":  14400,
}
ANALYSIS_TF  = ["M5"]     # trend timeframe
EXECUTION_TF = "M1"       # entry/candle-close timeframe
CANDLE_COUNT = 300

# ── ClaudeFX Trend Following v2.0 — EMAs ─────────────────────
EMA_TREND_FAST = 10   # M5 EMA10
EMA_TREND_SLOW = 20   # M5 EMA20
EMA_ENTRY      = 10   # M1 EMA10 (pullback level, Setup A)
# Alias kept so existing candle-history-length checks elsewhere
# (e.g. bot_engine._df) continue to work unchanged.
EMA_SLOW = EMA_TREND_SLOW

EMA_SLOPE_LOOKBACK         = 3    # candles back to measure EMA20 slope
SWING_ORDER                = 2    # bars either side to confirm a swing pivot
SWING_STRUCTURE_LOOKBACK   = 2    # compare last 2 swing highs/lows for HH/HL or LH/LL
CROSSOVER_WHIPSAW_LOOKBACK = 6    # "EMA10 crosses EMA20 repeatedly" filter window
CROSSOVER_WHIPSAW_MAX      = 2    # more crosses than this in the window = chop, skip
FLAT_EMA_THRESHOLD_PCT     = 0.02 # EMA20 move over lookback below this = "flat"

PULLBACK_BODY_MIN_PCT = 50        # Setup A confirmation candle body must be >50% of range
ENTRY_SETUP = "A"                 # "A" = EMA Pullback (implemented).
                                   # "B" = Breakout Retest — not implemented yet
                                   # (resistance/support levels need a separate
                                   # S/R engine); reserved for a future update.

# Optional filters from the strategy doc — off by default, matching "(Optional)"
ATR_PERIOD        = 14
ADX_PERIOD         = 14
ADX_MIN            = 20
USE_ADX_FILTER     = False
USE_ATR_SL         = False
ATR_SL_MULTIPLIER  = 1.5

# ── Stop loss (points = raw price distance; synthetic indices have no
# standard "pip" size, so these are used as direct price deltas) ────
STOP_LOSS_POINTS = {
    "R_75":     350,
    "R_100":    250,
    "BOOM500":  20,
    "CRASH500": 20,
}
STOP_LOSS_POINTS_DEFAULT = 300   # any symbol not listed above

# ── Position sizing / risk ───────────────────────────────────
RISK_PER_TRADE_PCT     = 1.0    # % of balance risked per trade
MAX_CONSECUTIVE_LOSSES = 5      # stop trading for the day after this many losses in a row

# ── Take profit — R multiples (R = initial stop-loss distance) ──
TP1_R          = 1.5
TP1_CLOSE_PCT  = 70     # % of the position closed at TP1
TP2_R          = 2.5
TP_MAX_R       = 3.0

# ── Boom/Crash spike handling ────────────────────────────────
BOOM_CRASH_SYMBOLS     = ("BOOM500", "CRASH500")
SPIKE_ATR_MULTIPLIER   = 3.0   # a candle whose range > this × ATR counts as "the spike"
SPIKE_WAIT_MIN_CANDLES = 5
SPIKE_WAIT_MAX_CANDLES = 10

# ── Time-based rules ──────────────────────────────────────────
MAX_HOLD_MINUTES        = 60
DAILY_RESET_START_UTC   = "23:55"
DAILY_RESET_END_UTC     = "00:05"

SL_BUFFER = 0.0   # kept for compatibility; ClaudeFX SL is points/ATR based, no extra buffer

# ── Risk defaults (user overrides via dashboard) ─────────────
DEFAULT_STAKE       = 1.0      # minimum stake on Deriv
MIN_STAKE           = 0.35     # absolute minimum Deriv allows
MAX_STAKE           = 50000.0
MAX_DAILY_LOSS_PCT  = float(os.getenv("MAX_DAILY_LOSS", 3.0))   # ClaudeFX default: 3%
MAX_OPEN_TRADES     = 10       # hard ceiling; user sets lower

# ── Lot size steps (MetaTrader style) ────────────────────────
LOT_STEP   = 0.01
LOT_MIN    = 0.01
LOT_MAX    = 100.0
# 1 lot = $1 stake on Deriv synthetics
LOT_TO_USD = 1.0

# ── Strategy selection (SIMPLIFIED) ──────────────────────────
# "EMA_CROSS" = one rule only: EMA10 crosses EMA20 on a CLOSED M1 candle.
#   BUY (CALL) when EMA10 crosses above EMA20, SELL (PUT) when it crosses below.
# USE_M5_FILTER: only take the cross if the M5 EMA10/EMA20 agree with it.
#   Set to False if you want the maximum number of trades.
STRATEGY_MODE = "EMA_CROSS"
USE_M5_FILTER = True

# False = demo mode places REAL contracts on your Deriv DEMO account
#         (they show up in Deriv back office, virtual money).
# True  = old behaviour: demo trades are only simulated inside the bot.
SIMULATE_DEMO = False
SIM_PAYOUT    = 0.95   # only used when SIMULATE_DEMO=True

# ── Contract ─────────────────────────────────────────────────
DURATION      = 15
DURATION_UNIT = "m"

# ── Web ──────────────────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")
# Render (and most PaaS hosts) inject PORT automatically — fall back to
# FLASK_PORT/5000 for local development where PORT isn't set.
FLASK_PORT = int(os.getenv("PORT", os.getenv("FLASK_PORT", 5000)))
# All accounts share the same Deriv token (set once in the environment,
# not per-user) — set ALLOW_REGISTRATION=false once you've created your
# own login, so a public link can't be used to sign up and trade on your
# Deriv account.
ALLOW_REGISTRATION = os.getenv("ALLOW_REGISTRATION", "true").lower() == "true"

# ── Paths ────────────────────────────────────────────────────
DB_PATH   = "data/market_data.db"
LOG_FILE  = "logs/bot.log"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")