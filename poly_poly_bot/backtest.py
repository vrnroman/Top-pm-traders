#!/usr/bin/env python3
"""Backtest the weather betting strategy against historical Polymarket data.

Fetches all resolved temperature events, applies the prediction model,
and simulates betting with configurable edge thresholds.

Usage:
  python backtest.py                               # Default params
  python backtest.py --min-edge 0.05 --bet-size 10 # Custom params
  python backtest.py --cities nyc,chicago           # Specific cities
"""

import os
import argparse
from datetime import datetime

import numpy as np
import pandas as pd

from cities import CITIES
from config import CACHE_DIR, RESULTS_DIR, POLYMARKET_FEE
from weather_data import fetch_all_weather
from weather_predictor import WeatherPredictor
from polymarket_fetcher import (
    fetch_temperature_events_by_tag, parse_all_markets,
    get_resolved_outcomes, get_buckets_for_market,
)


def run_backtest(cities: list[str] | None = None, min_edge: float = 0.05,
                 bet_size: float = 10.0, fee: float = None):
    """Run full backtest across all resolved Polymarket temperature events."""

    if fee is None:
        fee = POLYMARKET_FEE

    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)

    print("=" * 70)
    print(f"  WEATHER BETTING BACKTEST")
    print(f"  Min edge: {min_edge:.0%}, Bet size: ${bet_size:.2f}, Fee: {fee:.0%}")
    print("=" * 70)

    # 1. Fetch all Polymarket temperature events
    print(f"\n[1/3] Fetching Polymarket temperature events...")
    cache_path = os.path.join(CACHE_DIR, "all_temp_events.json")
    events = fetch_temperature_events_by_tag(cache_path)
    markets_df = parse_all_markets(events)
    outcomes_df = get_resolved_outcomes(markets_df)

    if cities:
        markets_df = markets_df[markets_df["city"].isin(cities)]
        outcomes_df = outcomes_df[outcomes_df["city"].isin(cities)]

    print(f"  {len(markets_df)} market buckets, {len(outcomes_df)} resolved outcomes")
    if len(outcomes_df) == 0:
        print("  No resolved outcomes to backtest against!")
        return

    resolved_cities = sorted(outcomes_df["city"].unique())
    print(f"  Cities with resolved data: {resolved_cities}")

    # 2. Fetch weather history for those cities
    print(f"\n[2/3] Fetching weather history...")
    weather_df = fetch_all_weather(resolved_cities, CACHE_DIR)
    if len(weather_df) == 0:
        print("  No weather data!")
        return
    print(f"  {len(weather_df)} daily records across {weather_df['city'].nunique()} cities")

    # 3. Run backtest
    print(f"\n[3/3] Running backtest...")
    predictor = WeatherPredictor(weather_df, window_days=15, recency_halflife=4.0)

    # Part A: Model accuracy
    print(f"\n{'─' * 60}")
    print(f"  [A] MODEL ACCURACY ON RESOLVED EVENTS")
    print(f"{'─' * 60}")

    accuracy_rows = []
    for _, outcome in outcomes_df.iterrows():
        city = outcome["city"]
        target_date = outcome["target_date"]
        actual_bucket = outcome["actual_bucket"]

        buckets = get_buckets_for_market(markets_df, city, target_date)
        if not buckets:
            continue

        probs, dist = predictor.predict_buckets(city, target_date, buckets)
        top_bucket = max(probs, key=probs.get)
        top_prob = probs[top_bucket]
        actual_prob = probs.get(actual_bucket, 0.0)

        accuracy_rows.append({
            "city": city,
            "date": target_date,
            "actual_bucket": actual_bucket,
            "actual_temp": outcome["actual_temp"],
            "model_top_bucket": top_bucket,
            "model_top_prob": round(top_prob, 4),
            "model_actual_prob": round(actual_prob, 4),
            "correct_top1": top_bucket == actual_bucket,
            "model_mean": round(dist["mean"], 1),
        })

    acc_df = pd.DataFrame(accuracy_rows)
    if len(acc_df) > 0:
        print(f"  Evaluated: {len(acc_df)} events across {acc_df['city'].nunique()} cities")
        print(f"  Top-1 accuracy: {acc_df['correct_top1'].mean():.1%}")
        print(f"  Avg prob assigned to actual: {acc_df['model_actual_prob'].mean():.1%}")

        # Per-city
        print(f"\n  Per-city:")
        city_acc = acc_df.groupby("city").agg(
            n=("correct_top1", "count"),
            top1=("correct_top1", "mean"),
            avg_prob=("model_actual_prob", "mean"),
        ).sort_values("top1", ascending=False)
        for city, row in city_acc.iterrows():
            name = CITIES.get(city, {}).get("name", city)
            print(f"    {name:20s}: {row['top1']:5.1%} top-1 ({row['n']:3.0f} days, "
                  f"avg_prob={row['avg_prob']:.1%})")

        # Brier score
        brier_rows = []
        for _, outcome in outcomes_df.iterrows():
            city = outcome["city"]
            target_date = outcome["target_date"]
            buckets = get_buckets_for_market(markets_df, city, target_date)
            if not buckets:
                continue
            probs, _ = predictor.predict_buckets(city, target_date, buckets)
            for b in buckets:
                label = b["label"]
                p = probs.get(label, 0)
                actual_win = 1.0 if label == outcome["actual_bucket"] else 0.0
                brier_rows.append((p - actual_win) ** 2)
        if brier_rows:
            print(f"\n  Brier score: {np.mean(brier_rows):.4f} (lower=better, 0.25=random)")

    # Part B: Simulated trading
    print(f"\n{'─' * 60}")
    print(f"  [B] SIMULATED TRADING (model vs market proxy)")
    print(f"{'─' * 60}")

    proxy_predictor = WeatherPredictor(weather_df, window_days=30, recency_halflife=8.0)
    trades = []

    for _, outcome in outcomes_df.iterrows():
        city = outcome["city"]
        target_date = outcome["target_date"]
        actual_bucket = outcome["actual_bucket"]

        buckets = get_buckets_for_market(markets_df, city, target_date)
        if not buckets:
            continue

        probs, _ = predictor.predict_buckets(city, target_date, buckets)
        proxy_probs, _ = proxy_predictor.predict_buckets(
            city, target_date, buckets, use_recent_boost=False
        )

        for b in buckets:
            label = b["label"]
            model_p = probs.get(label, 0.0)
            market_p = proxy_probs.get(label, 0.0)
            edge = model_p - market_p
            won = (label == actual_bucket)

            if edge >= min_edge and market_p > 0.01:
                cost = bet_size * (1 + fee)
                pnl = (bet_size / market_p - cost) if won else -cost
                trades.append({
                    "city": city,
                    "city_name": CITIES.get(city, {}).get("name", city),
                    "date": str(target_date)[:10],
                    "bucket": label,
                    "model_p": round(model_p, 4),
                    "market_proxy": round(market_p, 4),
                    "edge": round(edge, 4),
                    "won": won,
                    "pnl": round(pnl, 2),
                    "cost": round(cost, 2),
                    "actual": actual_bucket,
                })

    trades_df = pd.DataFrame(trades) if trades else pd.DataFrame()
    if len(trades_df) > 0:
        n = len(trades_df)
        w = int(trades_df["won"].sum())
        total_pnl = trades_df["pnl"].sum()
        total_cost = trades_df["cost"].sum()
        print(f"  Trades: {n}, Wins: {w} ({100 * w / n:.1f}%)")
        print(f"  Total PnL: ${total_pnl:.2f}")
        print(f"  ROI: {100 * total_pnl / total_cost:.1f}%")
        if w > 0:
            print(f"  Avg win: ${trades_df[trades_df['won']]['pnl'].mean():.2f}")
        if n > w:
            print(f"  Avg loss: ${trades_df[~trades_df['won']]['pnl'].mean():.2f}")
        print(f"  Avg edge at entry: {trades_df['edge'].mean():.1%}")

        # Per city
        print(f"\n  Per city:")
        for city, group in trades_df.groupby("city"):
            name = CITIES.get(city, {}).get("name", city)
            cn = len(group)
            cw = group["won"].sum()
            cpnl = group["pnl"].sum()
            print(f"    {name:20s}: {cn:3d} trades, {cw:3.0f} wins "
                  f"({100 * cw / cn:.0f}%), PnL=${cpnl:.2f}")
    else:
        print("  No trades at this edge threshold.")

    # Part C: Sensitivity analysis
    print(f"\n{'─' * 60}")
    print(f"  [C] EDGE SENSITIVITY ANALYSIS")
    print(f"{'─' * 60}")
    print(f"  {'Edge':>6s} {'Trades':>7s} {'Wins':>6s} {'Rate':>6s} {'PnL':>10s} {'ROI':>7s}")

    # Collect all possible trades across all edges
    all_possible = []
    for _, outcome in outcomes_df.iterrows():
        city = outcome["city"]
        target_date = outcome["target_date"]
        buckets = get_buckets_for_market(markets_df, city, target_date)
        if not buckets:
            continue
        probs, _ = predictor.predict_buckets(city, target_date, buckets)
        proxy_probs, _ = proxy_predictor.predict_buckets(
            city, target_date, buckets, use_recent_boost=False
        )
        for b in buckets:
            label = b["label"]
            mp = probs.get(label, 0.0)
            pp = proxy_probs.get(label, 0.0)
            if pp > 0.01:
                all_possible.append({
                    "edge": mp - pp,
                    "won": label == outcome["actual_bucket"],
                    "market_p": pp,
                })

    poss_df = pd.DataFrame(all_possible) if all_possible else pd.DataFrame()
    for e_thresh in [0.0, 0.01, 0.02, 0.03, 0.05, 0.07, 0.10, 0.15, 0.20]:
        if len(poss_df) == 0:
            break
        sub = poss_df[poss_df["edge"] >= e_thresh]
        if len(sub) == 0:
            continue
        n = len(sub)
        w = sub["won"].sum()
        pnls = []
        for _, s in sub.iterrows():
            cost = bet_size * (1 + fee)
            pnls.append((bet_size / s["market_p"] - cost) if s["won"] else -cost)
        tp = sum(pnls)
        tc = n * bet_size * (1 + fee)
        print(f"  {e_thresh:>5.0%} {n:>7d} {w:>6.0f} {100 * w / n:>5.1f}% "
              f"${tp:>9.2f} {100 * tp / tc:>6.1f}%")

    # Save results
    acc_df.to_csv(os.path.join(RESULTS_DIR, "backtest_accuracy.csv"), index=False)
    if len(trades_df) > 0:
        trades_df.to_csv(os.path.join(RESULTS_DIR, "backtest_trades.csv"), index=False)
    print(f"\n  Results saved to {RESULTS_DIR}/")

    return acc_df, trades_df


def main():
    parser = argparse.ArgumentParser(description="Weather betting backtest")
    parser.add_argument("--cities", type=str, default=None,
                        help="Comma-separated city keys (default: all with data)")
    parser.add_argument("--min-edge", type=float, default=0.05)
    parser.add_argument("--bet-size", type=float, default=10.0)
    args = parser.parse_args()

    cities = args.cities.split(",") if args.cities else None
    run_backtest(cities=cities, min_edge=args.min_edge, bet_size=args.bet_size)


if __name__ == "__main__":
    main()
