#!/usr/bin/env python3
"""Unified Bot Orchestrator — Strategy #1 (Copy Traders) + Strategy #2 (Weather) + Strategy #3 (Tennis Arb).

Manages all strategies with:
- Scheduled runs (Strategy #2 at 3pm SGT daily)
- Periodic scans (Strategy #3 every N seconds)
- Telegram commands for on-demand predictions
- Separate PnL tracking per strategy
- Optional Strategy #1 (TS copy-trader bot) as subprocess

Usage:
  python main.py              # Run with defaults from .env
  python main.py --once       # Run Strategy #2 once and exit
"""

import os
import sys
import json
import signal
import logging
import argparse
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

from config import (
    STRATEGY1_ENABLED, STRATEGY2_ENABLED,
    CITIES_TO_BET, DAYS_IN_ADVANCE, MIN_EDGE, BET_SIZE,
    MAX_BETS_PER_CITY, PREVIEW_MODE,
    SCHEDULE_HOUR_SGT, SCHEDULE_MINUTE_SGT,
    DATA_DIR, LOGS_DIR, BOT_DIR,
    STRATEGY3_ENABLED, TENNIS_ARB_PREVIEW_MODE,
    TENNIS_ODDS_PROVIDER, ODDSPAPI_API_KEY,
    TENNIS_MIN_DIVERGENCE, TENNIS_MAX_BET_SIZE,
    TENNIS_KELLY_FRACTION, TENNIS_SCAN_INTERVAL,
    TENNIS_TOURNAMENTS, TENNIS_MIN_POLYMARKET_VOLUME,
    TENNIS_MIN_POLYMARKET_LIQUIDITY,
)
import telegram_bot

logger = logging.getLogger("main")

SGT = timezone(timedelta(hours=8))

# ── Strategy #1: Copy Trader subprocess ──

_s1_process: subprocess.Popen | None = None


