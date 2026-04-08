"""Tests for tiered risk manager (1a/1b/1c tiers)."""

import math
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from src.models import DetectedTrade, TieredCopyDecision
from src.copy_trading.tiered_risk_manager import (
    TierExposure,
    _evaluate_tiered_trade_with_state,
)
from src.copy_trading.strategy_config import TierConfig


def _make_trade(
    side: str = "BUY",
    size: float = 10000.0,
    price: float = 0.50,
    market: str = "test-market",
    timestamp: str | None = None,
) -> DetectedTrade:
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()
    return DetectedTrade(
        id="trade-1",
        trader_address="0x" + "a" * 40,
        timestamp=timestamp,
        market=market,
        side=side,
        size=size,
        price=price,
    )


def _tier_cfg(**overrides) -> TierConfig:
    defaults = dict(
        tier="1a",
        enabled=True,
        wallets=[],
        copy_percentage=10.0,
        max_bet=50.0,
        min_bet=5.0,
        max_total_exposure=500.0,
        max_price=0.90,
        min_price=0.10,
        min_trader_bet=1000.0,
        hold_to_settlement=True,
        alert_only=False,
    )
    defaults.update(overrides)
    return TierConfig(**defaults)


def _fresh_exposure() -> TierExposure:
    return TierExposure(open_total=0.0, daily_date="2026-04-08", daily_volume=0.0)


class TestRawSizeCalculation:
    @patch("src.copy_trading.tiered_risk_manager.CONFIG")
    def test_raw_size_percentage(self, mock_cfg):
        mock_cfg.max_trade_age_hours = 1.0
        cfg = _tier_cfg(copy_percentage=10.0, min_bet=5.0, max_bet=50.0)
        trade = _make_trade(size=10000.0, price=0.50)
        result = _evaluate_tiered_trade_with_state(trade, "1a", _fresh_exposure(), cfg)
        # raw_size = 10000 * 10/100 = 1000, capped to max_bet 50
        assert result.should_copy is True
        assert result.copy_size == 50.0


class TestFloorsToMinBet:
    @patch("src.copy_trading.tiered_risk_manager.CONFIG")
    def test_floors_small_trade(self, mock_cfg):
        mock_cfg.max_trade_age_hours = 1.0
        cfg = _tier_cfg(copy_percentage=1.0, min_bet=5.0, max_bet=50.0, min_trader_bet=0)
        trade = _make_trade(size=100.0, price=0.50)  # raw = 100*1% = 1.0
        result = _evaluate_tiered_trade_with_state(trade, "1a", _fresh_exposure(), cfg)
        assert result.should_copy is True
        assert result.copy_size == 5.0  # Floored to min_bet


class TestCapsToMaxBet:
    @patch("src.copy_trading.tiered_risk_manager.CONFIG")
    def test_caps_large_trade(self, mock_cfg):
        mock_cfg.max_trade_age_hours = 1.0
        cfg = _tier_cfg(copy_percentage=50.0, min_bet=5.0, max_bet=25.0, min_trader_bet=0)
        trade = _make_trade(size=10000.0, price=0.50)  # raw = 5000
        result = _evaluate_tiered_trade_with_state(trade, "1b", _fresh_exposure(), cfg)
        assert result.should_copy is True
        assert result.copy_size == 25.0


class TestExposureLimit:
    @patch("src.copy_trading.tiered_risk_manager.CONFIG")
    def test_respects_exposure(self, mock_cfg):
        mock_cfg.max_trade_age_hours = 1.0
        cfg = _tier_cfg(copy_percentage=10.0, min_bet=5.0, max_bet=50.0, max_total_exposure=100.0, min_trader_bet=0)
        exposure = TierExposure(open_total=85.0)
        trade = _make_trade(size=10000.0, price=0.50)  # raw=1000, cap=50, but remaining=15
        result = _evaluate_tiered_trade_with_state(trade, "1a", exposure, cfg)
        assert result.should_copy is True
        assert result.copy_size == 15.0


class TestSkipWhenRemainingBelowMin:
    @patch("src.copy_trading.tiered_risk_manager.CONFIG")
    def test_skip_below_min(self, mock_cfg):
        mock_cfg.max_trade_age_hours = 1.0
        cfg = _tier_cfg(min_bet=5.0, max_bet=50.0, max_total_exposure=100.0, min_trader_bet=0)
        exposure = TierExposure(open_total=97.0)  # remaining=3 < min_bet=5
        trade = _make_trade(size=10000.0, price=0.50)
        result = _evaluate_tiered_trade_with_state(trade, "1a", exposure, cfg)
        assert result.should_copy is False
        assert "exposure full" in result.reason.lower()


class TestRejectsBadPricesAndOldTrades:
    @patch("src.copy_trading.tiered_risk_manager.CONFIG")
    def test_nan_price(self, mock_cfg):
        mock_cfg.max_trade_age_hours = 1.0
        cfg = _tier_cfg()
        trade = _make_trade(price=float("nan"))
        result = _evaluate_tiered_trade_with_state(trade, "1a", _fresh_exposure(), cfg)
        assert result.should_copy is False
        assert "NaN" in result.reason

    @patch("src.copy_trading.tiered_risk_manager.CONFIG")
    def test_old_trade(self, mock_cfg):
        mock_cfg.max_trade_age_hours = 1.0
        cfg = _tier_cfg()
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        trade = _make_trade(timestamp=old_ts)
        result = _evaluate_tiered_trade_with_state(trade, "1a", _fresh_exposure(), cfg)
        assert result.should_copy is False
        assert "old" in result.reason.lower()

    @patch("src.copy_trading.tiered_risk_manager.CONFIG")
    def test_price_too_high(self, mock_cfg):
        mock_cfg.max_trade_age_hours = 1.0
        cfg = _tier_cfg(max_price=0.85)
        trade = _make_trade(price=0.90)
        result = _evaluate_tiered_trade_with_state(trade, "1a", _fresh_exposure(), cfg)
        assert result.should_copy is False

    @patch("src.copy_trading.tiered_risk_manager.CONFIG")
    def test_price_too_low(self, mock_cfg):
        mock_cfg.max_trade_age_hours = 1.0
        cfg = _tier_cfg(min_price=0.10)
        trade = _make_trade(price=0.05)
        result = _evaluate_tiered_trade_with_state(trade, "1a", _fresh_exposure(), cfg)
        assert result.should_copy is False


class TestAlertOnlyMode:
    @patch("src.copy_trading.tiered_risk_manager.CONFIG")
    def test_alert_only_does_not_copy(self, mock_cfg):
        mock_cfg.max_trade_age_hours = 1.0
        cfg = _tier_cfg(alert_only=True, min_trader_bet=0)
        trade = _make_trade(size=10000.0, price=0.50)
        result = _evaluate_tiered_trade_with_state(trade, "1c", _fresh_exposure(), cfg)
        assert result.should_copy is False
        assert result.alert_only is True
        assert result.copy_size > 0  # Size calculated but not executed
        assert "alert-only" in result.reason.lower()
