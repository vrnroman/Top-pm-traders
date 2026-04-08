"""Tests for local inventory tracking."""

from unittest.mock import patch

import pytest

from src.copy_trading.inventory import (
    weighted_avg_price,
    record_buy,
    record_sell,
    get_inventory_summary,
    get_position,
    has_position,
    _positions,
)


@pytest.fixture(autouse=True)
def clear_positions():
    """Clear inventory state before each test."""
    _positions.clear()
    yield
    _positions.clear()


class TestWeightedAvgPrice:
    def test_initial_buy(self):
        result = weighted_avg_price(0, 0, 10, 0.50)
        assert result == 0.50

    def test_average_two_buys(self):
        result = weighted_avg_price(10, 0.40, 10, 0.60)
        assert abs(result - 0.50) < 0.0001

    def test_unequal_sizes(self):
        # 100 shares @ $0.40 + 50 shares @ $0.70
        # total cost = 40 + 35 = 75, total shares = 150
        result = weighted_avg_price(100, 0.40, 50, 0.70)
        assert abs(result - 0.50) < 0.0001

    def test_zero_total_shares(self):
        result = weighted_avg_price(0, 0.50, 0, 0.60)
        assert result == 0.0


class TestRecordBuy:
    @patch("src.copy_trading.inventory._save_inventory")
    def test_creates_new_position(self, mock_save):
        record_buy("tok_1", shares=10.0, price=0.50, market_key="mkt_1", market="Test")
        pos = get_position("tok_1")
        assert pos is not None
        assert pos["shares"] == 10.0
        assert pos["avg_price"] == 0.50
        assert pos["market"] == "Test"

    @patch("src.copy_trading.inventory._save_inventory")
    def test_updates_existing_position(self, mock_save):
        record_buy("tok_1", shares=10.0, price=0.40)
        record_buy("tok_1", shares=10.0, price=0.60)
        pos = get_position("tok_1")
        assert pos is not None
        assert pos["shares"] == 20.0
        assert abs(pos["avg_price"] - 0.50) < 0.0001


class TestRecordSell:
    @patch("src.copy_trading.inventory._save_inventory")
    def test_reduces_position(self, mock_save):
        record_buy("tok_1", shares=10.0, price=0.50)
        record_sell("tok_1", shares=3.0)
        pos = get_position("tok_1")
        assert pos is not None
        assert pos["shares"] == 7.0

    @patch("src.copy_trading.inventory._save_inventory")
    def test_removes_position_at_zero(self, mock_save):
        record_buy("tok_1", shares=10.0, price=0.50)
        record_sell("tok_1", shares=10.0)
        assert has_position("tok_1") is False
        assert get_position("tok_1") is None

    @patch("src.copy_trading.inventory._save_inventory")
    def test_sell_unknown_token_no_crash(self, mock_save):
        # Should not raise
        record_sell("unknown_tok", shares=5.0)


class TestGetInventorySummary:
    @patch("src.copy_trading.inventory._save_inventory")
    def test_summary_with_positions(self, mock_save):
        record_buy("tok_1", shares=10.0, price=0.40, market="Market A")
        record_buy("tok_2", shares=20.0, price=0.60, market="Market B")

        summary = get_inventory_summary()
        assert summary["total_positions"] == 2
        assert summary["total_shares"] == 30.0
        # cost basis: 10*0.40 + 20*0.60 = 4.0 + 12.0 = 16.0
        assert summary["total_cost_basis_usd"] == 16.0
        assert "tok_1" in summary["positions"]
        assert "tok_2" in summary["positions"]

    def test_summary_empty(self):
        summary = get_inventory_summary()
        assert summary["total_positions"] == 0
        assert summary["total_shares"] == 0.0
