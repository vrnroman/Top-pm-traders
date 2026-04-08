#!/usr/bin/env python3
"""Generate an HTML report with predictions and live Polymarket prices.

Usage:
  python generate_report.py                                    # Default: April 11, 5 cities
  python generate_report.py --date 2026-04-11                  # Specific date
  python generate_report.py --cities nyc,chicago,denver        # Specific cities
"""

import os
import argparse
from datetime import datetime

import pandas as pd
import numpy as np

from cities import CITIES
from config import CACHE_DIR, RESULTS_DIR, POLYMARKET_FEE
from weather_data import fetch_all_weather
from weather_predictor import WeatherPredictor
from polymarket_fetcher import fetch_markets_for_cities_and_dates


def generate_report(cities: list[str], target_date: datetime,
                     output_path: str = None) -> str:
    """Generate HTML report with predictions + live market prices."""

    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)

    date_str = target_date.strftime("%Y-%m-%d")
    print(f"Generating report for {date_str}...")
    print(f"Cities: {', '.join(cities)}")

    # Fetch data
    print("\n[1/3] Fetching weather history...")
    weather_df = fetch_all_weather(cities, CACHE_DIR)

    print("\n[2/3] Fetching Polymarket markets...")
    markets_df = fetch_markets_for_cities_and_dates(cities, [target_date])

    print("\n[3/3] Generating predictions...")
    predictor = WeatherPredictor(weather_df, window_days=15, recency_halflife=4.0)

    # Build report data
    report_rows = []
    city_summaries = []

    for city_key in cities:
        city_info = CITIES.get(city_key, {})
        city_name = city_info.get("name", city_key)
        unit = city_info.get("unit", "fahrenheit")
        deg = "°F" if unit == "fahrenheit" else "°C"

        city_markets = markets_df[
            (markets_df["city"] == city_key) &
            (markets_df["target_date"] == pd.Timestamp(target_date))
        ]

        if len(city_markets) == 0:
            print(f"  {city_name}: No market data found")
            continue

        # Get buckets
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

        # Sort by bucket_temp
        sorted_markets = city_markets.sort_values("bucket_temp")

        city_data = []
        for _, m in sorted_markets.iterrows():
            label = m["bucket_label"]
            market_price = m["yes_price"]
            model_prob = probs.get(label, 0.0)
            edge = model_prob - (market_price or 0)

            # Expected value per $1 bet
            if market_price and market_price > 0:
                ev = model_prob * (1.0 / market_price - 1 - POLYMARKET_FEE) - \
                     (1 - model_prob) * (1 + POLYMARKET_FEE)
            else:
                ev = 0

            row = {
                "city": city_key,
                "city_name": city_name,
                "bucket_label": label + deg,
                "bucket_temp": m["bucket_temp"],
                "market_price": market_price,
                "model_prob": model_prob,
                "edge": edge,
                "ev_per_dollar": ev,
                "signal": "BUY" if edge >= 0.05 else ("WATCH" if edge >= 0.02 else ""),
                "unit": unit,
            }
            city_data.append(row)
            report_rows.append(row)

        # City summary
        top2 = sorted(city_data, key=lambda x: x["model_prob"], reverse=True)[:2]
        city_summaries.append({
            "city_name": city_name,
            "unit": deg,
            "model_mean": round(dist["mean"], 1),
            "model_std": round(dist["std"], 1),
            "n_samples": dist["n_samples"],
            "top1_bucket": top2[0]["bucket_label"] if top2 else "?",
            "top1_prob": top2[0]["model_prob"] if top2 else 0,
            "top1_market": top2[0]["market_price"] if top2 else 0,
            "top1_edge": top2[0]["edge"] if top2 else 0,
            "top2_bucket": top2[1]["bucket_label"] if len(top2) > 1 else "?",
            "top2_prob": top2[1]["model_prob"] if len(top2) > 1 else 0,
            "top2_market": top2[1]["market_price"] if len(top2) > 1 else 0,
            "top2_edge": top2[1]["edge"] if len(top2) > 1 else 0,
        })

    # Generate HTML
    html = _build_html(report_rows, city_summaries, date_str)

    if output_path is None:
        output_path = os.path.join(RESULTS_DIR,
                                    f"weather_report_{date_str.replace('-', '')}.html")
    with open(output_path, "w") as f:
        f.write(html)
    print(f"\nReport saved to {output_path}")

    # Also save CSV
    csv_path = output_path.replace(".html", ".csv")
    pd.DataFrame(report_rows).to_csv(csv_path, index=False)
    print(f"CSV saved to {csv_path}")

    # Print summary to console
    print(f"\n{'=' * 80}")
    print(f"  WEATHER PREDICTION REPORT — {date_str}")
    print(f"{'=' * 80}")
    for cs in city_summaries:
        print(f"\n  {cs['city_name']} — Model: {cs['model_mean']}{cs['unit']} "
              f"(std={cs['model_std']}{cs['unit']}, {cs['n_samples']} samples)")
        print(f"    Top 1: {cs['top1_bucket']:>10s}  model={cs['top1_prob']:.1%}  "
              f"market={cs['top1_market']:.1%}  edge={cs['top1_edge']:+.1%}")
        print(f"    Top 2: {cs['top2_bucket']:>10s}  model={cs['top2_prob']:.1%}  "
              f"market={cs['top2_market']:.1%}  edge={cs['top2_edge']:+.1%}")

    return output_path


