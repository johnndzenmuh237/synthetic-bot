"""config.py — Central configuration for SyntheticBot Pro"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Deriv API ────────────────────────────────────────────────
def _env(name, default=""):
    """Environment value with stray spaces / line breaks / wrapping quotes removed."""
    return (os.getenv(name, default) or "").strip().strip('"\'').strip()


DERIV_APP_ID     = _env("DERIV_APP_ID", "1089")
DERIV_DEMO_TOKEN = _env("DERIV_DEMO_TOKEN")
DERIV_LIVE_TOKEN = _env("DERIV_LIVE_TOKEN")

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
DERIV_REST_TIMEOUT   = float(os.getenv("DERIV_REST_TIMEOUT", 30))      # seconds to wait for Deriv's REST answers
DERIV_FORCE_IPV4     = os.getenv("DERIV_FORCE_IPV4", "true").lower() == "true"   # avoids hangs on hosts with a broken IPv6 route
DERIV_CONNECT_TRIES  = int(os.getenv("DERIV_CONNECT_TRIES", 3))        # quick retries inside one connect attempt
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
    "1HZ15V":  "Volatility 15 (1s) Index",
    "1HZ25V":  "Volatility 25 (1s) Index",
    "1HZ30V":  "Volatility 30 (1s) Index",
    "1HZ50V":  "Volatility 50 (1s) Index",
    "1HZ75V":  "Volatility 75 (1s) Index",
    "1HZ90V":  "Volatility 90 (1s) Index",
    "1HZ100V": "Volatility 100 (1s) Index",
    "1HZ150V": "Volatility 150 (1s) Index",
    "1HZ250V": "Volatility 250 (1s) Index",
    "BOOM500":  "Boom 500 Index",
    "CRASH500": "Crash 500 Index",
    # Gold (Deriv symbol frxXAUUSD). A real market: it is closed on weekends / the daily break, and Deriv
    # then refuses orders ("Market is closed"); the bot just waits and tries again, it does not crash.
    "frxXAUUSD": "Gold / US Dollar (XAUUSD)",
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
ANALYSIS_TF  = ["M5", "M15"]   # candles kept for the chart tabs / context (M15 can also be the followed timeframe)
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
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", 5))   # stop for the day after this many losses in a row

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
# The bot never pauses by itself. These two safety switches exist but are OFF by default:
#   ENABLE_RISK_LIMITS=true  -> daily-loss limit + losses-in-a-row limit pause trading
#   DAILY_RESET_PAUSE=true   -> no new entries around the daily reset window below
ENABLE_RISK_LIMITS = os.getenv("ENABLE_RISK_LIMITS", "false").lower() == "true"
DAILY_RESET_PAUSE  = os.getenv("DAILY_RESET_PAUSE", "false").lower() == "true"
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

# ── Strategies (the user picks one on the dashboard) ─────────
STRATEGIES = {
    "EMA_TREND":        "EMA 21/50 Trend + Pullback Entry",
    "REVERSAL_SCALPER": "Reversal Scalper (EMA5/13 + RSI7)",
    "EMA_CROSS":        "EMA Cross (EMA10/20)",
}
DEFAULT_STRATEGY = "EMA_TREND"
# "Candle Follow" was removed from the dashboard. Anything still saved with it becomes EMA_TREND.
RETIRED_STRATEGIES = {"CANDLE_FOLLOW": "EMA_TREND"}

# EMA 21/50 Trend (periods ET_FAST/ET_SLOW) — every distance and slope is measured in "average candle ranges", so the same
# numbers work on any index and on both 1-minute and 5-minute candles. Starting points, not proven values.
ET_FAST             = int(os.getenv("ET_FAST", 21))
ET_SLOW             = int(os.getenv("ET_SLOW", 50))
ET_RANGE_PERIOD     = int(os.getenv("ET_RANGE_PERIOD", 14))        # candles used for the average candle range
ET_SLOPE_LOOKBACK   = int(os.getenv("ET_SLOPE_LOOKBACK", 3))       # candles over which a slope is measured
ET_MIN_SEP          = float(os.getenv("ET_MIN_SEP", 0.35))         # EMA10-EMA20 gap needed to enter (x avg range)
ET_MIN_SLOPE        = float(os.getenv("ET_MIN_SLOPE", 0.04))       # both EMAs must move at least this per candle (x avg range)
ET_WHIPSAW_LOOKBACK = int(os.getenv("ET_WHIPSAW_LOOKBACK", 12))    # candles checked for repeated crossings
ET_WHIPSAW_MAX      = int(os.getenv("ET_WHIPSAW_MAX", 2))          # more crossings than this = consolidation
ET_ENTRY_KEEP       = float(os.getenv("ET_ENTRY_KEEP", 0.80))      # gap must still be >= this share of its recent peak to enter
ET_CONVERGE_RATIO   = float(os.getenv("ET_CONVERGE_RATIO", 0.65))  # gap below this share of its peak = converging
ET_CONVERGE_CONFIRM = int(os.getenv("ET_CONVERGE_CONFIRM", 2))     # closed candles in a row before a convergence exit
ET_PEAK_LOOKBACK    = int(os.getenv("ET_PEAK_LOOKBACK", 12))      # candles used to find the recent peak gap
ET_EXIT_PEAK_LOOKBACK = int(os.getenv("ET_EXIT_PEAK_LOOKBACK", 30))  # exits compare the gap with its peak over this many candles (slow EMAs need a long look-back)
ET_COOLDOWN_CANDLES = int(os.getenv("ET_COOLDOWN_CANDLES", 2))     # pause after an exit before the next entry
# Pullback entry: the bot buys/sells only when price pulls back to the fast EMA (EMA21) inside the trend and holds it.
ET_PULLBACK_LOOKBACK = int(os.getenv("ET_PULLBACK_LOOKBACK", 12))  # candles before the retest in which price must have been away from EMA21
ET_AWAY             = float(os.getenv("ET_AWAY", 1.0))             # ...by at least this many candle ranges
ET_TOUCH_TOL        = float(os.getenv("ET_TOUCH_TOL", 0.15))       # how close to EMA21 counts as touching it (x avg range)
ET_RETEST_WINDOW    = int(os.getenv("ET_RETEST_WINDOW", 2))        # a retest this many closed candles ago still counts ("just happened")
ET_MAX_CHASE        = float(os.getenv("ET_MAX_CHASE", 1.5))        # never enter further than this from EMA21 (x avg range)
ET_PULLBACK_KEEP    = float(os.getenv("ET_PULLBACK_KEEP", 0.6))    # the EMA gap must still be this share of its recent peak

# Trading session length (dashboard). When it ends: no new trades, open ones are handled, the bot stops.
SESSION_CHOICES = {"1": "1 hour", "2": "2 hours", "4": "4 hours", "8": "8 hours", "12": "12 hours", "24": "24 hours"}
DEFAULT_SESSION_HOURS = os.getenv("DEFAULT_SESSION_HOURS", "24")
if DEFAULT_SESSION_HOURS not in SESSION_CHOICES:
    DEFAULT_SESSION_HOURS = "24"

# Candle Follow — starting points, NOT proven settings (see candle_follow.py).
CF_CONFIRM_SEC     = float(os.getenv("CF_CONFIRM_SEC", 5))      # seconds a direction must hold before an entry
CF_BAND_FRAC       = float(os.getenv("CF_BAND_FRAC", 0.10))     # dead-band = this x average recent candle range
CF_PROTECT_PROFIT  = os.getenv("CF_PROTECT_PROFIT", "true").lower() == "true"  # close a trade that was in profit before it turns negative
CF_ENTRY_RETRY_SEC = float(os.getenv("CF_ENTRY_RETRY_SEC", 15)) # pause after a refused/failed order before trying again
CF_MAX_TICK_AGE    = float(os.getenv("CF_MAX_TICK_AGE", 10))    # no entries if the last price update is older than this
# Deriv limits how many `sell` requests may be sent per moment. Sells are sent one after another with
# this gap, and a "rate limit" answer is retried automatically instead of being reported as a failure.
# Minimum seconds between requests of the same kind sent to Deriv (every request type is paced in
# broker.py, and a "rate limit" answer is waited out and retried automatically).
REQ_GAP_PROPOSAL   = float(os.getenv("REQ_GAP_PROPOSAL", 0.5))
REQ_GAP_BUY        = float(os.getenv("REQ_GAP_BUY", 0.5))
REQ_GAP_SELL       = float(os.getenv("REQ_GAP_SELL", 0.6))
REQ_GAP_CONTRACT   = float(os.getenv("REQ_GAP_CONTRACT", 0.25))   # proposal_open_contract (position pricing)
REQ_GAP_OTHER      = float(os.getenv("REQ_GAP_OTHER", 0.1))
RATE_LIMIT_RETRIES = int(os.getenv("RATE_LIMIT_RETRIES", 5))
SELL_MIN_GAP_SEC   = float(os.getenv("SELL_MIN_GAP_SEC", 0.6))
SELL_RATE_RETRIES  = int(os.getenv("SELL_RATE_RETRIES", 4))
# ── Trade guard (every strategy) ─────────────────────────────────────────────────────────────
# Protects each open trade on its own. All amounts are in account currency (USD).
#  * LOSS CAP:    a trade whose floating loss reaches GUARD_MAX_LOSS is closed at once (never left to run to expiry).
#  * PROFIT LOCK: once a trade is up GUARD_ARM_PROFIT, it is closed as soon as its profit falls GUARD_GIVEBACK below
#                 its best profit - so a winner is banked and never turns into a loss. While it keeps climbing it runs.
#  * COOLDOWN:    after a guard exit the bot waits GUARD_COOLDOWN_SEC before looking for the next entry.
# For small stakes the amounts shrink with the stake (a % cap), so they still work with a $1 stake.
GUARD_ENABLED      = os.getenv("GUARD_ENABLED", "false").lower() == "true"   # OFF by default = bot behaves exactly as before
GUARD_MAX_LOSS     = float(os.getenv("GUARD_MAX_LOSS", 2.0))
GUARD_MAX_LOSS_PCT = float(os.getenv("GUARD_MAX_LOSS_PCT", 0.5))    # never more than this fraction of the stake
GUARD_GIVEBACK     = float(os.getenv("GUARD_GIVEBACK", 2.0))
GUARD_GIVEBACK_PCT = float(os.getenv("GUARD_GIVEBACK_PCT", 0.5))    # of the stake, and never more than half the peak
GUARD_ARM_PROFIT   = float(os.getenv("GUARD_ARM_PROFIT", 1.0))
GUARD_ARM_PCT      = float(os.getenv("GUARD_ARM_PCT", 0.1))         # of the stake
GUARD_COOLDOWN_SEC = float(os.getenv("GUARD_COOLDOWN_SEC", 60))
# ── Long-running trades + profit lock (every strategy) - OFF by default: the bot trades exactly as before ───────────────────────────────────────
# A Rise/Fall contract always has an end time, so "no expiry" = the LONGEST duration Deriv accepts (tried in this
# order; the first one Deriv accepts for that symbol is remembered). The bot's own rules below close the trade first.
LONG_TRADES        = os.getenv("LONG_TRADES", "false").lower() == "true"
LONG_TRADE_DURATIONS = os.getenv("LONG_TRADE_DURATIONS", "1d,12h,4h,2h,1h,30m,15m")
# Profit rules, amounts for a $10 stake (they scale with the stake: a $5 stake uses half of each amount).
PROFIT_RULES       = os.getenv("PROFIT_RULES", "false").lower() == "true"
PROFIT_BASE_STAKE  = float(os.getenv("PROFIT_BASE_STAKE", 10))
PROFIT_LOCK_AT     = float(os.getenv("PROFIT_LOCK_AT", 2.40))     # profit that switches the lock on
PROFIT_LOCK_FLOOR  = float(os.getenv("PROFIT_LOCK_FLOOR", 2.00))   # once locked, never let the trade fall below this
PROFIT_TRAIL       = float(os.getenv("PROFIT_TRAIL", 1.00))        # once locked, exit if profit falls this far below its best
PROFIT_TARGET      = float(os.getenv("PROFIT_TARGET", 5.00))       # take the profit and exit here
PROFIT_COOLDOWN_SEC = float(os.getenv("PROFIT_COOLDOWN_SEC", 60))  # wait this long for the next opportunity after an exit
POSITION_POLL_SEC  = float(os.getenv("POSITION_POLL_SEC", 3))   # how often open positions are re-priced
TIMEFRAME_CHOICES  = {"M1": "1 minute", "M5": "5 minutes", "M15": "15 minutes"}
SELECTABLE_TF      = tuple(TIMEFRAME_CHOICES)          # timeframes the bot can follow
DEFAULT_TIMEFRAME  = "M1"

# Reversal Scalper — starting points for testing (NOT verified settings of any
# reference bot). Override with environment variables if you want to experiment.
REV_FAST_EMA      = int(os.getenv("REV_FAST_EMA", 5))
REV_SLOW_EMA      = int(os.getenv("REV_SLOW_EMA", 13))
REV_RSI_PERIOD    = int(os.getenv("REV_RSI_PERIOD", 7))
REV_RSI_LEVEL     = float(os.getenv("REV_RSI_LEVEL", 50))
REV_USE_M5_FILTER = os.getenv("REV_USE_M5_FILTER", "false").lower() == "true"
MAX_SIGNAL_AGE_SEC = 150     # skip a signal if the last closed candle is older than this

# What to do when a reversal signal appears while a position is LOSING:
#   "hold"  = keep it until the contract expires (default, no panic exit)
#   "close" = sell it at market immediately and take the loss
LOSS_POLICIES = {
    "hold":  "Hold losing trade until expiry",
    "close": "Close losing trade on reversal",
}
DEFAULT_LOSS_POLICY = "hold"
# Sequential reversal (default): never open the opposite direction while an
# opposite position is still open. Set ALLOW_HEDGE=true to allow both at once.
ALLOW_HEDGE = os.getenv("ALLOW_HEDGE", "false").lower() == "true"

# EMA Cross strategy (single rule): EMA10 crosses EMA20 on a CLOSED M1 candle.
# USE_M5_FILTER: only take the cross if the M5 EMA10/EMA20 agree with it.
USE_M5_FILTER = True

# ── Contract ─────────────────────────────────────────────────
DURATION      = int(os.getenv("CONTRACT_DURATION", 15))
DURATION_UNIT = os.getenv("CONTRACT_DURATION_UNIT", "m")
# Trade time the user can pick on the dashboard ("Trade time"). Deriv decides the real minimum/maximum for each market:
# if it refuses a length, the bot shows Deriv's own reason and places no trade.
DURATION_CHOICES = {"1m": "1 minute", "5m": "5 minutes", "15m": "15 minutes", "30m": "30 minutes", "1h": "1 hour",
                    "2h": "2 hours", "4h": "4 hours", "12h": "12 hours", "1d": "1 day"}
DEFAULT_TRADE_TIME = "{}{}".format(DURATION, DURATION_UNIT)
if DEFAULT_TRADE_TIME not in DURATION_CHOICES:
    DURATION_CHOICES[DEFAULT_TRADE_TIME] = "{} {}".format(DURATION, {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}.get(DURATION_UNIT, DURATION_UNIT))

# ── Web ──────────────────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")
SECRET_KEY_IS_DEFAULT = SECRET_KEY == "dev-secret"
# ── Multi-user ────────────────────────────────────────────────
# Admin accounts: comma-separated usernames (e.g. ADMIN_USERNAMES=john). Admins see the Admin page and are the
# only users who may trade with the SERVER's own DERIV_DEMO_TOKEN / DERIV_LIVE_TOKEN. Everyone else connects their
# own tokens on the dashboard. If this is empty, the very first account ever created is treated as the admin.
ADMIN_USERNAMES = [u.strip().lower() for u in os.getenv("ADMIN_USERNAMES", "").split(",") if u.strip()]
# The admin console lives at a SECRET address with its own login (it is not linked from anywhere in the app and
# answers 404 to anyone who is not signed in as an admin). Choose your own, e.g. ADMIN_PATH=/control-room-7k2x.
# If unset, one is derived from SECRET_KEY and printed once in the server log at start-up.
import hashlib as _hl
_ap = os.getenv("ADMIN_PATH", "").strip()
if _ap:
    ADMIN_PATH = "/" + _ap.strip().strip("/")
else:
    ADMIN_PATH = "/ops-" + _hl.sha256(("admin-path|" + os.getenv("SECRET_KEY", "dev-secret")).encode()).hexdigest()[:12]
ADMIN_PATH_IS_DEFAULT = not _ap
ADMIN_SESSION_HOURS = float(os.getenv("ADMIN_SESSION_HOURS", 12))   # admin must sign in again after this long
ADMIN_LOGIN_MAX_FAILS = int(os.getenv("ADMIN_LOGIN_MAX_FAILS", 5))  # wrong passwords from one address before a pause
ADMIN_LOGIN_LOCK_SEC = int(os.getenv("ADMIN_LOGIN_LOCK_SEC", 300))
# Key used to encrypt users' saved Deriv tokens (falls back to SECRET_KEY). Keep it fixed.
TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
# Each running bot is a thread + a Deriv connection; this protects a small server from being overloaded.
MAX_CONCURRENT_BOTS = int(os.getenv("MAX_CONCURRENT_BOTS", 25))

# A login stays valid for this many days (the cookie is permanent, not "until the browser closes").
SESSION_DAYS = int(os.getenv("SESSION_DAYS", 3650))

# ── Firebase Realtime Database (permanent storage for accounts + history) ──
# These two are public project identifiers (safe to keep in code). The SECRET part is
# FIREBASE_SERVICE_ACCOUNT (or FIREBASE_DB_SECRET) and must only live in Render's
# Environment tab. With no credential set, the bot works exactly as before (local database only).
FIREBASE_DATABASE_URL = _env("FIREBASE_DATABASE_URL", "https://swiftchain-827f2-default-rtdb.firebaseio.com")
# All of the bot's data lives under this one node, so it can share a Firebase project with other apps
# (their `users`, `settings`... nodes are never read or written by the bot).
FIREBASE_ROOT = _env("FIREBASE_ROOT", "synthbot").strip("/")
FIREBASE_SERVICE_ACCOUNT = (os.getenv("FIREBASE_SERVICE_ACCOUNT", "") or "").strip()
FIREBASE_DB_SECRET = _env("FIREBASE_DB_SECRET")
# Render (and most PaaS hosts) inject PORT automatically — fall back to
# FLASK_PORT/5000 for local development where PORT isn't set.
FLASK_PORT = int(os.getenv("PORT", os.getenv("FLASK_PORT", 5000)))
# All accounts share the same Deriv token (set once in the environment,
# not per-user) — set ALLOW_REGISTRATION=false once you've created your
# own login, so a public link can't be used to sign up and trade on your
# Deriv account.
ALLOW_REGISTRATION = os.getenv("ALLOW_REGISTRATION", "true").lower() == "true"

# ── Paths ────────────────────────────────────────────────────
DB_PATH   = os.getenv("DB_PATH", "data/market_data.db")
LOG_FILE  = os.getenv("LOG_FILE", "logs/bot.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")