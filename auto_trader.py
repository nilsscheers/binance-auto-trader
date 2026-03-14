"""
Binance Auto Trading Launcher
================================
One script to rule them all. Run this and it will:

  1. SCAN     -- Fetch real market data from live Binance (public, no key needed)
  2. SELECT   -- Pick the top N pairs automatically
  3. OPTIMISE -- Backtest SL/TP settings for each selected pair
  4. TRADE    -- Launch the multi-currency bot (on testnet or live)

Market data is ALWAYS fetched from live Binance so scanning works
correctly even in testnet mode. Orders are placed on testnet or live
depending on the TESTNET setting.

Requirements:
    pip install python-binance pandas numpy requests

Optional:
    Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID for alerts
"""

import os
import sys
import csv
import time
import json
import logging
import threading
import itertools
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from binance.client import Client
from binance.exceptions import BinanceAPIException
import requests

# Auto-updater (safe to import -- does nothing if config not set)
try:
    from updater import check_for_updates as _check_for_updates
except ImportError:
    _check_for_updates = None

# -- Windows UTF-8 fix -------------------------------------------------
# Prevents 'charmap' codec errors on Windows terminals when printing
# special characters. Safe to run on Mac/Linux too.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# =====================================================================
# ##  CONFIGURATION  -- only section you need to edit
# =====================================================================

# ── Load settings from config.py ─────────────────────────────────────
# All user settings live in config.py so they survive updates.
try:
    import config as _cfg
    API_KEY             = getattr(_cfg, "API_KEY",             "YOUR_API_KEY")
    API_SECRET          = getattr(_cfg, "API_SECRET",          "YOUR_API_SECRET")
    TESTNET             = getattr(_cfg, "TESTNET",             True)
    TELEGRAM_BOT_TOKEN  = getattr(_cfg, "TELEGRAM_BOT_TOKEN",  "")
    TELEGRAM_CHAT_ID    = getattr(_cfg, "TELEGRAM_CHAT_ID",    "")
    TOP_N               = getattr(_cfg, "TOP_N",               0)
    MAX_PAIRS           = getattr(_cfg, "MAX_PAIRS",           20)
    TRADE_VALUE_USDC    = getattr(_cfg, "TRADE_VALUE_USDC",    100.0)
    BALANCE_BUFFER      = getattr(_cfg, "BALANCE_BUFFER",      1.5)
    MIN_VOLUME_USDC     = getattr(_cfg, "MIN_VOLUME_USDC",     5_000_000)
    MIN_TRADES_24H      = getattr(_cfg, "MIN_TRADES_24H",      1000)
    MIN_VOLATILITY      = getattr(_cfg, "MIN_VOLATILITY",      1.5)
    MAX_VOLATILITY      = getattr(_cfg, "MAX_VOLATILITY",      8.0)
    OPT_LOOKBACK        = getattr(_cfg, "OPT_LOOKBACK",        "90 days ago UTC")
    OPT_MIN_TRADES      = getattr(_cfg, "OPT_MIN_TRADES",      10)
    CHECK_INTERVAL_SEC  = getattr(_cfg, "CHECK_INTERVAL_SEC",  30)
    RESCAN_INTERVAL_HRS = getattr(_cfg, "RESCAN_INTERVAL_HRS", 6)
    CLOSE_ON_EXIT       = getattr(_cfg, "CLOSE_ON_EXIT",       False)
    DASHBOARD_ENABLED   = getattr(_cfg, "DASHBOARD_ENABLED",   True)
    DASHBOARD_PORT      = getattr(_cfg, "DASHBOARD_PORT",      8888)
    AUTO_UPDATE         = getattr(_cfg, "AUTO_UPDATE",         True)
    GITHUB_USER         = getattr(_cfg, "GITHUB_USER",         "")
    GITHUB_REPO         = getattr(_cfg, "GITHUB_REPO",         "binance-auto-trader")
    GITHUB_BRANCH       = getattr(_cfg, "GITHUB_BRANCH",       "main")
except ImportError:
    print("  WARNING: config.py not found -- using defaults. Copy config.py to your folder!")
    API_KEY             = os.getenv("BINANCE_API_KEY",    "YOUR_API_KEY")
    API_SECRET          = os.getenv("BINANCE_API_SECRET", "YOUR_API_SECRET")
    TESTNET             = True
    TELEGRAM_BOT_TOKEN  = ""
    TELEGRAM_CHAT_ID    = ""
    TOP_N               = 0
    MAX_PAIRS           = 20
    TRADE_VALUE_USDC    = 100.0
    BALANCE_BUFFER      = 1.5
    MIN_VOLUME_USDC     = 5_000_000
    MIN_TRADES_24H      = 1000
    MIN_VOLATILITY      = 1.5
    MAX_VOLATILITY      = 8.0
    OPT_LOOKBACK        = "90 days ago UTC"
    OPT_MIN_TRADES      = 10
    CHECK_INTERVAL_SEC  = 30
    RESCAN_INTERVAL_HRS = 6
    CLOSE_ON_EXIT       = False
    DASHBOARD_ENABLED   = True
    DASHBOARD_PORT      = 8888
    AUTO_UPDATE         = False
    GITHUB_USER         = ""
    GITHUB_REPO         = "binance-auto-trader"
    GITHUB_BRANCH       = "main"

# Fixed constants (not user-configurable)
BLACKLIST    = ["USDTUSDC","BUSDUSDC","TUSDUSDC","BETHUSDC","WBETHUSDC","EURUSDC"]
OPT_INTERVAL = Client.KLINE_INTERVAL_1HOUR
OPT_SL_RANGE = [round(x,1) for x in np.arange(0.5, 5.5, 0.5)]
OPT_TP_RANGE = [round(x,1) for x in np.arange(1.0, 15.0, 0.5)]
MAKER_FEE    = 0.001
LOG_FILE     = "auto_trader.log"
TRADE_FILE   = "trade_history.csv"
SCORES_FILE  = "pair_scores.csv"
DASHBOARD_ENABLED = True

# =====================================================================
# LOGGING
# =====================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("launcher")
_csv_lock = threading.Lock()

# Active positions lock -- tracks which symbols are currently held
# Prevents double-buying the same coin across threads or restarts
_active_symbols      = set()
_active_symbols_lock = threading.Lock()


def _claim_symbol(symbol: str) -> bool:
    """
    Attempt to claim a symbol for trading.
    Returns True if successfully claimed, False if already held.
    """
    with _active_symbols_lock:
        if symbol in _active_symbols:
            return False
        _active_symbols.add(symbol)
        return True


def _release_symbol(symbol: str) -> None:
    """Release a symbol after a position closes."""
    with _active_symbols_lock:
        _active_symbols.discard(symbol)


# =====================================================================
# TELEGRAM
# =====================================================================
def send_telegram(msg: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10
        )
    except Exception as e:
        logging.getLogger("telegram").warning(f"Failed: {e}")


# =====================================================================
# BINANCE CLIENTS
# Two clients:
#   market_client -- always points to LIVE Binance for real market data
#   trade_client  -- points to testnet or live depending on TESTNET flag
# =====================================================================
def _sync_timestamp(client: Client, label: str = "client") -> int:
    """
    Calculates the offset between local system time and Binance server
    time, then applies it to the client. This fixes APIError -1022
    (invalid signature) caused by clock drift on Windows.
    Returns the offset in milliseconds.
    """
    try:
        local_time  = int(time.time() * 1000)
        server_time = client.get_server_time()["serverTime"]
        offset      = server_time - local_time
        client.timestamp_offset = offset
        log.info(f"  {label} clock offset: {offset:+d} ms")
        return offset
    except Exception as e:
        log.warning(f"  Could not sync timestamp for {label}: {e}")
        return 0


def create_clients():
    # Market data client -- always live Binance (public endpoints, no auth needed)
    # This ensures pair scanning works correctly even in testnet mode
    market_client = Client("", "")   # Empty keys are fine for public endpoints
    log.info("Market data client -> Live Binance (public data)")

    # Trading client -- testnet or live depending on TESTNET flag
    trade_client = Client(API_KEY, API_SECRET, testnet=TESTNET)
    if TESTNET:
        trade_client.API_URL = "https://testnet.binance.vision/api"
        log.info("Trading client    -> Binance TESTNET (fake money)")
    else:
        log.warning("Trading client    -> Binance LIVE (REAL MONEY)")

    # Sync clocks with Binance server time to prevent signature errors.
    # This fixes APIError -1022 caused by Windows clock drift.
    log.info("Syncing timestamps with Binance server ...")
    _sync_timestamp(market_client, "market_client")
    _sync_timestamp(trade_client,  "trade_client ")

    return market_client, trade_client


