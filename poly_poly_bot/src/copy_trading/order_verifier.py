"""Order fill verification — polls CLOB for fill status."""

from __future__ import annotations

import asyncio
from typing import Optional

from py_clob_client.client import ClobClient

from src.constants import FILL_CHECK_DELAY_S, FILL_CHECK_RETRIES
from src.logger import logger
from src.models import FillResult
from src.utils import error_message


def _parse_fill_from_order(order_data: dict) -> FillResult:
    """Parse fill information from a CLOB order response.

    Expected fields:
        original_size / size: total order size in shares
        size_matched / filled_size: how many shares have been filled
        price / average_price: fill price
    """
    original_size = float(
        order_data.get("original_size")
        or order_data.get("size")
        or order_data.get("originalSize")
        or 0
    )
    size_matched = float(
        order_data.get("size_matched")
        or order_data.get("filled_size")
        or order_data.get("sizeMatched")
        or 0
    )
    avg_price = float(
        order_data.get("average_price")
        or order_data.get("price")
        or order_data.get("averagePrice")
        or 0
    )

    filled_usd = size_matched * avg_price if avg_price > 0 else 0.0

    if original_size > 0 and size_matched >= original_size:
        status = "FILLED"
    elif size_matched > 0:
        status = "PARTIAL"
    else:
        status = "UNFILLED"

    return FillResult(
        status=status,  # type: ignore[arg-type]
        filled_shares=size_matched,
        filled_usd=round(filled_usd, 4),
        fill_price=avg_price,
    )


async def verify_order_fill(
    clob_client: ClobClient,
    order_id: str,
) -> FillResult:
    """Poll the CLOB for order fill status.

    Attempts up to FILL_CHECK_RETRIES times with FILL_CHECK_DELAY_S between polls.
    Returns the best fill result observed.

    Args:
        clob_client: Authenticated CLOB client.
        order_id: The order ID to verify.

    Returns:
        FillResult with status FILLED, PARTIAL, UNFILLED, or UNKNOWN.
    """
    if not order_id:
        logger.warn("verify_order_fill called with empty order_id")
        return FillResult(status="UNKNOWN")

    best_result: Optional[FillResult] = None

    for attempt in range(FILL_CHECK_RETRIES):
        if attempt > 0:
            await asyncio.sleep(FILL_CHECK_DELAY_S)

        try:
            order_data = clob_client.get_order(order_id)
        except Exception as exc:
            logger.warn(
                f"Fill check error for {order_id} (attempt {attempt + 1}/"
                f"{FILL_CHECK_RETRIES}): {error_message(exc)}"
            )
            continue

        if order_data is None:
            logger.debug(f"Order {order_id} not found (attempt {attempt + 1})")
            continue

        if isinstance(order_data, dict):
            result = _parse_fill_from_order(order_data)
        else:
            logger.warn(f"Unexpected order_data type for {order_id}: {type(order_data)}")
            continue

        # Keep the best result (most filled)
        if best_result is None or result.filled_shares > best_result.filled_shares:
            best_result = result

        # Early exit if fully filled
        if result.status == "FILLED":
            logger.info(
                f"Order {order_id} FILLED: {result.filled_shares} shares "
                f"@ {result.fill_price}"
            )
            return result

    if best_result is not None:
        logger.info(
            f"Order {order_id} after {FILL_CHECK_RETRIES} checks: "
            f"{best_result.status} ({best_result.filled_shares} shares)"
        )
        return best_result

    logger.warn(f"Order {order_id} verification failed after {FILL_CHECK_RETRIES} attempts")
    return FillResult(status="UNKNOWN")
