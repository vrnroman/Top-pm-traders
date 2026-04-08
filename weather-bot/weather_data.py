"""Fetch historical weather data from Open-Meteo."""

import os
import requests
import pandas as pd
from datetime import datetime

from cities import CITIES


def fetch_weather_history(city_key: str, cache_dir: str,
                           start_date: str = "2010-01-01",
                           end_date: str = None) -> pd.DataFrame:
    """Fetch daily max temp history from Open-Meteo for a city."""
    if end_date is None:
        end_date = (datetime.now()).strftime("%Y-%m-%d")

    cache_path = os.path.join(cache_dir, f"weather_{city_key}.parquet")
    if os.path.exists(cache_path):
        df = pd.read_parquet(cache_path)
        last = pd.to_datetime(df["date"]).max()
        if (datetime.now() - last).days <= 2:
            return df

    city = CITIES[city_key]
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": city["lat"],
        "longitude": city["lon"],
        "start_date": start_date,
        "end_date": end_date,
        "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": city["unit"],
        "timezone": city["tz"],
    }

    print(f"  Fetching weather history for {city['name']}...")
    resp = requests.get(url, params=params, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    daily = data.get("daily", {})
    df = pd.DataFrame({
        "date": pd.to_datetime(daily["time"]),
        "max_temp": daily["temperature_2m_max"],
        "min_temp": daily["temperature_2m_min"],
    })
    df["city"] = city_key
    df["month"] = df["date"].dt.month
    df["doy"] = df["date"].dt.dayofyear

    # Drop rows with NaN temps
    df = df.dropna(subset=["max_temp"])

    os.makedirs(cache_dir, exist_ok=True)
    df.to_parquet(cache_path)
    print(f"    {len(df)} days loaded")
    return df


def fetch_all_weather(cities: list[str], cache_dir: str) -> pd.DataFrame:
    """Fetch weather history for all specified cities."""
    frames = []
    for city_key in cities:
        if city_key not in CITIES:
            print(f"  WARNING: Unknown city '{city_key}', skipping")
            continue
        try:
            df = fetch_weather_history(city_key, cache_dir)
            frames.append(df)
        except Exception as e:
            print(f"  ERROR fetching {city_key}: {e}")

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
