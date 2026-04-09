"""Market metadata cache — in-memory with JSON persistence and CLOB API lookup."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

import httpx

from src.config import CONFIG
from src.logger import logger
from src.models import MarketMeta
from src.utils import error_message

_CACHE_PATH = Path(CONFIG.data_dir) / "market-cache.json"

# In-memory cache: token_id -> MarketMeta
_cache: dict[str, MarketMeta] = {}
_loaded = False


def _ensure_loaded() -> None:
    """Load cache from disk on first access."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        if _CACHE_PATH.exists():
            data = json.loads(_CACHE_PATH.read_text())
            for token_id, entry in data.items():
                _cache[token_id] = MarketMeta(**entry)
            logger.info(f"Market cache loaded: {len(_cache)} entries")
    except Exception as exc:
        logger.warn(f"Failed to load market cache: {error_message(exc)}")


def _save_cache() -> None:
    """Persist in-memory cache to disk."""
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {tid: meta.model_dump() for tid, meta in _cache.items()}
        _CACHE_PATH.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        logger.warn(f"Failed to save market cache: {error_message(exc)}")


def _fetch_from_api_sync(token_id: str) -> Optional[MarketMeta]:
    """Synchronous CLOB API lookup for a single token_id."""
    url = f"{CONFIG.clob_api_url}/markets"
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url, params={"asset_id": token_id})
            if resp.status_code != 200:
                return None
            data = resp.json()
            # Response may be a list or single object
            item = data[0] if isinstance(data, list) and data else data
            if not isinstance(item, dict):
                return None
            condition_id = item.get("condition_id", "") or item.get("conditionId", "")
            market = item.get("question", "") or item.get("title", "") or item.get("market", "")
            outcome = ""
            tokens = item.get("tokens", [])
            for tok in tokens:
                if tok.get("token_id") == token_id or tok.get("tokenId") == token_id:
                    outcome = tok.get("outcome", "")
                    break
            return MarketMeta(
                condition_id=str(condition_id),
                market=str(market),
                outcome=str(outcome),
                token_id=token_id,
            )
    except Exception as exc:
        logger.debug(f"Market cache API miss for {token_id}: {error_message(exc)}")
        return None


def get_market_meta(token_id: str) -> Optional[MarketMeta]:
    """Get market metadata for a token_id, fetching from API on cache miss.

    Returns None if the token cannot be resolved.
    """
    _ensure_loaded()

    if token_id in _cache:
        return _cache[token_id]

    meta = _fetch_from_api_sync(token_id)
    if meta is not None:
        _cache[token_id] = meta
        _save_cache()
    return meta


async def warm_cache(token_ids: list[str]) -> None:
    """Pre-populate cache for a batch of token IDs.

    Fetches missing entries concurrently via the CLOB API.
    """
    _ensure_loaded()

    missing = [tid for tid in token_ids if tid not in _cache]
    if not missing:
        return

    logger.info(f"Warming market cache for {len(missing)} tokens...")

    async def _fetch_one(token_id: str) -> Optional[MarketMeta]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{CONFIG.clob_api_url}/markets",
                    params={"asset_id": token_id},
                )
                if resp.status_code != 200:
                    return None
                data = resp.json()
                item = data[0] if isinstance(data, list) and data else data
                if not isinstance(item, dict):
                    return None
                condition_id = item.get("condition_id", "") or item.get("conditionId", "")
                market = item.get("question", "") or item.get("title", "") or item.get("market", "")
                outcome = ""
                tokens = item.get("tokens", [])
                for tok in tokens:
                    if tok.get("token_id") == token_id or tok.get("tokenId") == token_id:
                        outcome = tok.get("outcome", "")
                        break
                return MarketMeta(
                    condition_id=str(condition_id),
                    market=str(market),
                    outcome=str(outcome),
                    token_id=token_id,
                )
        except Exception:
            return None

    sem = asyncio.Semaphore(5)

    async def _bounded(tid: str) -> tuple[str, Optional[MarketMeta]]:
        async with sem:
            meta = await _fetch_one(tid)
            return tid, meta

    results = await asyncio.gather(*[_bounded(tid) for tid in missing])
    added = 0
    for tid, meta in results:
        if meta is not None:
            _cache[tid] = meta
            added += 1

    if added > 0:
        _save_cache()
        logger.info(f"Market cache warmed: {added} new entries")
