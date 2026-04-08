"""Tests for legacy risk manager."""

import math
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from src.models import DetectedTrade, CopyDecision
from src.copy_trading.risk_manager import (
    RiskState,
    _evaluate_trade_with_state,
    adjust_placement,
)


def _make_trade(
    side: str = "BUY",
    size: float = 1000.0,
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


def _fresh_state() -> RiskState:
    return RiskState(daily_volume_usd=0.0, daily_volume_date="2026-04-08", daily_spend_by_market={})


class TestAcceptsValidTrades:
    @patch("src.copy_trading.risk_manager.CONFIG")
    def test_accepts_normal_buy(self, mock_cfg):
        mock_cfg.copy_strategy = "PERCENTAGE"
        mock_cfg.copy_size = 10.0
        mock_cfg.min_order_size_usd = 1.0
        mock_cfg.max_order_size_usd = 100.0
        mock_cfg.max_daily_volume_usd = 1000.0
        mock_cfg.max_trade_age_hours = 1.0
        mock_cfg.max_position_per_market_usd = 500.0

        trade = _make_trade(size=1000.0, price=0.50)
        decision = _evaluate_trade_with_state(trade, _fresh_state())
        assert decision.should_copy is True
        assert decision.copy_size == 100.0  # 10% of 1000


class TestRejectsNaNZeroSize:
    @patch("src.copy_trading.risk_manager.CONFIG")
    def test_nan_price(self, mock_cfg):
        mock_cfg.max_daily_volume_usd = 1000.0
        trade = _make_trade(price=float("nan"))
        decision = _evaluate_trade_with_state(trade, _fresh_state())
        assert decision.should_copy is False
        assert "NaN" in decision.reason

    @patch("src.copy_trading.risk_manager.CONFIG")
    def test_nan_size(self, mock_cfg):
        mock_cfg.max_daily_volume_usd = 1000.0
        trade = _make_trade(size=float("nan"))
        decision = _evaluate_trade_with_state(trade, _fresh_state())
        assert decision.should_copy is False


class TestRejectsOldTrades:
    @patch("src.copy_trading.risk_manager.CONFIG")
    def test_old_trade_rejected(self, mock_cfg):
        mock_cfg.max_daily_volume_usd = 1000.0
        mock_cfg.max_trade_age_hours = 1.0

        old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        trade = _make_trade(timestamp=old_ts)
        decision = _evaluate_trade_with_state(trade, _fresh_state())
        assert decision.should_copy is False
        assert "old" in decision.reason.lower()


class TestRejectsDailyVolumeExhausted:
    @patch("src.copy_trading.risk_manager.CONFIG")
    def test_daily_volume_exhausted(self, mock_cfg):
        mock_cfg.max_daily_volume_usd = 100.0
        state = RiskState(daily_volume_usd=100.0, daily_volume_date="2026-04-08")
        trade = _make_trade()
        decision = _evaluate_trade_with_state(trade, state)
        assert decision.should_copy is False
        assert "Daily volume" in decision.reason


class TestResetsOnNewDay:
    @patch("src.copy_trading.risk_manager.CONFIG")
    def test_new_day_resets(self, mock_cfg):
        mock_cfg.copy_strategy = "PERCENTAGE"
        mock_cfg.copy_size = 10.0
        mock_cfg.min_order_size_usd = 1.0
        mock_cfg.max_order_size_usd = 100.0
        mock_cfg.max_daily_volume_usd = 1000.0
        mock_cfg.max_trade_age_hours = 1.0
        mock_cfg.max_position_per_market_usd = 500.0

        # State from yesterday is exhausted, but function uses fresh state (today)
        state = _fresh_state()
        state.daily_volume_usd = 0.0
        trade = _make_trade(size=500.0, price=0.50)
        decision = _evaluate_trade_with_state(trade, state)
        assert decision.should_copy is True


class TestCopyStrategyModes:
    @patch("src.copy_trading.risk_manager.CONFIG")
    def test_percentage_strategy(self, mock_cfg):
        mock_cfg.copy_strategy = "PERCENTAGE"
        mock_cfg.copy_size = 10.0
        mock_cfg.min_order_size_usd = 1.0
        mock_cfg.max_order_size_usd = 200.0
        mock_cfg.max_daily_volume_usd = 5000.0
        mock_cfg.max_trade_age_hours = 1.0
        mock_cfg.max_position_per_market_usd = 500.0

        trade = _make_trade(size=500.0, price=0.50)
        decision = _evaluate_trade_with_state(trade, _fresh_state())
        assert decision.should_copy is True
        assert decision.copy_size == 50.0  # 10% of 500

    @patch("src.copy_trading.risk_manager.CONFIG")
    def test_fixed_strategy(self, mock_cfg):
        mock_cfg.copy_strategy = "FIXED"
        mock_cfg.copy_size = 25.0
        mock_cfg.min_order_size_usd = 1.0
        mock_cfg.max_order_size_usd = 100.0
        mock_cfg.max_daily_volume_usd = 5000.0
        mock_cfg.max_trade_age_hours = 1.0
        mock_cfg.max_position_per_market_usd = 500.0

        trade = _make_trade(size=5000.0, price=0.50)
        decision = _evaluate_trade_with_state(trade, _fresh_state())
        assert decision.should_copy is True
        assert decision.copy_size == 25.0


class TestMinMaxBounds:
    @patch("src.copy_trading.risk_manager.CONFIG")
    def test_below_min_floors_to_min(self, mock_cfg):
        mock_cfg.copy_strategy = "PERCENTAGE"
        mock_cfg.copy_size = 1.0  # 1% of 10 = 0.10
        mock_cfg.min_order_size_usd = 5.0
        mock_cfg.max_order_size_usd = 100.0
        mock_cfg.max_daily_volume_usd = 5000.0
        mock_cfg.max_trade_age_hours = 1.0
        mock_cfg.max_position_per_market_usd = 500.0

        trade = _make_trade(size=10.0, price=0.50)
        decision = _evaluate_trade_with_state(trade, _fresh_state())
        assert decision.should_copy is True
        assert decision.copy_size == 5.0

    @patch("src.copy_trading.risk_manager.CONFIG")
    def test_above_max_caps(self, mock_cfg):
        mock_cfg.copy_strategy = "PERCENTAGE"
        mock_cfg.copy_size = 50.0  # 50% of 1000 = 500
        mock_cfg.min_order_size_usd = 1.0
        mock_cfg.max_order_size_usd = 100.0
        mock_cfg.max_daily_volume_usd = 5000.0
        mock_cfg.max_trade_age_hours = 1.0
        mock_cfg.max_position_per_market_usd = 500.0

        trade = _make_trade(size=1000.0, price=0.50)
        decision = _evaluate_trade_with_state(trade, _fresh_state())
        assert decision.should_copy is True
        assert decision.copy_size == 100.0


class TestInvalidPrices:
    @patch("src.copy_trading.risk_manager.CONFIG")
    def test_price_too_low(self, mock_cfg):
        mock_cfg.copy_strategy = "FIXED"
        mock_cfg.copy_size = 10.0
        mock_cfg.min_order_size_usd = 1.0
        mock_cfg.max_order_size_usd = 100.0
        mock_cfg.max_daily_volume_usd = 5000.0
        mock_cfg.max_trade_age_hours = 1.0

        trade = _make_trade(price=0.05)
        decision = _evaluate_trade_with_state(trade, _fresh_state())
        assert decision.should_copy is False
        assert "too low" in decision.reason.lower()

    @patch("src.copy_trading.risk_manager.CONFIG")
    def test_price_too_high(self, mock_cfg):
        mock_cfg.copy_strategy = "FIXED"
        mock_cfg.copy_size = 10.0
        mock_cfg.min_order_size_usd = 1.0
        mock_cfg.max_order_size_usd = 100.0
        mock_cfg.max_daily_volume_usd = 5000.0
        mock_cfg.max_trade_age_hours = 1.0

        trade = _make_trade(price=0.98)
        decision = _evaluate_trade_with_state(trade, _fresh_state())
        assert decision.should_copy is False
        assert "too high" in decision.reason.lower()


class TestPerMarketCap:
    @patch("src.copy_trading.risk_manager.CONFIG")
    def test_per_market_cap_reached(self, mock_cfg):
        mock_cfg.copy_strategy = "FIXED"
        mock_cfg.copy_size = 50.0
        mock_cfg.min_order_size_usd = 10.0
        mock_cfg.max_order_size_usd = 100.0
        mock_cfg.max_daily_volume_usd = 5000.0
        mock_cfg.max_trade_age_hours = 1.0
        mock_cfg.max_position_per_market_usd = 100.0

        state = _fresh_state()
        state.daily_spend_by_market["test-market"] = 95.0

        trade = _make_trade(price=0.50, side="BUY")
        decision = _evaluate_trade_with_state(trade, state)
        # Remaining is only $5, which is < min_order_size_usd ($10)
        assert decision.should_copy is False


class TestBalanceCheck:
    @patch("src.copy_trading.risk_manager.CONFIG")
    def test_buy_insufficient_balance(self, mock_cfg):
        mock_cfg.copy_strategy = "FIXED"
        mock_cfg.copy_size = 50.0
        mock_cfg.min_order_size_usd = 10.0
        mock_cfg.max_order_size_usd = 100.0
        mock_cfg.max_daily_volume_usd = 5000.0
        mock_cfg.max_trade_age_hours = 1.0
        mock_cfg.max_position_per_market_usd = 500.0

        trade = _make_trade(side="BUY", price=0.50)
        decision = _evaluate_trade_with_state(trade, _fresh_state(), balance=5.0)
        assert decision.should_copy is False
        assert "balance" in decision.reason.lower()

    @patch("src.copy_trading.risk_manager.CONFIG")
    def test_sell_ignores_balance(self, mock_cfg):
        mock_cfg.copy_strategy = "FIXED"
        mock_cfg.copy_size = 50.0
        mock_cfg.min_order_size_usd = 1.0
        mock_cfg.max_order_size_usd = 100.0
        mock_cfg.max_daily_volume_usd = 5000.0
        mock_cfg.max_trade_age_hours = 1.0
        mock_cfg.max_position_per_market_usd = 500.0

        trade = _make_trade(side="SELL", price=0.50)
        decision = _evaluate_trade_with_state(trade, _fresh_state(), balance=0.0)
        assert decision.should_copy is True


class TestAdjustPlacement:
    @patch("src.copy_trading.risk_manager._save_state")
    @patch("src.copy_trading.risk_manager._state")
    def test_adjust_reduces_delta(self, mock_state, mock_save):
        mock_state.daily_volume_usd = 100.0
        mock_state.daily_spend_by_market = {"mkt": 50.0}
        mock_state.side = "BUY"

        trade = _make_trade(side="BUY", market="mkt")
        adjust_placement(trade, -20.0)

        assert mock_state.daily_volume_usd == 80.0
        assert mock_state.daily_spend_by_market["mkt"] == 30.0

    @patch("src.copy_trading.risk_manager._save_state")
    @patch("src.copy_trading.risk_manager._state")
    def test_adjust_does_not_go_negative(self, mock_state, mock_save):
        mock_state.daily_volume_usd = 10.0
        mock_state.daily_spend_by_market = {"mkt": 5.0}

        trade = _make_trade(side="BUY", market="mkt")
        adjust_placement(trade, -100.0)

        assert mock_state.daily_volume_usd == 0
        assert mock_state.daily_spend_by_market["mkt"] == 0
