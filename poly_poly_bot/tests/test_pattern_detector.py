"""Tests for Strategy 1c pattern detector."""

import time
from unittest.mock import patch, AsyncMock

import pytest

from src.models import DetectedTrade
from src.copy_trading.pattern_detector import (
    is_geopolitical_market,
    _check_new_account_geo,
    _find_cluster,
    _add_recent_bet,
    _get_or_create_wallet,
    _update_wallet_activity,
    _reset_pattern_detector,
    RecentBet,
    WalletActivity,
)
from src.copy_trading.strategy_config import TIER_1C


@pytest.fixture(autouse=True)
def reset_detector():
    """Reset all module-level state before each test."""
    _reset_pattern_detector()
    yield
    _reset_pattern_detector()


def _make_trade(
    market: str = "Will Russia invade Ukraine?",
    side: str = "BUY",
    size: float = 10000.0,
    price: float = 0.50,
    trader: str = "0x" + "a" * 40,
) -> DetectedTrade:
    return DetectedTrade(
        id="trade-1",
        trader_address=trader,
        timestamp="2026-04-08T12:00:00Z",
        market=market,
        side=side,
        size=size,
        price=price,
    )


class TestIsGeopoliticalMarket:
    def test_detects_war(self):
        assert is_geopolitical_market("Will war break out in 2026?") is True

    def test_detects_invasion(self):
        assert is_geopolitical_market("Russia invasion of Ukraine") is True

    def test_detects_nuclear(self):
        assert is_geopolitical_market("Nuclear weapons usage in conflict") is True

    def test_detects_election(self):
        assert is_geopolitical_market("US Presidential Election 2028") is True

    def test_detects_sanctions(self):
        assert is_geopolitical_market("New sanctions on Iran") is True

    def test_detects_china(self):
        assert is_geopolitical_market("Will China invade Taiwan?") is True

    def test_case_insensitive(self):
        assert is_geopolitical_market("NUCLEAR MISSILE LAUNCH") is True

    def test_rejects_non_geo(self):
        assert is_geopolitical_market("Will Bitcoin hit $100k?") is False

    def test_rejects_sports(self):
        assert is_geopolitical_market("Super Bowl 2027 winner") is False

    def test_rejects_weather(self):
        assert is_geopolitical_market("NYC temperature above 80F?") is False


class TestNewAccountLargeGeoAlert:
    def test_new_account_large_geo_bet(self):
        trader = "0x" + "b" * 40
        # Create a new wallet (first_seen = now)
        wa = _get_or_create_wallet(trader)
        wa.first_seen = time.time()  # Just created

        trade = _make_trade(
            market="Will Russia use nuclear weapons?",
            size=10000.0,
            trader=trader,
        )

        alert = _check_new_account_geo(trade, wa)
        assert alert is not None
        assert alert.pattern == "new_account_geo"
        assert alert.severity == "high"

    def test_old_account_no_alert(self):
        trader = "0x" + "c" * 40
        wa = _get_or_create_wallet(trader)
        wa.first_seen = time.time() - (60 * 86400)  # 60 days old

        trade = _make_trade(
            market="Will Russia use nuclear weapons?",
            size=10000.0,
            trader=trader,
        )
        alert = _check_new_account_geo(trade, wa)
        assert alert is None

    def test_small_bet_no_alert(self):
        trader = "0x" + "d" * 40
        wa = _get_or_create_wallet(trader)

        trade = _make_trade(
            market="Will Russia use nuclear weapons?",
            size=100.0,  # Below min_first_bet threshold
            trader=trader,
        )
        alert = _check_new_account_geo(trade, wa)
        assert alert is None


class TestClusterDetection:
    def test_cluster_with_three_wallets(self):
        market = "Will Ukraine join NATO?"
        side = "BUY"

        # Add bets from 2 other wallets
        _add_recent_bet("0x" + "1" * 40, market, side, 5000.0)
        _add_recent_bet("0x" + "2" * 40, market, side, 3000.0)

        # Third wallet triggers detection
        cluster = _find_cluster(market, side, "0x" + "3" * 40)
        assert len(cluster) >= 2  # 2 other wallets found

    def test_no_cluster_with_one_wallet(self):
        market = "Will Ukraine join NATO?"
        side = "BUY"

        _add_recent_bet("0x" + "1" * 40, market, side, 5000.0)

        cluster = _find_cluster(market, side, "0x" + "3" * 40)
        assert len(cluster) < 2

    def test_different_markets_not_clustered(self):
        side = "BUY"
        _add_recent_bet("0x" + "1" * 40, "Market A", side, 5000.0)
        _add_recent_bet("0x" + "2" * 40, "Market B", side, 5000.0)

        cluster = _find_cluster("Market A", side, "0x" + "3" * 40)
        assert len(cluster) < 2

    def test_different_sides_not_clustered(self):
        market = "Same market"
        _add_recent_bet("0x" + "1" * 40, market, "BUY", 5000.0)
        _add_recent_bet("0x" + "2" * 40, market, "SELL", 5000.0)

        cluster = _find_cluster(market, "BUY", "0x" + "3" * 40)
        assert len(cluster) < 2
