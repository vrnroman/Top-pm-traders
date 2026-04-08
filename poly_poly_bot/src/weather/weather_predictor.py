"""KDE-based weather prediction model.

Predicts daily max temperature distribution using historical data with:
- Seasonal windowing (same time of year +/- window_days)
- Recency weighting (recent years weighted more)
- Recent trend boosting (last 7 days anomaly adjustment)
"""

import numpy as np
import pandas as pd
from scipy import stats


class WeatherPredictor:
    """Predicts daily max temperature distributions using weighted KDE."""

    def __init__(self, weather_df: pd.DataFrame, window_days: int = 15,
                 recency_halflife: float = 4.0):
        self.weather_df = weather_df.copy()
        self.weather_df["date"] = pd.to_datetime(self.weather_df["date"])
        self.weather_df["doy"] = self.weather_df["date"].dt.dayofyear
        self.weather_df["year"] = self.weather_df["date"].dt.year
        self.weather_df["month"] = self.weather_df["date"].dt.month
        self.window_days = window_days
        self.recency_halflife = recency_halflife

    def predict_distribution(self, city: str, target_date,
                              use_recent_boost: bool = True) -> dict:
        """Predict temperature distribution for a city on a target date.

        Returns dict with: mean, std, pdf (grid + density), n_samples
        """
        target_date = pd.Timestamp(target_date)
        city_data = self.weather_df[self.weather_df["city"] == city]
        if len(city_data) == 0:
            return {"mean": 70, "std": 10, "grid": np.array([]), "pdf": np.array([]),
                    "n_samples": 0}

        target_doy = target_date.timetuple().tm_yday
        target_year = target_date.year

        # Select same-season historical data
        doy_diff = np.abs(city_data["doy"] - target_doy)
        doy_diff = np.minimum(doy_diff, 365 - doy_diff)
        mask = (doy_diff <= self.window_days) & (city_data["date"] < target_date)
        subset = city_data[mask]

        if len(subset) < 10:
            mask2 = (doy_diff <= 30) & (city_data["date"] < target_date)
            subset = city_data[mask2]

        if len(subset) < 5:
            mean = city_data["max_temp"].mean()
            return {"mean": mean, "std": 10, "grid": np.array([]), "pdf": np.array([]),
                    "n_samples": 0}

        temps = subset["max_temp"].values
        years_ago = target_year - subset["year"].values
        weights = np.exp(-np.log(2) * years_ago / self.recency_halflife)
        weights /= weights.sum()

        # Weighted statistics
        wmean = np.average(temps, weights=weights)
        wstd = np.sqrt(np.average((temps - wmean) ** 2, weights=weights))
        bw = max(1.06 * wstd * len(temps) ** (-0.2), 0.5)

        # Build KDE on a fine grid
        grid = np.linspace(temps.min() - 10, temps.max() + 10, 500)
        pdf = np.zeros_like(grid)
        for t, w in zip(temps, weights):
            pdf += w * stats.norm.pdf(grid, loc=t, scale=bw)
        total = np.trapezoid(pdf, grid)
        if total > 0:
            pdf /= total

        # Recent trend boost
        if use_recent_boost:
            recent = city_data[city_data["date"] < target_date].tail(7)
            if len(recent) >= 3:
                month_data = city_data[city_data["month"] == target_date.month]
                if len(month_data) > 0:
                    month_mean = month_data["max_temp"].mean()
                    recent_mean = recent["max_temp"].mean()
                    anomaly = recent_mean - month_mean
                    shifted_pdf = np.zeros_like(grid)
                    for t, w in zip(temps, weights):
                        shifted_pdf += w * stats.norm.pdf(grid, loc=t + anomaly * 0.2, scale=bw)
                    shifted_total = np.trapezoid(shifted_pdf, grid)
                    if shifted_total > 0:
                        shifted_pdf /= shifted_total
                    pdf = 0.7 * pdf + 0.3 * shifted_pdf
                    total = np.trapezoid(pdf, grid)
                    if total > 0:
                        pdf /= total

        return {
            "mean": wmean,
            "std": wstd,
            "grid": grid,
            "pdf": pdf,
            "n_samples": len(subset),
            "bw": bw,
        }

    def predict_buckets(self, city: str, target_date, buckets: list[dict],
                         use_recent_boost: bool = True) -> tuple[dict, dict]:
        """Predict probability for each temperature bucket.

        Args:
            city: city key
            target_date: target date
            buckets: list of dicts with keys:
                temp (int or float), is_lower (bool), is_upper (bool),
                label (str), and optionally temp_high (for range buckets)

        Returns:
            (bucket_probs, dist_info)
        """
        dist = self.predict_distribution(city, target_date, use_recent_boost)
        grid = dist["grid"]
        pdf = dist["pdf"]

        if len(grid) == 0 or len(pdf) == 0:
            n = len(buckets)
            return {b["label"]: 1.0 / n for b in buckets}, dist

        probs = {}
        for b in buckets:
            temp = b["temp"]
            temp_high = b.get("temp_high", temp)

            if b["is_lower"]:
                # e.g., "<=27F" means temp <= 27
                mask_b = grid <= (temp + 0.5)
            elif b["is_upper"]:
                # e.g., ">=38F" means temp >= 38
                mask_b = grid >= (temp - 0.5)
            elif temp_high > temp:
                # Range bucket, e.g., "28-29F" means 28 <= temp <= 29
                mask_b = (grid >= (temp - 0.5)) & (grid < (temp_high + 0.5))
            else:
                # Single degree bucket, e.g., "25C"
                mask_b = (grid >= (temp - 0.5)) & (grid < (temp + 0.5))

            p = np.trapezoid(pdf[mask_b], grid[mask_b]) if mask_b.any() else 0.0
            probs[b["label"]] = max(0.0, float(p))

        # Normalize
        total = sum(probs.values())
        if total > 0:
            probs = {k: v / total for k, v in probs.items()}

        return probs, dist

    def get_top_predictions(self, city: str, target_date, buckets: list[dict],
                             n: int = 2) -> list[dict]:
        """Get the top N most likely buckets with probabilities.

        Returns list of dicts with: label, probability, temp, is_lower, is_upper
        """
        probs, dist = self.predict_buckets(city, target_date, buckets)
        sorted_buckets = sorted(probs.items(), key=lambda x: x[1], reverse=True)

        results = []
        for label, prob in sorted_buckets[:n]:
            bucket = next(b for b in buckets if b["label"] == label)
            results.append({
                "label": label,
                "probability": prob,
                "temp": bucket["temp"],
                "temp_high": bucket.get("temp_high", bucket["temp"]),
                "is_lower": bucket["is_lower"],
                "is_upper": bucket["is_upper"],
            })

        return results, dist
