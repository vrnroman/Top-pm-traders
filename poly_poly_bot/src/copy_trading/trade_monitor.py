"""Data API trade detection — fetches trader activity and maps to DetectedTrade."""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from typing import Optional

import httpx

from src.config import CONFIG
from src.logger import logger
from src.models import DetectedTrade
from src.utils import short_address

# Per-trader cursor: address -> last-seen timestamp (epoch ms).
# We overlap by CURSOR_OVERLAP_MS to avoid missing trades due to clock skew.
CURSOR_OVERLAP_MS = 5000

_trader_cursors: dict[str, int] = {}


def _canonical_trade_id(tx_hash: str, token_id: str, side: str) -> str:
    """Canonical trade identifier: txHash-tokenId-side."""
    return f"{tx_hash}-{token_id}-{side}"


def _parse_timestamp(raw: int | float | str) -> str:
    """Convert a numeric epoch (seconds or ms) or ISO string to ISO 8601."""
    if isinstance(raw, (int, float)):
        # If the value is unreasonably large it's probably milliseconds
        ts = raw / 1000 if raw > 1e12 else raw
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    if isinstance(raw, str):
        return raw
    return datetime.now(timezone.utc).isoformat()


def _parse_size(item: dict) -> float:
    """Extract USDC size from an activity item, trying multiple fields."""
    # Prefer explicit usdcSize
    if "usdcSize" in item:
        try:
            val = float(item["usdcSize"])
            if not math.isnan(val) and val > 0:
                return val
        except (ValueError, TypeError):
            pass

    # amount field
    if "amount" in item:
        try:
            val = float(item["amount"])
            if not math.isnan(val) and val > 0:
                return val
        except (ValueError, TypeError):
            pass

    # size * price
    try:
        size = float(item.get("size", 0))
        price = float(item.get("price", 0))
        val = size * price
        if not math.isnan(val) and val > 0:
            return val
    except (ValueError, TypeError):
        pass

    return 0.0


def _parse_price(item: dict) -> float:
    """Extract price from an activity item."""
    for key in ("price", "avgPrice"):
        if key in item:
            try:
                val = float(item[key])
                if not math.isnan(val):
                    return val
            except (ValueError, TypeError):
                continue
    return 0.0


def _item_to_trade(item: dict, trader_address: str) -> Optional[DetectedTrade]:
    """Map a single Data API activity item to a DetectedTrade, or None if invalid."""
    token_id = item.get("asset", "") or item.get("tokenId", "") or item.get("token_id", "")
    if not token_id:
        return None

    size = _parse_size(item)
    if size <= 0 or math.isnan(size):
        return None

    side_raw = (item.get("side") or item.get("type") or "BUY").upper()
    side = "SELL" if side_raw in ("SELL", "SOLD") else "BUY"

    tx_hash = item.get("transactionHash", "") or item.get("txHash", "") or item.get("proxyWalletAddress", "") or ""

    trade_id = _canonical_trade_id(tx_hash, token_id, side)

    raw_ts = item.get("timestamp") or item.get("createdAt") or item.get("blockTimestamp") or 0
    timestamp = _parse_timestamp(raw_ts)

    price = _parse_price(item)

    market = item.get("title") or item.get("market") or item.get("question") or ""
    condition_id = item.get("conditionId") or item.get("condition_id") or ""
    outcome = item.get("outcome") or item.get("outcomeIndex") or ""

    return DetectedTrade(
        id=trade_id,
        trader_address=trader_address,
        timestamp=timestamp,
        market=str(market),
        condition_id=str(condition_id),
        token_id=token_id,
        side=side,  # type: ignore[arg-type]
        size=size,
        price=price,
        outcome=str(outcome),
    )


def _get_cursor(address: str) -> Optional[int]:
    """Get the cursor for a trader (epoch ms), with overlap subtracted."""
    cursor = _trader_cursors.get(address.lower())
    if cursor is not None:
        return max(0, cursor - CURSOR_OVERLAP_MS)
    return None


def _update_cursor(address: str, ts_iso: str) -> None:
    """Update the per-trader cursor to the latest seen timestamp."""
    try:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        epoch_ms = int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return

    key = address.lower()
    existing = _trader_cursors.get(key, 0)
    if epoch_ms > existing:
        _trader_cursors[key] = epoch_ms


async def fetch_trader_activity(address: str) -> list[DetectedTrade]:
    """Fetch recent activity for a single trader from the Data API.

    Returns a list of DetectedTrade objects, deduplicated by canonical ID.
    Handles 429 rate-limits and network errors gracefully.
    """
    url = f"{CONFIG.data_api_url}/activity"
    params: dict[str, str] = {"address": address}

    cursor = _get_cursor(address)
    if cursor is not None:
        params["startTime"] = str(cursor)

    trades: list[DetectedTrade] = []
    seen_ids: set[str] = set()

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, params=params)

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "2"))
                logger.warn(f"Rate limited fetching {short_address(address)}, backing off {retry_after}s")
                await asyncio.sleep(retry_after)
                return []

            resp.raise_for_status()
            data = resp.json()

    except httpx.TimeoutException:
        logger.warn(f"Timeout fetching activity for {short_address(address)}")
        return []
    except httpx.HTTPStatusError as exc:
        logger.error(f"HTTP {exc.response.status_code} fetching activity for {short_address(address)}")
        return []
    except Exception as exc:
        logger.error(f"Network error fetching {short_address(address)}: {exc}")
        return []

    # data can be a list or wrapped in a key
    items: list[dict] = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("history", data.get("data", data.get("activities", [])))
        if isinstance(items, dict):
            items = []

    for item in items:
        trade = _item_to_trade(item, address)
        if trade is None:
            continue
        if trade.id in seen_ids:
            continue
        seen_ids.add(trade.id)

        # Cursor filtering: skip trades older than cursor
        if cursor is not None:
            try:
                dt = datetime.fromisoformat(trade.timestamp.replace("Z", "+00:00"))
                trade_ms = int(dt.timestamp() * 1000)
                if trade_ms < cursor:
                    continue
            except (ValueError, TypeError):
                pass

        trades.append(trade)
        _update_cursor(address, trade.timestamp)

    return trades


async def fetch_all_trader_activities() -> list[DetectedTrade]:
    """Fetch activities for all tracked traders with bounded concurrency.

    Returns a flat list of all detected trades across all traders.
    """
    addresses = CONFIG.user_addresses
    if not addresses:
        return []

    semaphore = asyncio.Semaphore(CONFIG.fetch_concurrency)

    async def _bounded_fetch(addr: str) -> list[DetectedTrade]:
        async with semaphore:
            return await fetch_trader_activity(addr)

    results = await asyncio.gather(
        *[_bounded_fetch(addr) for addr in addresses],
        return_exceptions=True,
    )

    all_trades: list[DetectedTrade] = []
    for i, result in enumerate(results):
        if isinstance(result, BaseException):
            logger.error(f"Error fetching {short_address(addresses[i])}: {result}")
            continue
        all_trades.extend(result)

    return all_trades
