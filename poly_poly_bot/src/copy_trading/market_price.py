"""Live market snapshot with TTL caching and drift calculation."""

from __future__ import annotations

import math
import time
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BUY, SELL

from src.logger import logger
from src.models import MarketSnapshot
from src.utils import error_message

FETCH_TIMEOUT_S = 0.2
CACHE_TTL_S = 5.0

# Cache: token_id -> (MarketSnapshot, fetched_at_monotonic)
_snapshot_cache: dict[str, tuple[MarketSnapshot, float]] = {}


def _is_valid_price(p: float) -> bool:
    """Check that a price is a positive finite number."""
    return not math.isnan(p) and not math.isinf(p) and p > 0


def fetch_market_snapshot(
    clob_client: ClobClient,
    token_id: str,
) -> Optional[MarketSnapshot]:
    """Fetch a live bid/ask snapshot for a token, with TTL caching.

    Returns None if the market data is invalid or unavailable.
    """
    now = time.monotonic()

    # Check cache
    cached = _snapshot_cache.get(token_id)
    if cached is not None:
        snapshot, fetched_at = cached
        if (now - fetched_at) < CACHE_TTL_S:
            return snapshot

    try:
        bid_str = clob_client.get_price(token_id, SELL)  # best bid
        ask_str = clob_client.get_price(token_id, BUY)   # best ask

        best_bid = float(bid_str)
        best_ask = float(ask_str)

    except Exception as exc:
        logger.debug(f"Failed to fetch snapshot for {token_id}: {error_message(exc)}")
        # Return stale cache if available
        if cached is not None:
            return cached[0]
        return None

    # Validate
    if not _is_valid_price(best_bid) or not _is_valid_price(best_ask):
        logger.debug(f"Invalid prices for {token_id}: bid={best_bid}, ask={best_ask}")
        return None

    # No crossed book
    if best_bid >= best_ask:
        logger.debug(f"Crossed book for {token_id}: bid={best_bid} >= ask={best_ask}")
        return None

    spread = best_ask - best_bid
    midpoint = (best_bid + best_ask) / 2.0
    spread_bps = int(round((spread / midpoint) * 10000)) if midpoint > 0 else 0

    snapshot = MarketSnapshot(
        best_bid=best_bid,
        best_ask=best_ask,
        midpoint=midpoint,
        spread=spread,
        spread_bps=spread_bps,
        fetched_at=time.time(),
    )

    _snapshot_cache[token_id] = (snapshot, now)
    return snapshot


def compute_drift_bps(
    trader_price: float,
    snapshot: MarketSnapshot,
    side: str,
) -> int:
    """Compute price drift in basis points between trader price and current market.

    For BUY: drift = (bestAsk - traderPrice) / traderPrice * 10000
    For SELL: drift = (traderPrice - bestBid) / traderPrice * 10000

    Positive drift means the market has moved against us since the trader executed.
    """
    if trader_price <= 0:
        return 0

    if side == "BUY":
        drift = (snapshot.best_ask - trader_price) / trader_price
    else:
        drift = (trader_price - snapshot.best_bid) / trader_price

    return int(round(drift * 10000))
