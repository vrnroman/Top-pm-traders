"""On-chain wallet age lookup via Etherscan V2 API, cached in SQLite.

Returns (first_tx_epoch_s, lifetime_tx_count) for any Polygon address.
Gracefully returns (None, None) when no API key is configured or the lookup
fails — callers must treat None as "unknown" and fail closed.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from src.config import CONFIG
from src.logger import logger
from src.utils import error_message

_DB_PATH = Path(CONFIG.data_dir) / "wallet-age.sqlite"
_ETHERSCAN_URL = "https://api.etherscan.io/v2/api"
_CHAIN_ID = 137  # Polygon

_NEGATIVE_CACHE_S = 3600  # retry failed lookups at most hourly


class _TokenBucket:
    """Async token-bucket limiter. Etherscan free tier allows 5 req/s.

    Bare asyncio.Semaphore caps concurrency but not rate — under load it would
    happily fire off 50 requests in a second from 50 distinct callers and
    immediately trip 429s. This implementation refills continuously and
    sleeps callers outside the lock so they don't starve each other.
    """

    def __init__(self, rate_per_s: float, burst: int) -> None:
        self._rate = rate_per_s
        self._burst = float(burst)
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait_s = (1.0 - self._tokens) / self._rate
            await asyncio.sleep(wait_s)


_RATE_LIMIT = _TokenBucket(rate_per_s=4.5, burst=5)


@dataclass
class WalletProfile:
    first_tx_ts: int   # epoch seconds; 0 means unknown
    lifetime_tx: int   # approximate lifetime tx count; 0 means unknown

    @property
    def age_days(self) -> Optional[float]:
        if self.first_tx_ts <= 0:
            return None
        return max(0.0, (time.time() - self.first_tx_ts) / 86400)


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_DB_PATH)
    c.execute(
        """CREATE TABLE IF NOT EXISTS wallet_age(
            address TEXT PRIMARY KEY,
            first_tx_ts INTEGER NOT NULL DEFAULT 0,
            lifetime_tx INTEGER NOT NULL DEFAULT 0,
            fetched_at INTEGER NOT NULL DEFAULT 0
        )"""
    )
    return c


def _get_cached(address: str) -> Optional[tuple[int, int, int]]:
    key = address.lower()
    c = _conn()
    try:
        row = c.execute(
            "SELECT first_tx_ts, lifetime_tx, fetched_at FROM wallet_age WHERE address = ?",
            (key,),
        ).fetchone()
        return tuple(row) if row else None
    finally:
        c.close()


def _put_cached(address: str, first_tx_ts: int, lifetime_tx: int) -> None:
    key = address.lower()
    now = int(time.time())
    c = _conn()
    try:
        c.execute(
            "INSERT OR REPLACE INTO wallet_age(address, first_tx_ts, lifetime_tx, fetched_at) VALUES(?,?,?,?)",
            (key, first_tx_ts, lifetime_tx, now),
        )
        c.commit()
    finally:
        c.close()


async def _etherscan_txlist(address: str, sort: str, offset: int = 1) -> list[dict]:
    api_key = getattr(CONFIG, "etherscan_api_key", "") or ""
    if not api_key:
        return []
    params = {
        "chainid": str(_CHAIN_ID),
        "module": "account",
        "action": "txlist",
        "address": address,
        "startblock": "0",
        "endblock": "99999999",
        "page": "1",
        "offset": str(offset),
        "sort": sort,
        "apikey": api_key,
    }
    try:
        await _RATE_LIMIT.acquire()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_ETHERSCAN_URL, params=params)
        if resp.status_code != 200:
            return []
        data = resp.json()
        if data.get("status") != "1":
            return []
        result = data.get("result")
        return result if isinstance(result, list) else []
    except Exception as exc:
        logger.debug(f"[wallet-age] etherscan error for {address[:10]}: {error_message(exc)}")
        return []


async def _fetch_profile(address: str) -> WalletProfile:
    """Fetch first-tx timestamp and approximate lifetime tx count from Etherscan."""
    first_list = await _etherscan_txlist(address, sort="asc", offset=1)
    if not first_list:
        return WalletProfile(first_tx_ts=0, lifetime_tx=0)
    try:
        first_ts = int(first_list[0].get("timeStamp", "0"))
    except (TypeError, ValueError):
        first_ts = 0

    # Approximate lifetime tx via a second descending-order call.
    # We only care about "is this wallet busy?" not the exact count.
    last_list = await _etherscan_txlist(address, sort="desc", offset=1)
    lifetime_tx = 1 if first_ts > 0 else 0
    if last_list:
        try:
            last_block = int(last_list[0].get("blockNumber", "0"))
            first_block = int(first_list[0].get("blockNumber", "0"))
            # Crude: if last_block == first_block → 1 tx total; otherwise we don't know
            # exact count without pagination. Use nonce-style probe below.
            if last_block == first_block:
                lifetime_tx = 1
            else:
                lifetime_tx = 2  # at least 2; exact count not needed for our gate
        except (TypeError, ValueError):
            pass
    return WalletProfile(first_tx_ts=first_ts, lifetime_tx=lifetime_tx)


_lookup_lock = asyncio.Lock()


async def get_wallet_profile(address: str) -> WalletProfile:
    """Return the cached wallet profile, fetching from Etherscan on cache miss.

    Returns a WalletProfile where first_tx_ts == 0 and lifetime_tx == 0 means
    the lookup failed or no API key is configured — callers should treat as unknown.
    """
    if not address:
        return WalletProfile(0, 0)

    cached = _get_cached(address)
    now = int(time.time())
    if cached is not None:
        first_ts, lifetime, fetched_at = cached
        if first_ts > 0:
            return WalletProfile(first_tx_ts=first_ts, lifetime_tx=lifetime)
        if now - fetched_at < _NEGATIVE_CACHE_S:
            return WalletProfile(0, 0)

    async with _lookup_lock:
        cached = _get_cached(address)
        if cached is not None and cached[0] > 0:
            return WalletProfile(first_tx_ts=cached[0], lifetime_tx=cached[1])
        profile = await _fetch_profile(address)
        _put_cached(address, profile.first_tx_ts, profile.lifetime_tx)
        return profile


async def get_wallet_age_days(address: str) -> Optional[float]:
    """Shortcut: return on-chain age in days, or None if unknown."""
    return (await get_wallet_profile(address)).age_days
