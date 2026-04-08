"""Tests for trade queues (pending trades and pending orders)."""

import time
from unittest.mock import patch

import pytest

from src.models import DetectedTrade, QueuedTrade, PendingOrder
from src.copy_trading.trade_queue import (
    enqueue_trade,
    drain_trades,
    enqueue_pending_order,
    peek_pending_orders,
    remove_pending_order,
    _pending_trades,
    _pending_orders,
)


def _make_queued_trade(trade_id: str = "t1") -> QueuedTrade:
    return QueuedTrade(
        trade=DetectedTrade(
            id=trade_id,
            trader_address="0x" + "a" * 40,
            timestamp="2026-04-08T12:00:00Z",
            market="test-market",
            side="BUY",
            size=100.0,
            price=0.50,
        ),
        enqueued_at=time.time() * 1000,
        source_detected_at=time.time() * 1000,
    )


def _make_pending_order(order_id: str = "ord-1") -> PendingOrder:
    return PendingOrder(
        trade=DetectedTrade(
            id="t1",
            trader_address="0x" + "a" * 40,
            timestamp="2026-04-08T12:00:00Z",
            market="test-market",
            side="BUY",
            size=100.0,
            price=0.50,
        ),
        order_id=order_id,
        order_price=0.50,
        copy_size=10.0,
        placed_at=time.time(),
        market_key="cond-1",
        side="BUY",
        source_detected_at=time.time() * 1000,
        enqueued_at=time.time() * 1000,
        order_submitted_at=time.time() * 1000,
    )


@pytest.fixture(autouse=True)
def clear_queues():
    """Reset queue state before each test."""
    _pending_trades.clear()
    _pending_orders.clear()
    yield
    _pending_trades.clear()
    _pending_orders.clear()


class TestTradeQueue:
    def test_enqueue_and_drain(self):
        qt = _make_queued_trade("t1")
        enqueue_trade(qt)
        assert len(_pending_trades) == 1

        drained = drain_trades()
        assert len(drained) == 1
        assert drained[0].trade.id == "t1"
        assert len(_pending_trades) == 0

    def test_drain_empty(self):
        drained = drain_trades()
        assert drained == []

    def test_multiple_enqueue(self):
        enqueue_trade(_make_queued_trade("t1"))
        enqueue_trade(_make_queued_trade("t2"))
        drained = drain_trades()
        assert len(drained) == 2


class TestPendingOrderQueue:
    @patch("src.copy_trading.trade_queue._save_pending_orders")
    def test_enqueue_persists(self, mock_save):
        order = _make_pending_order("ord-1")
        enqueue_pending_order(order)
        assert len(_pending_orders) == 1
        mock_save.assert_called()

    @patch("src.copy_trading.trade_queue._save_pending_orders")
    def test_peek_returns_copy(self, mock_save):
        order = _make_pending_order("ord-1")
        enqueue_pending_order(order)

        peeked = peek_pending_orders()
        assert len(peeked) == 1
        assert peeked[0].order_id == "ord-1"
        # Original list not cleared
        assert len(_pending_orders) == 1

    @patch("src.copy_trading.trade_queue._save_pending_orders")
    def test_remove_removes_from_list(self, mock_save):
        enqueue_pending_order(_make_pending_order("ord-1"))
        enqueue_pending_order(_make_pending_order("ord-2"))

        removed = remove_pending_order("ord-1")
        assert removed is not None
        assert removed.order_id == "ord-1"
        assert len(_pending_orders) == 1
        assert _pending_orders[0].order_id == "ord-2"

    @patch("src.copy_trading.trade_queue._save_pending_orders")
    def test_remove_nonexistent_returns_none(self, mock_save):
        result = remove_pending_order("does-not-exist")
        assert result is None
