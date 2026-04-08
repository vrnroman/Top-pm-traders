#!/usr/bin/env python3
"""Weather Betting Bot — Strategy #2.

Predicts daily max temperature for specified cities, compares model probability
with Polymarket YES bet prices, and identifies/places bets when edge exceeds
threshold.

Usage:
  python bot.py                          # Run with defaults from .env
  python bot.py --cities nyc,chicago     # Specific cities
  python bot.py --days-ahead 3           # Days in advance
  python bot.py --min-edge 0.10          # 10% minimum edge
  python bot.py --bet-size 15            # $15 per bet
  python bot.py --no-preview             # Actually place bets (via TS bot)
"""

import os
import sys
import json
import argparse
from datetime import datetime, timedelta

import pandas as pd

from cities import CITIES
from config import (
    CITIES_TO_BET, DAYS_IN_ADVANCE, MIN_EDGE, BET_SIZE,
    MAX_BETS_PER_CITY, POLYMARKET_FEE, CACHE_DIR, RESULTS_DIR,
    DATA_DIR, PREVIEW_MODE, KDE_WINDOW_DAYS, RECENCY_HALFLIFE,
)
from weather_data import fetch_all_weather
from weather_predictor import WeatherPredictor
from polymarket_fetcher import fetch_markets_for_cities_and_dates


