# =====================================================================
# config.py  --  Your personal settings
# =====================================================================
# This file holds ALL your settings. It is never overwritten by updates.
# Edit this file to change your API keys, trade size, filters etc.
# The main bot (auto_trader.py) reads this file on startup.
# =====================================================================

# ── API Keys ──────────────────────────────────────────────────────────
API_KEY    = "YOUR_API_KEY"
API_SECRET = "YOUR_API_SECRET"
TESTNET    = False      # True = Testnet (fake money), False = Live

# ── Telegram Alerts (leave blank to disable) ─────────────────────────
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID   = ""

# ── Trading ───────────────────────────────────────────────────────────
TOP_N            = 0        # 0 = auto-calculate from balance
MAX_PAIRS        = 20       # Hard cap on simultaneous pairs
TRADE_VALUE_USDC = 100.0    # Target trade size per pair in USDC
BALANCE_BUFFER   = 1.5      # Safety multiplier (1.5 = keep 50% reserve)

# ── Pair Filters ──────────────────────────────────────────────────────
MIN_VOLUME_USDC  = 5_000_000   # Minimum 24h volume in USDC
MIN_TRADES_24H   = 1000        # Minimum trades per day (filters illiquid pairs)
MIN_VOLATILITY   = 1.5         # Minimum daily price range %
MAX_VOLATILITY   = 8.0         # Maximum daily price range %

# ── Optimiser ─────────────────────────────────────────────────────────
OPT_LOOKBACK     = "90 days ago UTC"   # Historical data window
OPT_MIN_TRADES   = 10                  # Minimum trades to validate SL/TP

# ── Bot Behaviour ─────────────────────────────────────────────────────
CHECK_INTERVAL_SEC  = 30    # How often each pair checks price (seconds)
RESCAN_INTERVAL_HRS = 6     # How often bot rescans and re-optimises (hours)
CLOSE_ON_EXIT       = False  # Sell all positions on Ctrl+C?

# ── Dashboard ─────────────────────────────────────────────────────────
DASHBOARD_ENABLED = True
DASHBOARD_PORT    = 8888

# ── Updates ───────────────────────────────────────────────────────────
AUTO_UPDATE       = True    # Check for updates on startup
# Your GitHub repo details -- change these after you fork the repo
GITHUB_USER       = "YOUR_GITHUB_USERNAME"
GITHUB_REPO       = "binance-auto-trader"
GITHUB_BRANCH     = "main"
