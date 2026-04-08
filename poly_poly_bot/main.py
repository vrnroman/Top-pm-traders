#!/usr/bin/env python3
"""Unified Polymarket Trading Bot — all three strategies in Python.

Manages all strategies:
- Strategy #1 (Copy Trading): runs natively via asyncio
- Strategy #2 (Weather Betting): scheduled daily at configured SGT time
- Strategy #3 (Tennis Arb): periodic scans every N seconds
- Unified Telegram bot for all commands

Usage:
  python main.py              # Run with defaults from .env
  python main.py --once       # Run Strategy #2 once and exit
"""

import asyncio
import os
import sys
import signal
import logging
import threading
import argparse
import json
from datetime import datetime, timedelta, timezone

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import CONFIG
from src.logger import logger

import src.telegram_bot as telegram_bot

SGT = timezone(timedelta(hours=8))

_shutdown_event = threading.Event()


# -- Strategy #2: Weather Betting --

def run_strategy2(target_date: datetime) -> list[dict]:
    """Run a single Strategy #2 prediction cycle."""
    from src.weather.weather_data import fetch_all_weather
    from src.weather.weather_predictor import WeatherPredictor
    from src.weather.polymarket_fetcher import fetch_markets_for_cities_and_dates
    from src.weather.cities import CITIES
    import pandas as pd

    cities_list = [c.strip() for c in CONFIG.cities_to_bet.split(",")]
    date_str = target_date.strftime("%Y-%m-%d")
    logger.info(f"Strategy #2: Running prediction for {date_str}")

    os.makedirs(CONFIG.cache_dir, exist_ok=True)

    # Fetch weather history
    weather_df = fetch_all_weather(cities_list, CONFIG.cache_dir)
    if len(weather_df) == 0:
        logger.error("No weather data available")
        return []

    # Fetch Polymarket markets
    markets_df = fetch_markets_for_cities_and_dates(cities_list, [target_date])
    if len(markets_df) == 0:
        logger.warn(f"No Polymarket markets found for {date_str}")
        return []

    # Predict
    predictor = WeatherPredictor(weather_df, window_days=15, recency_halflife=4.0)

    # Polymarket fee
    polymarket_fee = float(os.getenv("POLYMARKET_FEE", "0.02"))

    signals = []
    for city_key in cities_list:
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

            if edge >= CONFIG.min_edge:
                ev = (model_prob * (CONFIG.bet_size / market_price - CONFIG.bet_size * (1 + polymarket_fee))
                      + (1 - model_prob) * (-CONFIG.bet_size * (1 + polymarket_fee)))
                city_signals.append({
                    "city": city_key,
                    "city_name": city_name,
                    "target_date": date_str,
                    "bucket_label": label,
                    "bucket_temp": m["bucket_temp"],
                    "market_price": round(market_price, 4),
                    "model_prob": round(model_prob, 4),
                    "edge": round(edge, 4),
                    "bet_size": CONFIG.bet_size,
                    "expected_pnl": round(ev, 2),
                    "clob_token_yes": m.get("clob_token_yes"),
                    "market_id": m.get("market_id"),
                    "unit": unit,
                })

        # Take top N by edge
        city_signals.sort(key=lambda x: x["edge"], reverse=True)
        signals.extend(city_signals[:CONFIG.max_bets_per_city])

    # Log signals
    if signals:
        logger.info(f"Strategy #2: {len(signals)} signal(s) for {date_str}")
        for s in signals:
            deg = "\u00b0F" if s["unit"] == "fahrenheit" else "\u00b0C"
            logger.info(f"  {s['city_name']} {s['bucket_label']}{deg} "
                        f"model={s['model_prob']:.1%} market={s['market_price']:.1%} "
                        f"edge={s['edge']:+.1%}")
    else:
        logger.info(f"Strategy #2: No signals for {date_str}")

    # Save signals
    os.makedirs(CONFIG.results_dir, exist_ok=True)
    if signals:
        import pandas as pd
        sig_path = os.path.join(CONFIG.results_dir,
                                 f"signals_{date_str.replace('-', '')}.csv")
        pd.DataFrame(signals).to_csv(sig_path, index=False)

    # Record to trade history (for PnL tracking)
    polymarket_fee = float(os.getenv("POLYMARKET_FEE", "0.02"))
    os.makedirs(CONFIG.data_dir, exist_ok=True)
    history_path = os.path.join(CONFIG.data_dir, "weather_trades.jsonl")
    with open(history_path, "a") as f:
        for s in signals:
            record = {
                **s,
                "timestamp": datetime.now(SGT).isoformat(),
                "preview": CONFIG.preview_mode,
                "resolved": False,
                "won": None,
                "pnl": None,
                "cost": CONFIG.bet_size * (1 + polymarket_fee),
            }
            f.write(json.dumps(record) + "\n")

    return signals


