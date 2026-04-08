"""Fetch weather temperature markets and prices from Polymarket.

Handles three bucket formats:
  - Single degree: "37°F on ..." or "25°C on ..."
  - Range: "28-29°F" (2°F range, common for US cities)
  - Open-ended lower: "27°F or below" / "20°C or below"
  - Open-ended upper: "38°F or higher" / "30°C or higher"
"""

import os
import json
import re
import time
import requests
import pandas as pd
from datetime import datetime, timedelta

from cities import CITIES

GAMMA_BASE = "https://gamma-api.polymarket.com"


def fetch_temperature_events_by_tag(cache_path: str = None) -> list[dict]:
    """Fetch ALL temperature events from Polymarket via tag_slug pagination."""
    if cache_path and os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f)

    print("  Fetching temperature events from Polymarket...")
    all_events = []
    offset = 0

    while True:
        resp = requests.get(f"{GAMMA_BASE}/events", params={
            "tag_slug": "temperature",
            "limit": 100,
            "offset": offset,
        }, timeout=30)
        data = resp.json()
        if not data:
            break
        all_events.extend(data)
        print(f"    Fetched {offset + len(data)} events...")
        offset += 100
        time.sleep(0.15)
        if offset > 5000:
            break

    # Enrich with city and target_date
    for event in all_events:
        city, date = _parse_event_title(event.get("title", ""))
        event["_city"] = city
        event["_target_date"] = date

    if cache_path:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump(all_events, f)

    print(f"  Total: {len(all_events)} temperature events")
    return all_events


def fetch_event_by_slug(city_slug: str, target_date: datetime) -> dict | None:
    """Fetch a single temperature event by constructing its slug."""
    month_name = target_date.strftime("%B").lower()
    day = target_date.day
    year = target_date.year

    # Try with year first, then without
    for slug in [
        f"highest-temperature-in-{city_slug}-on-{month_name}-{day}-{year}",
        f"highest-temperature-in-{city_slug}-on-{month_name}-{day}",
    ]:
        try:
            resp = requests.get(f"{GAMMA_BASE}/events", params={"slug": slug}, timeout=10)
            data = resp.json()
            if data and len(data) > 0:
                return data[0]
        except Exception:
            pass
        time.sleep(0.1)

    return None


def fetch_markets_for_cities_and_dates(cities: list[str],
                                        dates: list[datetime]) -> pd.DataFrame:
    """Fetch Polymarket markets for specific cities and dates.

    Returns DataFrame with columns:
        city, target_date, bucket_label, bucket_temp, temp_high,
        is_lower, is_upper, yes_price, market_id, condition_id,
        clob_token_yes, clob_token_no, resolved_yes, unit
    """
    rows = []

    for city_key in cities:
        city_info = CITIES.get(city_key)
        if not city_info:
            print(f"  WARNING: Unknown city '{city_key}', skipping")
            continue

        slug = city_info["slug"]
        unit = city_info["unit"]

        for target_date in dates:
            event = fetch_event_by_slug(slug, target_date)
            if not event:
                print(f"  No market found for {city_info['name']} on "
                      f"{target_date.strftime('%Y-%m-%d')}")
                continue

            markets = event.get("markets", [])
            print(f"  {city_info['name']} {target_date.strftime('%Y-%m-%d')}: "
                  f"{len(markets)} buckets")

            for market in markets:
                question = market.get("question", "")
                bucket = _parse_bucket(question, unit)
                if bucket is None:
                    continue

                # Parse prices
                prices = market.get("outcomePrices")
                if isinstance(prices, str):
                    try:
                        prices = json.loads(prices)
                    except Exception:
                        prices = None
                yes_price = float(prices[0]) if prices and len(prices) > 0 else None

                # Parse token IDs
                token_ids = market.get("clobTokenIds")
                if isinstance(token_ids, str):
                    try:
                        token_ids = json.loads(token_ids)
                    except Exception:
                        token_ids = None

                resolved_yes = None
                if yes_price is not None:
                    if yes_price >= 0.99:
                        resolved_yes = True
                    elif yes_price <= 0.01:
                        resolved_yes = False

                rows.append({
                    "city": city_key,
                    "target_date": target_date.strftime("%Y-%m-%d"),
                    "bucket_label": bucket["label"],
                    "bucket_temp": bucket["temp"],
                    "temp_high": bucket.get("temp_high", bucket["temp"]),
                    "is_lower": bucket["is_lower"],
                    "is_upper": bucket["is_upper"],
                    "yes_price": yes_price,
                    "market_id": market.get("id"),
                    "condition_id": market.get("conditionId"),
                    "clob_token_yes": token_ids[0] if token_ids and len(token_ids) > 0 else None,
                    "clob_token_no": token_ids[1] if token_ids and len(token_ids) > 1 else None,
                    "resolved_yes": resolved_yes,
                    "unit": unit,
                    "question": question,
                })

            time.sleep(0.15)

    df = pd.DataFrame(rows)
    if len(df) > 0:
        df["target_date"] = pd.to_datetime(df["target_date"])
    return df


