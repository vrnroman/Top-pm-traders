"""Tests for Strategy #3: Tennis Odds Arbitrage."""

import json
import os
import tempfile
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.odds.models import MatchOdds, OddsComparison
from src.strategies.tennis_arb import (
    TennisArbStrategy,
    _extract_player_from_question,
    _match_player_to_odds,
    _normalize_name,
)


# ── Player name extraction tests ────────────────────────────────────────


class TestExtractPlayerFromQuestion:
    def test_will_pattern(self):
        assert _extract_player_from_question(
            "Will Jannik Sinner win the 2026 ATP Monte-Carlo Masters?"
        ) == "Jannik Sinner"

    def test_to_win_pattern(self):
        assert _extract_player_from_question(
            "Jannik Sinner to win ATP Monte Carlo"
        ) == "Jannik Sinner"

    def test_vs_pattern(self):
        assert _extract_player_from_question(
            "Sinner vs Rublev: Who will win?"
        ) == "Sinner"

    def test_empty_question(self):
        assert _extract_player_from_question("") == ""

    def test_unrecognized_format(self):
        assert _extract_player_from_question("Random market question") == ""


# ── Name normalization tests ────────────────────────────────────────────


class TestNormalizeName:
    def test_lowercase(self):
        assert _normalize_name("Jannik SINNER") == "jannik sinner"

    def test_strips_whitespace(self):
        assert _normalize_name("  Sinner  ") == "sinner"

    def test_removes_jr_suffix(self):
        assert _normalize_name("John Smith Jr.") == "john smith"

    def test_removes_special_chars(self):
        assert _normalize_name("O'Brien-Smith") == "obriensmith"


# ── Player matching tests ───────────────────────────────────────────────


class TestMatchPlayerToOdds:
    def test_exact_match_player_a(self):
        side, prob = _match_player_to_odds(
            "Jannik Sinner", "Jannik Sinner", "Andrey Rublev", 0.72, 0.28
        )
        assert side == "A"
        assert prob == 0.72

    def test_exact_match_player_b(self):
        side, prob = _match_player_to_odds(
            "Andrey Rublev", "Jannik Sinner", "Andrey Rublev", 0.72, 0.28
        )
        assert side == "B"
        assert prob == 0.28

    def test_last_name_match(self):
        side, prob = _match_player_to_odds(
            "Sinner", "Jannik Sinner", "Andrey Rublev", 0.72, 0.28
        )
        assert side == "A"
        assert prob == 0.72

    def test_substring_match(self):
        side, prob = _match_player_to_odds(
            "J. Sinner", "Jannik Sinner", "Andrey Rublev", 0.72, 0.28
        )
        # Should match via last name
        assert side == "A"
        assert prob == 0.72

    def test_no_match(self):
        side, prob = _match_player_to_odds(
            "Carlos Alcaraz", "Jannik Sinner", "Andrey Rublev", 0.72, 0.28
        )
        assert side is None
        assert prob == 0.0

    def test_case_insensitive(self):
        side, prob = _match_player_to_odds(
            "jannik sinner", "Jannik Sinner", "Andrey Rublev", 0.72, 0.28
        )
        assert side == "A"


# ── Kelly sizing tests ──────────────────────────────────────────────────


class TestKellySizing:
    def setup_method(self):
        self.strategy = TennisArbStrategy(
            odds_provider="oddspapi",
            oddspapi_api_key="test",
            max_bet_size=100.0,
            kelly_fraction=0.25,
            preview_mode=True,
        )

    def test_positive_edge_gives_positive_size(self):
        size = self.strategy._calculate_bet_size(sharp_prob=0.72, market_price=0.58)
        assert size > 0
        assert size <= 100.0

    def test_no_edge_gives_zero(self):
        size = self.strategy._calculate_bet_size(sharp_prob=0.50, market_price=0.55)
        assert size == 0.0

    def test_capped_at_max(self):
        # Very large edge should still be capped
        size = self.strategy._calculate_bet_size(sharp_prob=0.95, market_price=0.20)
        assert size <= 100.0

    def test_zero_price_gives_zero(self):
        size = self.strategy._calculate_bet_size(sharp_prob=0.72, market_price=0.0)
        assert size == 0.0

    def test_edge_price_at_one(self):
        size = self.strategy._calculate_bet_size(sharp_prob=0.72, market_price=1.0)
        assert size == 0.0


# ── Signal saving tests ─────────────────────────────────────────────────