def get_price(client: Client, symbol: str) -> float:
    return float(client.get_symbol_ticker(symbol=symbol)["price"])


def get_free_balance(client: Client, asset: str) -> float:
    """Returns the free balance of an asset, 0.0 on any error."""
    try:
        bal = client.get_asset_balance(asset=asset)
        return float(bal["free"]) if bal else 0.0
    except Exception:
        return 0.0


def get_step_size(client: Client, symbol: str) -> float:
    """
    Fetches the minimum qty step size for a symbol from Binance exchange info.
    e.g. BTCUSDC = 0.00001, BNBUSDC = 0.001, XRPUSDC = 0.1
    This prevents APIError -1022 / -1111 caused by invalid qty precision.
    """
    try:
        info = client.get_symbol_info(symbol)
        for f in info["filters"]:
            if f["filterType"] == "LOT_SIZE":
                return float(f["stepSize"])
    except Exception as e:
        log.warning(f"Could not get step size for {symbol}: {e} -- defaulting to 0.001")
    return 0.001


def round_qty(qty: float, step_size: float) -> float:
    """
    Rounds qty DOWN to the nearest valid step size.
    e.g. qty=0.0761, step=0.01 -> 0.07
         qty=35.69,  step=0.1  -> 35.6
    """
    import math
    if step_size <= 0:
        return qty
    precision = max(0, round(-math.log10(step_size)))
    return round(math.floor(qty / step_size) * step_size, precision)


# =====================================================================
# POSITION RECOVERY
# =====================================================================
def get_base_asset(symbol: str) -> str:
    """Extract base asset from symbol e.g. BTCUSDC -> BTC"""
    return symbol.replace("USDC", "")


def recover_position(trade_client: Client, symbol: str, qty: float,
                     step_size: float, sl_pct: float, tp_pct: float,
                     plog) -> tuple:
    """
    Checks the account balance for the base asset of this pair.
    If a meaningful balance is found (>= 80% of intended trade qty),
    it assumes a position is already open from a previous run and
    restores it using the current market price as the entry price.

    Returns (in_trade, entry, sl_price, tp_price, qty_held)
    """
    base = get_base_asset(symbol)
    try:
        bal   = trade_client.get_asset_balance(asset=base)
        held  = float(bal["free"]) + float(bal["locked"]) if bal else 0.0
        # Round held qty to step size
        held  = round_qty(held, step_size)

        if held >= qty * 0.8:
            # Position detected -- claim symbol to prevent other threads buying same coin
            if not _claim_symbol(symbol):
                plog.warning(f"Symbol {symbol} already claimed -- skipping recovery")
                return False, 0.0, 0.0, 0.0, qty
            price = get_price(trade_client, symbol)
            sl_p  = price * (1 - sl_pct / 100)
            tp_p  = price * (1 + tp_pct / 100)
            plog.info(
                f"Existing position detected: holding {held} {base}  "
                f"-- resuming at current price {price:.4f}"
            )
            plog.info(
                f"Restored SL={sl_p:.4f}  TP={tp_p:.4f}  "
                f"(note: entry price estimated from current market price)"
            )
            send_telegram(
                f"[INFO] [{symbol}] Existing position restored on restart\n"
                f"Holding: {held} {base}\n"
                f"Current price: {price:.4f}\n"
                f"SL: {sl_p:.4f}  TP: {tp_p:.4f}"
            )
            return True, price, sl_p, tp_p, held
        else:
            if held > 0:
                plog.info(
                    f"Small {base} balance found ({held}) -- "
                    f"below 80% of trade qty ({qty}), treating as dust. Starting fresh."
                )
            return False, 0.0, 0.0, 0.0, qty
    except Exception as e:
        plog.warning(f"Could not check existing balance for {base}: {e} -- starting fresh")
        return False, 0.0, 0.0, 0.0, qty


# =====================================================================
# PORTFOLIO CLEANUP  (runs once at startup)
# =====================================================================
def cleanup_portfolio(trade_client: Client, market_client: Client) -> None:
    """
    Checks the entire Binance account on startup.
    For each non-USDC asset found:
      - If we hold MORE than 1 full trade quantity worth: sell the excess
      - If we hold exactly 1 trade quantity (80%+): keep it (position recovery handles it)
      - If we hold dust (< 80% of trade qty): leave it (too small to sell profitably)

    This prevents the bot from starting with over-invested positions from
    previous runs, crashes, or manual trades.
    """
    cleanup_log = logging.getLogger("cleanup")
    cleanup_log.info("=" * 60)
    cleanup_log.info("PORTFOLIO CLEANUP CHECK")
    cleanup_log.info("=" * 60)

    try:
        account  = trade_client.get_account()
        balances = account.get("balances", [])
    except Exception as e:
        cleanup_log.warning(f"Could not fetch account balances: {e} -- skipping cleanup")
        return

    # Get all current prices in one call from live Binance
    try:
        tickers = {t["symbol"]: float(t["price"]) for t in market_client.get_ticker()}
    except Exception as e:
        cleanup_log.warning(f"Could not fetch prices: {e} -- skipping cleanup")
        return

    cleaned = 0
    for b in balances:
        asset = b["asset"]
        free  = float(b["free"])
        locked = float(b["locked"])
        total  = free + locked

        # Skip USDC, stablecoins, and BNB (BNB used for fees)
        if asset in ("USDC", "USDT", "EUR", "BUSD", "TUSD", "USDP", "FDUSD", "BNB"):
            continue
        if total < 0.00001:
            continue

        # Get current price for this asset
        symbol = asset + "USDC"
        price  = tickers.get(symbol, 0)
        if price == 0:
            cleanup_log.debug(f"No USDC price for {asset} -- skipping")
            continue

        # Calculate what one trade quantity should be
        step_size    = get_step_size(trade_client, symbol)
        trade_qty    = round_qty(TRADE_VALUE_USDC / price, step_size)
        value_held   = total * price

        cleanup_log.info(
            f"  {asset}: holding {total:.6f} "
            f"(~{value_held:.2f} USDC)  trade_qty={trade_qty}"
        )

        # If holding more than 1.8x the trade quantity -- sell the excess
        if total > trade_qty * 1.8:
            excess_qty = round_qty(total - trade_qty, step_size)
            if excess_qty <= 0:
                continue

            excess_value = excess_qty * price
            cleanup_log.warning(
                f"  EXCESS detected: {asset} -- holding {total:.6f}, "
                f"selling excess {excess_qty:.6f} (~{excess_value:.2f} USDC)"
            )
            send_telegram(
                f"[CLEANUP] Excess {asset} detected on startup. Holding: {total:.6f} ({value_held:.2f} USDC). Selling excess: {excess_qty:.6f} ({excess_value:.2f} USDC)"
            )
            try:
                order = trade_client.order_market_sell(symbol=symbol, quantity=excess_qty)
                sell_price = float(order.get("fills", [{}])[0].get("price", 0)) or price
                cleanup_log.info(
                    f"  [OK] Sold {excess_qty:.6f} {asset} @ {sell_price:.4f} USDC"
                )
                log_trade(symbol, "SELL", excess_qty, sell_price,
                          pnl_pct=0.0, note="startup-cleanup")
                cleaned += 1
                time.sleep(0.5)  # Small delay between cleanup sells
            except BinanceAPIException as e:
                cleanup_log.error(f"  Failed to sell excess {asset}: {e}")
        elif total >= trade_qty * 0.8:
            cleanup_log.info(f"  {asset}: normal position size -- keeping (recovery will handle)")
        else:
            cleanup_log.info(f"  {asset}: dust amount -- leaving ({total:.6f})")

    if cleaned == 0:
        cleanup_log.info("Portfolio clean -- no excess positions found.")
    else:
        cleanup_log.info(f"Cleanup complete -- sold excess from {cleaned} coin(s).")
        send_telegram(f"[CLEANUP] Startup cleanup complete -- sold excess from {cleaned} coin(s)")

    cleanup_log.info("=" * 60)


