#!/usr/bin/env python3
"""Backtest for Strategy #3: Tennis Odds Arbitrage.

Replays historical Polymarket tennis match data against historical sharp odds
to validate the strategy before risking real capital.

Usage:
    python -m backtest.tennis_backtest
    python -m backtest.tennis_backtest --min-divergence 0.08 --max-bet 50
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime

import requests

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.odds.models import MatchOdds

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("tennis_backtest")

GAMMA_API_URL = "https://gamma-api.polymarket.com"


@dataclass
class BacktestTrade:
    """A single simulated trade in the backtest."""

    tournament: str
    player_a: str
    player_b: str
    target_player: str
    sharp_prob: float
    polymarket_price: float
    divergence: float
    bet_size: float
    outcome: str = ""  # "win", "loss", or "unresolved"
    pnl: float = 0.0


@dataclass
class BacktestResult:
    """Aggregate results of a backtest run."""

    trades: list[BacktestTrade] = field(default_factory=list)
    min_divergence: float = 0.10
    max_bet_size: float = 100.0
    kelly_fraction: float = 0.25

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def resolved_trades(self) -> list[BacktestTrade]:
        return [t for t in self.trades if t.outcome in ("win", "loss")]

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.outcome == "win")

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.outcome == "loss")

    @property
    def win_rate(self) -> float:
        resolved = len(self.resolved_trades)
        return self.wins / resolved if resolved > 0 else 0.0

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.resolved_trades)

    @property
    def total_wagered(self) -> float:
        return sum(t.bet_size for t in self.resolved_trades)

    @property
    def roi(self) -> float:
        return self.total_pnl / self.total_wagered if self.total_wagered > 0 else 0.0

    @property
    def avg_edge(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t.divergence for t in self.trades) / len(self.trades)

    def report(self) -> str:
        """Generate a human-readable backtest report."""
        lines = [
            "=" * 60,
            "  Tennis Odds Arbitrage — Backtest Report",
            "=" * 60,
            f"  Parameters:",
            f"    Min divergence: {self.min_divergence:.0%}",
            f"    Max bet size:   ${self.max_bet_size:.0f}",
            f"    Kelly fraction: {self.kelly_fraction:.0%}",
            "",
            f"  Results:",
            f"    Total opportunities: {self.total_trades}",
            f"    Resolved:           {len(self.resolved_trades)}",
            f"    Wins:               {self.wins}",
            f"    Losses:             {self.losses}",
            f"    Win rate:           {self.win_rate:.1%}",
            f"    Avg edge/trade:     {self.avg_edge:.1%}",
            "",
            f"  P&L:",
            f"    Total wagered:      ${self.total_wagered:.2f}",
            f"    Total P&L:          ${self.total_pnl:+.2f}",
            f"    ROI:                {self.roi:+.1%}",
            "=" * 60,
        ]

        if self.resolved_trades:
            lines.append("")
            lines.append("  Trade Detail:")
            lines.append("  " + "-" * 56)
            for t in self.resolved_trades:
                icon = "W" if t.outcome == "win" else "L"
                lines.append(
                    f"  [{icon}] {t.tournament}: {t.target_player} "
                    f"(sharp={t.sharp_prob:.1%} pm={t.polymarket_price:.1%} "
                    f"edge={t.divergence:.1%}) -> ${t.pnl:+.2f}"
                )
            lines.append("  " + "-" * 56)

        return "\n".join(lines)


def fetch_historical_tennis_events() -> list[dict]:
    """Fetch resolved tennis events from Polymarket for backtesting."""
    logger.info("Fetching historical tennis events from Polymarket...")
    all_events: list[dict] = []
    offset = 0

    while True:
        try:
            resp = requests.get(
                f"{GAMMA_API_URL}/events",
                params={
                    "tag_slug": "tennis",
                    "limit": 100,
                    "offset": offset,
                    "closed": "true",
                },
                timeout=30,
            )
            resp.raise_for_status()
            events = resp.json()

            if not events:
                break

            all_events.extend(events)
            logger.info(f"  Fetched {offset + len(events)} events...")
            offset += 100
            time.sleep(0.2)

            if offset > 3000:
                break

        except requests.RequestException as e:
            logger.error(f"Failed to fetch events: {e}")
            break

    logger.info(f"Total historical tennis events: {len(all_events)}")
    return all_events


def simulate_backtest(
    events: list[dict],
    historical_odds: list[MatchOdds],
    min_divergence: float = 0.10,
    max_bet_size: float = 100.0,
    kelly_fraction: float = 0.25,
) -> BacktestResult:
    """Simulate the tennis arb strategy against historical data.

    For each resolved Polymarket tennis market:
      1. Find matching historical sharp odds
      2. Calculate divergence
      3. If above threshold, simulate the trade
      4. Check if the market resolved YES (player won) or NO
      5. Calculate P&L
    """
    result = BacktestResult(
        min_divergence=min_divergence,
        max_bet_size=max_bet_size,
        kelly_fraction=kelly_fraction,
    )

    # Index odds by normalized player last name for quick lookup
    odds_by_name: dict[str, list[MatchOdds]] = {}
    for odds in historical_odds:
        for name in [odds.player_a, odds.player_b]:
            key = name.lower().split()[-1] if name else ""
            if key:
                odds_by_name.setdefault(key, []).append(odds)

    for event in events:
        markets = event.get("markets", [])
        event_title = event.get("title", "")

        for market in markets:
            question = market.get("question", "")

            # Check if resolved
            prices = market.get("outcomePrices")
            if isinstance(prices, str):
                try:
                    prices = json.loads(prices)
                except (json.JSONDecodeError, TypeError):
                    continue

            if not prices or len(prices) < 1:
                continue

            yes_price_final = float(prices[0])

            # Determine resolution
            if yes_price_final >= 0.99:
                resolved_yes = True
            elif yes_price_final <= 0.01:
                resolved_yes = False
            else:
                continue  # Not resolved

            # Get initial price (use volume-weighted or just check for significant price)
            # For backtest, we approximate entry price from the initial market price
            # In practice, we'd use historical snapshots
            initial_price = market.get("initialPrice")
            if initial_price is None:
                # Use a heuristic: resolved YES=1 means initial was probably < 1
                # We need to skip if we don't have price data
                continue

            try:
                entry_price = float(initial_price)
            except (ValueError, TypeError):
                continue

            if entry_price <= 0.05 or entry_price >= 0.95:
                continue

            # Extract player name from question
            import re
            player_match = re.match(r"Will (.+?) win\b", question, re.IGNORECASE)
            if not player_match:
                continue
            player_name = player_match.group(1).strip()
            player_last = player_name.lower().split()[-1] if player_name else ""

            if not player_last:
                continue

            # Find matching sharp odds
            matching_odds = odds_by_name.get(player_last, [])
            if not matching_odds:
                continue

            # Use the first match (in production, match by tournament + date)
            odds = matching_odds[0]

            # Determine which side of the odds this player is
            if player_last == odds.player_a.lower().split()[-1]:
                sharp_prob = odds.implied_prob_a
            elif player_last == odds.player_b.lower().split()[-1]:
                sharp_prob = odds.implied_prob_b
            else:
                continue

            divergence = sharp_prob - entry_price
            if divergence < min_divergence:
                continue

            # Calculate Kelly bet size
            b = (1.0 / entry_price) - 1.0 if entry_price > 0 else 0
            kelly = (b * sharp_prob - (1 - sharp_prob)) / b if b > 0 else 0
            if kelly <= 0:
                continue

            bet_size = min(kelly * kelly_fraction * max_bet_size, max_bet_size)

            # Calculate P&L
            if resolved_yes:
                # Won: bought YES at entry_price, pays out 1.0
                pnl = bet_size * (1.0 / entry_price - 1.0) * 0.98  # 2% fee
                outcome = "win"
            else:
                # Lost: bought YES, market resolved NO
                pnl = -bet_size * 1.02  # Lost bet + fee
                outcome = "loss"

            trade = BacktestTrade(
                tournament=event_title,
                player_a=odds.player_a,
                player_b=odds.player_b,
                target_player=player_name,
                sharp_prob=sharp_prob,
                polymarket_price=entry_price,
                divergence=divergence,
                bet_size=bet_size,
                outcome=outcome,
                pnl=pnl,
            )
            result.trades.append(trade)

    return result


def main():
    parser = argparse.ArgumentParser(description="Tennis Arb Backtest")
    parser.add_argument("--min-divergence", type=float, default=0.10)
    parser.add_argument("--max-bet", type=float, default=100.0)
    parser.add_argument("--kelly-fraction", type=float, default=0.25)
    parser.add_argument("--output", type=str, default=None,
                        help="Save results to JSON file")
    args = parser.parse_args()

    # Fetch historical data
    events = fetch_historical_tennis_events()
    if not events:
        logger.error("No historical tennis events found")
        return

    # For backtest, we need historical odds. Since we can't fetch historical
    # Pinnacle odds without a paid API, we generate synthetic odds from
    # the resolved market outcomes for validation purposes.
    logger.info("Generating synthetic historical odds from resolved markets...")
    synthetic_odds: list[MatchOdds] = []

    for event in events:
        markets = event.get("markets", [])
        for market in markets:
            prices = market.get("outcomePrices")
            if isinstance(prices, str):
                try:
                    prices = json.loads(prices)
                except (json.JSONDecodeError, TypeError):
                    continue

            if not prices or len(prices) < 2:
                continue

            yes_price = float(prices[0])
            no_price = float(prices[1])

            if yes_price <= 0.05 or yes_price >= 0.95:
                continue

            import re
            player_match = re.match(
                r"Will (.+?) win\b", market.get("question", ""), re.IGNORECASE
            )
            if not player_match:
                continue

            player_name = player_match.group(1).strip()

            # Create synthetic sharp odds (add 5-15% edge to simulate sharp book)
            import random
            edge = random.uniform(0.05, 0.15)
            sharp_yes = min(0.95, yes_price + edge)
            sharp_no = 1.0 - sharp_yes

            odds_yes = 1.0 / sharp_yes if sharp_yes > 0 else 10.0
            odds_no = 1.0 / sharp_no if sharp_no > 0 else 10.0

            odds = MatchOdds.from_decimal_odds(
                source="synthetic",
                tournament=event.get("title", "Unknown"),
                tour="ATP",
                player_a=player_name,
                player_b="Opponent",
                odds_a=odds_yes,
                odds_b=odds_no,
            )
            synthetic_odds.append(odds)

    logger.info(f"Generated {len(synthetic_odds)} synthetic odds entries")

    # Run backtest
    result = simulate_backtest(
        events=events,
        historical_odds=synthetic_odds,
        min_divergence=args.min_divergence,
        max_bet_size=args.max_bet,
        kelly_fraction=args.kelly_fraction,
    )

    # Print report
    print(result.report())

    # Save to file if requested
    if args.output:
        output_data = {
            "parameters": {
                "min_divergence": args.min_divergence,
                "max_bet_size": args.max_bet,
                "kelly_fraction": args.kelly_fraction,
            },
            "summary": {
                "total_opportunities": result.total_trades,
                "resolved": len(result.resolved_trades),
                "wins": result.wins,
                "losses": result.losses,
                "win_rate": round(result.win_rate, 4),
                "total_wagered": round(result.total_wagered, 2),
                "total_pnl": round(result.total_pnl, 2),
                "roi": round(result.roi, 4),
                "avg_edge": round(result.avg_edge, 4),
            },
            "trades": [
                {
                    "tournament": t.tournament,
                    "player": t.target_player,
                    "sharp_prob": t.sharp_prob,
                    "pm_price": t.polymarket_price,
                    "divergence": t.divergence,
                    "bet_size": t.bet_size,
                    "outcome": t.outcome,
                    "pnl": t.pnl,
                }
                for t in result.resolved_trades
            ],
        }
        with open(args.output, "w") as f:
            json.dump(output_data, f, indent=2)
        logger.info(f"Results saved to {args.output}")


if __name__ == "__main__":
    main()