def run_bot(cities: list[str], days_ahead: int, min_edge: float,
            bet_size: float, max_bets: int = 2, preview: bool = True):
    """Main bot loop: predict, compare with market, generate signals."""

    today = datetime.now().date()
    target_date = today + timedelta(days=days_ahead)
    target_dt = datetime(target_date.year, target_date.month, target_date.day)

    print("=" * 70)
    print(f"  WEATHER BETTING BOT — Strategy #2")
    print(f"  Today: {today}")
    print(f"  Target date: {target_date} (days ahead: {days_ahead})")
    print(f"  Cities: {', '.join(cities)}")
    print(f"  Min edge: {min_edge:.0%}, Bet size: ${bet_size:.2f}")
    print(f"  Preview mode: {preview}")
    print("=" * 70)

    # 1. Fetch weather history
    print(f"\n[1/3] Fetching weather history...")
    os.makedirs(CACHE_DIR, exist_ok=True)
    weather_df = fetch_all_weather(cities, CACHE_DIR)
    if len(weather_df) == 0:
        print("ERROR: No weather data available")
        return []
    print(f"  {len(weather_df)} total daily records")

    # 2. Fetch Polymarket markets
    print(f"\n[2/3] Fetching Polymarket markets for {target_date}...")
    markets_df = fetch_markets_for_cities_and_dates(cities, [target_dt])
    if len(markets_df) == 0:
        print("ERROR: No Polymarket markets found")
        return []

    # 3. Predict and compare
    print(f"\n[3/3] Predicting temperatures and finding edges...")
    predictor = WeatherPredictor(weather_df, window_days=KDE_WINDOW_DAYS,
                                  recency_halflife=RECENCY_HALFLIFE)

    signals = []
    for city_key in cities:
        city_info = CITIES.get(city_key, {})
        city_name = city_info.get("name", city_key)
        unit = city_info.get("unit", "fahrenheit")
        deg = "°F" if unit == "fahrenheit" else "°C"

        # Get market buckets for this city
        city_markets = markets_df[
            (markets_df["city"] == city_key) &
            (markets_df["target_date"] == pd.Timestamp(target_dt))
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

        # Predict
        probs, dist = predictor.predict_buckets(city_key, target_dt, buckets)

        print(f"\n  {city_name} ({target_date}) — Model: mean={dist['mean']:.1f}{deg}, "
              f"std={dist['std']:.1f}{deg}, samples={dist['n_samples']}")
        print(f"  {'Bucket':>12s} {'Market':>8s} {'Model':>8s} {'Edge':>8s} {'Signal':>8s}")
        print(f"  {'-' * 48}")

        # Evaluate each bucket
        city_signals = []
        for _, m in city_markets.sort_values("bucket_temp").iterrows():
            label = m["bucket_label"]
            market_price = m["yes_price"]
            if market_price is None:
                continue

            model_prob = probs.get(label, 0.0)
            edge = model_prob - market_price

            signal = ""
            if edge >= min_edge and market_price > 0.01 and market_price < 0.95:
                signal = f"BUY +{edge:.0%}"
                city_signals.append({
                    "city": city_key,
                    "city_name": city_name,
                    "target_date": str(target_date),
                    "bucket_label": label,
                    "bucket_temp": m["bucket_temp"],
                    "temp_high": m.get("temp_high", m["bucket_temp"]),
                    "market_price": round(market_price, 4),
                    "model_prob": round(model_prob, 4),
                    "edge": round(edge, 4),
                    "bet_size": bet_size,
                    "expected_pnl": round(
                        model_prob * (bet_size / market_price - bet_size * (1 + POLYMARKET_FEE))
                        + (1 - model_prob) * (-bet_size * (1 + POLYMARKET_FEE)),
                        2
                    ),
                    "clob_token_yes": m.get("clob_token_yes"),
                    "market_id": m.get("market_id"),
                    "unit": unit,
                })

            print(f"  {label:>12s} {market_price:>7.1%} {model_prob:>7.1%} "
                  f"{edge:>+7.1%} {signal:>8s}")

        # Take top N signals by edge
        city_signals.sort(key=lambda x: x["edge"], reverse=True)
        signals.extend(city_signals[:max_bets])

    # Summary
    print(f"\n{'=' * 70}")
    print(f"  SIGNALS SUMMARY")
    print(f"{'=' * 70}")

    if not signals:
        print("  No betting signals found above threshold.")
    else:
        print(f"  Found {len(signals)} signal(s):\n")
        total_expected = 0
        for s in signals:
            deg = "°F" if s["unit"] == "fahrenheit" else "°C"
            print(f"    {s['city_name']:15s} {s['target_date']} "
                  f"{s['bucket_label']:>8s}{deg} "
                  f"market={s['market_price']:.1%} model={s['model_prob']:.1%} "
                  f"edge={s['edge']:+.1%} E[PnL]=${s['expected_pnl']:.2f}")
            total_expected += s["expected_pnl"]

        print(f"\n  Total expected PnL: ${total_expected:.2f}")
        print(f"  Total capital at risk: ${len(signals) * bet_size:.2f}")

    # Save signals
    os.makedirs(RESULTS_DIR, exist_ok=True)
    if signals:
        sig_df = pd.DataFrame(signals)
        sig_path = os.path.join(RESULTS_DIR,
                                 f"signals_{target_date.strftime('%Y%m%d')}.csv")
        sig_df.to_csv(sig_path, index=False)
        print(f"\n  Signals saved to {sig_path}")

    # Place bets (or just preview)
    if signals and not preview:
        print(f"\n  Placing {len(signals)} bet(s) via Polymarket...")
        _place_bets(signals, bet_size)
    elif signals and preview:
        print(f"\n  PREVIEW MODE: Bets NOT placed. Set PREVIEW_MODE=false to trade.")

    return signals


def _place_bets(signals: list[dict], bet_size: float):
    """Place bets using the Polymarket TS bot's order infrastructure.

    Writes a JSON file that can be consumed by a simple TS script
    that uses the existing CLOB client to place orders.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    orders = []
    for s in signals:
        if not s.get("clob_token_yes"):
            print(f"    SKIP {s['city_name']} {s['bucket_label']}: no token ID")
            continue
        orders.append({
            "tokenId": s["clob_token_yes"],
            "price": s["market_price"],
            "size": bet_size,
            "side": "BUY",
            "meta": {
                "city": s["city_name"],
                "date": s["target_date"],
                "bucket": s["bucket_label"],
                "edge": s["edge"],
                "model_prob": s["model_prob"],
            },
        })

    orders_path = os.path.join(DATA_DIR, "pending_orders.json")
    with open(orders_path, "w") as f:
        json.dump(orders, f, indent=2)
    print(f"    {len(orders)} order(s) written to {orders_path}")
    print(f"    Execute with: cd polymarket && npx ts-node src/scripts/execute-weather-orders.ts")


def main():
    parser = argparse.ArgumentParser(description="Weather Betting Bot")
    parser.add_argument("--cities", type=str, default=None,
                        help="Comma-separated city keys (default: from .env)")
    parser.add_argument("--days-ahead", type=int, default=None,
                        help="Days in advance to bet (default: from .env)")
    parser.add_argument("--min-edge", type=float, default=None,
                        help="Minimum edge threshold (default: from .env)")
    parser.add_argument("--bet-size", type=float, default=None,
                        help="USD per bet (default: from .env)")
    parser.add_argument("--max-bets", type=int, default=None,
                        help="Max bets per city (default: 2)")
    parser.add_argument("--no-preview", action="store_true",
                        help="Actually place bets (default: preview only)")
    args = parser.parse_args()

    cities = args.cities.split(",") if args.cities else CITIES_TO_BET
    days_ahead = args.days_ahead if args.days_ahead is not None else DAYS_IN_ADVANCE
    min_edge = args.min_edge if args.min_edge is not None else MIN_EDGE
    bet_size = args.bet_size if args.bet_size is not None else BET_SIZE
    max_bets = args.max_bets if args.max_bets is not None else MAX_BETS_PER_CITY
    preview = PREVIEW_MODE and not args.no_preview

    run_bot(cities, days_ahead, min_edge, bet_size, max_bets, preview)


if __name__ == "__main__":
    main()
