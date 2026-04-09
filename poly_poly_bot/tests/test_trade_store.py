"""Tests for trade store — seen trades, retries, copy counts."""

from unittest.mock import patch

import pytest

from src.copy_trading.trade_store import (
    is_seen_trade,
    mark_trade_as_seen,
    increment_retry,
    is_max_retries,
    get_copy_count,
    increment_copy_count,
    _seen_trades,
    _retry_counts,
    _trader_counts,
)


@pytest.fixture(autouse=True)
def clear_state():
    """Reset all module-level state before each test."""
    _seen_trades.clear()
    _retry_counts.clear()
    _trader_counts.clear()
    yield
    _seen_trades.clear()
    _retry_counts.clear()
    _trader_counts.clear()


class TestSeenTrades:
    @patch("src.copy_trading.trade_store._save_seen_trades")
    def test_unknown_trade_is_not_seen(self, mock_save):
        assert is_seen_trade("trade-abc") is False

    @patch("src.copy_trading.trade_store._save_seen_trades")
    def test_mark_then_is_seen(self, mock_save):
        mark_trade_as_seen("trade-abc")
        assert is_seen_trade("trade-abc") is True

    @patch("src.copy_trading.trade_store._save_seen_trades")
    def test_different_trades_independent(self, mock_save):
        mark_trade_as_seen("trade-1")
        assert is_seen_trade("trade-1") is True
        assert is_seen_trade("trade-2") is False


class TestRetryCount:
    def test_increment_starts_at_one(self):
        count = increment_retry("t1")
        assert count == 1

    def test_increment_counts_up(self):
        increment_retry("t1")
        count = increment_retry("t1")
        assert count == 2

    def test_is_max_retries_at_three(self):
        assert is_max_retries("t1") is False
        increment_retry("t1")
        increment_retry("t1")
        assert is_max_retries("t1") is False
        increment_retry("t1")
        assert is_max_retries("t1") is True

    def test_independent_counters(self):
        increment_retry("a")
        increment_retry("a")
        increment_retry("a")
        assert is_max_retries("a") is True
        assert is_max_retries("b") is False


class TestCopyCount:
    @patch("src.copy_trading.trade_store._save_trader_counts")
    def test_get_copy_count_unknown(self, mock_save):
        assert get_copy_count("0xABC") == 0

    @patch("src.copy_trading.trade_store._save_trader_counts")
    def test_increment_copy_count(self, mock_save):
        count = increment_copy_count("0xABC")
        assert count == 1
        count = increment_copy_count("0xABC")
        assert count == 2

    @patch("src.copy_trading.trade_store._save_trader_counts")
    def test_case_insensitive(self, mock_save):
        increment_copy_count("0xABC")
        assert get_copy_count("0xabc") == 1
        assert get_copy_count("0xABC") == 1