# =====================================================================
# ##  STAGE 1 -- PAIR SELECTOR  (uses market_client)
# =====================================================================
def _fetch_rsi(client, symbol, periods=14) -> float:
    try:
        klines = client.get_klines(
            symbol=symbol, interval=Client.KLINE_INTERVAL_1HOUR, limit=periods+1)
        closes = np.array([float(k[4]) for k in klines])
        d      = np.diff(closes)
        gains  = np.where(d > 0, d, 0)
        losses = np.where(d < 0, -d, 0)
        ag, al = gains[-periods:].mean(), losses[-periods:].mean()
        return 100.0 if al == 0 else round(100 - 100 / (1 + ag / al), 2)
    except Exception:
        return 50.0


def _fetch_sma_distance(client, symbol, period=20) -> float:
    try:
        klines = client.get_klines(
            symbol=symbol, interval=Client.KLINE_INTERVAL_1HOUR, limit=period)
        closes = [float(k[4]) for k in klines]
        sma    = np.mean(closes[:-1])
        return round((closes[-1] - sma) / sma * 100, 3)
    except Exception:
        return 0.0


def _score(row, rsi, sma_dist) -> dict:
    price  = row["lastPrice"]
    volume = row["quoteVolume"]
    change = row["priceChangePercent"]
    high   = row["highPrice"]
    low    = row["lowPrice"]
    rng    = (high - low) / low * 100 if low > 0 else 0

    # Guard against log10(0) if somehow volume slips through
    vol_s = 0.0
    if volume > 0 and MIN_VOLUME_USDC > 0:
        try:
            vol_s = min(20, max(0, (np.log10(volume) - np.log10(MIN_VOLUME_USDC)) * 6))
        except Exception:
            vol_s = 0.0

    rng_s = max(0, min(20, 20 - abs(rng - 3.5) * 4)) if MIN_VOLATILITY <= rng <= MAX_VOLATILITY else 0
    rsi_s = 20 if 45 <= rsi <= 65 else (12 if 35 <= rsi <= 75 else 4)
    mom_s = 20 if 0.5 <= change <= 5 else (10 if -1 <= change < 0.5 or 5 < change <= 10 else 2)
    sma_s = 20 if 0.5 <= sma_dist <= 3 else (14 if 0 <= sma_dist < 0.5 else (8 if -1 <= sma_dist < 0 else 4))

    return {
        "symbol":          row["symbol"],
        "price":           round(price, 6),
        "volume_24h_M":    round(volume / 1e6, 1),
        "change_24h_pct":  round(change, 2),
        "daily_range_pct": round(rng, 2),
        "rsi_14":          rsi,
        "sma_dist":        sma_dist,
        "total_score":     round(vol_s + rng_s + rsi_s + mom_s + sma_s, 1),
    }


def get_permitted_symbols(trade_client: Client) -> set:
    """
    Fetches the list of symbols actually available on the account's
    exchange (testnet or live). Used to filter out pairs that exist
    on live Binance but are not supported on testnet.
    """
    try:
        info    = trade_client.get_exchange_info()
        symbols = {
            s["symbol"] for s in info["symbols"]
            if s["status"] == "TRADING" and s["symbol"].endswith("USDC")
        }
        log.info(f"Exchange info: {len(symbols)} USDC pairs available on this account")
        return symbols
    except Exception as e:
        log.warning(f"Could not fetch exchange info: {e} -- skipping symbol filter")
        return set()


def run_pair_selection(market_client: Client, trade_client: Client) -> list:
    """Scan live Binance market data and return top pair configs."""
    _dashboard_state["stage"] = "Stage 1: Scanning pairs"
    _notify()
    log.info("=" * 60)
    log.info("STAGE 1 -- PAIR SELECTION  (live market data)")
    log.info("=" * 60)

    # Get the symbols actually permitted on this account (testnet has fewer pairs)
    permitted = get_permitted_symbols(trade_client)

    # Fetch all 24h tickers from live Binance
    tickers = pd.DataFrame(market_client.get_ticker())
    df = tickers[tickers["symbol"].str.endswith("USDC")].copy()
    df = df[~df["symbol"].isin(BLACKLIST)]

    # Filter to only pairs permitted on this account
    if permitted:
        before_perm = len(df)
        df = df[df["symbol"].isin(permitted)]
        log.info(f"Permitted symbol filter: {before_perm} -> {len(df)} pairs")

    for col in ["lastPrice","quoteVolume","priceChangePercent","highPrice","lowPrice"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["lastPrice","quoteVolume","priceChangePercent","highPrice","lowPrice"])

    # Volume filter
    before_vol = len(df)
    df = df[df["quoteVolume"] >= MIN_VOLUME_USDC]
    log.info(f"Volume filter  (>=${MIN_VOLUME_USDC/1e6:.0f}M USDC): {before_vol} -> {len(df)} pairs")

    # Volatility filter
    df["_rng"] = (df["highPrice"] - df["lowPrice"]) / df["lowPrice"] * 100
    before_vol2 = len(df)
    df = df[(df["_rng"] >= MIN_VOLATILITY) & (df["_rng"] <= MAX_VOLATILITY)].reset_index(drop=True)
    log.info(f"Volatility filter ({MIN_VOLATILITY}-{MAX_VOLATILITY}%): {before_vol2} -> {len(df)} pairs")

    # Trade count filter -- removes illiquid pairs that have volume but very few trades
    if "count" in df.columns:
        df["count"] = pd.to_numeric(df["count"], errors="coerce").fillna(0)
        before_trades = len(df)
        df = df[df["count"] >= MIN_TRADES_24H].reset_index(drop=True)
        log.info(f"Trade count filter (>={MIN_TRADES_24H}/day): {before_trades} -> {len(df)} pairs")
    else:
        log.warning("Trade count data unavailable -- skipping trade count filter")

    if len(df) == 0:
        log.error("No pairs passed filters! Try lowering MIN_VOLUME_USDC, MIN_TRADES_24H or MIN_VOLATILITY.")
        raise SystemExit(1)

    log.info(f"Analysing {len(df)} pairs (RSI + SMA) -- ~1-2 min ...")

    scores = []
    for i, (_, row) in enumerate(df.iterrows()):
        rsi = _fetch_rsi(market_client, row["symbol"])
        sma = _fetch_sma_distance(market_client, row["symbol"])
        scores.append(_score(row, rsi, sma))
        if (i + 1) % 15 == 0:
            log.info(f"  Scored {i+1}/{len(df)} pairs ...")
        time.sleep(0.1)

    if not scores:
        log.error("Scoring produced no results. Check your filters.")
        raise SystemExit(1)

    results = pd.DataFrame(scores).sort_values("total_score", ascending=False).reset_index(drop=True)
    results.to_csv(SCORES_FILE, index=False)

    # Use auto-calculated TOP_N if balance-based mode is active
    import builtins
    effective_n = getattr(builtins, '_AUTO_TOP_N', TOP_N) or TOP_N
    top = results.head(effective_n)

    log.info(f"\n{'='*62}")
    log.info(f"  TOP {effective_n} PAIRS SELECTED")
    log.info(f"{'='*62}")
    log.info(f"  {'#':<3} {'SYMBOL':<12} {'SCORE':>6} {'VOL $M':>8} {'RSI':>6} {'24H%':>7}")
    log.info(f"  {'-'*56}")
    for rank, (_, row) in enumerate(top.iterrows(), 1):
        log.info(f"  {rank:<3} {row['symbol']:<12} {row['total_score']:>6.1f}"
                 f"  ${row['volume_24h_M']:>5.0f}M  {row['rsi_14']:>6.1f}  {row['change_24h_pct']:>+6.1f}%")
    log.info(f"{'='*62}\n")

    # Build pair configs with auto-calculated qty
    pairs = []
    for _, row in top.iterrows():
        symbol    = row["symbol"]
        price     = row["price"]
        raw       = TRADE_VALUE_USDC / price if price > 0 else 0
        step_size = get_step_size(market_client, symbol)
        qty       = round_qty(raw, step_size)
        log.info(f"  {symbol}: price={price:.4f}  raw_qty={raw:.6f}  step={step_size}  qty={qty}")
        pairs.append({
            "symbol":          symbol,
            "qty":             qty,
            "step_size":       step_size,
            "stop_loss_pct":   2.0,   # overwritten by optimiser
            "take_profit_pct": 4.0,
            "score":           row["total_score"],
        })

    send_telegram(
        f"[CHART] Pair Selection Complete\n"
        f"Selected: {', '.join(p['symbol'] for p in pairs)}\n"
        f"Scores: {', '.join(str(p['score']) for p in pairs)}"
    )
    return pairs


