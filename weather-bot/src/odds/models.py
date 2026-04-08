"""Pydantic models for odds data and comparisons."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class MatchOdds(BaseModel):
    """Sharp sportsbook odds for a tennis match."""

    source: str = Field(description="Odds provider name (e.g. 'pinnacle', 'oddspapi')")
    sport: str = Field(default="tennis", description="Sport type")
    tournament: str = Field(description="Tournament name (e.g. 'ATP Monte-Carlo')")
    tour: str = Field(description="Tour: ATP or WTA")
    player_a: str = Field(description="Player A name")
    player_b: str = Field(description="Player B name")
    odds_a: float = Field(description="Decimal odds for player A")
    odds_b: float = Field(description="Decimal odds for player B")
    implied_prob_a: float = Field(description="Implied probability for player A (no-vig)")
    implied_prob_b: float = Field(description="Implied probability for player B (no-vig)")
    match_time: Optional[datetime] = Field(default=None, description="Scheduled match start")
    last_updated: Optional[datetime] = Field(default=None, description="When odds were fetched")

    @field_validator("implied_prob_a", "implied_prob_b", mode="before")
    @classmethod
    def clamp_probability(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

    @classmethod
    def from_decimal_odds(
        cls,
        source: str,
        tournament: str,
        tour: str,
        player_a: str,
        player_b: str,
        odds_a: float,
        odds_b: float,
        match_time: Optional[datetime] = None,
    ) -> MatchOdds:
        """Create from raw decimal odds, removing vig via multiplicative method."""
        raw_a = 1.0 / odds_a if odds_a > 0 else 0.5
        raw_b = 1.0 / odds_b if odds_b > 0 else 0.5
        total = raw_a + raw_b

        # Remove vig: normalize to sum to 1.0
        implied_a = raw_a / total if total > 0 else 0.5
        implied_b = raw_b / total if total > 0 else 0.5

        return cls(
            source=source,
            tournament=tournament,
            tour=tour,
            player_a=player_a,
            player_b=player_b,
            odds_a=odds_a,
            odds_b=odds_b,
            implied_prob_a=round(implied_a, 4),
            implied_prob_b=round(implied_b, 4),
            match_time=match_time,
            last_updated=datetime.utcnow(),
        )


class OddsComparison(BaseModel):
    """Comparison between sharp odds and Polymarket price for one side of a match."""

    match_odds: MatchOdds
    polymarket_condition_id: str = Field(description="Polymarket market condition ID")
    polymarket_token_id: str = Field(description="CLOB token ID for YES outcome")
    polymarket_market_id: str = Field(description="Polymarket market slug or ID")
    polymarket_question: str = Field(description="Market question text")
    polymarket_player: str = Field(description="Which player this market is for")
    polymarket_price: float = Field(description="Current YES price on Polymarket (0-1)")
    sharp_prob: float = Field(description="Sharp implied probability for this player")
    divergence: float = Field(description="sharp_prob - polymarket_price")
    polymarket_volume: float = Field(default=0.0, description="Market volume in USD")
    polymarket_liquidity: float = Field(default=0.0, description="Market liquidity in USD")

    @property
    def has_edge(self) -> bool:
        return self.divergence > 0

    @property
    def side(self) -> str:
        """BUY YES if sharp thinks it's underpriced."""
        return "BUY YES" if self.divergence > 0 else "SKIP"
