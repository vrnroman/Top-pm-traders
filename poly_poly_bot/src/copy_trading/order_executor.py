"""Order execution with adaptive pricing for copy trades."""

from __future__ import annotations

from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BUY, SELL, OrderArgs, OrderType

from src.logger import logger
from src.models import DetectedTrade, MarketSnapshot, OrderResult
from src.utils import ceil_cents, error_message, round_cents

# Price buffers: allow up to 2% slippage from trader price
BUY_BUFFER = 1.02
SELL_BUFFER = 0.98


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def execute_copy_order(
    clob_client: ClobClient,
    trade: DetectedTrade,
    copy_size: float,
    snapshot: Optional[MarketSnapshot] = None,
) -> OrderResult:
    """Execute a copy order on the CLOB with adaptive pricing.

    For BUY orders:
        - Use bestAsk from snapshot, capped at trader_price * 1.02
        - If no snapshot, use trader_price * 1.02
        - Shares = ceil_cents(copy_size / order_price)

    For SELL orders:
        - Use bestBid from snapshot, floored at trader_price * 0.98
        - If no snapshot, use trader_price * 0.98
        - Shares = round_cents(copy_size / order_price), capped at position size

    Args:
        clob_client: Authenticated CLOB client.
        trade: The detected trade to copy.
        copy_size: USDC size for this copy order.
        snapshot: Optional live market snapshot for adaptive pricing.

    Returns:
        OrderResult with order_id, shares, and order_price.

    Raises:
        RuntimeError: If the order creation or posting fails.
    """
    side = trade.side
    trader_price = trade.price

    if side == "BUY":
        # Adaptive pricing: use best ask, capped at trader_price * buffer
        if snapshot is not None and snapshot.best_ask > 0:
            max_price = trader_price * BUY_BUFFER if trader_price > 0 else 1.0
            order_price = min(snapshot.best_ask, max_price)
        else:
            # Fallback: fixed 2% buffer above trader price
            order_price = trader_price * BUY_BUFFER if trader_price > 0 else 0.50

        order_price = _clamp(round_cents(order_price), 0.01, 0.99)
        shares = ceil_cents(copy_size / order_price) if order_price > 0 else 0.0
        clob_side = BUY

    else:  # SELL
        # Adaptive pricing: use best bid, floored at trader_price * buffer
        if snapshot is not None and snapshot.best_bid > 0:
            min_price = trader_price * SELL_BUFFER if trader_price > 0 else 0.0
            order_price = max(snapshot.best_bid, min_price)
        else:
            # Fallback: fixed 2% buffer below trader price
            order_price = trader_price * SELL_BUFFER if trader_price > 0 else 0.50

        order_price = _clamp(round_cents(order_price), 0.01, 0.99)
        shares = round_cents(copy_size / order_price) if order_price > 0 else 0.0
        clob_side = SELL

    if shares <= 0:
        raise RuntimeError(
            f"Computed non-positive shares ({shares}) for {side} "
            f"copy_size={copy_size}, order_price={order_price}"
        )

    logger.info(
        f"Placing {side} order: {shares} shares @ {order_price} "
        f"(copy_size=${copy_size:.2f}, trader_price={trader_price}, "
        f"token={trade.token_id[:12]}...)"
    )

    try:
        order = clob_client.create_order(
            OrderArgs(
                price=order_price,
                size=shares,
                side=clob_side,
                token_id=trade.token_id,
            )
        )
        resp = clob_client.post_order(order, OrderType.GTC)
    except Exception as exc:
        raise RuntimeError(f"CLOB order failed: {error_message(exc)}") from exc

    # Extract order ID from response
    order_id = ""
    if isinstance(resp, dict):
        order_id = resp.get("orderID", "") or resp.get("order_id", "") or resp.get("id", "")
    elif isinstance(resp, str):
        order_id = resp

    if not order_id:
        logger.warn(f"Order posted but no order_id in response: {resp}")

    logger.trade(
        f"Order placed: {order_id} | {side} {shares}@{order_price} | "
        f"market={trade.market[:40]}"
    )

    return OrderResult(
        order_id=order_id,
        shares=shares,
        order_price=order_price,
    )
