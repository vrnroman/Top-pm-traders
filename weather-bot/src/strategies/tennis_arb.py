"""Strategy #3: Tennis Odds Arbitrage — Pinnacle vs Polymarket.

Compares sharp sportsbook odds (Pinnacle) against Polymarket tennis match
prices and generates trade signals on divergences above a configurable
threshold.

Core loop:
  1. Fetch sharp odds from configured provider (OddsPapi / scraper)
  2. Fetch Polymarket tennis match markets via Gamma API
  3. Match sportsbook events to Polymarket markets by player names
  4. Calculate divergence = sharp_implied_prob - polymarket_price
  5. If divergence > threshold, generate signal (BUY YES on underpriced side)
  6. Size using fractional Kelly criterion, capped at max bet
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import requests

from src.odds.base import OddsProvider
from src.odds.models import MatchOdds, OddsComparison
from src.odds.oddspapi import OddsPapiProvider
from src.odds.scraper import OddsScraperProvider

logger = logging.getLogger("strategy.tennis_arb")

SGT = timezone(timedelta(hours=8))

GAMMA_API_URL = "https://gamma-api.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"


class TennisArbStrategy:
    """Tennis odds arbitrage: sharp books vs Polymarket."""

    def __init__(
        self,
        odds_provider: str = "oddspapi",
        oddspapi_api_key: str = "",
        min_divergence: float = 0.10,
        max_bet_size: float = 100.0,
        kelly_fraction: float = 0.25,
        tournaments: list[str] | None = None,
        min_volume: float = 50_000.0,
        min_liquidity: float = 10_000.0,
        preview_mode: bool = True,
        data_dir: str = "",
    ):
        self.min_divergence = min_divergence
        self.max_bet_size = max_bet_size
        self.kelly_fraction = kelly_fraction
        self.tournaments = tournaments or ["ATP", "WTA"]
        self.min_volume = min_volume
        self.min_liquidity = min_liquidity
        self.preview_mode = preview_mode
        self.data_dir = data_dir

        # Initialize odds provider
        self._provider: OddsProvider
        if odds_provider == "oddspapi":
            self._provider = OddsPapiProvider(api_key=oddspapi_api_key)
        elif odds_provider == "scraper":
            self._provider = OddsScraperProvider()
        else:
            logger.warning(f"Unknown odds provider '{odds_provider}', using OddsPapi")
            self._provider = OddsPapiProvider(api_key=oddspapi_api_key)

    def scan(self) -> list[dict]:
        """Run a single scan: fetch odds, find markets, detect divergences.

        Returns list of signal dicts ready for execution or logging.
        """
        logger.info(f"Tennis arb scan starting (provider={self._provider.name})")

        # Step 1: Fetch sharp odds
        sharp_odds = self._provider.fetch_tennis_odds(tours=self.tournaments)
        if not sharp_odds:
            logger.info("No sharp odds available")
            return []
        logger.info(f"Fetched {len(sharp_odds)} matches from {self._provider.name}")

        # Step 2: Fetch Polymarket tennis markets
        poly_markets = self._fetch_polymarket_tennis_markets()
        if not poly_markets:
            logger.info("No Polymarket tennis markets found")
            return []
        logger.info(f"Found {len(poly_markets)} Polymarket tennis markets")

        # Step 3: Match and compare
        comparisons = self._match_and_compare(sharp_odds, poly_markets)
        logger.info(f"Matched {len(comparisons)} market-odds pairs")

        # Step 4: Filter by divergence threshold
        signals = []
        for comp in comparisons:
            if comp.divergence < self.min_divergence:
                continue

            bet_size = self._calculate_bet_size(comp.sharp_prob, comp.polymarket_price)

            signal = {
                "strategy": "tennis_arb",
                "tournament": comp.match_odds.tournament,
                "tour": comp.match_odds.tour,
                "player_a": comp.match_odds.player_a,
                "player_b": comp.match_odds.player_b,
                "target_player": comp.polymarket_player,
                "sharp_source": comp.match_odds.source,
                "sharp_prob": round(comp.sharp_prob, 4),
                "sharp_odds_a": comp.match_odds.odds_a,
                "sharp_odds_b": comp.match_odds.odds_b,
                "polymarket_price": round(comp.polymarket_price, 4),
                "divergence": round(comp.divergence, 4),
                "side": comp.side,
                "bet_size": round(bet_size, 2),
                "kelly_size": round(bet_size, 2),
                "market_id": comp.polymarket_market_id,
                "condition_id": comp.polymarket_condition_id,
                "token_id": comp.polymarket_token_id,
                "polymarket_volume": comp.polymarket_volume,
                "polymarket_liquidity": comp.polymarket_liquidity,
                "match_time": (
                    comp.match_odds.match_time.isoformat()
                    if comp.match_odds.match_time
                    else None
                ),
                "timestamp": datetime.now(SGT).isoformat(),
                "preview": self.preview_mode,
            }
            signals.append(signal)

        # Sort by divergence descending
        signals.sort(key=lambda s: s["divergence"], reverse=True)

        if signals:
            logger.info(f"Tennis arb: {len(signals)} signal(s) above "
                        f"{self.min_divergence:.0%} threshold")
            for s in signals:
                logger.info(
                    f"  {s['tournament']}: {s['player_a']} vs {s['player_b']} — "
                    f"Sharp: {s['sharp_prob']:.1%} / PM: {s['polymarket_price']:.1%} — "
                    f"Edge: {s['divergence']:.1%} — {s['side']} @ ${s['bet_size']:.0f}"
                )
        else:
            logger.info("Tennis arb: no signals above threshold")

        # Save signals to history
        self._save_signals(signals)

        return signals

    def _fetch_polymarket_tennis_markets(self) -> list[dict]:
        """Fetch active tennis match markets from Polymarket Gamma API."""
        all_markets: list[dict] = []
        offset = 0

        while True:
            try:
                resp = requests.get(
                    f"{GAMMA_API_URL}/events",
                    params={
                        "tag_slug": "tennis",
                        "limit": 100,
                        "offset": offset,
                        "active": "true",
                    },
                    timeout=30,
                )
                resp.raise_for_status()
                events = resp.json()

                if not events:
                    break

                for event in events:
                    markets = event.get("markets", [])
                    event_title = event.get("title", "")

                    for market in markets:
                        # Parse market metadata
                        volume_str = market.get("volume", "0")
                        try:
                            volume = float(volume_str)
                        except (ValueError, TypeError):
                            volume = 0.0

                        liquidity_str = market.get("liquidity", "0")
                        try:
                            liquidity = float(liquidity_str)
                        except (ValueError, TypeError):
                            liquidity = 0.0

                        # Filter by volume and liquidity thresholds
                        if volume < self.min_volume:
                            continue
                        if liquidity < self.min_liquidity:
                            continue

                        # Parse prices
                        prices = market.get("outcomePrices")
                        if isinstance(prices, str):
                            try:
                                prices = json.loads(prices)
                            except (json.JSONDecodeError, TypeError):
                                prices = None

                        yes_price = float(prices[0]) if prices and len(prices) > 0 else None
                        if yes_price is None or yes_price <= 0.01 or yes_price >= 0.99:
                            continue

                        # Parse token IDs
                        token_ids = market.get("clobTokenIds")
                        if isinstance(token_ids, str):
                            try:
                                token_ids = json.loads(token_ids)
                            except (json.JSONDecodeError, TypeError):
                                token_ids = None

                        question = market.get("question", "")
                        player = _extract_player_from_question(question)

                        all_markets.append({
                            "event_title": event_title,
                            "question": question,
                            "player": player,
                            "yes_price": yes_price,
                            "volume": volume,
                            "liquidity": liquidity,
                            "market_id": market.get("id", ""),
                            "condition_id": market.get("conditionId", ""),
                            "token_id_yes": (
                                token_ids[0] if token_ids and len(token_ids) > 0 else ""
                            ),
                        })

                offset += 100
                if offset > 2000:
                    break
                time.sleep(0.15)

            except requests.RequestException as e:
                logger.error(f"Polymarket tennis fetch failed: {e}")
                break

        return all_markets

    def _match_and_compare(
        self, sharp_odds: list[MatchOdds], poly_markets: list[dict]
    ) -> list[OddsComparison]:
        """Match sharp odds to Polymarket markets by player name similarity."""
        comparisons: list[OddsComparison] = []

        for odds in sharp_odds:
            for pm in poly_markets:
                pm_player = pm.get("player", "")
                if not pm_player:
                    continue

                # Try to match PM player to either side of the sharp odds
                side, sharp_prob = _match_player_to_odds(
                    pm_player, odds.player_a, odds.player_b,
                    odds.implied_prob_a, odds.implied_prob_b
                )

                if side is None:
                    continue

                divergence = sharp_prob - pm["yes_price"]

                comparisons.append(OddsComparison(
                    match_odds=odds,
                    polymarket_condition_id=pm["condition_id"],
                    polymarket_token_id=pm["token_id_yes"],
                    polymarket_market_id=pm["market_id"],
                    polymarket_question=pm["question"],
                    polymarket_player=pm_player,
                    polymarket_price=pm["yes_price"],
                    sharp_prob=sharp_prob,
                    divergence=divergence,
                    polymarket_volume=pm["volume"],
                    polymarket_liquidity=pm["liquidity"],
                ))

        return comparisons

    def _calculate_bet_size(self, sharp_prob: float, market_price: float) -> float:
        """Calculate bet size using fractional Kelly criterion.

        Kelly fraction = (bp - q) / b
        where b = (1/price) - 1 (net odds), p = sharp_prob, q = 1 - p
        """
        if market_price <= 0 or market_price >= 1:
            return 0.0

        b = (1.0 / market_price) - 1.0  # Net payout odds
        p = sharp_prob
        q = 1.0 - p

        kelly = (b * p - q) / b if b > 0 else 0.0

        if kelly <= 0:
            return 0.0

        # Apply fractional Kelly and cap
        size = kelly * self.kelly_fraction * self.max_bet_size
        return min(size, self.max_bet_size)

    def _save_signals(self, signals: list[dict]) -> None:
        """Append signals to trade history JSONL file."""
        if not self.data_dir or not signals:
            return

        os.makedirs(self.data_dir, exist_ok=True)
        history_path = os.path.join(self.data_dir, "tennis_trades.jsonl")

        try:
            with open(history_path, "a") as f:
                for s in signals:
                    f.write(json.dumps(s) + "\n")
        except OSError as e:
            logger.error(f"Failed to save tennis signals: {e}")


# ── Helpers ──────────────────────────────────────────────────────────────


def _extract_player_from_question(question: str) -> str:
    """Extract player name from Polymarket market question.

    Common patterns:
      - "Will Jannik Sinner win the 2026 ATP Monte-Carlo Masters?"
      - "Sinner vs Rublev: Who will win?"
      - "Jannik Sinner to win ATP Monte Carlo"
    """
    # Pattern: "Will <player> win..."
    m = re.match(r"Will (.+?) win\b", question, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # Pattern: "<player> to win..."
    m = re.match(r"(.+?) to win\b", question, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # Pattern: "<player_a> vs <player_b>"
    m = re.search(r"(.+?)\s+vs\.?\s+(.+?)[\s:?]", question, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    return ""


def _normalize_name(name: str) -> str:
    """Normalize a player name for fuzzy matching."""
    name = name.lower().strip()
    # Remove common prefixes/suffixes
    name = re.sub(r"\b(jr\.?|sr\.?|ii|iii)\b", "", name)
    # Remove non-alpha chars except spaces
    name = re.sub(r"[^a-z\s]", "", name)
    return " ".join(name.split())


def _match_player_to_odds(
    pm_player: str,
    player_a: str,
    player_b: str,
    prob_a: float,
    prob_b: float,
    threshold: float = 0.6,
) -> tuple[str | None, float]:
    """Match a Polymarket player name to one side of the sharp odds.

    Uses fuzzy string matching (SequenceMatcher) on normalized names.
    Also checks if last name alone matches (common for tennis).

    Returns:
        (side, sharp_prob) or (None, 0.0) if no match.
    """
    pm_norm = _normalize_name(pm_player)
    a_norm = _normalize_name(player_a)
    b_norm = _normalize_name(player_b)

    # Full name similarity
    sim_a = SequenceMatcher(None, pm_norm, a_norm).ratio()
    sim_b = SequenceMatcher(None, pm_norm, b_norm).ratio()

    # Last name matching (common in tennis contexts)
    pm_last = pm_norm.split()[-1] if pm_norm.split() else ""
    a_last = a_norm.split()[-1] if a_norm.split() else ""
    b_last = b_norm.split()[-1] if b_norm.split() else ""

    if pm_last and a_last and pm_last == a_last:
        sim_a = max(sim_a, 0.85)
    if pm_last and b_last and pm_last == b_last:
        sim_b = max(sim_b, 0.85)

    # Check if PM player is contained in the sharp player name or vice versa
    if pm_norm in a_norm or a_norm in pm_norm:
        sim_a = max(sim_a, 0.90)
    if pm_norm in b_norm or b_norm in pm_norm:
        sim_b = max(sim_b, 0.90)

    best_sim = max(sim_a, sim_b)
    if best_sim < threshold:
        return None, 0.0

    if sim_a >= sim_b:
        return "A", prob_a
    else:
        return "B", prob_b
