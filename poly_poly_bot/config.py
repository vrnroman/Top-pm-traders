"""Poly Poly Bot — unified configuration for all trading strategies."""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Strategy Toggles ──

# Strategy #1 auto-enables when any tier (1a/1b/1c) is enabled
STRATEGY_1A_ENABLED = os.getenv("STRATEGY_1A_ENABLED", "false").lower() == "true"
STRATEGY_1B_ENABLED = os.getenv("STRATEGY_1B_ENABLED", "false").lower() == "true"
STRATEGY_1C_ENABLED = os.getenv("STRATEGY_1C_ENABLED", "false").lower() == "true"
STRATEGY1_ENABLED = STRATEGY_1A_ENABLED or STRATEGY_1B_ENABLED or STRATEGY_1C_ENABLED
STRATEGY2_ENABLED = os.getenv("STRATEGY2_ENABLED", "true").lower() == "true"

# ── Strategy #2: Weather Betting Parameters ──

# Cities to bet on (comma-separated city keys from cities.py)
CITIES_TO_BET = [c.strip() for c in os.getenv("CITIES_TO_BET", "nyc,chicago,denver,dallas").split(",")]

# How many days in advance to look for betting opportunities
DAYS_IN_ADVANCE = int(os.getenv("DAYS_IN_ADVANCE", "4"))

# Minimum edge (model_prob - market_price) to place a bet, e.g. 0.10 = 10%
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.10"))

# USD amount per bet
BET_SIZE = float(os.getenv("BET_SIZE", "10.0"))

# Max number of bets per city per day (top N most likely buckets)
MAX_BETS_PER_CITY = int(os.getenv("MAX_BETS_PER_CITY", "2"))

# Polymarket trading fee (2%)
POLYMARKET_FEE = float(os.getenv("POLYMARKET_FEE", "0.02"))

# ── Prediction Model Parameters ──

KDE_WINDOW_DAYS = int(os.getenv("KDE_WINDOW_DAYS", "15"))
RECENCY_HALFLIFE = float(os.getenv("RECENCY_HALFLIFE", "4.0"))

# ── Schedule ──

# Auto-run Strategy #2 at this hour in SGT (Singapore Time, UTC+8)
# Default: 15 = 3:00 PM SGT = 07:00 UTC
SCHEDULE_HOUR_SGT = int(os.getenv("SCHEDULE_HOUR_SGT", "15"))
SCHEDULE_MINUTE_SGT = int(os.getenv("SCHEDULE_MINUTE_SGT", "0"))

# ── Telegram ──

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── API ──

GAMMA_API_URL = "https://gamma-api.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"

# For placing actual bets (only needed when PREVIEW_MODE=false)
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
PROXY_WALLET = os.getenv("PROXY_WALLET", "")

# ── Paths ──

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BOT_DIR, "cache")
RESULTS_DIR = os.path.join(BOT_DIR, "results")
DATA_DIR = os.path.join(BOT_DIR, "data")
LOGS_DIR = os.path.join(BOT_DIR, "logs")

# Preview mode: log signals but don't place bets
PREVIEW_MODE = os.getenv("PREVIEW_MODE", "true").lower() == "true"

# ── Strategy #3: Tennis Odds Arbitrage ──

STRATEGY3_ENABLED = os.getenv("STRATEGY3_ENABLED", "false").lower() == "true"
TENNIS_ARB_PREVIEW_MODE = os.getenv("TENNIS_ARB_PREVIEW_MODE", "true").lower() == "true"
TENNIS_ODDS_PROVIDER = os.getenv("TENNIS_ODDS_PROVIDER", "oddspapi")
ODDSPAPI_API_KEY = os.getenv("ODDSPAPI_API_KEY", "")
TENNIS_MIN_DIVERGENCE = float(os.getenv("TENNIS_MIN_DIVERGENCE", "0.10"))
TENNIS_MAX_BET_SIZE = float(os.getenv("TENNIS_MAX_BET_SIZE", "100"))
TENNIS_KELLY_FRACTION = float(os.getenv("TENNIS_KELLY_FRACTION", "0.25"))
TENNIS_SCAN_INTERVAL = int(os.getenv("TENNIS_SCAN_INTERVAL", "300"))
TENNIS_TOURNAMENTS = [t.strip() for t in os.getenv("TENNIS_TOURNAMENTS", "ATP,WTA").split(",")]
TENNIS_MIN_POLYMARKET_VOLUME = float(os.getenv("TENNIS_MIN_POLYMARKET_VOLUME", "50000"))
TENNIS_MIN_POLYMARKET_LIQUIDITY = float(os.getenv("TENNIS_MIN_POLYMARKET_LIQUIDITY", "10000"))