# =====================================================================
# ##  STAGE 2 -- OPTIMISER  (uses market_client for historical data)
# =====================================================================
def _backtest(df: pd.DataFrame, sl_pct: float, tp_pct: float) -> dict:
    trades, in_trade, entry, sl_p, tp_p = [], False, 0.0, 0.0, 0.0
    for _, row in df.iterrows():
        if not in_trade:
            entry    = row["open"]
            sl_p     = entry * (1 - sl_pct / 100)
            tp_p     = entry * (1 + tp_pct / 100)
            in_trade = True
            continue
        sl_hit = row["low"]  <= sl_p
        tp_hit = row["high"] >= tp_p
        if sl_hit or tp_hit:
            exit_p = sl_p if (sl_hit and tp_hit) or sl_hit else tp_p
            pnl    = (exit_p - entry) / entry - MAKER_FEE * 2
            trades.append({"win": exit_p >= entry, "pnl": pnl * 100})
            in_trade = False
    if len(trades) < OPT_MIN_TRADES:
        return None
    t      = pd.DataFrame(trades)
    wins   = t["win"].sum()
    net    = t["pnl"].sum()
    cum    = t["pnl"].cumsum()
    max_dd = (cum - cum.cummax()).min()
    score  = (wins / len(t) * 100 * net) / (1 + abs(max_dd))
    return {
        "sl": sl_pct, "tp": tp_pct, "trades": len(t),
        "win_rate": round(wins / len(t) * 100, 1),
        "net_pnl":  round(net, 2),
        "max_dd":   round(max_dd, 2),
        "score":    round(score, 3),
    }


def optimise_pair(market_client: Client, symbol: str) -> tuple:
    """Returns (best_sl_pct, best_tp_pct) using live historical data."""
    log.info(f"  Optimising {symbol} ...")
    try:
        klines = market_client.get_historical_klines(symbol, OPT_INTERVAL, OPT_LOOKBACK)
    except Exception as e:
        log.warning(f"  Could not fetch data for {symbol}: {e} -- using defaults")
        return 2.0, 4.0

    df = pd.DataFrame(klines, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","qv","trades","tbb","tbq","ignore"])
    for col in ["open","high","low","close"]:
        df[col] = df[col].astype(float)

    combos  = [(sl, tp) for sl, tp in itertools.product(OPT_SL_RANGE, OPT_TP_RANGE) if tp > sl]
    results = [r for r in (_backtest(df, sl, tp) for sl, tp in combos) if r]

    if not results:
        log.warning(f"  Not enough trades to optimise {symbol} -- using defaults")
        return 2.0, 4.0

    best = max(results, key=lambda x: x["score"])
    log.info(f"  {symbol} best -> SL={best['sl']}%  TP={best['tp']}%  "
             f"WinRate={best['win_rate']}%  NetPnL={best['net_pnl']}%")
    return best["sl"], best["tp"]


def run_optimisation(market_client: Client, pairs: list) -> list:
    _dashboard_state["stage"] = "Stage 2: Optimising"
    _notify()
    log.info("=" * 60)
    log.info("STAGE 2 -- OPTIMISATION  (live historical data)")
    log.info("=" * 60)
    log.info(f"Backtesting SL/TP for {len(pairs)} pairs ...")

    for pair in pairs:
        sl, tp = optimise_pair(market_client, pair["symbol"])
        pair["stop_loss_pct"]   = sl
        pair["take_profit_pct"] = tp

    log.info("\nOptimised settings:")
    log.info(f"  {'SYMBOL':<12} {'SL%':>6} {'TP%':>6}")
    log.info(f"  {'-'*28}")
    for p in pairs:
        log.info(f"  {p['symbol']:<12} {p['stop_loss_pct']:>6}  {p['take_profit_pct']:>6}")

    send_telegram(
        "[GEAR] Optimisation Complete\n" +
        "\n".join(f"{p['symbol']}: SL={p['stop_loss_pct']}% TP={p['take_profit_pct']}%" for p in pairs)
    )
    return pairs


# =====================================================================
# TRADE HISTORY
# =====================================================================
def log_trade(symbol, action, qty, price, pnl_pct=0.0, note=""):
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol":    symbol,
        "action":    action,
        "qty":       qty,
        "price":     round(price, 6),
        "pnl_pct":   round(pnl_pct, 3),
        "note":      note,
    }
    with _csv_lock:
        exists = os.path.isfile(TRADE_FILE)
        with open(TRADE_FILE, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=row.keys())
            if not exists:
                w.writeheader()
            w.writerow(row)


# =====================================================================
# ORDER HELPERS  (use trade_client)
# =====================================================================
def market_buy(trade_client, symbol, qty, plog) -> float:
    plog.info(f"BUY  {qty} {symbol}")
    for attempt in range(3):
        try:
            order = trade_client.order_market_buy(symbol=symbol, quantity=qty)
            price = float(order.get("fills",[{}])[0].get("price", 0)) or get_price(trade_client, symbol)
            log_trade(symbol, "BUY", qty, price, note="market")
            send_telegram(f"[OK] BUY  {qty} {symbol} @ {price:.4f}")
            _notify()
            return price
        except BinanceAPIException as e:
            if e.code == -1022 and attempt < 2:
                plog.warning(f"Signature error on BUY attempt {attempt+1} -- resyncing clock ...")
                _sync_timestamp(trade_client, symbol)
                time.sleep(0.5)
            elif e.code in (-1100, -2010, -1013):
                # Insufficient balance or order would trigger immediately
                # Don't retry -- just raise so _step can handle gracefully
                plog.warning(f"BUY rejected (code {e.code}) -- insufficient balance or invalid order")
                raise
            else:
                raise
    raise RuntimeError(f"BUY failed after 3 attempts for {symbol}")


def market_sell(trade_client, symbol, qty, entry, plog, note="market") -> float:
    plog.info(f"SELL {qty} {symbol}  [{note}]")
    for attempt in range(3):
        try:
            order = trade_client.order_market_sell(symbol=symbol, quantity=qty)
            price = float(order.get("fills",[{}])[0].get("price", 0)) or get_price(trade_client, symbol)
            pnl   = (price - entry) / entry * 100 if entry else 0
            log_trade(symbol, "SELL", qty, price, pnl_pct=pnl, note=note)
            emoji = "[PROFIT]" if pnl >= 0 else "[LOSS]"
            send_telegram(f"{emoji} SELL {qty} {symbol} @ {price:.4f}  PnL: {pnl:+.2f}%  [{note}]")
            _notify()
            return price
        except BinanceAPIException as e:
            if e.code == -1022 and attempt < 2:
                plog.warning(f"Signature error on SELL attempt {attempt+1} -- resyncing clock ...")
                _sync_timestamp(trade_client, symbol)
                time.sleep(0.5)
            else:
                raise
    raise RuntimeError(f"SELL failed after 3 attempts for {symbol}")


