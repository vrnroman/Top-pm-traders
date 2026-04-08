"""OddsPapi free-tier provider for sharp tennis odds.

OddsPapi (oddspapi.io) provides Pinnacle odds via REST API.
Free tier: 250 requests/month, no credit card required.

API docs: https://oddspapi.io/docs
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any

import requests

from src.odds.base import OddsProvider
from src.odds.models import MatchOdds

logger = logging.getLogger("odds.oddspapi")

ODDSPAPI_BASE_URL = "https://api.oddspapi.io/v1"

# Mapping from OddsPapi sport keys to our tour labels
TOUR_SPORT_KEYS = {
    "ATP": "tennis_atp",
    "WTA": "tennis_wta",
}


class OddsPapiProvider(OddsProvider):
    """Fetch sharp tennis odds from OddsPapi (Pinnacle odds)."""

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key or os.getenv("ODDSPAPI_API_KEY", "")
        if not self._api_key:
            logger.warning("ODDSPAPI_API_KEY not set — OddsPapi provider will not work")

    @property
    def name(self) -> str:
        return "oddspapi"

    def fetch_tennis_odds(self, tours: list[str] | None = None) -> list[MatchOdds]:
        """Fetch upcoming tennis match odds from OddsPapi."""
        if not self._api_key:
            logger.error("OddsPapi API key not configured")
            return []

        target_tours = tours or ["ATP", "WTA"]
        all_odds: list[MatchOdds] = []

        for tour in target_tours:
            sport_key = TOUR_SPORT_KEYS.get(tour.upper())
            if not sport_key:
                logger.warning(f"Unknown tour: {tour}")
                continue

            try:
                matches = self._fetch_sport(sport_key, tour)
                all_odds.extend(matches)
                logger.info(f"OddsPapi: fetched {len(matches)} {tour} matches")
            except Exception as e:
                logger.error(f"OddsPapi fetch failed for {tour}: {e}")

            time.sleep(0.5)  # Rate limit respect

        return all_odds

    def _fetch_sport(self, sport_key: str, tour: str) -> list[MatchOdds]:
        """Fetch odds for a single sport key."""
        url = f"{ODDSPAPI_BASE_URL}/odds"
        params = {
            "apiKey": self._api_key,
            "sport": sport_key,
            "regions": "pinnacle",
            "markets": "h2h",
            "oddsFormat": "decimal",
        }

        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if not isinstance(data, list):
            logger.warning(f"Unexpected OddsPapi response format: {type(data)}")
            return []

        return self._parse_response(data, tour)

    def _parse_response(self, data: list[dict[str, Any]], tour: str) -> list[MatchOdds]:
        """Parse OddsPapi JSON response into MatchOdds objects."""
        results: list[MatchOdds] = []

        for event in data:
            try:
                match_odds = self._parse_event(event, tour)
                if match_odds:
                    results.append(match_odds)
            except Exception as e:
                logger.debug(f"Failed to parse event: {e}")

        return results

    def _parse_event(self, event: dict[str, Any], tour: str) -> MatchOdds | None:
        """Parse a single event from OddsPapi response."""
        tournament = event.get("sport_title", f"{tour} Tennis")

        # Get the two players from home/away teams
        home = event.get("home_team", "")
        away = event.get("away_team", "")
        if not home or not away:
            return None

        # Parse match time
        commence_time = event.get("commence_time")
        match_time = None
        if commence_time:
            try:
                match_time = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        # Find Pinnacle (or first available) bookmaker odds
        bookmakers = event.get("bookmakers", [])
        odds_a, odds_b = self._extract_best_odds(bookmakers, home, away)

        if odds_a is None or odds_b is None:
            return None

        return MatchOdds.from_decimal_odds(
            source="pinnacle",
            tournament=tournament,
            tour=tour,
            player_a=home,
            player_b=away,
            odds_a=odds_a,
            odds_b=odds_b,
            match_time=match_time,
        )

    @staticmethod
    def _extract_best_odds(
        bookmakers: list[dict], home: str, away: str
    ) -> tuple[float | None, float | None]:
        """Extract Pinnacle odds (preferred) or fallback to first available."""
        # Prefer Pinnacle
        for bm in bookmakers:
            if "pinnacle" in bm.get("key", "").lower():
                return _extract_h2h_odds(bm, home, away)

        # Fallback to first bookmaker
        if bookmakers:
            return _extract_h2h_odds(bookmakers[0], home, away)

        return None, None


def _extract_h2h_odds(
    bookmaker: dict, home: str, away: str
) -> tuple[float | None, float | None]:
    """Extract h2h odds from a bookmaker entry."""
    for market in bookmaker.get("markets", []):
        if market.get("key") != "h2h":
            continue

        outcomes = market.get("outcomes", [])
        odds_map: dict[str, float] = {}
        for o in outcomes:
            odds_map[o.get("name", "")] = o.get("price", 0)

        odds_a = odds_map.get(home)
        odds_b = odds_map.get(away)

        if odds_a and odds_b and odds_a > 1.0 and odds_b > 1.0:
            return odds_a, odds_b

    return None, None