def parse_all_markets(events: list[dict]) -> pd.DataFrame:
    """Parse bulk events into a flat markets DataFrame (for backtest)."""
    rows = []
    for event in events:
        city_key = event.get("_city")
        target_date = event.get("_target_date")
        if not city_key or not target_date:
            continue

        city_info = CITIES.get(city_key, {})
        unit = city_info.get("unit", "fahrenheit")

        for market in event.get("markets", []):
            question = market.get("question", "")
            bucket = _parse_bucket(question, unit)
            if bucket is None:
                continue

            prices = market.get("outcomePrices")
            if isinstance(prices, str):
                try:
                    prices = json.loads(prices)
                except Exception:
                    prices = None
            yes_price = float(prices[0]) if prices and len(prices) > 0 else None

            token_ids = market.get("clobTokenIds")
            if isinstance(token_ids, str):
                try:
                    token_ids = json.loads(token_ids)
                except Exception:
                    token_ids = None

            resolved_yes = None
            if yes_price is not None:
                if yes_price >= 0.99:
                    resolved_yes = True
                elif yes_price <= 0.01:
                    resolved_yes = False

            rows.append({
                "city": city_key,
                "target_date": target_date,
                "bucket_label": bucket["label"],
                "bucket_temp": bucket["temp"],
                "temp_high": bucket.get("temp_high", bucket["temp"]),
                "is_lower": bucket["is_lower"],
                "is_upper": bucket["is_upper"],
                "yes_price": yes_price,
                "market_id": market.get("id"),
                "clob_token_yes": token_ids[0] if token_ids and len(token_ids) > 0 else None,
                "resolved_yes": resolved_yes,
                "unit": unit,
            })

    df = pd.DataFrame(rows)
    if len(df) > 0:
        df["target_date"] = pd.to_datetime(df["target_date"])
    return df


def get_resolved_outcomes(markets_df: pd.DataFrame) -> pd.DataFrame:
    """Extract actual outcomes for resolved events."""
    rows = []
    for (city, date), group in markets_df.groupby(["city", "target_date"]):
        winners = group[group["resolved_yes"] == True]
        if len(winners) == 1:
            w = winners.iloc[0]
            rows.append({
                "city": city,
                "target_date": date,
                "actual_bucket": w["bucket_label"],
                "actual_temp": w["bucket_temp"],
                "is_lower": w["is_lower"],
                "is_upper": w["is_upper"],
                "unit": w["unit"],
            })
    return pd.DataFrame(rows)


def get_buckets_for_market(markets_df: pd.DataFrame, city: str,
                            target_date) -> list[dict]:
    """Extract bucket list for a specific city+date from markets DataFrame."""
    day = markets_df[
        (markets_df["city"] == city) &
        (markets_df["target_date"] == pd.Timestamp(target_date))
    ]
    buckets = []
    for _, m in day.iterrows():
        buckets.append({
            "temp": m["bucket_temp"],
            "temp_high": m.get("temp_high", m["bucket_temp"]),
            "is_lower": m["is_lower"],
            "is_upper": m["is_upper"],
            "label": m["bucket_label"],
        })
    return buckets


