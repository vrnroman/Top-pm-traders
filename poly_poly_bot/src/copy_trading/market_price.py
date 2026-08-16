"""Live market snapshot with TTL caching and drift calculation."""

from __future__ import annotations

import math
import time
from typing import Optional

from py_clob_client_v2 import ClobClient
from py_clob_client_v2.order_builder.constants import BUY, SELL

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


def _price_of(resp) -> Optional[float]:
    """Pull a float price out of whatever the CLOB client hands back.

    py-clob-client-v2 returns ``{"price": "0.68"}`` from ``get_price`` and
    ``{"mid": "0.675"}`` from ``get_midpoint``. Older code here assumed a bare
    string; a plain ``float()`` therefore raised on every call. Accepts both
    shapes so a future client change in either direction cannot silently
    reintroduce that failure.
    """
    if resp is None:
        return None
    if isinstance(resp, dict):
        resp = resp.get("price", resp.get("mid"))
    try:
        return float(resp)
    except (TypeError, ValueError):
        return None


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
        # Two contract facts, both probed against the live CLOB on
        # 2026-08-16 and both previously wrong here:
        #
        # 1. get_price returns a JSON object ``{"price": "0.68"}``, not a
        #    bare string. ``float()`` on it raised every single call, so this
        #    function returned None for every token — silently, on the debug
        #    channel — and every caller degraded to a fallback.
        # 2. ``side`` names the side of the BOOK, not your intent:
        #    side=BUY -> the buy side -> the best BID;
        #    side=SELL -> the sell side -> the best ASK.
        #    This was inverted, which after fixing (1) alone would have
        #    produced bid >= ask, tripped the crossed-book guard, and still
        #    returned None — or, if someone then relaxed that guard, quoted a
        #    BUY at the bid and understated the entry penalty by the whole
        #    spread. That penalty is the number being measured.
        best_bid = _price_of(clob_client.get_price(token_id, BUY))
        best_ask = _price_of(clob_client.get_price(token_id, SELL))
        if best_bid is None or best_ask is None:
            return cached[0] if cached is not None else None

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
