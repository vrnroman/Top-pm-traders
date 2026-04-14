"""Tests for the monitor-mode watchlist alerter noise gates."""

from unittest.mock import AsyncMock, patch

import pytest

from src.copy_trading import watchlist_alerter
from src.copy_trading.strategy_config import WATCHLIST_ALERT
from src.copy_trading.watchlist_alerter import (
    maybe_alert_watchlist_trade,
    _reset_watchlist_alerter,
)
from src.models import DetectedTrade


@pytest.fixture(autouse=True)
def reset_alerter():
    _reset_watchlist_alerter()
    yield
    _reset_watchlist_alerter()


def _make_trade(
    market: str = "Will the U.S. invade Iran before 2027?",
    condition_id: str = "0xabc",
    side: str = "BUY",
    size: float = 1500.0,
    price: float = 0.34,
    trader: str = "0x" + "a" * 40,
) -> DetectedTrade:
    return DetectedTrade(
        id=f"trade-{trader[-4:]}-{side}-{size}-{price}",
        trader_address=trader,
        timestamp="2026-04-14T12:00:00Z",
        market=market,
        condition_id=condition_id,
        side=side,
        size=size,
        price=price,
    )


@pytest.mark.asyncio
async def test_material_first_fill_fires():
    trade = _make_trade(size=2000, price=0.30)  # $600 cash
    with patch(
        "src.copy_trading.telegram_notifier._send_message",
        new=AsyncMock(),
    ) as send:
        sent = await maybe_alert_watchlist_trade(trade, "1b")
    assert sent is True
    send.assert_awaited_once()
    msg = send.call_args[0][0]
    assert "Watchlist [1B]" in msg
    assert trade.market in msg
    assert trade.trader_address in msg


@pytest.mark.asyncio
async def test_near_cert_buy_is_suppressed():
    # BUY at 0.96 → already priced as near-lock, no insider edge.
    trade = _make_trade(size=10000, price=WATCHLIST_ALERT.near_cert_buy_price + 0.01)
    with patch(
        "src.copy_trading.telegram_notifier._send_message",
        new=AsyncMock(),
    ) as send:
        sent = await maybe_alert_watchlist_trade(trade, "1a")
    assert sent is False
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_sell_at_near_cert_still_fires():
    # SELLs aren't gated — profit-taking on a near-lock is still worth seeing.
    trade = _make_trade(side="SELL", size=10000, price=0.98)
    with patch(
        "src.copy_trading.telegram_notifier._send_message",
        new=AsyncMock(),
    ) as send:
        sent = await maybe_alert_watchlist_trade(trade, "1a")
    assert sent is True
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_min_cash_gate_suppresses_micro_fill():
    # $200 fill — below the default $500 floor → dropped.
    trade = _make_trade(size=600, price=0.333)  # ~$200 cash
    with patch(
        "src.copy_trading.telegram_notifier._send_message",
        new=AsyncMock(),
    ) as send:
        sent = await maybe_alert_watchlist_trade(trade, "1b")
    assert sent is False
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_dedup_coalesces_rapid_same_market_fills():
    # First fill at $600 fires; a second fill on the same (wallet, market,
    # side) within the cooldown is suppressed even though it also clears
    # every individual gate. This is the 100-fill scale-in collapse.
    with patch(
        "src.copy_trading.telegram_notifier._send_message",
        new=AsyncMock(),
    ) as send:
        first = await maybe_alert_watchlist_trade(
            _make_trade(size=2000, price=0.30), "1b"  # $600
        )
        second = await maybe_alert_watchlist_trade(
            _make_trade(size=2500, price=0.31), "1b"  # $775, same wallet+market+side
        )
    assert first is True
    assert second is False
    assert send.await_count == 1


@pytest.mark.asyncio
async def test_dedup_respects_side_separately():
    # SELL after a BUY on the same market is a different signal — should fire.
    with patch(
        "src.copy_trading.telegram_notifier._send_message",
        new=AsyncMock(),
    ) as send:
        await maybe_alert_watchlist_trade(
            _make_trade(side="BUY", size=2000, price=0.30), "1b"
        )
        sold = await maybe_alert_watchlist_trade(
            _make_trade(side="SELL", size=2000, price=0.35), "1b"
        )
    assert sold is True
    assert send.await_count == 2


@pytest.mark.asyncio
async def test_dedup_scoped_per_wallet():
    # Two different wallets betting on the same market+side each get one alert.
    with patch(
        "src.copy_trading.telegram_notifier._send_message",
        new=AsyncMock(),
    ) as send:
        a = await maybe_alert_watchlist_trade(
            _make_trade(size=2000, price=0.30, trader="0x" + "1" * 40), "1a"
        )
        b = await maybe_alert_watchlist_trade(
            _make_trade(size=2000, price=0.30, trader="0x" + "2" * 40), "1b"
        )
    assert a is True
    assert b is True
    assert send.await_count == 2


@pytest.mark.asyncio
async def test_dedup_scoped_per_condition_id():
    # Same wallet, two different markets → both fire.
    with patch(
        "src.copy_trading.telegram_notifier._send_message",
        new=AsyncMock(),
    ) as send:
        a = await maybe_alert_watchlist_trade(
            _make_trade(condition_id="0xaaa", size=2000, price=0.30), "1a"
        )
        b = await maybe_alert_watchlist_trade(
            _make_trade(condition_id="0xbbb", size=2000, price=0.30), "1a"
        )
    assert a is True
    assert b is True
    assert send.await_count == 2