class TestSignalSaving:
    def test_saves_to_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            strategy = TennisArbStrategy(
                odds_provider="oddspapi",
                oddspapi_api_key="test",
                preview_mode=True,
                data_dir=tmpdir,
            )

            signals = [
                {
                    "strategy": "tennis_arb",
                    "player_a": "Sinner",
                    "player_b": "Rublev",
                    "divergence": 0.14,
                    "timestamp": "2026-04-08T15:00:00+08:00",
                }
            ]

            strategy._save_signals(signals)

            history_path = os.path.join(tmpdir, "tennis_trades.jsonl")
            assert os.path.exists(history_path)

            with open(history_path) as f:
                lines = f.readlines()
            assert len(lines) == 1
            data = json.loads(lines[0])
            assert data["strategy"] == "tennis_arb"
            assert data["divergence"] == 0.14

    def test_appends_to_existing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            strategy = TennisArbStrategy(
                odds_provider="oddspapi",
                oddspapi_api_key="test",
                preview_mode=True,
                data_dir=tmpdir,
            )

            strategy._save_signals([{"signal": 1}])
            strategy._save_signals([{"signal": 2}])

            history_path = os.path.join(tmpdir, "tennis_trades.jsonl")
            with open(history_path) as f:
                lines = f.readlines()
            assert len(lines) == 2

    def test_no_data_dir_skips(self):
        strategy = TennisArbStrategy(
            odds_provider="oddspapi",
            oddspapi_api_key="test",
            preview_mode=True,
            data_dir="",
        )
        # Should not raise
        strategy._save_signals([{"signal": 1}])


# ── Full scan integration test (mocked) ─────────────────────────────────


class TestScanIntegration:
    @patch.object(TennisArbStrategy, "_fetch_polymarket_tennis_markets")
    @patch("src.strategies.tennis_arb.OddsPapiProvider.fetch_tennis_odds")
    def test_scan_finds_divergence(self, mock_odds, mock_pm):
        mock_odds.return_value = [
            MatchOdds.from_decimal_odds(
                source="pinnacle",
                tournament="ATP Monte-Carlo",
                tour="ATP",
                player_a="Jannik Sinner",
                player_b="Andrey Rublev",
                odds_a=1.40,
                odds_b=3.00,
            )
        ]

        mock_pm.return_value = [
            {
                "event_title": "ATP Monte-Carlo: Sinner vs Rublev",
                "question": "Will Jannik Sinner win the ATP Monte-Carlo Masters?",
                "player": "Jannik Sinner",
                "yes_price": 0.58,
                "volume": 200000,
                "liquidity": 50000,
                "market_id": "mkt_123",
                "condition_id": "cond_123",
                "token_id_yes": "tok_123",
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            strategy = TennisArbStrategy(
                odds_provider="oddspapi",
                oddspapi_api_key="test",
                min_divergence=0.10,
                max_bet_size=100,
                kelly_fraction=0.25,
                preview_mode=True,
                data_dir=tmpdir,
            )

            signals = strategy.scan()

        assert len(signals) >= 1
        sig = signals[0]
        assert sig["strategy"] == "tennis_arb"
        assert sig["player_a"] == "Jannik Sinner"
        assert sig["polymarket_price"] == 0.58
        assert sig["divergence"] > 0.10
        assert sig["side"] == "BUY YES"
        assert sig["bet_size"] > 0
        assert sig["preview"] is True

    @patch.object(TennisArbStrategy, "_fetch_polymarket_tennis_markets")
    @patch("src.strategies.tennis_arb.OddsPapiProvider.fetch_tennis_odds")
    def test_scan_no_signals_below_threshold(self, mock_odds, mock_pm):
        mock_odds.return_value = [
            MatchOdds.from_decimal_odds(
                source="pinnacle",
                tournament="ATP Test",
                tour="ATP",
                player_a="A",
                player_b="B",
                odds_a=2.0,
                odds_b=2.0,
            )
        ]

        mock_pm.return_value = [
            {
                "event_title": "Test match",
                "question": "Will A win the test?",
                "player": "A",
                "yes_price": 0.48,  # Close to sharp 0.50, divergence ~0.02
                "volume": 100000,
                "liquidity": 20000,
                "market_id": "mkt_1",
                "condition_id": "cond_1",
                "token_id_yes": "tok_1",
            }
        ]

        strategy = TennisArbStrategy(
            odds_provider="oddspapi",
            oddspapi_api_key="test",
            min_divergence=0.10,
            preview_mode=True,
        )

        signals = strategy.scan()
        assert len(signals) == 0

    @patch.object(TennisArbStrategy, "_fetch_polymarket_tennis_markets")
    @patch("src.strategies.tennis_arb.OddsPapiProvider.fetch_tennis_odds")
    def test_scan_empty_odds(self, mock_odds, mock_pm):
        mock_odds.return_value = []
        mock_pm.return_value = []

        strategy = TennisArbStrategy(
            odds_provider="oddspapi",
            oddspapi_api_key="test",
            preview_mode=True,
        )

        signals = strategy.scan()
        assert signals == []
