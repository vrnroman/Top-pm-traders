"""Local position (inventory) tracking for copy-trading.

Tracks token positions with weighted average cost basis.
Persists to data/inventory.json (or data/preview-inventory.json in preview mode).
Supports syncing from the Polymarket Data API.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Optional

import httpx

from src.config import CONFIG
from src.logger import logger
from src.utils import round_cents


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# Position dict: tokenId -> {shares, avg_price, market_key, market}
Position = dict  # keys: shares, avg_price, market_key, market


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_positions: dict[str, Position] = {}

_INVENTORY_FILE = os.path.join(
    CONFIG.data_dir,
    "preview-inventory.json" if CONFIG.preview_mode else "inventory.json",
)


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _atomic_write_json(path: str, data: dict) -> None:
    """Write JSON atomically: write to tmp file then rename."""
    dir_path = os.path.dirname(path)
    os.makedirs(dir_path, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _load_inventory() -> None:
    """Load inventory from disk."""
    global _positions
    try:
        with open(_INVENTORY_FILE, "r") as f:
            raw = json.load(f)
        _positions = raw if isinstance(raw, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        _positions = {}


def _save_inventory() -> None:
    """Persist inventory to disk."""
    _atomic_write_json(_INVENTORY_FILE, _positions)


# Load on import
_load_inventory()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def weighted_avg_price(
    existing_shares: float,
    existing_avg: float,
    new_shares: float,
    new_price: float,
) -> float:
    """Calculate weighted average price after adding new shares.

    Args:
        existing_shares: Current number of shares.
        existing_avg: Current average price per share.
        new_shares: Number of shares being added.
        new_price: Price of the new shares.

    Returns:
        New weighted average price.
    """
    total_shares = existing_shares + new_shares
    if total_shares <= 0:
        return 0.0
    total_cost = (existing_shares * existing_avg) + (new_shares * new_price)
    return total_cost / total_shares


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def record_buy(
    token_id: str,
    shares: float,
    price: float,
    market_key: str = "",
    market: str = "",
) -> None:
    """Record a BUY fill. Updates weighted average price."""
    pos = _positions.get(token_id)
    if pos is None:
        _positions[token_id] = {
            "shares": shares,
            "avg_price": price,
            "market_key": market_key,
            "market": market,
        }
    else:
        new_avg = weighted_avg_price(pos["shares"], pos["avg_price"], shares, price)
        pos["shares"] = pos["shares"] + shares
        pos["avg_price"] = new_avg
        if market_key:
            pos["market_key"] = market_key
        if market:
            pos["market"] = market
    _save_inventory()
    logger.info(
        f"[inventory] BUY {shares:.4f} shares of {token_id[:12]}... @ ${price:.4f} | "
        f"total: {_positions[token_id]['shares']:.4f} shares"
    )


def record_sell(token_id: str, shares: float) -> None:
    """Record a SELL fill. Reduces shares; removes position if zero."""
    pos = _positions.get(token_id)
    if pos is None:
        logger.warn(f"[inventory] SELL for unknown position {token_id[:12]}...")
        return

    pos["shares"] = pos["shares"] - shares
    if pos["shares"] <= 0.0001:
        del _positions[token_id]
        logger.info(f"[inventory] Position closed: {token_id[:12]}...")
    else:
        logger.info(
            f"[inventory] SELL {shares:.4f} shares of {token_id[:12]}... | "
            f"remaining: {pos['shares']:.4f}"
        )
    _save_inventory()


def has_position(token_id: str) -> bool:
    """Check if we have a non-zero position for a token."""
    pos = _positions.get(token_id)
    return pos is not None and pos.get("shares", 0) > 0


def get_position(token_id: str) -> Optional[Position]:
    """Get position details for a token, or None."""
    pos = _positions.get(token_id)
    if pos is None:
        return None
    return dict(pos)  # Return a copy


def get_positions() -> dict[str, Position]:
    """Get all positions (copy)."""
    return {k: dict(v) for k, v in _positions.items()}


def get_inventory_summary() -> dict:
    """Return a summary of current inventory for status display."""
    total_positions = len(_positions)
    total_shares = 0.0
    total_cost_basis = 0.0
    for pos in _positions.values():
        shares = pos.get("shares", 0)
        avg = pos.get("avg_price", 0)
        total_shares += shares
        total_cost_basis += shares * avg
    return {
        "total_positions": total_positions,
        "total_shares": round(total_shares, 4),
        "total_cost_basis_usd": round_cents(total_cost_basis),
        "positions": {
            tid: {
                "shares": round(p["shares"], 4),
                "avg_price": round(p["avg_price"], 4),
                "cost_basis": round_cents(p["shares"] * p["avg_price"]),
                "market": p.get("market", ""),
            }
            for tid, p in _positions.items()
        },
    }


async def sync_inventory_from_api(proxy_wallet: str) -> int:
    """Sync local inventory from the Polymarket Data API.

    Fetches open positions for the proxy wallet and reconciles with local state.
    Returns the number of positions synced.
    """
    if not proxy_wallet:
        logger.warn("[inventory] No proxy wallet configured, skipping API sync")
        return 0

    url = f"{CONFIG.data_api_url}/positions"
    params = {"user": proxy_wallet}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.error(f"[inventory] API sync failed: {e}")
        return 0

    if not isinstance(data, list):
        logger.warn(f"[inventory] Unexpected API response type: {type(data)}")
        return 0

    synced = 0
    remote_token_ids: set[str] = set()

    for entry in data:
        token_id = entry.get("asset", {}).get("id", "") or entry.get("tokenId", "")
        if not token_id:
            continue

        remote_token_ids.add(token_id)
        shares = float(entry.get("size", 0) or entry.get("shares", 0))
        avg_price = float(entry.get("avgPrice", 0) or entry.get("avg_price", 0))
        market = entry.get("market", {}).get("question", "") or entry.get("market", "")
        condition_id = entry.get("market", {}).get("conditionId", "") or entry.get("conditionId", "")

        if shares <= 0:
            continue

        _positions[token_id] = {
            "shares": shares,
            "avg_price": avg_price,
            "market_key": condition_id,
            "market": market,
        }
        synced += 1

    # Remove local positions not found remotely
    stale_ids = [tid for tid in _positions if tid not in remote_token_ids]
    for tid in stale_ids:
        logger.info(f"[inventory] Removing stale position: {tid[:12]}...")
        del _positions[tid]

    _save_inventory()
    logger.info(f"[inventory] Synced {synced} positions from API ({len(stale_ids)} stale removed)")
    return synced
