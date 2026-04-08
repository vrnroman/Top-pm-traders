"""Web scraper fallback for tennis odds from OddsPortal.

Scrapes odds from free aggregator sites that display Pinnacle lines.
Used when OddsPapi is unavailable or API quota is exhausted.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime

import requests

from src.odds.base import OddsProvider
from src.odds.models import MatchOdds

logger = logging.getLogger("odds.scraper")

ODDSPORTAL_TENNIS_URL = "https://www.oddsportal.com/tennis/"

# User-Agent to avoid bot detection
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


class OddsScraperProvider(OddsProvider):
    """Scrape tennis odds from OddsPortal as a fallback."""

    @property
    def name(self) -> str:
        return "scraper"

    def fetch_tennis_odds(self, tours: list[str] | None = None) -> list[MatchOdds]:
        """Scrape upcoming tennis match odds.

        Note: This is a best-effort scraper. OddsPortal uses heavy JavaScript
        rendering, so results may be limited. For production use, prefer
        OddsPapi or another API provider.
        """
        target_tours = tours or ["ATP", "WTA"]
        all_odds: list[MatchOdds] = []

        for tour in target_tours:
            try:
                matches = self._scrape_tour(tour)
                all_odds.extend(matches)
                logger.info(f"Scraper: found {len(matches)} {tour} matches")
            except Exception as e:
                logger.error(f"Scraper failed for {tour}: {e}")

            time.sleep(1.0)  # Be respectful

        return all_odds

    def _scrape_tour(self, tour: str) -> list[MatchOdds]:
        """Scrape odds for a single tour from OddsPortal feed."""
        # OddsPortal uses JS rendering heavily, so we use their JSON feed
        # which returns match data in a parseable format
        tour_lower = tour.lower()
        url = f"https://fb.oddsportal.com/feed/tennis/{tour_lower}/1/1/odds.dat"

        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if not resp.ok:
                logger.debug(f"OddsPortal feed returned {resp.status_code}")
                return []

            return self._parse_feed(resp.text, tour)
        except requests.RequestException as e:
            logger.debug(f"OddsPortal request failed: {e}")
            return []

    def _parse_feed(self, text: str, tour: str) -> list[MatchOdds]:
        """Parse OddsPortal feed data into MatchOdds."""
        results: list[MatchOdds] = []

        # OddsPortal feed is JavaScript-formatted data
        # Extract match entries using regex patterns
        # Pattern: 'player_a' vs 'player_b', odds: [decimal_a, decimal_b]
        match_pattern = re.compile(
            r'"home":"([^"]+)".*?"away":"([^"]+)".*?'
            r'"odds":\s*\[([0-9.]+),\s*([0-9.]+)\]',
            re.DOTALL,
        )

        tournament_pattern = re.compile(r'"tournament_name":"([^"]+)"')
        tournament_match = tournament_pattern.search(text)
        tournament = tournament_match.group(1) if tournament_match else f"{tour} Tennis"

        for m in match_pattern.finditer(text):
            try:
                player_a = m.group(1).strip()
                player_b = m.group(2).strip()
                odds_a = float(m.group(3))
                odds_b = float(m.group(4))

                if odds_a <= 1.0 or odds_b <= 1.0:
                    continue

                match_odds = MatchOdds.from_decimal_odds(
                    source="oddsportal",
                    tournament=tournament,
                    tour=tour,
                    player_a=player_a,
                    player_b=player_b,
                    odds_a=odds_a,
                    odds_b=odds_b,
                )
                results.append(match_odds)
            except (ValueError, IndexError) as e:
                logger.debug(f"Failed to parse match: {e}")

        return results
