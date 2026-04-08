"""Tests for market price snapshot and drift calculation."""

import time
from unittest.mock import MagicMock, patch

import pytest

from src.models import MarketSnapshot
from src.copy_trading.market_price import (
    fetch_market_snapshot,
    compute_drift_bps,
    _snapshot_cache,
)


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear snapshot cache before each test."""
    _snapshot_cache.clear()
    yield
    _snapshot_cache.clear()


class TestFetchMarketSnapshot:
    def test_valid_snapshot(self):
        mock_client = MagicMock()
        mock_client.get_price.side_effect = ["0.45", "0.55"]  # bid, ask

        snapshot = fetch_market_snapshot(mock_client, "tok_1")
        assert snapshot is not None
        assert snapshot.best_bid == 0.45
        assert snapshot.best_ask == 0.55
        assert abs(snapshot.midpoint - 0.50) < 0.001
        assert snapshot.spread == pytest.approx(0.10, abs=0.001)
        assert snapshot.spread_bps > 0

    def test_returns_none_on_invalid_data(self):
        mock_client = MagicMock()
        mock_client.get_price.side_effect = ["0.0", "0.0"]  # Invalid prices

        snapshot = fetch_market_snapshot(mock_client, "tok_2")
        assert snapshot is None

    def test_returns_none_on_crossed_book(self):
        mock_client = MagicMock()
        mock_client.get_price.side_effect = ["0.60", "0.40"]  # bid > ask (crossed)

        snapshot = fetch_market_snapshot(mock_client, "tok_3")
        assert snapshot is None

    def test_returns_none_on_exception(self):
        mock_client = MagicMock()
        mock_client.get_price.side_effect = Exception("API down")

        snapshot = fetch_market_snapshot(mock_client, "tok_4")
        assert snapshot is None


class TestComputeDriftBps:
    def test_buy_drift(self):
        snapshot = MarketSnapshot(
            best_bid=0.45,
            best_ask=0.55,
            midpoint=0.50,
            spread=0.10,
            spread_bps=2000,
            fetched_at=time.time(),
        )
        # BUY: drift = (0.55 - 0.50) / 0.50 * 10000 = 1000 bps
        drift = compute_drift_bps(0.50, snapshot, "BUY")
        assert drift == 1000

    def test_sell_drift(self):
        snapshot = MarketSnapshot(
            best_bid=0.45,
            best_ask=0.55,
            midpoint=0.50,
            spread=0.10,
            spread_bps=2000,
            fetched_at=time.time(),
        )
        # SELL: drift = (0.50 - 0.45) / 0.50 * 10000 = 1000 bps
        drift = compute_drift_bps(0.50, snapshot, "SELL")
        assert drift == 1000

    def test_zero_trader_price(self):
        snapshot = MarketSnapshot(
            best_bid=0.45,
            best_ask=0.55,
            midpoint=0.50,
            spread=0.10,
            spread_bps=2000,
            fetched_at=time.time(),
        )
        drift = compute_drift_bps(0.0, snapshot, "BUY")
        assert drift == 0

    def test_no_drift_when_at_market(self):
        snapshot = MarketSnapshot(
            best_bid=0.49,
            best_ask=0.50,
            midpoint=0.495,
            spread=0.01,
            spread_bps=202,
            fetched_at=time.time(),
        )
        # BUY at 0.50, ask is 0.50 -> drift = 0
        drift = compute_drift_bps(0.50, snapshot, "BUY")
        assert drift == 0