# ─── Parsing helpers ────────────────────────────────────────────────────

def _parse_event_title(title: str) -> tuple:
    """Parse city and date from event title."""
    m = re.search(r'Highest temperature in (.+?) on (.+?)\?', title, re.IGNORECASE)
    if not m:
        return None, None
    city_key = _normalize_city(m.group(1).strip())
    target_date = _parse_date(m.group(2).strip())
    return city_key, target_date


CITY_ALIASES = {
    "nyc": "nyc", "new york city": "nyc", "new york": "nyc",
    "miami": "miami", "chicago": "chicago",
    "los angeles": "los-angeles", "la": "los-angeles",
    "atlanta": "atlanta", "dallas": "dallas",
    "denver": "denver", "seattle": "seattle",
    "houston": "houston", "austin": "austin",
    "san francisco": "san-francisco",
    "london": "london", "hong kong": "hong-kong",
    "tokyo": "tokyo", "seoul": "seoul",
    "paris": "paris", "toronto": "toronto",
    "buenos aires": "buenos-aires",
    "moscow": "moscow", "beijing": "beijing",
    "shanghai": "shanghai", "singapore": "singapore",
    "madrid": "madrid", "istanbul": "istanbul",
    "milan": "milan", "taipei": "taipei",
    "amsterdam": "amsterdam", "munich": "munich",
    "ankara": "ankara", "warsaw": "warsaw",
    "tel aviv": "tel-aviv",
    "phoenix": "phoenix", "dc": "dc",
    "mexico city": "mexico-city",
    "sao paulo": "sao-paulo", "são paulo": "sao-paulo",
}


def _normalize_city(city_raw: str) -> str:
    key = CITY_ALIASES.get(city_raw.lower())
    if key:
        return key
    return city_raw.lower().replace(" ", "-")


MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _parse_date(date_raw: str) -> str | None:
    parts = date_raw.replace(",", "").split()
    if len(parts) >= 2:
        month = MONTH_MAP.get(parts[0].lower())
        day_str = parts[1].rstrip("stndrdth")
        if month and day_str.isdigit():
            day = int(day_str)
            year = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 2026
            try:
                return datetime(year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                pass
    return None


def _parse_bucket(question: str, unit: str) -> dict | None:
    """Parse temperature bucket from market question.

    Handles:
      - "27°F or below" → lower bound
      - "38°F or higher" → upper bound
      - "37°F on ..." → single degree (exact)
      - "28-29°F" → 2-degree range
      - Same patterns with °C
    """
    deg = "°F" if unit == "fahrenheit" else "°C"
    esc_deg = re.escape(deg)

    # "XX°F or below" / "XX°C or below"
    m = re.search(r'(\d+)' + esc_deg + r' or below', question)
    if m:
        temp = int(m.group(1))
        return {"temp": temp, "label": f"<={temp}", "is_lower": True, "is_upper": False}

    # "XX°F or higher" / "XX°C or higher"
    m = re.search(r'(\d+)' + esc_deg + r' or higher', question)
    if m:
        temp = int(m.group(1))
        return {"temp": temp, "label": f">={temp}", "is_lower": False, "is_upper": True}

    # Range: "XX-YY°F" (e.g., "28-29°F")
    m = re.search(r'(\d+)-(\d+)' + esc_deg, question)
    if m:
        low = int(m.group(1))
        high = int(m.group(2))
        return {"temp": low, "temp_high": high, "label": f"{low}-{high}",
                "is_lower": False, "is_upper": False}

    # Single degree: "XX°F on" or "XX°C on"
    m = re.search(r'(\d+)' + esc_deg + r' on', question)
    if m:
        temp = int(m.group(1))
        return {"temp": temp, "label": f"{temp}", "is_lower": False, "is_upper": False}

    # Fallback: just a number with degree
    m = re.search(r'be (\d+)' + esc_deg, question)
    if m:
        temp = int(m.group(1))
        return {"temp": temp, "label": f"{temp}", "is_lower": False, "is_upper": False}

    return None