# =====================================================================
# ##  STAGE 3 -- PER-PAIR TRADING THREAD  (uses trade_client)
# =====================================================================
class PairBot(threading.Thread):
    def __init__(self, trade_client, config, stop_event):
        super().__init__(name=config["symbol"], daemon=True)
        self.client    = trade_client
        self.symbol    = config["symbol"]
        self.step_size = config.get("step_size", 0.001)
        self.qty       = round_qty(config["qty"], self.step_size)
        self.sl_pct    = config["stop_loss_pct"]
        self.tp_pct    = config["take_profit_pct"]
        self.stop     = stop_event
        self.in_trade = False
        self.entry    = 0.0
        self.sl_price = 0.0
        self.tp_price = 0.0
        self.pnl_pct  = 0.0
        self.log      = logging.getLogger(self.symbol)

    def update_settings(self, sl_pct, tp_pct):
        """Hot-reload SL/TP between trades."""
        if not self.in_trade:
            self.sl_pct = sl_pct
            self.tp_pct = tp_pct
            self.log.info(f"Settings updated -> SL={sl_pct}%  TP={tp_pct}%")

    def run(self):
        self.log.info(f"Started  SL={self.sl_pct}%  TP={self.tp_pct}%  qty={self.qty}")
        # Re-sync clock for this thread -- each thread needs its own sync
        _sync_timestamp(self.client, self.symbol)
        # Check if we are already holding this asset from a previous run
        in_trade, entry, sl_p, tp_p, qty = recover_position(
            self.client, self.symbol, self.qty,
            self.step_size, self.sl_pct, self.tp_pct, self.log
        )
        if in_trade:
            self.in_trade = True
            self.entry    = entry
            self.sl_price = sl_p
            self.tp_price = tp_p
            self.qty      = qty
        while not self.stop.is_set():
            try:
                self._step()
            except BinanceAPIException as e:
                self.log.error(f"API error: {e}")
                send_telegram(f"[WARN] [{self.symbol}] API error: {e}")
            except Exception as e:
                self.log.error(f"Error: {e}", exc_info=True)
            for _ in range(CHECK_INTERVAL_SEC * 2):
                if self.stop.is_set():
                    break
                time.sleep(0.5)

        if self.in_trade and CLOSE_ON_EXIT:
            try:
                market_sell(self.client, self.symbol, self.qty,
                            self.entry, self.log, note="shutdown")
                _release_symbol(self.symbol)
            except Exception as e:
                self.log.error(f"Failed to close on exit: {e}")
        self.log.info("Stopped.")

    def _step(self):
        price = get_price(self.client, self.symbol)
        if not self.in_trade:
            # Check we have enough balance before attempting a buy
            quote_asset  = "USDC"
            free_balance = get_free_balance(self.client, quote_asset)
            cost_needed  = self.qty * price * 1.01  # 1% buffer for price movement

            if free_balance < cost_needed:
                self.log.warning(
                    f"Insufficient {quote_asset} balance to buy {self.symbol} "
                    f"(need ~{cost_needed:.2f}, have {free_balance:.2f}) -- waiting ..."
                )
                _notify()
                return  # Skip this cycle, try again next interval

            # Claim symbol to prevent another thread buying the same coin simultaneously
            if not _claim_symbol(self.symbol):
                self.log.warning(f"{self.symbol} already held by another thread -- skipping")
                return

            self.entry    = market_buy(self.client, self.symbol, self.qty, self.log)
            self.sl_price = self.entry * (1 - self.sl_pct / 100)
            self.tp_price = self.entry * (1 + self.tp_pct / 100)
            self.in_trade = True
            self.pnl_pct  = 0.0
        else:
            self.pnl_pct = (price - self.entry) / self.entry * 100
            self.log.info(f"price={price:.4f}  PnL={self.pnl_pct:+.2f}%  "
                          f"SL={self.sl_price:.4f}  TP={self.tp_price:.4f}")
            _notify()
            if price <= self.sl_price:
                market_sell(self.client, self.symbol, self.qty,
                            self.entry, self.log, note="stop-loss")
                self.in_trade = False
                _release_symbol(self.symbol)
            elif price >= self.tp_price:
                market_sell(self.client, self.symbol, self.qty,
                            self.entry, self.log, note="take-profit")
                self.in_trade = False
                _release_symbol(self.symbol)


