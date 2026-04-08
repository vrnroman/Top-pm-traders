"""Tests for odds provider module."""

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.odds.models import MatchOdds, OddsComparison
from src.odds.oddspapi import OddsPapiProvider, _extract_h2h_odds
from src.odds.scraper import OddsScraperProvider


# ── MatchOdds model tests ────────────────────────────────────────────────


class TestMatchOdds:
    def test_from_decimal_odds_removes_vig(self):
        """Implied probabilities should sum to 1.0 after vig removal."""
        odds = MatchOdds.from_decimal_odds(
            source="pinnacle",
            tournament="ATP Monte-Carlo",
            tour="ATP",
            player_a="Sinner",
            player_b="Rublev",
            odds_a=1.40,  # ~71.4% raw
            odds_b=3.00,  # ~33.3% raw (total ~104.8% = vig)
        )
        assert abs(odds.implied_prob_a + odds.implied_prob_b - 1.0) < 0.001
        assert odds.implied_prob_a > 0.5  # Sinner is favorite
        assert odds.implied_prob_b < 0.5

    def test_from_decimal_odds_even_match(self):
        odds = MatchOdds.from_decimal_odds(
            source="test",
            tournament="Test",
            tour="ATP",
            player_a="A",
            player_b="B",
            odds_a=2.0,
            odds_b=2.0,
        )
        assert abs(odds.implied_prob_a - 0.5) < 0.001
        assert abs(odds.implied_prob_b - 0.5) < 0.001

    def test_from_decimal_odds_heavy_favorite(self):
        odds = MatchOdds.from_decimal_odds(
            source="test",
            tournament="Test",
            tour="ATP",
            player_a="Djokovic",
            player_b="Qualifier",
            odds_a=1.05,
            odds_b=12.0,
        )
        assert odds.implied_prob_a > 0.90
        assert odds.implied_prob_b < 0.10

    def test_clamp_probability(self):
        """Probabilities should be clamped to [0, 1]."""
        odds = MatchOdds(
            source="test",
            tournament="Test",
            tour="ATP",
            player_a="A",
            player_b="B",
            odds_a=2.0,
            odds_b=2.0,
            implied_prob_a=1.5,  # Should be clamped to 1.0
            implied_prob_b=-0.3,  # Should be clamped to 0.0
        )
        assert odds.implied_prob_a == 1.0
        assert odds.implied_prob_b == 0.0

    def test_match_time_optional(self):
        odds = MatchOdds.from_decimal_odds(
            source="test", tournament="Test", tour="ATP",
            player_a="A", player_b="B", odds_a=2.0, odds_b=2.0,
        )
        assert odds.match_time is None

    def test_last_updated_set(self):
        odds = MatchOdds.from_decimal_odds(
            source="test", tournament="Test", tour="ATP",
            player_a="A", player_b="B", odds_a=2.0, odds_b=2.0,
        )
        assert odds.last_updated is not None


# ── OddsComparison model tests ──────────────────────────────────────────


class TestOddsComparison:
    def _make_comparison(self, sharp_prob: float, pm_price: float) -> OddsComparison:
        odds = MatchOdds.from_decimal_odds(
            source="pinnacle", tournament="Test", tour="ATP",
            player_a="A", player_b="B", odds_a=2.0, odds_b=2.0,
        )
        return OddsComparison(
            match_odds=odds,
            polymarket_condition_id="cond_123",
            polymarket_token_id="tok_123",
            polymarket_market_id="mkt_123",
            polymarket_question="Will A win?",
            polymarket_player="A",
            polymarket_price=pm_price,
            sharp_prob=sharp_prob,
            divergence=sharp_prob - pm_price,
        )

    def test_has_edge_positive(self):
        comp = self._make_comparison(0.72, 0.58)
        assert comp.has_edge is True
        assert comp.side == "BUY YES"

    def test_has_edge_negative(self):
        comp = self._make_comparison(0.50, 0.60)
        assert comp.has_edge is False
        assert comp.side == "SKIP"

    def test_divergence_calculation(self):
        comp = self._make_comparison(0.72, 0.58)
        assert abs(comp.divergence - 0.14) < 0.001