def start_strategy1():
    """Start the TS copy-trader bot as a subprocess."""
    global _s1_process

    polymarket_dir = os.path.join(BOT_DIR, "polymarket")
    if not os.path.isdir(polymarket_dir):
        logger.error("polymarket/ directory not found, cannot start Strategy #1")
        return

    dist_index = os.path.join(polymarket_dir, "dist", "index.js")
    if not os.path.exists(dist_index):
        logger.info("Building Strategy #1 (npm run build)...")
        try:
            subprocess.run(
                ["npm", "run", "build"],
                cwd=polymarket_dir,
                check=True,
                capture_output=True,
                timeout=120,
            )
        except Exception as e:
            logger.error(f"Failed to build Strategy #1: {e}")
            return

    logger.info("Starting Strategy #1 (copy-trader bot)...")
    _s1_process = subprocess.Popen(
        ["node", "dist/index.js"],
        cwd=polymarket_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    # Log output in background
    def _stream_s1_logs():
        for line in iter(_s1_process.stdout.readline, b""):
            logger.info(f"[S1] {line.decode().rstrip()}")
        _s1_process.stdout.close()

    t = threading.Thread(target=_stream_s1_logs, daemon=True, name="s1-logs")
    t.start()
    logger.info(f"Strategy #1 started (PID: {_s1_process.pid})")


def stop_strategy1():
    """Stop the TS copy-trader bot subprocess."""
    global _s1_process
    if _s1_process and _s1_process.poll() is None:
        logger.info("Stopping Strategy #1...")
        _s1_process.terminate()
        try:
            _s1_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _s1_process.kill()
        logger.info("Strategy #1 stopped")
    _s1_process = None


# ── Strategy #2: Weather Betting ──

def run_strategy2(target_date: datetime) -> list[dict]:
    """Run a single Strategy #2 prediction cycle."""
    from weather_data import fetch_all_weather
    from weather_predictor import WeatherPredictor
    from polymarket_fetcher import fetch_markets_for_cities_and_dates
    from config import CACHE_DIR, POLYMARKET_FEE, KDE_WINDOW_DAYS, RECENCY_HALFLIFE
    from cities import CITIES

    date_str = target_date.strftime("%Y-%m-%d")
    logger.info(f"Strategy #2: Running prediction for {date_str}")

    os.makedirs(CACHE_DIR, exist_ok=True)

    # Fetch weather history
    weather_df = fetch_all_weather(CITIES_TO_BET, CACHE_DIR)
    if len(weather_df) == 0:
        logger.error("No weather data available")
        return []

    # Fetch Polymarket markets
    markets_df = fetch_markets_for_cities_and_dates(CITIES_TO_BET, [target_date])
    if len(markets_df) == 0:
        logger.warning(f"No Polymarket markets found for {date_str}")
        return []

    # Predict
    import pandas as pd
    predictor = WeatherPredictor(weather_df, window_days=KDE_WINDOW_DAYS,
                                  recency_halflife=RECENCY_HALFLIFE)

    signals = []
    for city_key in CITIES_TO_BET:
        city_info = CITIES.get(city_key, {})
        if not city_info:
            continue
        city_name = city_info.get("name", city_key)
        unit = city_info.get("unit", "fahrenheit")

        city_markets = markets_df[
            (markets_df["city"] == city_key) &
            (markets_df["target_date"] == pd.Timestamp(target_date))
        ]
        if len(city_markets) == 0:
            continue

        buckets = []
        for _, m in city_markets.iterrows():
            buckets.append({
                "temp": m["bucket_temp"],
                "temp_high": m.get("temp_high", m["bucket_temp"]),
                "is_lower": m["is_lower"],
                "is_upper": m["is_upper"],
                "label": m["bucket_label"],
            })

        probs, dist = predictor.predict_buckets(city_key, target_date, buckets)

        city_signals = []
        for _, m in city_markets.iterrows():
            label = m["bucket_label"]
            market_price = m["yes_price"]
            if market_price is None or market_price <= 0.01 or market_price >= 0.95:
                continue

            model_prob = probs.get(label, 0.0)
            edge = model_prob - market_price

            if edge >= MIN_EDGE:
                ev = (model_prob * (BET_SIZE / market_price - BET_SIZE * (1 + POLYMARKET_FEE))
                      + (1 - model_prob) * (-BET_SIZE * (1 + POLYMARKET_FEE)))
                city_signals.append({
                    "city": city_key,
                    "city_name": city_name,
                    "target_date": date_str,
                    "bucket_label": label,
                    "bucket_temp": m["bucket_temp"],
                    "market_price": round(market_price, 4),
                    "model_prob": round(model_prob, 4),
                    "edge": round(edge, 4),
                    "bet_size": BET_SIZE,
                    "expected_pnl": round(ev, 2),
                    "clob_token_yes": m.get("clob_token_yes"),
                    "market_id": m.get("market_id"),
                    "unit": unit,
                })

        # Take top N by edge
        city_signals.sort(key=lambda x: x["edge"], reverse=True)
        signals.extend(city_signals[:MAX_BETS_PER_CITY])

    # Log signals
    if signals:
        logger.info(f"Strategy #2: {len(signals)} signal(s) for {date_str}")
        for s in signals:
            deg = "°F" if s["unit"] == "fahrenheit" else "°C"
            logger.info(f"  {s['city_name']} {s['bucket_label']}{deg} "
                        f"model={s['model_prob']:.1%} market={s['market_price']:.1%} "
                        f"edge={s['edge']:+.1%}")
    else:
        logger.info(f"Strategy #2: No signals for {date_str}")

    # Save signals
    results_dir = os.path.join(BOT_DIR, "results")
    os.makedirs(results_dir, exist_ok=True)
    if signals:
        sig_path = os.path.join(results_dir,
                                 f"signals_{date_str.replace('-', '')}.csv")
        pd.DataFrame(signals).to_csv(sig_path, index=False)

    # Record to trade history (for PnL tracking)
    os.makedirs(DATA_DIR, exist_ok=True)
    history_path = os.path.join(DATA_DIR, "weather_trades.jsonl")
    with open(history_path, "a") as f:
        for s in signals:
            record = {
                **s,
                "timestamp": datetime.now(SGT).isoformat(),
                "preview": PREVIEW_MODE,
                "resolved": False,
                "won": None,
                "pnl": None,
                "cost": BET_SIZE * (1 + POLYMARKET_FEE),
            }
            f.write(json.dumps(record) + "\n")

    return signals


# ── Strategy #3: Tennis Odds Arbitrage ──

_tennis_strategy = None


def _init_tennis_strategy():
    """Initialize the Tennis Arb strategy instance."""
    global _tennis_strategy
    from src.strategies.tennis_arb import TennisArbStrategy

    _tennis_strategy = TennisArbStrategy(
        odds_provider=TENNIS_ODDS_PROVIDER,
        oddspapi_api_key=ODDSPAPI_API_KEY,
        min_divergence=TENNIS_MIN_DIVERGENCE,
        max_bet_size=TENNIS_MAX_BET_SIZE,
        kelly_fraction=TENNIS_KELLY_FRACTION,
        tournaments=TENNIS_TOURNAMENTS,
        min_volume=TENNIS_MIN_POLYMARKET_VOLUME,
        min_liquidity=TENNIS_MIN_POLYMARKET_LIQUIDITY,
        preview_mode=TENNIS_ARB_PREVIEW_MODE or PREVIEW_MODE,
        data_dir=DATA_DIR,
    )
    return _tennis_strategy


def run_strategy3() -> list[dict]:
    """Run a single Strategy #3 scan."""
    global _tennis_strategy
    if _tennis_strategy is None:
        _init_tennis_strategy()
    return _tennis_strategy.scan()


def _tennis_scanner_loop():
    """Periodically scan for tennis arb opportunities."""
    global _tennis_strategy
    if _tennis_strategy is None:
        _init_tennis_strategy()

    logger.info(f"Tennis arb scanner started (interval={TENNIS_SCAN_INTERVAL}s)")

    while not _shutdown_event.is_set():
        try:
            signals = _tennis_strategy.scan()
            if signals:
                telegram_bot.send_tennis_signals(signals)
        except Exception as e:
            logger.exception(f"Tennis arb scan failed: {e}")
            telegram_bot.send_message(f"[TENNIS] Scan failed: <code>{e}</code>")

        _shutdown_event.wait(TENNIS_SCAN_INTERVAL)


# ── Scheduler ──

def _scheduler_loop():
    """Run Strategy #2 daily at the configured SGT time."""
    last_run_date = None

    while not _shutdown_event.is_set():
        now = datetime.now(SGT)
        today = now.date()

        # Check if it's time to run
        target_time = now.replace(hour=SCHEDULE_HOUR_SGT, minute=SCHEDULE_MINUTE_SGT,
                                   second=0, microsecond=0)

        if (now >= target_time and last_run_date != today):
            last_run_date = today
            logger.info(f"Scheduled run triggered at {now.strftime('%H:%M SGT')}")

            target_date = datetime(
                (today + timedelta(days=DAYS_IN_ADVANCE)).year,
                (today + timedelta(days=DAYS_IN_ADVANCE)).month,
                (today + timedelta(days=DAYS_IN_ADVANCE)).day,
            )

            try:
                signals = run_strategy2(target_date)
                telegram_bot.send_strategy2_signals(
                    signals, target_date.strftime("%Y-%m-%d")
                )
            except Exception as e:
                logger.exception(f"Scheduled run failed: {e}")
                telegram_bot.send_message(f"Scheduled run failed: <code>{e}</code>")

        # Sleep 30s between checks
        _shutdown_event.wait(30)


# ── Main ──

_shutdown_event = threading.Event()


def _setup_logging():
    """Configure logging to console and file."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    log_file = os.path.join(LOGS_DIR,
                             f"bot-{datetime.now().strftime('%Y-%m-%d')}.log")

    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)

    # File handler
    fh = logging.FileHandler(log_file)
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(ch)
    root.addHandler(fh)


def _signal_handler(sig, frame):
    logger.info(f"Received signal {sig}, shutting down...")
    _shutdown_event.set()


def main():
    parser = argparse.ArgumentParser(description="Unified Trading Bot")
    parser.add_argument("--once", action="store_true",
                        help="Run Strategy #2 once and exit")
    parser.add_argument("--date", type=str, default=None,
                        help="Target date for --once mode (YYYY-MM-DD)")
    args = parser.parse_args()

    _setup_logging()

    logger.info("=" * 60)
    logger.info("  Polymarket Trading Bot")
    logger.info(f"  Strategy #1 (Copy Traders): {'ENABLED' if STRATEGY1_ENABLED else 'DISABLED'}")
    logger.info(f"  Strategy #2 (Weather):      {'ENABLED' if STRATEGY2_ENABLED else 'DISABLED'}")
    logger.info(f"  Strategy #3 (Tennis Arb):   {'ENABLED' if STRATEGY3_ENABLED else 'DISABLED'}")
    logger.info(f"  Preview mode: {PREVIEW_MODE}")
    logger.info(f"  Schedule: {SCHEDULE_HOUR_SGT:02d}:{SCHEDULE_MINUTE_SGT:02d} SGT daily")
    logger.info(f"  Cities: {', '.join(CITIES_TO_BET)}")
    logger.info(f"  Days ahead: {DAYS_IN_ADVANCE}")
    if STRATEGY3_ENABLED:
        logger.info(f"  Tennis scan interval: {TENNIS_SCAN_INTERVAL}s")
        logger.info(f"  Tennis min divergence: {TENNIS_MIN_DIVERGENCE:.0%}")
        logger.info(f"  Tennis tournaments: {', '.join(TENNIS_TOURNAMENTS)}")
    logger.info("=" * 60)

    # Single run mode
    if args.once:
        if args.date:
            target_date = datetime.strptime(args.date, "%Y-%m-%d")
        else:
            today = datetime.now(SGT).date()
            td = today + timedelta(days=DAYS_IN_ADVANCE)
            target_date = datetime(td.year, td.month, td.day)

        signals = run_strategy2(target_date)
        if telegram_bot.is_configured():
            telegram_bot.send_strategy2_signals(
                signals, target_date.strftime("%Y-%m-%d")
            )
        return

    # Register prediction callback for telegram
    telegram_bot.on_predict_request = run_strategy2

    # Signal handlers
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Start Strategy #1 (if enabled)
    if STRATEGY1_ENABLED:
        start_strategy1()
    else:
        logger.info("Strategy #1 disabled, skipping copy-trader bot")

    # Start Telegram polling
    if telegram_bot.is_configured():
        telegram_bot.start_polling()
        logger.info("Telegram bot started")
    else:
        logger.info("Telegram not configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)")

    # Register tennis scan callback for telegram
    telegram_bot.on_tennis_scan_request = run_strategy3

    # Startup notification
    telegram_bot.send_message(
        "<b>Bot Started</b>\n"
        f"Strategy #1: {'ON' if STRATEGY1_ENABLED else 'OFF'}\n"
        f"Strategy #2: {'ON' if STRATEGY2_ENABLED else 'OFF'}\n"
        f"Strategy #3: {'ON' if STRATEGY3_ENABLED else 'OFF'}\n"
        f"Mode: {'PREVIEW' if PREVIEW_MODE else 'LIVE'}\n"
        f"Schedule: {SCHEDULE_HOUR_SGT:02d}:{SCHEDULE_MINUTE_SGT:02d} SGT\n"
        f"Cities: {', '.join(CITIES_TO_BET)}"
    )

    # Start scheduler
    if STRATEGY2_ENABLED:
        scheduler_thread = threading.Thread(
            target=_scheduler_loop, daemon=True, name="scheduler"
        )
        scheduler_thread.start()
        logger.info("Scheduler started")

    # Start Strategy #3 scanner
    if STRATEGY3_ENABLED:
        tennis_thread = threading.Thread(
            target=_tennis_scanner_loop, daemon=True, name="tennis-scanner"
        )
        tennis_thread.start()
        logger.info("Tennis arb scanner started")
    else:
        logger.info("Strategy #3 disabled, skipping tennis arb scanner")

    # Main loop — keep alive
    try:
        while not _shutdown_event.is_set():
            _shutdown_event.wait(1)
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Shutting down...")
        telegram_bot.send_message("Bot shutting down.")
        telegram_bot.stop_polling()
        if STRATEGY1_ENABLED:
            stop_strategy1()
        logger.info("Goodbye.")


if __name__ == "__main__":
    main()