# =====================================================================
# PORTFOLIO SUMMARY
# =====================================================================
def print_portfolio(bots):
    print("\n" + "=" * 65)
    print(f"  PORTFOLIO  --  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print("=" * 65)
    print(f"  {'PAIR':<12} {'STATUS':<11} {'SL%':>5} {'TP%':>5} {'ENTRY':>10} {'PnL%':>8}")
    print("  " + "-" * 60)
    for b in bots:
        status = "IN TRADE" if b.in_trade else "WAITING"
        entry  = f"{b.entry:.4f}" if b.in_trade else "-"
        pnl    = f"{b.pnl_pct:+.2f}%" if b.in_trade else "-"
        print(f"  {b.symbol:<12} {status:<11} {b.sl_pct:>5} {b.tp_pct:>5} {entry:>10} {pnl:>8}")
    print("=" * 65 + "\n")


# =====================================================================
# ##  AUTO-RESCAN  (uses market_client for fresh data)
# =====================================================================
def rescan(market_client, trade_client, bots, stop_event):
    rescan_log = logging.getLogger("rescan")
    while not stop_event.is_set():
        for _ in range(RESCAN_INTERVAL_HRS * 3600):
            if stop_event.is_set():
                return
            time.sleep(1)

        rescan_log.info("Starting scheduled rescan ...")
        send_telegram(f"[RESCAN] Scheduled rescan starting (every {RESCAN_INTERVAL_HRS}h)")
        try:
            # Recalculate how many pairs we can afford with current balance
            try:
                import builtins
                bal = trade_client.get_asset_balance(asset="USDC")
                eur  = float(bal["free"]) if bal else 0.0
                if TOP_N == 0:
                    cost_per_pair = TRADE_VALUE_USDC * BALANCE_BUFFER
                    auto_n = max(1, min(int(eur // cost_per_pair), MAX_PAIRS))
                    builtins._AUTO_TOP_N = auto_n
                    rescan_log.info(f"Rescan auto TOP_N: {auto_n} pairs (USDC balance: {eur:.2f})")
            except Exception as e:
                rescan_log.warning(f"Could not recalculate TOP_N: {e}")

            new_pairs = run_pair_selection(market_client, trade_client)
            new_pairs = run_optimisation(market_client, new_pairs)

            new_cfg          = {p["symbol"]: p for p in new_pairs}
            existing_symbols = {b.symbol for b in bots}
            new_symbols      = set(new_cfg.keys())

            for bot in bots:
                if bot.symbol in new_cfg:
                    cfg = new_cfg[bot.symbol]
                    bot.update_settings(cfg["stop_loss_pct"], cfg["take_profit_pct"])

            added   = new_symbols - existing_symbols
            removed = existing_symbols - new_symbols
            if added:
                rescan_log.info(f"New top pairs identified: {added}")
                send_telegram(f"[INFO] New top pairs: {', '.join(added)}\n(Restart bot to trade them)")
            if removed:
                rescan_log.info(f"Pairs dropped from top {TOP_N}: {removed}")

        except Exception as e:
            rescan_log.error(f"Rescan failed: {e}", exc_info=True)
            send_telegram(f"[WARN] Rescan failed: {e}")


# =====================================================================
# ##  MAIN
# =====================================================================
# =====================================================================
# WEB DASHBOARD SERVER  (real-time via Server-Sent Events)
# =====================================================================
import queue as _queue_module

_dashboard_state = {
    "bots":       [],
    "start_time": None,
    "mode":       "TESTNET" if TESTNET else "LIVE",
    "stage":      "Starting...",
}

_sse_clients = []
_sse_lock    = threading.Lock()


def _push_update():
    data = _get_api_data()
    msg  = ("data: " + json.dumps(data) + "\n\n").encode("utf-8")
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)


def _notify():
    threading.Thread(target=_push_update, daemon=True).start()


def _get_trade_history():
    if not os.path.isfile(TRADE_FILE):
        return []
    try:
        rows = []
        with open(TRADE_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        return rows[-200:]
    except Exception:
        return []


def _get_pair_scores():
    if not os.path.isfile(SCORES_FILE):
        return []
    try:
        rows = []
        with open(SCORES_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        rows.sort(key=lambda x: float(x.get("total_score", 0)), reverse=True)
        return rows[:20]
    except Exception:
        return []


def _get_api_data():
    bots   = _dashboard_state.get("bots", [])
    trades = _get_trade_history()
    scores = _get_pair_scores()
    start  = _dashboard_state.get("start_time")
    uptime = ""
    if start:
        delta = datetime.now(timezone.utc) - start
        h, rem = divmod(int(delta.total_seconds()), 3600)
        m, s   = divmod(rem, 60)
        uptime = f"{h}h {m}m {s}s"

    positions = []
    for b in bots:
        positions.append({
            "symbol":   b.symbol,
            "status":   "IN TRADE" if b.in_trade else "WAITING",
            "sl_pct":   b.sl_pct,
            "tp_pct":   b.tp_pct,
            "entry":    round(b.entry, 6) if b.in_trade else None,
            "pnl_pct":  round(b.pnl_pct, 3) if b.in_trade else None,
            "qty":      b.qty,
        })

    now_ts = datetime.now(timezone.utc)
    pnl_24h = 0.0
    wins_24h = losses_24h = 0
    pnl_chart = []
    cumulative = 0.0
    for t in trades:
        if t.get("action") == "SELL":
            try:
                ts      = datetime.fromisoformat(t["timestamp"].replace("Z", ""))
                pnl     = float(t.get("pnl_pct", 0))
                val     = float(t.get("price", 0)) * float(t.get("qty", 0))
                eur_pnl = val * pnl / 100
                cumulative += eur_pnl
                pnl_chart.append({"time": t["timestamp"][:16], "pnl": round(cumulative, 2)})
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if (now_ts - ts).total_seconds() < 86400:
                    pnl_24h += eur_pnl
                    if pnl >= 0:
                        wins_24h += 1
                    else:
                        losses_24h += 1
            except Exception:
                pass

    return {
        "stage":        _dashboard_state.get("stage", "Running"),
        "mode":         _dashboard_state.get("mode", "TESTNET"),
        "uptime":       uptime,
        "timestamp":    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "positions":    positions,
        "trades":       trades[-50:][::-1],
        "scores":       scores,
        "pnl_24h":      round(pnl_24h, 2),
        "wins_24h":     wins_24h,
        "losses_24h":   losses_24h,
        "pnl_chart":    pnl_chart[-100:],
        "total_pairs":  len(bots),
        "active_pairs": sum(1 for b in bots if b.in_trade),
    }


_DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Binance Auto Trader</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@400;500;600&display=swap');
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0a0a0f;--bg2:#111118;--bg3:#1a1a24;--border:rgba(255,255,255,0.07);--text:#e2e8f0;--muted:#64748b;--hint:#334155;--gold:#F0B90B;--gold2:rgba(240,185,11,0.12);--green:#22c55e;--green2:rgba(34,197,94,0.1);--red:#ef4444;--red2:rgba(239,68,68,0.1);--blue:#3b82f6;--blue2:rgba(59,130,246,0.1);--radius:14px}
body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;min-height:100vh}
.shell{display:grid;grid-template-rows:auto auto 1fr;min-height:100vh}
.topbar{background:var(--bg2);border-bottom:1px solid var(--border);padding:0 24px;height:56px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.topbar-l{display:flex;align-items:center;gap:12px}
.logo{width:32px;height:32px;background:var(--gold);border-radius:8px;display:flex;align-items:center;justify-content:center;font-family:'Space Mono',monospace;font-weight:700;font-size:13px;color:#000}
.app-name{font-family:'Space Mono',monospace;font-size:12px;font-weight:700;color:var(--gold);letter-spacing:2px;text-transform:uppercase}
.topbar-r{display:flex;align-items:center;gap:12px}
.badge{padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;font-family:'Space Mono',monospace;letter-spacing:1px;text-transform:uppercase}
.badge-live{background:rgba(34,197,94,.12);color:#22c55e;border:1px solid rgba(34,197,94,.2)}
.badge-test{background:rgba(234,179,8,.12);color:#eab308;border:1px solid rgba(234,179,8,.2)}
.badge-stage{background:var(--blue2);color:var(--blue);border:1px solid rgba(59,130,246,.2)}
.uptime{font-size:11px;color:var(--muted);font-family:'Space Mono',monospace}
.live-dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 6px var(--green)}
.live-dot.off{background:var(--muted);box-shadow:none}
.live-label{font-size:11px;color:var(--green);font-family:'Space Mono',monospace}
.live-label.off{color:var(--muted)}
.nav{background:var(--bg2);border-bottom:1px solid var(--border);padding:0 24px;display:flex;gap:4px}
.nav-btn{padding:12px 16px;font-size:13px;font-weight:500;color:var(--muted);cursor:pointer;border:none;background:none;border-bottom:2px solid transparent;transition:.15s;white-space:nowrap}
.nav-btn:hover{color:var(--text)}
.nav-btn.active{color:var(--gold);border-bottom-color:var(--gold)}
.content{padding:24px;overflow-y:auto}
.page{display:none}.page.active{display:block}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:24px}
.stat{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:16px 18px}
.stat-label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}
.stat-val{font-family:'Space Mono',monospace;font-size:24px;font-weight:700;color:var(--text);line-height:1}
.stat-sub{font-size:11px;color:var(--muted);margin-top:4px}
.stat.gold{background:linear-gradient(135deg,rgba(240,185,11,.1),rgba(240,185,11,.04));border-color:rgba(240,185,11,.2)}
.stat.gold .stat-val{color:var(--gold)}
.stat.green .stat-val{color:var(--green)}
.stat.red .stat-val{color:var(--red)}
.updated{font-size:11px;color:var(--hint);font-family:'Space Mono',monospace;text-align:right;margin-bottom:16px}
.section-title{font-family:'Space Mono',monospace;font-size:10px;color:var(--hint);letter-spacing:2px;text-transform:uppercase;margin-bottom:12px;margin-top:24px}
.tbl-wrap{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;margin-bottom:24px}
.tbl{width:100%;border-collapse:collapse}
.tbl th{padding:10px 16px;text-align:left;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;border-bottom:1px solid var(--border);background:var(--bg3);font-weight:500}
.tbl td{padding:12px 16px;font-size:13px;border-bottom:1px solid rgba(255,255,255,.03)}
.tbl tr:last-child td{border-bottom:none}
.tbl tr:hover td{background:rgba(255,255,255,.02)}
.tbl tr.new-row{animation:rowflash .8s ease}
@keyframes rowflash{0%{background:rgba(240,185,11,.15)}100%{background:transparent}}
.mono{font-family:'Space Mono',monospace;font-size:12px}
.pill{padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600;display:inline-block}
.pill-green{background:var(--green2);color:var(--green)}
.pill-red{background:var(--red2);color:var(--red)}
.pill-gold{background:var(--gold2);color:var(--gold)}
.pill-blue{background:var(--blue2);color:var(--blue)}
.pill-gray{background:rgba(100,116,139,.1);color:var(--muted)}
.chart-wrap{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:20px;margin-bottom:24px}
.chart-container{position:relative;height:220px}
.score-bar-bg{background:var(--bg3);border-radius:4px;height:6px;width:100%}
.score-bar-fill{height:6px;border-radius:4px;background:linear-gradient(90deg,var(--gold),#F8D12F);transition:width .6s}
.empty{text-align:center;padding:40px 20px;color:var(--muted);font-size:13px}
.conn-banner{background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.2);border-radius:10px;padding:10px 16px;font-size:12px;color:#fca5a5;margin-bottom:16px;display:none}
@media(max-width:600px){.topbar{padding:0 12px}.content{padding:12px}.stat-grid{grid-template-columns:repeat(2,1fr)}.nav-btn{padding:10px 10px;font-size:12px}}
</style>
</head>
<body>
<div class="shell">
  <div>
    <div class="topbar">
      <div class="topbar-l">
        <div class="logo">B</div>
        <span class="app-name">Auto Trader</span>
        <span class="badge badge-stage" id="stage-badge">Starting</span>
      </div>
      <div class="topbar-r">
        <span class="badge" id="mode-badge">--</span>
        <span class="uptime" id="uptime">--</span>
        <div class="live-dot" id="live-dot"></div>
        <span class="live-label" id="live-label">LIVE</span>
      </div>
    </div>
    <div class="nav">
      <button class="nav-btn active" onclick="showPage('overview',this)">Overview</button>
      <button class="nav-btn" onclick="showPage('portfolio',this)">Portfolio</button>
      <button class="nav-btn" onclick="showPage('trades',this)">Trade History</button>
      <button class="nav-btn" onclick="showPage('scores',this)">Pair Scores</button>
    </div>
  </div>
  <div class="conn-banner" id="conn-banner">Disconnected -- reconnecting...</div>
  <div class="content">
    <div class="updated" id="updated">--</div>
    <div class="page active" id="page-overview">
      <div class="stat-grid">
        <div class="stat gold"><div class="stat-label">24h PnL</div><div class="stat-val" id="stat-pnl24">--</div><div class="stat-sub" id="stat-pnl24-sub">-- wins / -- losses</div></div>
        <div class="stat"><div class="stat-label">Active Pairs</div><div class="stat-val" id="stat-active">--</div><div class="stat-sub" id="stat-total">of -- pairs</div></div>
        <div class="stat green"><div class="stat-label">Wins 24h</div><div class="stat-val" id="stat-wins">--</div><div class="stat-sub">profitable trades</div></div>
        <div class="stat red"><div class="stat-label">Losses 24h</div><div class="stat-val" id="stat-losses">--</div><div class="stat-sub">stop-loss hits</div></div>
      </div>
      <div class="section-title">PnL Over Time (USDC)</div>
      <div class="chart-wrap"><div class="chart-container"><canvas id="pnl-chart"></canvas></div></div>
      <div class="section-title">Open Positions</div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Pair</th><th>Status</th><th>Entry</th><th>SL%</th><th>TP%</th><th>PnL%</th></tr></thead><tbody id="overview-positions"></tbody></table></div>
    </div>
    <div class="page" id="page-portfolio">
      <div class="stat-grid">
        <div class="stat"><div class="stat-label">Total Pairs</div><div class="stat-val" id="port-total">--</div><div class="stat-sub">trading threads</div></div>
        <div class="stat green"><div class="stat-label">In Trade</div><div class="stat-val" id="port-active">--</div><div class="stat-sub">holding positions</div></div>
        <div class="stat"><div class="stat-label">Waiting</div><div class="stat-val" id="port-waiting">--</div><div class="stat-sub">looking to enter</div></div>
        <div class="stat gold"><div class="stat-label">Win Rate 24h</div><div class="stat-val" id="port-winrate">--</div><div class="stat-sub">last 24 hours</div></div>
      </div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Pair</th><th>Status</th><th>Qty</th><th>Entry</th><th>SL%</th><th>TP%</th><th>PnL%</th></tr></thead><tbody id="portfolio-table"></tbody></table></div>
    </div>
    <div class="page" id="page-trades">
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Time</th><th>Pair</th><th>Action</th><th>Qty</th><th>Price</th><th>PnL%</th><th>Note</th></tr></thead><tbody id="trades-table"></tbody></table></div>
    </div>
    <div class="page" id="page-scores">
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Pair</th><th>Score</th><th>Volume</th><th>24h%</th><th>Range%</th><th>RSI</th><th>SMA</th></tr></thead><tbody id="scores-table"></tbody></table></div>
    </div>
  </div>
</div>
<script>
let pnlChart=null,lastTradeCount=0,connected=false;
function showPage(n,b){document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));document.querySelectorAll('.nav-btn').forEach(x=>x.classList.remove('active'));document.getElementById('page-'+n).classList.add('active');if(b)b.classList.add('active')}
function fmt2(n){return parseFloat(n).toFixed(2)}
function fmtPct(n){const v=parseFloat(n);return(v>=0?'+':'')+v.toFixed(2)+'%'}
function pnlPill(v){const n=parseFloat(v);if(isNaN(n))return'<span class="pill pill-gray">--</span>';if(n>0)return'<span class="pill pill-green">'+fmtPct(n)+'</span>';if(n<0)return'<span class="pill pill-red">'+fmtPct(n)+'</span>';return'<span class="pill pill-gray">0.00%</span>'}
function setConnected(ok){connected=ok;const d=document.getElementById('live-dot'),l=document.getElementById('live-label'),b=document.getElementById('conn-banner');d.className='live-dot'+(ok?'':' off');l.className='live-label'+(ok?'':' off');l.textContent=ok?'LIVE':'OFFLINE';b.style.display=ok?'none':'block'}
function render(d){
  document.getElementById('stage-badge').textContent=d.stage||'Running';
  const mb=document.getElementById('mode-badge');mb.textContent=d.mode;mb.className='badge '+(d.mode==='LIVE'?'badge-live':'badge-test');
  document.getElementById('uptime').textContent=d.uptime||'--';
  document.getElementById('updated').textContent='Last update: '+d.timestamp;
  const p24=d.pnl_24h||0;
  document.getElementById('stat-pnl24').textContent=(p24>=0?'+':'')+fmt2(p24)+' USDC';
  document.getElementById('stat-pnl24').parentElement.className='stat '+(p24>=0?'green':'red');
  document.getElementById('stat-pnl24-sub').textContent=d.wins_24h+' wins / '+d.losses_24h+' losses';
  document.getElementById('stat-active').textContent=d.active_pairs;
  document.getElementById('stat-total').textContent='of '+d.total_pairs+' pairs';
  document.getElementById('stat-wins').textContent=d.wins_24h;
  document.getElementById('stat-losses').textContent=d.losses_24h;
  document.getElementById('port-total').textContent=d.total_pairs;
  document.getElementById('port-active').textContent=d.active_pairs;
  document.getElementById('port-waiting').textContent=d.total_pairs-d.active_pairs;
  const t24=d.wins_24h+d.losses_24h;document.getElementById('port-winrate').textContent=t24>0?Math.round(d.wins_24h/t24*100)+'%':'--';
  let posH='',portH='';
  if(!d.positions||!d.positions.length){posH='<tr><td colspan="6" class="empty">No positions yet</td></tr>';portH='<tr><td colspan="7" class="empty">No positions yet</td></tr>'}
  else d.positions.forEach(p=>{const sp=p.status==='IN TRADE'?'<span class="pill pill-gold">IN TRADE</span>':'<span class="pill pill-gray">WAITING</span>';posH+='<tr><td class="mono">'+p.symbol+'</td><td>'+sp+'</td><td class="mono">'+(p.entry||'--')+'</td><td class="mono">'+p.sl_pct+'%</td><td class="mono">'+p.tp_pct+'%</td><td>'+(p.pnl_pct!=null?pnlPill(p.pnl_pct):'--')+'</td></tr>';portH+='<tr><td class="mono">'+p.symbol+'</td><td>'+sp+'</td><td class="mono">'+p.qty+'</td><td class="mono">'+(p.entry||'--')+'</td><td class="mono">'+p.sl_pct+'%</td><td class="mono">'+p.tp_pct+'%</td><td>'+(p.pnl_pct!=null?pnlPill(p.pnl_pct):'--')+'</td></tr>'});
  document.getElementById('overview-positions').innerHTML=posH;
  document.getElementById('portfolio-table').innerHTML=portH;
  const nc=d.trades?d.trades.length:0;let trH='';
  if(!d.trades||!d.trades.length)trH='<tr><td colspan="7" class="empty">No trades yet</td></tr>';
  else d.trades.forEach((t,i)=>{const isNew=i<(nc-lastTradeCount)&&lastTradeCount>0;const ap=t.action==='BUY'?'<span class="pill pill-blue">BUY</span>':'<span class="pill '+(parseFloat(t.pnl_pct)>=0?'pill-green':'pill-red')+'">SELL</span>';trH+='<tr'+(isNew?' class="new-row"':'')+'><td class="mono" style="font-size:11px">'+(t.timestamp||'').slice(0,16)+'</td><td class="mono">'+(t.symbol||'--')+'</td><td>'+ap+'</td><td class="mono">'+parseFloat(t.qty||0).toFixed(4)+'</td><td class="mono">'+parseFloat(t.price||0).toFixed(4)+'</td><td>'+(t.action==='SELL'?pnlPill(t.pnl_pct):'--')+'</td><td style="font-size:11px;color:var(--muted)">'+(t.note||'')+'</td></tr>'});
  document.getElementById('trades-table').innerHTML=trH;lastTradeCount=nc;
  let scH='';
  if(!d.scores||!d.scores.length)scH='<tr><td colspan="7" class="empty">Waiting for Stage 1...</td></tr>';
  else d.scores.forEach((s,i)=>{const sc=parseFloat(s.total_score||0),pct=Math.round(sc/100*100),chg=parseFloat(s.change_24h_pct||0);scH+='<tr><td class="mono">'+(i<4?'<span class="pill pill-gold" style="margin-right:6px">#'+(i+1)+'</span>':'')+s.symbol+'</td><td><div style="display:flex;align-items:center;gap:8px"><div class="score-bar-bg" style="width:80px"><div class="score-bar-fill" style="width:'+pct+'%"></div></div><span class="mono" style="font-size:11px">'+sc.toFixed(1)+'</span></div></td><td class="mono">$'+parseFloat(s.volume_24h_M||0).toFixed(0)+'M</td><td class="mono" style="color:'+(chg>=0?'var(--green)':'var(--red))+'">'+(chg>=0?'+':'')+chg.toFixed(2)+'%</td><td class="mono">'+parseFloat(s.daily_range_pct||0).toFixed(2)+'%</td><td class="mono">'+parseFloat(s.rsi_14||0).toFixed(1)+'</td><td class="mono">'+parseFloat(s.sma_dist||0).toFixed(2)+'%</td></tr>'});
  document.getElementById('scores-table').innerHTML=scH;
  const labels=d.pnl_chart.map(p=>p.time),vals=d.pnl_chart.map(p=>p.pnl);
  const lc=vals.length&&vals[vals.length-1]>=0?'#22c55e':'#ef4444';
  if(!pnlChart){const ctx=document.getElementById('pnl-chart').getContext('2d');pnlChart=new Chart(ctx,{type:'line',data:{labels,datasets:[{label:'PnL (USDC)',data:vals,borderColor:lc,backgroundColor:lc==='#22c55e'?'rgba(34,197,94,0.08)':'rgba(239,68,68,0.08)',borderWidth:2,pointRadius:0,fill:true,tension:0.3}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{display:false},y:{grid:{color:'rgba(255,255,255,0.05)'},ticks:{color:'#64748b',font:{family:'Space Mono',size:10},callback:v=>(v>=0?'+':'')+v.toFixed(2)+' USDC'}}}}})}
  else{pnlChart.data.labels=labels;pnlChart.data.datasets[0].data=vals;pnlChart.data.datasets[0].borderColor=lc;pnlChart.update('none')}
}
function connectSSE(){
  const es=new EventSource('/stream');
  es.onopen=()=>setConnected(true);
  es.onmessage=e=>{try{render(JSON.parse(e.data))}catch(err){}};
  es.onerror=()=>{setConnected(false);es.close();setTimeout(connectSSE,3000)};
}
connectSSE();
</script>
</body>
</html>
"""

class _DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path == "/":
            body = _DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/api/data":
            body = json.dumps(_get_api_data()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type",  "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection",    "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            q = _queue_module.Queue(maxsize=10)
            with _sse_lock:
                _sse_clients.append(q)

            try:
                init = ("data: " + json.dumps(_get_api_data()) + "\n\n").encode("utf-8")
                self.wfile.write(init)
                self.wfile.flush()
            except Exception:
                pass

            try:
                while True:
                    try:
                        msg = q.get(timeout=25)
                        self.wfile.write(msg)
                        self.wfile.flush()
                    except _queue_module.Empty:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
            except Exception:
                pass
            finally:
                with _sse_lock:
                    if q in _sse_clients:
                        _sse_clients.remove(q)
        else:
            self.send_error(404)


def start_dashboard(bots):
    _dashboard_state["bots"]       = bots
    _dashboard_state["start_time"] = datetime.now(timezone.utc)
    _dashboard_state["mode"]       = "TESTNET" if TESTNET else "LIVE"
    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer(("localhost", DASHBOARD_PORT), _DashboardHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True, name="dashboard")
    t.start()
    log.info(f"Dashboard running at http://localhost:{DASHBOARD_PORT}")
    return server


if __name__ == "__main__":
    print("\n" + "=" * 62)
    print("  Binance Auto Trading Suite")
    print(f"  Trading mode: {'TESTNET (fake money)' if TESTNET else 'LIVE (REAL MONEY)'}")
    print("  Market data:  Live Binance (always)")
    print("=" * 62 + "\n")

    # -- Create both clients -------------------------------------------
    market_client, trade_client = create_clients()

    # -- Check for updates --------------------------------------------------
    if AUTO_UPDATE and _check_for_updates:
        log.info("Checking for updates ...")
        _check_for_updates(GITHUB_USER, GITHUB_REPO, GITHUB_BRANCH)
        # If updated, bot auto-restarts via os.execv -- we continue if already latest

    # -- Verify trading connection -------------------------------------
    try:
        trade_client.get_server_time()
        log.info("Trading connection verified.")
    except BinanceAPIException as e:
        log.error(f"Trading connection failed: {e}")
        log.error("Check your API_KEY and API_SECRET are correct.")
        raise SystemExit(1)

    # -- Deep API key test + auto TOP_N calculation -------------------
    log.info("Testing API key with authenticated request ...")
    try:
        balance = trade_client.get_asset_balance(asset="USDC")
        eur     = float(balance["free"]) if balance else 0.0
        log.info(f"API key OK  --  USDC balance: {eur:.2f}")

        # Auto-calculate how many pairs we can safely trade
        if TOP_N == 0:
            # Each pair needs TRADE_VALUE_USDT * BALANCE_BUFFER reserved
            cost_per_pair = TRADE_VALUE_USDC * BALANCE_BUFFER
            auto_n = int(eur // cost_per_pair)
            auto_n = max(1, min(auto_n, MAX_PAIRS))  # at least 1, at most MAX_PAIRS
            log.info(
                f"Auto TOP_N: USDC {eur:.2f} / (USDC {TRADE_VALUE_USDC} x {BALANCE_BUFFER} buffer) "
                f"= {auto_n} pairs"
            )
            # Override the global TOP_N for this session
            import builtins
            builtins._AUTO_TOP_N = auto_n
        else:
            builtins._AUTO_TOP_N = TOP_N
            if eur < TRADE_VALUE_USDC * TOP_N * BALANCE_BUFFER:
                log.warning(
                    f"Low balance: {eur:.2f} USDC available. "
                    f"Recommended minimum: {TRADE_VALUE_USDC * TOP_N * BALANCE_BUFFER:.2f} USDC"
                )
    except BinanceAPIException as e:
        if e.code == -1022:
            log.error("=" * 60)
            log.error("API KEY ERROR: Signature invalid (-1022)")
            log.error("Most likely cause: Testnet API keys have expired.")
            log.error("Fix:")
            log.error("  1. Go to testnet.binance.vision")
            log.error("  2. Log in with GitHub")
            log.error("  3. Click 'Generate HMAC_SHA256 Key'")
            log.error("  4. Copy NEW API Key and Secret into auto_trader.py")
            log.error("=" * 60)
            raise SystemExit(1)
        elif e.code == -2015:
            log.error("=" * 60)
            log.error("API KEY ERROR: Invalid API key or wrong testnet/live setting.")
            log.error("Fix:")
            log.error("  - Testnet keys -> make sure TESTNET = True")
            log.error("  - Live keys    -> make sure TESTNET = False")
            log.error("  - Check API_KEY and API_SECRET have no extra spaces")
            log.error("=" * 60)
            raise SystemExit(1)
        else:
            log.error(f"API key test failed: {e}")
            raise SystemExit(1)

    # -- Portfolio cleanup: sell excess positions from previous runs ----
    _dashboard_state["stage"] = "Startup: Checking portfolio"
    _notify()
    cleanup_portfolio(trade_client, market_client)

    # -- Stage 1: Select pairs using live market data ------------------
    _dashboard_state["stage"] = "Stage 1: Scanning pairs"
    _notify()
    pairs = run_pair_selection(market_client, trade_client)

    # -- Stage 2: Optimise using live historical data ------------------
    pairs = run_optimisation(market_client, pairs)

    # -- Stage 3: Launch trading threads ------------------------------
    _dashboard_state["stage"] = "Stage 3: Trading"
    _notify()
    log.info("=" * 60)
    log.info("STAGE 3 -- TRADING")
    log.info("=" * 60)
    log.info(f"Launching {len(pairs)} trading threads ...")

    stop_event = threading.Event()
    bots = []
    for cfg in pairs:
        bot = PairBot(trade_client, cfg, stop_event)
        bot.start()
        bots.append(bot)
        time.sleep(1)

    # -- Start web dashboard -------------------------------------------
    if DASHBOARD_ENABLED:
        start_dashboard(bots)
        print(f"\n  Dashboard: http://localhost:{DASHBOARD_PORT}")
        print("  Open that URL in your browser.\n")
        _dashboard_state["stage"] = "Trading"
        _notify()

    # -- Background rescan thread --------------------------------------
    threading.Thread(
        target=rescan, args=(market_client, trade_client, bots, stop_event),
        daemon=True, name="rescan"
    ).start()
    log.info(f"Auto-rescan scheduled every {RESCAN_INTERVAL_HRS} hours.")

    send_telegram(
        f"[START] Auto Trading Suite Started\n"
        f"Mode: {'TESTNET' if TESTNET else 'LIVE'}\n"
        f"Pairs: {', '.join(p['symbol'] for p in pairs)}\n" +
        "\n".join(f"  {p['symbol']}: SL={p['stop_loss_pct']}% TP={p['take_profit_pct']}%" for p in pairs)
    )

    # -- Main loop: portfolio summary ----------------------------------
    last_summary = 0
    try:
        while True:
            time.sleep(1)
            if time.time() - last_summary >= 120:
                print_portfolio(bots)
                last_summary = time.time()
    except KeyboardInterrupt:
        log.info("Shutdown requested ...")
        send_telegram("[STOP] Auto Trading Suite shutting down ...")
        stop_event.set()
        for bot in bots:
            bot.join(timeout=15)
        print_portfolio(bots)
        log.info("All bots stopped.")
        send_telegram("[OK] All bots stopped.")
