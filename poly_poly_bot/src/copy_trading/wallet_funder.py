"""Wallet funding-source lookup via Etherscan V2 USDC token transfers.

For every wallet we want to classify, we find the *first* external address that
sent USDC to it on Polygon. That address is the wallet's "funder". If two
wallets share the same funder AND that funder is not a known CEX hot wallet,
it's the strongest cluster signal we can get without on-chain graph analysis.

Results are cached in SQLite because funders are immutable once set — we never
need to re-query them. The cache persists across bot restarts (same rationale
as wallet_age's SQLite cache; this is a request-deduplication cache over an
external API, not bot-side behavioral state).

Known CEX hot wallets are filtered out. The seed list is small and deliberately
under-inclusive; extend it via STRATEGY_1C_CEX_FUNDERS (comma-separated lower-
case addresses) as you observe false positives in your own alert stream.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from src.config import CONFIG
from src.logger import logger
from src.utils import error_message

_DB_PATH = Path(CONFIG.data_dir) / "wallet-funder.sqlite"
_ETHERSCAN_URL = "https://api.etherscan.io/v2/api"
_CHAIN_ID = 137

# Polymarket historically used bridged USDC.e; native USDC is now also accepted.
# We scan both contracts and take whichever had the earliest inflow.
_USDC_BRIDGED = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
_USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"

_NEGATIVE_CACHE_S = 3600
_EMPTY_FUNDER = ""  # sentinel for "looked up, no funder found" (vs NULL = "not yet looked up")

# Minimal seed list. These are frequently-observed CEX / bridge addresses that
# route USDC onto Polygon. Intentionally small — users should extend as they
# see false positives.
_BAKED_CEX_LOWER: set[str] = {
    # Binance (Polygon hot wallets)
    "0xf977814e90da44bfa03b6295a0616a897441acec",
    "0xe7804c37c13166ff0b37f5ae0bb07a3aebb6e245",
    # Coinbase
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3",
    "0x503828976d22510aad0201ac7ec88293211d23da",
    "0xddfabcdc4d8ffc6d5beaf154f18b778f892a0740",
    "0x3cd751e6b0078be393132286c442345e5dc49699",
    # Kraken
    "0x2910543af39aba0cd09dbb2d50200b3e800a63d2",
    "0x0a869d79a7052c7f1b55a8ebabbea3420f0d1e13",
    # OKX
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b",
    "0x236f9f97e0e62388479bf9e5ba4889e46b0273c3",
    # Polygon PoS Bridge (USDC ERC20 predicate)
    "0x40ec5b33f54e0e8a33a975908c5ba1c14e5bbbdf",
    "0xa0c68c638235ee32657e8f720a23cec1bfc77c77",
    # Circle Treasury / USDC issuer
    "0x55fe002aeff02f77364de339a1292923a15844b8",
    # Zero address (mint)
    "0x0000000000000000000000000000000000000000",
}


def _load_extended_cex_set() -> set[str]:
    extra = os.environ.get("STRATEGY_1C_CEX_FUNDERS", "").strip()
    extras = {x.strip().lower() for x in extra.split(",") if x.strip()}
    return _BAKED_CEX_LOWER | extras


_CEX_LOWER = _load_extended_cex_set()


def is_cex_funder(address: str) -> bool:
    return (address or "").lower() in _CEX_LOWER


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_DB_PATH)
    c.execute(
        """CREATE TABLE IF NOT EXISTS wallet_funder(
            address TEXT PRIMARY KEY,
            funder TEXT NOT NULL DEFAULT '',
            first_usdc_ts INTEGER NOT NULL DEFAULT 0,
            fetched_at INTEGER NOT NULL DEFAULT 0
        )"""
    )
    return c


def _get_cached(address: str) -> Optional[tuple[str, int, int]]:
    key = address.lower()
    c = _conn()
    try:
        row = c.execute(
            "SELECT funder, first_usdc_ts, fetched_at FROM wallet_funder WHERE address = ?",
            (key,),
        ).fetchone()
        return tuple(row) if row else None
    finally:
        c.close()


def _put_cached(address: str, funder: str, first_usdc_ts: int) -> None:
    key = address.lower()
    now = int(time.time())
    c = _conn()
    try:
        c.execute(
            "INSERT OR REPLACE INTO wallet_funder(address, funder, first_usdc_ts, fetched_at) VALUES(?,?,?,?)",
            (key, funder.lower(), first_usdc_ts, now),
        )
        c.commit()
    finally:
        c.close()


async def _fetch_first_inflow(
    client: httpx.AsyncClient,
    address: str,
    contract: str,
) -> Optional[tuple[str, int]]:
    """Return (from_address, epoch_s) of the earliest USDC inflow, or None."""
    # Import here to avoid circular coupling with wallet_age's rate limiter.
    from src.copy_trading.wallet_age import _RATE_LIMIT

    api_key = getattr(CONFIG, "etherscan_api_key", "") or ""
    if not api_key:
        return None

    params = {
        "chainid": str(_CHAIN_ID),
        "module": "account",
        "action": "tokentx",
        "contractaddress": contract,
        "address": address,
        "page": "1",
        "offset": "10",
        "sort": "asc",
        "apikey": api_key,
    }
    try:
        await _RATE_LIMIT.acquire()
        resp = await client.get(_ETHERSCAN_URL, params=params, timeout=10.0)
    except Exception as exc:
        logger.debug(f"[wallet-funder] etherscan err {address[:10]} {contract[:10]}: {error_message(exc)}")
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except Exception:
        return None
    if data.get("status") != "1":
        return None
    results = data.get("result")
    if not isinstance(results, list):
        return None

    target = address.lower()
    for tx in results:
        to_addr = (tx.get("to") or "").lower()
        from_addr = (tx.get("from") or "").lower()
        if to_addr != target:
            continue
        if not from_addr:
            continue
        try:
            ts = int(tx.get("timeStamp", "0"))
        except (TypeError, ValueError):
            continue
        return from_addr, ts
    return None


_lookup_lock = asyncio.Lock()
_inflight: dict[str, asyncio.Task] = {}


async def _lookup(address: str) -> tuple[str, int]:
    """Fetch the earliest non-CEX USDC sender across both USDC contracts."""
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            _fetch_first_inflow(client, address, _USDC_BRIDGED),
            _fetch_first_inflow(client, address, _USDC_NATIVE),
            return_exceptions=False,
        )

    best: Optional[tuple[str, int]] = None
    for r in results:
        if r is None:
            continue
        funder, ts = r
        if is_cex_funder(funder):
            continue
        if best is None or ts < best[1]:
            best = (funder, ts)

    if best is None:
        return _EMPTY_FUNDER, 0
    return best


@dataclass
class FunderInfo:
    funder: str  # lowercase hex, or "" if unknown / only CEX-funded
    first_usdc_ts: int  # epoch seconds, or 0 if unknown


async def get_funder(address: str) -> FunderInfo:
    """Return the wallet's first non-CEX USDC sender.

    `funder` is an empty string when the lookup failed, the wallet has no USDC
    inflows, or its only inflows came from blacklisted CEX/bridge addresses.
    Callers should treat an empty funder as "unknown — do not cluster".
    """
    if not address:
        return FunderInfo("", 0)

    cached = _get_cached(address)
    now = int(time.time())
    if cached is not None:
        funder, first_ts, fetched_at = cached
        if funder:
            return FunderInfo(funder=funder, first_usdc_ts=first_ts)
        if now - fetched_at < _NEGATIVE_CACHE_S:
            return FunderInfo("", 0)

    key = address.lower()
    async with _lookup_lock:
        cached = _get_cached(address)
        if cached is not None and cached[0]:
            return FunderInfo(funder=cached[0], first_usdc_ts=cached[1])
        task = _inflight.get(key)
        if task is None:
            task = asyncio.create_task(_lookup(address))
            _inflight[key] = task

    try:
        funder, ts = await task
    finally:
        _inflight.pop(key, None)

    _put_cached(address, funder, ts)
    return FunderInfo(funder=funder, first_usdc_ts=ts)