# ── OddsPapi provider tests ─────────────────────────────────────────────


class TestOddsPapiProvider:
    def test_no_api_key_returns_empty(self):
        provider = OddsPapiProvider(api_key="")
        result = provider.fetch_tennis_odds()
        assert result == []

    def test_name(self):
        provider = OddsPapiProvider(api_key="test")
        assert provider.name == "oddspapi"

    @patch("src.odds.oddspapi.requests.get")
    def test_fetch_parses_response(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.ok = True
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = [
            {
                "sport_title": "ATP Monte-Carlo Masters",
                "home_team": "Jannik Sinner",
                "away_team": "Andrey Rublev",
                "commence_time": "2026-04-10T12:00:00Z",
                "bookmakers": [
                    {
                        "key": "pinnacle",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Jannik Sinner", "price": 1.40},
                                    {"name": "Andrey Rublev", "price": 3.00},
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
        mock_get.return_value = mock_response

        provider = OddsPapiProvider(api_key="test_key")
        results = provider.fetch_tennis_odds(tours=["ATP"])

        assert len(results) == 1
        assert results[0].player_a == "Jannik Sinner"
        assert results[0].player_b == "Andrey Rublev"
        assert results[0].source == "pinnacle"
        assert results[0].implied_prob_a > results[0].implied_prob_b

    @patch("src.odds.oddspapi.requests.get")
    def test_fetch_handles_empty_response(self, mock_get):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = []
        mock_get.return_value = mock_response

        provider = OddsPapiProvider(api_key="test_key")
        results = provider.fetch_tennis_odds(tours=["ATP"])
        assert results == []

    @patch("src.odds.oddspapi.requests.get")
    def test_fetch_handles_api_error(self, mock_get):
        mock_get.side_effect = Exception("API down")

        provider = OddsPapiProvider(api_key="test_key")
        results = provider.fetch_tennis_odds(tours=["ATP"])
        assert results == []


# ── extract_h2h_odds tests ──────────────────────────────────────────────


class TestExtractH2HOdds:
    def test_extracts_correct_odds(self):
        bookmaker = {
            "key": "pinnacle",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Player A", "price": 1.50},
                        {"name": "Player B", "price": 2.60},
                    ],
                }
            ],
        }
        a, b = _extract_h2h_odds(bookmaker, "Player A", "Player B")
        assert a == 1.50
        assert b == 2.60

    def test_returns_none_for_missing_market(self):
        bookmaker = {"key": "pinnacle", "markets": []}
        a, b = _extract_h2h_odds(bookmaker, "A", "B")
        assert a is None
        assert b is None

    def test_returns_none_for_invalid_odds(self):
        bookmaker = {
            "key": "test",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "A", "price": 0.0},
                        {"name": "B", "price": 0.0},
                    ],
                }
            ],
        }
        a, b = _extract_h2h_odds(bookmaker, "A", "B")
        assert a is None
        assert b is None


# ── Scraper provider tests ──────────────────────────────────────────────


class TestOddsScraperProvider:
    def test_name(self):
        provider = OddsScraperProvider()
        assert provider.name == "scraper"

    @patch("src.odds.scraper.requests.get")
    def test_handles_http_error(self, mock_get):
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.status_code = 403
        mock_get.return_value = mock_response

        provider = OddsScraperProvider()
        results = provider.fetch_tennis_odds(tours=["ATP"])
        assert results == []

    @patch("src.odds.scraper.requests.get")
    def test_handles_network_error(self, mock_get):
        mock_get.side_effect = Exception("Network error")

        provider = OddsScraperProvider()
        results = provider.fetch_tennis_odds(tours=["ATP"])
        assert results == []