def _build_html(rows: list[dict], summaries: list[dict], date_str: str) -> str:
    """Build the HTML report."""

    # Group rows by city
    from collections import defaultdict
    by_city = defaultdict(list)
    for r in rows:
        by_city[r["city_name"]].append(r)

    summary_cards = ""
    for cs in summaries:
        edge1_class = "positive" if cs["top1_edge"] >= 0.05 else ("neutral" if cs["top1_edge"] >= 0 else "negative")
        edge2_class = "positive" if cs["top2_edge"] >= 0.05 else ("neutral" if cs["top2_edge"] >= 0 else "negative")
        summary_cards += f"""
        <div class="city-card">
            <h3>{cs['city_name']}</h3>
            <div class="model-info">
                Model: <strong>{cs['model_mean']}{cs['unit']}</strong>
                (std={cs['model_std']}{cs['unit']}, {cs['n_samples']} samples)
            </div>
            <table class="mini-table">
                <tr>
                    <th>Rank</th><th>Bucket</th><th>Model</th><th>Market</th><th>Edge</th>
                </tr>
                <tr>
                    <td>#1</td>
                    <td><strong>{cs['top1_bucket']}</strong></td>
                    <td>{cs['top1_prob']:.1%}</td>
                    <td>{cs['top1_market']:.1%}</td>
                    <td class="{edge1_class}">{cs['top1_edge']:+.1%}</td>
                </tr>
                <tr>
                    <td>#2</td>
                    <td><strong>{cs['top2_bucket']}</strong></td>
                    <td>{cs['top2_prob']:.1%}</td>
                    <td>{cs['top2_market']:.1%}</td>
                    <td class="{edge2_class}">{cs['top2_edge']:+.1%}</td>
                </tr>
            </table>
        </div>
        """

    detail_tables = ""
    for city_name, city_rows in by_city.items():
        detail_rows = ""
        for r in sorted(city_rows, key=lambda x: x["bucket_temp"]):
            edge_class = "positive" if r["edge"] >= 0.05 else ("neutral" if r["edge"] >= 0 else "negative")
            signal_class = "signal-buy" if r["signal"] == "BUY" else ("signal-watch" if r["signal"] == "WATCH" else "")
            mp = f"{r['market_price']:.1%}" if r['market_price'] is not None else "N/A"

            # Highlight row if it's a signal
            row_class = "highlight-buy" if r["signal"] == "BUY" else ""

            detail_rows += f"""
            <tr class="{row_class}">
                <td>{r['bucket_label']}</td>
                <td>{r['model_prob']:.1%}</td>
                <td>{mp}</td>
                <td class="{edge_class}">{r['edge']:+.1%}</td>
                <td>{r['ev_per_dollar']:+.3f}</td>
                <td class="{signal_class}">{r['signal']}</td>
            </tr>
            """

        detail_tables += f"""
        <div class="detail-section">
            <h3>{city_name}</h3>
            <table class="detail-table">
                <thead>
                    <tr>
                        <th>Bucket</th>
                        <th>Model Prob</th>
                        <th>YES Price</th>
                        <th>Edge</th>
                        <th>EV/$1</th>
                        <th>Signal</th>
                    </tr>
                </thead>
                <tbody>
                    {detail_rows}
                </tbody>
            </table>
        </div>
        """

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Weather Prediction Report — {date_str}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0f1117;
            color: #e1e1e6;
            padding: 20px;
            max-width: 1200px;
            margin: 0 auto;
        }}
        h1 {{
            text-align: center;
            margin: 20px 0;
            font-size: 1.8em;
            color: #fff;
        }}
        h2 {{
            margin: 30px 0 15px;
            color: #8b8fa3;
            font-size: 1.1em;
            text-transform: uppercase;
            letter-spacing: 1px;
        }}
        h3 {{
            margin-bottom: 10px;
            color: #fff;
            font-size: 1.2em;
        }}
        .subtitle {{
            text-align: center;
            color: #8b8fa3;
            margin-bottom: 30px;
        }}
        .cards {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(350px, 1fr));
            gap: 15px;
            margin-bottom: 30px;
        }}
        .city-card {{
            background: #1a1d29;
            border-radius: 10px;
            padding: 18px;
            border: 1px solid #2a2d3a;
        }}
        .model-info {{
            color: #8b8fa3;
            margin: 8px 0 12px;
            font-size: 0.9em;
        }}
        .mini-table {{
            width: 100%;
            border-collapse: collapse;
        }}
        .mini-table th, .mini-table td {{
            padding: 6px 10px;
            text-align: center;
            font-size: 0.9em;
        }}
        .mini-table th {{
            color: #8b8fa3;
            border-bottom: 1px solid #2a2d3a;
        }}
        .detail-section {{
            background: #1a1d29;
            border-radius: 10px;
            padding: 18px;
            margin-bottom: 15px;
            border: 1px solid #2a2d3a;
        }}
        .detail-table {{
            width: 100%;
            border-collapse: collapse;
        }}
        .detail-table th {{
            padding: 8px 12px;
            text-align: center;
            color: #8b8fa3;
            border-bottom: 2px solid #2a2d3a;
            font-size: 0.85em;
            text-transform: uppercase;
        }}
        .detail-table td {{
            padding: 8px 12px;
            text-align: center;
            border-bottom: 1px solid #1f2233;
            font-size: 0.95em;
        }}
        .positive {{ color: #4ade80; font-weight: 600; }}
        .negative {{ color: #f87171; }}
        .neutral {{ color: #fbbf24; }}
        .signal-buy {{
            color: #4ade80;
            font-weight: 700;
            background: rgba(74, 222, 128, 0.1);
            border-radius: 4px;
            padding: 2px 8px;
        }}
        .signal-watch {{
            color: #fbbf24;
            font-weight: 600;
        }}
        .highlight-buy {{
            background: rgba(74, 222, 128, 0.05);
        }}
        .legend {{
            background: #1a1d29;
            border-radius: 10px;
            padding: 18px;
            margin-top: 20px;
            border: 1px solid #2a2d3a;
            font-size: 0.9em;
            color: #8b8fa3;
        }}
        .legend h3 {{ margin-bottom: 10px; }}
        .legend ul {{ padding-left: 20px; }}
        .legend li {{ margin: 5px 0; }}
        .timestamp {{
            text-align: center;
            color: #555;
            margin-top: 20px;
            font-size: 0.8em;
        }}
    </style>
</head>
<body>
    <h1>Weather Prediction Report</h1>
    <div class="subtitle">
        Target Date: <strong>{date_str}</strong> |
        Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} |
        Model: KDE (window=15d, halflife=4yr, recent-boost)
    </div>

    <h2>City Summary — Top 2 Predictions</h2>
    <div class="cards">
        {summary_cards}
    </div>

    <h2>Full Bucket Breakdown</h2>
    {detail_tables}

    <div class="legend">
        <h3>How to Read This Report</h3>
        <ul>
            <li><strong>Model Prob</strong>: Our KDE model's estimated probability for each temperature bucket</li>
            <li><strong>YES Price</strong>: Current Polymarket YES share price (= market-implied probability)</li>
            <li><strong>Edge</strong>: Model Prob minus Market Price. Positive = we think it's underpriced</li>
            <li><strong>EV/$1</strong>: Expected value per dollar bet (after 2% Polymarket fee)</li>
            <li><strong>BUY signal</strong>: Edge >= 5%. Consider betting.</li>
            <li><strong>WATCH signal</strong>: Edge 2-5%. Monitor but don't bet yet.</li>
            <li>Bucket formats: single degree (25°C), range (28-29°F), open-ended (<=27°F, >=38°F)</li>
        </ul>
        <h3 style="margin-top:15px">Bot Parameters to Consider</h3>
        <ul>
            <li><strong>MIN_EDGE</strong>: Start with 10% (0.10) for conservative, 5% (0.05) for aggressive</li>
            <li><strong>BET_SIZE</strong>: $5-$20 per bet recommended for testing</li>
            <li><strong>DAYS_IN_ADVANCE</strong>: 2-4 days. Markets further out have more edge but more uncertainty</li>
            <li><strong>MAX_BETS_PER_CITY</strong>: 2 (bet on top 2 most likely buckets per city)</li>
        </ul>
    </div>

    <div class="timestamp">
        Weather Bot Strategy #2 | Data from Polymarket Gamma API + Open-Meteo Archive
    </div>
</body>
</html>"""

    return html


def main():
    parser = argparse.ArgumentParser(description="Generate weather prediction report")
    parser.add_argument("--date", type=str, default="2026-04-11",
                        help="Target date (YYYY-MM-DD)")
    parser.add_argument("--cities", type=str,
                        default="nyc,chicago,denver,dallas,los-angeles",
                        help="Comma-separated city keys")
    parser.add_argument("--output", type=str, default=None,
                        help="Output HTML path")
    args = parser.parse_args()

    target_date = datetime.strptime(args.date, "%Y-%m-%d")
    cities = [c.strip() for c in args.cities.split(",")]
    generate_report(cities, target_date, args.output)


if __name__ == "__main__":
    main()
