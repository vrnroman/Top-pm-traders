"""Gamma-API-backed geo market discovery.

Queries the Gamma `/events` endpoint (NOT `/markets` — that endpoint silently
ignores `tag_slug`) for each configured geo tag, paginates until exhausted,
then unwraps each event's sub-markets. Sub-markets whose `closed` flag is set
or whose `active` flag is false are dropped. The result is a deduped,
tag-unioned cache keyed by condition_id.

Other modules consult this cache to decide whether a given trade belongs to a
geopolitical market via its condition_id.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from src.copy_trading.strategy_config import TIER_1C
from src.logger import logger
from src.utils import error_message

_GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"

_PAGE_SIZE = 500
_MAX_PAGES_PER_TAG = 10  # safety stop: 5,000 events per tag
_PER_TAG_DELAY_S = 0.1   # tiny pacing between pagination pages


@dataclass
class GeoMarket:
    condition_id: str
    slug: str
    title: str
    tags: list[str] = field(default_factory=list)
    clob_token_ids: list[str] = field(default_factory=list)
    end_ts: int = 0              # epoch seconds of market resolution, or 0 if unknown
    liquidity_usd: float = 0.0   # Gamma-reported book depth in USD, 0 if unknown
    volume_1w_usd: float = 0.0   # last-7-day CLOB volume, 0 if unknown
    best_bid: float = 0.0        # last-known top of book, 0 if unknown
    best_ask: float = 0.0
    last_price: float = 0.0      # Gamma's lastTradePrice, 0 if unknown


_by_cid: dict[str, GeoMarket] = {}
_by_slug: dict[str, GeoMarket] = {}
_last_refresh_ts: float = 0.0
_refresh_lock = asyncio.Lock()


def is_geo_market_cid(condition_id: str) -> bool:
    return bool(condition_id) and condition_id.lower() in _by_cid


def is_geo_market_slug(slug: str) -> bool:
    return bool(slug) and slug in _by_slug


def get_geo_market(condition_id: str) -> Optional[GeoMarket]:
    return _by_cid.get((condition_id or "").lower())


def geo_markets_snapshot() -> list[GeoMarket]:
    return list(_by_cid.values())


def last_refresh_ts() -> float:
    return _last_refresh_ts


def _extract_token_ids(m: dict) -> list[str]:
    raw = m.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            import json
            parsed = json.loads(raw)
            return [str(t) for t in parsed] if isinstance(parsed, list) else []
        except Exception:
            return []
    if isinstance(raw, list):
        return [str(t) for t in raw]
    return []


def _extract_event_tag_slugs(ev: dict) -> list[str]:
    tags = ev.get("tags")
    if not isinstance(tags, list):
        return []
    out: list[str] = []
    for t in tags:
        if isinstance(t, dict):
            s = t.get("slug") or ""
            if s:
                out.append(s)
    return out


def _parse_iso_to_epoch(s: Optional[str]) -> int:
    if not s:
        return 0
    from datetime import datetime
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def _parse_float(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _event_to_geo_markets(ev: dict) -> list[GeoMarket]:
    """Flatten one event into its open, active sub-markets.

    Events can contain a mix of closed and open sub-markets — a "when will X
    happen" event typically has one open market per time horizon. We keep only
    the ones that are still tradable.
    """
    ev_tags = _extract_event_tag_slugs(ev)
    out: list[GeoMarket] = []
    for m in ev.get("markets", []) or []:
        if m.get("closed"):
            continue
        if m.get("active") is False:
            continue
        cid = str(m.get("conditionId") or "").lower()
        if not cid:
            continue
        slug = str(m.get("slug") or "")
        end_ts = _parse_iso_to_epoch(m.get("endDate") or m.get("umaEndDate"))
        liquidity = _parse_float(m.get("liquidity") or m.get("liquidityClob") or m.get("liquidityNum"))
        vol_1w = _parse_float(m.get("volume1wkClob") or m.get("volume1wk"))
        best_bid = _parse_float(m.get("bestBid"))
        best_ask = _parse_float(m.get("bestAsk"))
        last_price = _parse_float(m.get("lastTradePrice"))
        out.append(
            GeoMarket(
                condition_id=cid,
                slug=slug,
                title=str(m.get("question") or m.get("title") or ""),
                tags=list(ev_tags),
                clob_token_ids=_extract_token_ids(m),
                end_ts=end_ts,
                liquidity_usd=liquidity,
                volume_1w_usd=vol_1w,
                best_bid=best_bid,
                best_ask=best_ask,
                last_price=last_price,
            )
        )
    return out


async def _fetch_tag_events(client: httpx.AsyncClient, tag_slug: str) -> list[dict]:
    """Fetch every active/open event for a tag, paginating until exhausted."""
    out: list[dict] = []
    offset = 0
    for page_idx in range(_MAX_PAGES_PER_TAG):
        params = {
            "closed": "false",
            "active": "true",
            "limit": str(_PAGE_SIZE),
            "offset": str(offset),
            "tag_slug": tag_slug,
        }
        try:
            resp = await client.get(_GAMMA_EVENTS_URL, params=params, timeout=15.0)
        except Exception as exc:
            logger.debug(f"[geo-scan] tag={tag_slug} offset={offset} err: {error_message(exc)}")
            break
        if resp.status_code != 200:
            logger.debug(f"[geo-scan] tag={tag_slug} offset={offset} HTTP {resp.status_code}")
            break
        try:
            items = resp.json()
        except Exception:
            break
        if not isinstance(items, list) or not items:
            break
        out.extend(items)
        if len(items) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
        await asyncio.sleep(_PER_TAG_DELAY_S)
    return out


async def refresh_geo_markets() -> int:
    """Rebuild the geo market cache from Gamma. Returns the new cache size."""
    global _last_refresh_ts

    tags = TIER_1C.geo_tags or []
    if not tags:
        logger.warn("[geo-scan] no geo tags configured")
        return 0

    async with _refresh_lock:
        per_tag_counts: dict[str, int] = {}
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                fetched = await asyncio.gather(
                    *[_fetch_tag_events(client, t) for t in tags],
                    return_exceptions=True,
                )
        except Exception as exc:
            logger.warn(f"[geo-scan] refresh failed: {error_message(exc)}")
            return len(_by_cid)

        new_by_cid: dict[str, GeoMarket] = {}
        new_by_slug: dict[str, GeoMarket] = {}

        for tag, events in zip(tags, fetched):
            if isinstance(events, BaseException):
                logger.debug(f"[geo-scan] tag={tag} exception: {error_message(events)}")
                per_tag_counts[tag] = 0
                continue
            per_tag_counts[tag] = len(events)
            for ev in events:
                for gm in _event_to_geo_markets(ev):
                    existing = new_by_cid.get(gm.condition_id)
                    if existing is None:
                        new_by_cid[gm.condition_id] = gm
                        if gm.slug:
                            new_by_slug[gm.slug] = gm
                    else:
                        # Same market reached via multiple tags — union the tag set.
                        merged = sorted(set(existing.tags) | set(gm.tags))
                        existing.tags = merged

        _by_cid.clear()
        _by_cid.update(new_by_cid)
        _by_slug.clear()
        _by_slug.update(new_by_slug)
        _last_refresh_ts = time.time()

    summary = ", ".join(f"{t}={per_tag_counts.get(t, 0)}" for t in tags)
    logger.info(
        f"[geo-scan] loaded {len(_by_cid)} unique geo markets "
        f"(events per tag: {summary})"
    )
    return len(_by_cid)


async def run_geo_market_scanner() -> None:
    """Background loop: periodically refresh the geo market cache."""
    interval = max(60.0, TIER_1C.market_scan_interval_s)
    try:
        if not _by_cid:
            await refresh_geo_markets()
    except Exception as exc:
        logger.warn(f"[geo-scan] initial refresh failed: {error_message(exc)}")

    while True:
        await asyncio.sleep(interval)
        try:
            await refresh_geo_markets()
        except Exception as exc:
            logger.warn(f"[geo-scan] refresh failed: {error_message(exc)}")
