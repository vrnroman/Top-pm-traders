"""Polymarket-side wallet history lookups.

Answers two questions without a local DB:
  1. What was this wallet's most recent trade *before* a reference timestamp?
     → drives the `dormant_reactivation` pattern (no in-memory state needed).
  2. How many trades has this wallet placed in total (capped at LOOKUP_LIMIT)?
     → drives the `new_account_geo` scalper gate, correct even after bot restart.

Both answers come from a single call to `/trades?user=<addr>&limit=50`, which
is deduped by tx_hash (takerOnly=false returns two rows per fill) and served
from an in-memory TTL LRU. Concurrent callers for the same address share one
in-flight request via an asyncio.Task handle, so popular wallets do not
stampede the Data API.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

import httpx

from src.config import CONFIG
from src.logger import logger
from src.utils import error_message

_LOOKUP_URL_PATH = "/trades"
_LOOKUP_LIMIT = 50
_CACHE_TTL_S = 600.0
_CACHE_MAX = 5000

_HTTP_SEM = asyncio.Semaphore(8)


@dataclass
class WalletHistory:
    """Cached snapshot of a wallet's most-recent trades (deduped by tx_hash)."""

    fetched_at: float
    # [(epoch_s, tx_hash), ...] sorted newest-first, deduped by tx_hash.
    entries: list[tuple[int, str]]
    truncated: bool  # True if the wallet has at least _LOOKUP_LIMIT fills.


_cache: "OrderedDict[str, WalletHistory]" = OrderedDict()
_inflight: dict[str, asyncio.Task] = {}


async def _fetch(address: str) -> WalletHistory:
    url = f"{CONFIG.data_api_url}{_LOOKUP_URL_PATH}"
    params = {
        "user": address,
        "limit": str(_LOOKUP_LIMIT),
        "takerOnly": "false",
    }
    now = time.time()
    try:
        async with _HTTP_SEM:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url, params=params)
    except Exception as exc:
        logger.debug(f"[wallet-hist] lookup err for {address[:10]}: {error_message(exc)}")
        return WalletHistory(now, [], False)

    if resp.status_code != 200:
        logger.debug(f"[wallet-hist] HTTP {resp.status_code} for {address[:10]}")
        return WalletHistory(now, [], False)

    try:
        data = resp.json()
    except Exception:
        return WalletHistory(now, [], False)

    if not isinstance(data, list):
        return WalletHistory(now, [], False)

    raw: list[tuple[int, str]] = []
    for item in data:
        try:
            ts = int(item.get("timestamp", 0))
        except (TypeError, ValueError):
            ts = 0
        if ts > 1_000_000_000_000:  # ms → s
            ts //= 1000
        tx = str(item.get("transactionHash") or "")
        if ts > 0:
            raw.append((ts, tx))

    # Newest first, then dedupe by tx_hash so maker+taker rows count as one fill.
    raw.sort(key=lambda x: -x[0])
    seen_tx: set[str] = set()
    deduped: list[tuple[int, str]] = []
    for ts, tx in raw:
        if tx and tx in seen_tx:
            continue
        if tx:
            seen_tx.add(tx)
        deduped.append((ts, tx))

    # The /trades row count is what the API returned — use that (not deduped)
    # to decide "truncated" because the API's limit is applied pre-dedupe.
    truncated = len(raw) >= _LOOKUP_LIMIT
    return WalletHistory(now, deduped, truncated)


async def _get_or_fetch(address: str) -> WalletHistory:
    key = address.lower()
    now = time.time()

    existing = _cache.get(key)
    if existing is not None and (now - existing.fetched_at) < _CACHE_TTL_S:
        _cache.move_to_end(key)
        return existing

    task = _inflight.get(key)
    if task is None:
        task = asyncio.create_task(_fetch(address))
        _inflight[key] = task

    try:
        history = await task
    finally:
        _inflight.pop(key, None)

    _cache[key] = history
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)
    return history


async def get_prior_trade_ts(
    address: str,
    exclude_tx: str = "",
) -> tuple[Optional[int], int, bool]:
    """Look up the wallet's most-recent trade that isn't `exclude_tx`.

    Returns (prior_ts, known_count, truncated):
      - prior_ts:   epoch seconds of the most-recent prior fill, or None if the
                    wallet has no prior activity on Polymarket.
      - known_count: number of distinct fills currently cached (≤ _LOOKUP_LIMIT).
      - truncated:  True if the wallet has at least _LOOKUP_LIMIT fills (exact
                    count not determinable from this cache alone).

    `exclude_tx` should be the tx_hash of the trade the caller just observed,
    so that the current trade does not get counted as its own "prior" trade.
    """
    history = await _get_or_fetch(address)
    prior: Optional[int] = None
    ex = exclude_tx.lower() if exclude_tx else ""
    for ts, tx in history.entries:
        if ex and tx.lower() == ex:
            continue
        prior = ts
        break
    return prior, len(history.entries), history.truncated


def _reset_wallet_history_cache() -> None:
    """Test helper — clears all cached state."""
    _cache.clear()
    _inflight.clear()