# -- Strategy #3: Tennis Odds Arbitrage --

_tennis_strategy = None


def _init_tennis_strategy():
    """Initialize the Tennis Arb strategy instance."""
    global _tennis_strategy
    from src.tennis.tennis_arb import TennisArbStrategy

    tennis_tournaments = [t.strip() for t in CONFIG.tennis_tournaments.split(",")]

    _tennis_strategy = TennisArbStrategy(
        odds_provider=CONFIG.tennis_odds_provider,
        oddspapi_api_key=CONFIG.oddspapi_api_key,
        min_divergence=CONFIG.tennis_min_divergence,
        max_bet_size=CONFIG.tennis_max_bet_size,
        kelly_fraction=CONFIG.tennis_kelly_fraction,
        tournaments=tennis_tournaments,
        min_volume=CONFIG.tennis_min_polymarket_volume,
        min_liquidity=CONFIG.tennis_min_polymarket_liquidity,
        preview_mode=CONFIG.preview_mode,
        data_dir=CONFIG.data_dir,
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

    logger.info(f"Tennis arb scanner started (interval={CONFIG.tennis_scan_interval}s)")

    while not _shutdown_event.is_set():
        try:
            signals = _tennis_strategy.scan()
            if signals:
                telegram_bot.send_tennis_signals(signals)
        except Exception as e:
            logger.error(f"Tennis arb scan failed: {e}")
            telegram_bot.send_message(f"[TENNIS] Scan failed: <code>{e}</code>")

        _shutdown_event.wait(CONFIG.tennis_scan_interval)


# -- Scheduler --

def _scheduler_loop():
    """Run Strategy #2 daily at the configured SGT time."""
    last_run_date = None

    while not _shutdown_event.is_set():
        now = datetime.now(SGT)
        today = now.date()

        # Check if it's time to run
        target_time = now.replace(hour=CONFIG.schedule_hour_sgt,
                                   minute=CONFIG.schedule_minute_sgt,
                                   second=0, microsecond=0)

        if (now >= target_time and last_run_date != today):
            last_run_date = today
            logger.info(f"Scheduled run triggered at {now.strftime('%H:%M SGT')}")

            target_date = datetime(
                (today + timedelta(days=CONFIG.days_in_advance)).year,
                (today + timedelta(days=CONFIG.days_in_advance)).month,
                (today + timedelta(days=CONFIG.days_in_advance)).day,
            )

            try:
                signals = run_strategy2(target_date)
                telegram_bot.send_strategy2_signals(
                    signals, target_date.strftime("%Y-%m-%d")
                )
            except Exception as e:
                logger.error(f"Scheduled run failed: {e}")
                telegram_bot.send_message(f"Scheduled run failed: <code>{e}</code>")

        # Sleep 30s between checks
        _shutdown_event.wait(30)


# -- Main --

def _setup_logging():
    """Configure logging to console and file."""
    os.makedirs(CONFIG.logs_dir, exist_ok=True)
    log_file = os.path.join(CONFIG.logs_dir,
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


async def main():
    """Main entry point — runs all enabled strategies."""
    parser = argparse.ArgumentParser(description="Unified Trading Bot")
    parser.add_argument("--once", action="store_true",
                        help="Run Strategy #2 once and exit")
    parser.add_argument("--date", type=str, default=None,
                        help="Target date for --once mode (YYYY-MM-DD)")
    args = parser.parse_args()

    _setup_logging()

    cities_list = [c.strip() for c in CONFIG.cities_to_bet.split(",")]
    tennis_tournaments = [t.strip() for t in CONFIG.tennis_tournaments.split(",")]

    logger.info("=" * 60)
    logger.info("  Polymarket Trading Bot (Unified Python)")
    logger.info(f"  Strategy #1 (Copy Trading): {'ENABLED' if CONFIG.strategy1_enabled else 'DISABLED'}")
    logger.info(f"  Strategy #2 (Weather):      {'ENABLED' if CONFIG.strategy2_enabled else 'DISABLED'}")
    logger.info(f"  Strategy #3 (Tennis Arb):   {'ENABLED' if CONFIG.strategy3_enabled else 'DISABLED'}")
    logger.info(f"  Preview mode: {CONFIG.preview_mode}")
    logger.info(f"  Schedule: {CONFIG.schedule_hour_sgt:02d}:{CONFIG.schedule_minute_sgt:02d} SGT daily")
    logger.info(f"  Cities: {', '.join(cities_list)}")
    logger.info(f"  Days ahead: {CONFIG.days_in_advance}")
    if CONFIG.strategy3_enabled:
        logger.info(f"  Tennis scan interval: {CONFIG.tennis_scan_interval}s")
        logger.info(f"  Tennis min divergence: {CONFIG.tennis_min_divergence:.0%}")
        logger.info(f"  Tennis tournaments: {', '.join(tennis_tournaments)}")
    logger.info("=" * 60)

    # Single run mode
    if args.once:
        if args.date:
            target_date = datetime.strptime(args.date, "%Y-%m-%d")
        else:
            today = datetime.now(SGT).date()
            td = today + timedelta(days=CONFIG.days_in_advance)
            target_date = datetime(td.year, td.month, td.day)

        signals = run_strategy2(target_date)
        if telegram_bot.is_configured():
            telegram_bot.send_strategy2_signals(
                signals, target_date.strftime("%Y-%m-%d")
            )
        return

    # Register prediction callback for telegram
    telegram_bot.on_predict_request = run_strategy2

    # Register tennis scan callback for telegram
    telegram_bot.on_tennis_scan_request = run_strategy3

    # Signal handlers
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Start Telegram polling
    if telegram_bot.is_configured():
        telegram_bot.start_polling()
        logger.info("Telegram bot started")
    else:
        logger.info("Telegram not configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)")

    # Startup notification
    telegram_bot.send_message(
        "<b>Bot Started (Unified Python)</b>\n"
        f"Strategy #1 (Copy): {'ON' if CONFIG.strategy1_enabled else 'OFF'}\n"
        f"Strategy #2 (Weather): {'ON' if CONFIG.strategy2_enabled else 'OFF'}\n"
        f"Strategy #3 (Tennis): {'ON' if CONFIG.strategy3_enabled else 'OFF'}\n"
        f"Mode: {'PREVIEW' if CONFIG.preview_mode else 'LIVE'}\n"
        f"Schedule: {CONFIG.schedule_hour_sgt:02d}:{CONFIG.schedule_minute_sgt:02d} SGT\n"
        f"Cities: {', '.join(cities_list)}"
    )

    # Start Strategy #2 scheduler in a thread
    if CONFIG.strategy2_enabled:
        scheduler_thread = threading.Thread(
            target=_scheduler_loop, daemon=True, name="scheduler"
        )
        scheduler_thread.start()
        logger.info("Strategy #2 scheduler started")

    # Start Strategy #3 scanner in a thread
    if CONFIG.strategy3_enabled:
        tennis_thread = threading.Thread(
            target=_tennis_scanner_loop, daemon=True, name="tennis-scanner"
        )
        tennis_thread.start()
        logger.info("Tennis arb scanner started")
    else:
        logger.info("Strategy #3 disabled, skipping tennis arb scanner")

    # Start Strategy #1 (Copy Trading) natively via asyncio
    if CONFIG.strategy1_enabled:
        logger.info("Starting Strategy #1 (Copy Trading) via asyncio...")
        from src.copy_trading.runner import run_copy_trading
        try:
            # Run copy trading as the main async task; it blocks until shutdown
            # Meanwhile, Strategy #2 and #3 run in daemon threads
            await run_copy_trading()
        except KeyboardInterrupt:
            pass
        except Exception as e:
            logger.error(f"Strategy #1 crashed: {e}")
            telegram_bot.send_message(f"Strategy #1 crashed: <code>{e}</code>")
    else:
        logger.info("Strategy #1 disabled, skipping copy-trader bot")
        # Main loop — keep alive while strategies #2/#3 run in threads
        try:
            while not _shutdown_event.is_set():
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            pass

    # Shutdown
    logger.info("Shutting down...")
    _shutdown_event.set()
    telegram_bot.send_message("Bot shutting down.")
    telegram_bot.stop_polling()
    logger.info("Goodbye.")


if __name__ == "__main__":
    asyncio.run(main())
