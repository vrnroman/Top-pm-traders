"""Geo-market activity poller for Strategy 1c pattern detection.

Iterates the geo-market cache from geo_market_scanner and polls
`data-api.polymarket.com/trades?market=<conditionId>` for each, feeding the
resulting DetectedTrade objects directly into analyze_trade_for_patterns.

This path is independent of the 1a/1b watchlist — it discovers *unknown*
wallets betting on geo markets, which is the whole point of Strategy 1c.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Optional

import httpx

from src.config import CONFIG
from src.copy_trading.geo_market_scanner import (
    GeoMarket,
    geo_markets_snapshot,
    get_geo_market,
)
from src.copy_trading.strategy_config import TIER_1C
from src.logger import logger
from src.models import DetectedTrade
from src.utils import error_message

_TRADES_URL_PATH = "/trades"

# Maximum age of a trade we're willing to analyze as "realtime". Anything older
# is skipped at the intake layer — this prevents the bot from firing pattern
# alerts on a backlog of stale trades when a market is first discovered (cold
# start or new market added by the scanner refresh).
_MAX_TRADE_AGE_S = 3600

# Per-market cursor: condition_id -> latest seen epoch seconds.
_cursor: dict[str, int] = {}

# Trade ID LRU to avoid re-analyzing the same fill twice (e.g. when a wallet
# trades across multiple markets or when a trade straddles our cursor overlap).
_SEEN_MAX = 5000
_seen: "OrderedDict[str, None]" = OrderedDict()


def _mark_seen(trade_id: str) -> bool:
    if trade_id in _seen:
        return True
    _seen[trade_id] = None
    while len(_seen) > _SEEN_MAX:
        _seen.popitem(last=False)
    return False


def _canonical_trade_id(tx_hash: str, token_id: str, side: str) -> str:
    return f"{tx_hash}-{token_id}-{side}"


def _item_to_detected_trade(item: dict) -> Optional[DetectedTrade]:
    """Map a /trades response item to a DetectedTrade, or None if invalid."""
    trader = (item.get("proxyWallet") or item.get("user") or "").strip()
    token_id = str(item.get("asset") or "")
    side = str(item.get("side") or "").upper()
    if not trader or not token_id or side not in ("BUY", "SELL"):
        return None

    try:
        size_tokens = float(item.get("size", 0))
        price = float(item.get("price", 0))
    except (TypeError, ValueError):
        return None
    if size_tokens <= 0 or price <= 0:
        return None

    usdc_size = size_tokens * price

    ts_raw = item.get("timestamp", 0)
    try:
        ts_int = int(ts_raw)
    except (TypeError, ValueError):
        ts_int = 0
    if ts_int > 1e12:  # milliseconds
        ts_int //= 1000
    if ts_int <= 0:
        ts_int = int(time.time())
    ts_iso = datetime.fromtimestamp(ts_int, tz=timezone.utc).isoformat()

    tx_hash = str(item.get("transactionHash") or "")
    trade_id = _canonical_trade_id(tx_hash, token_id, side)

    return DetectedTrade(
        id=trade_id,
        trader_address=trader,
        timestamp=ts_iso,
        market=str(item.get("title") or ""),
        condition_id=str(item.get("conditionId") or ""),
        token_id=token_id,
        side=side,  # type: ignore[arg-type]
        size=usdc_size,
        price=price,
        outcome=str(item.get("outcome") or ""),
    )


async def _poll_market(client: httpx.AsyncClient, market: GeoMarket) -> int:
    """Poll one geo market, analyzing any new trades. Returns trades analyzed."""
    if not market.condition_id:
        return 0

    # takerOnly=false is critical: with true, Polymarket's /trades endpoint hides
    # every maker-side fill. Insiders who limit-order into a market ARE the
    # maker — they'd be invisible. We pull both sides and let the pattern
    # detector's trade_id ({txHash}-{tokenId}-{side}) dedupe per row.
    # Page size doubled from 100 → 200 because each fill now consumes 2 rows
    # (one per side), so we need more headroom to cover the same fill volume.
    params: dict[str, str] = {
        "market": market.condition_id,
        "limit": "200",
        "takerOnly": "false",
    }

    try:
        resp = await client.get(
            f"{CONFIG.data_api_url}{_TRADES_URL_PATH}", params=params, timeout=10.0
        )
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "2") or 2)
            await asyncio.sleep(min(retry_after, 10))
            return 0
        if resp.status_code != 200:
            logger.debug(f"[market-poll] {market.slug[:40]} HTTP {resp.status_code}")
            return 0
        items = resp.json()
    except Exception as exc:
        logger.debug(f"[market-poll] {market.slug[:40]} error: {error_message(exc)}")
        return 0

    if not isinstance(items, list):
        return 0

    # Lazy import to avoid a circular edge via pattern_detector → strategy_config.
    from src.copy_trading.pattern_detector import analyze_trade_for_patterns

    cursor_before = _cursor.get(market.condition_id, 0)
    first_poll = cursor_before == 0
    newest_ts = cursor_before
    analyzed = 0
    now = int(time.time())
    fresh_cutoff = now - _MAX_TRADE_AGE_S

    for item in items:
        try:
            ts_raw = int(item.get("timestamp", 0))
        except (TypeError, ValueError):
            ts_raw = 0
        if ts_raw > 1e12:
            ts_raw //= 1000
        if ts_raw <= 0:
            continue

        # Advance the cursor off every row we see, not just the ones we
        # analyze — otherwise a market with only stale trades would be
        # re-walked from scratch on every poll.
        if ts_raw > newest_ts:
            newest_ts = ts_raw

        # Skip anything we've already processed (cursor overlap of 10s for safety).
        if cursor_before and ts_raw <= cursor_before - 10:
            continue

        # Hard freshness gate: never analyze a trade older than the cluster
        # window. This makes "realtime" mean what it says — no stale trades
        # from before the bot started can trigger pattern alerts.
        if ts_raw < fresh_cutoff:
            continue

        # First-poll baseline: on the very first poll of a market, we just
        # establish the cursor without analyzing anything. Subsequent polls
        # will only see trades that arrive *after* the baseline, which is
        # the actual definition of "new activity on this market".
        if first_poll:
            continue

        trade = _item_to_detected_trade(item)
        if trade is None:
            continue
        if _mark_seen(trade.id):
            continue

        try:
            await analyze_trade_for_patterns(trade, trade_ts=ts_raw)
            analyzed += 1
        except Exception as exc:
            logger.debug(f"[market-poll] analyze error: {error_message(exc)}")

    if newest_ts > cursor_before:
        _cursor[market.condition_id] = newest_ts

    return analyzed


# ---------------------------------------------------------------------------
# Priority scheduler — hot markets get polled more often than cold ones
# ---------------------------------------------------------------------------

import heapq
from typing import Optional as _Optional

# Tier base intervals (seconds) by time-to-resolution.
# Not configurable right now; if tuning is needed, promote to env vars.
_TIER_IMMINENT_S = 15.0   # closes in < 6h
_TIER_NEAR_S = 60.0       # closes in < 24h
_TIER_WARM_S = 300.0      # closes in < 72h
_TIER_COOL_S = 1200.0     # closes in < 14 days
_TIER_COLD_S = 3600.0     # everything else (including unknown end_ts)

# Volume boost: busy markets are upgraded a tier or two toward faster polling.
_BOOST_HOT_VOLUME_1W_USD = 100_000.0   # upgrade by 2 tiers
_BOOST_WARM_VOLUME_1W_USD = 10_000.0   # upgrade by 1 tier

# Safety floor: never poll any market faster than this.
_MIN_POLL_INTERVAL_S = 10.0

# Scheduler constants
_SYNC_INTERVAL_S = 60.0         # how often we reconcile heap against scanner cache
_SCHEDULER_WORKER_COUNT = 5     # concurrent poll workers
_SCHEDULER_QUEUE_CAPACITY = 20  # bound the hand-off queue
_TELEMETRY_INTERVAL_S = 300.0   # log scheduler heartbeat every 5 minutes

_TIER_LADDER = (
    _TIER_IMMINENT_S,
    _TIER_NEAR_S,
    _TIER_WARM_S,
    _TIER_COOL_S,
    _TIER_COLD_S,
)


def _base_tier_index(market: GeoMarket, now: float) -> int:
    """Return the tier index based on time-to-resolution alone.

    0 = imminent (<6h), 1 = near (<24h), 2 = warm (<72h),
    3 = cool (<14d), 4 = cold (else / unknown end_ts).
    """
    if market.end_ts <= 0:
        return 4
    hours_to_close = (market.end_ts - now) / 3600.0
    if hours_to_close <= 0:
        return 4  # already past close — poll slowly, it'll be removed soon
    if hours_to_close < 6:
        return 0
    if hours_to_close < 24:
        return 1
    if hours_to_close < 72:
        return 2
    if hours_to_close < 24 * 14:
        return 3
    return 4


def compute_poll_interval(market: GeoMarket, now: _Optional[float] = None) -> float:
    """Return the next-poll interval (seconds) for this market.

    Derived from (time-to-close tier) with a volume boost and a liquidity
    penalty. Pure function — no module state — so it is trivially testable.
    """
    t = now if now is not None else time.time()
    tier = _base_tier_index(market, t)

    # Volume boost (upgrade toward faster polling)
    if market.volume_1w_usd >= _BOOST_HOT_VOLUME_1W_USD:
        tier = max(0, tier - 2)
    elif market.volume_1w_usd >= _BOOST_WARM_VOLUME_1W_USD:
        tier = max(0, tier - 1)

    # Liquidity floor — markets with effectively dead books deserve nothing better
    # than cold-tier even if they carry a long weekly volume number from stale fills.
    if market.liquidity_usd > 0 and market.liquidity_usd < 100.0:
        tier = max(tier, 4)

    return max(_MIN_POLL_INTERVAL_S, _TIER_LADDER[tier])


# Heap entries are (next_poll_at, condition_id). Ties on timestamp break by cid.
_heap: list[tuple[float, str]] = []
_tracked_cids: set[str] = set()
_heap_lock = asyncio.Lock()

# Scheduler telemetry
_sched_polls = 0
_sched_analyzed = 0
_sched_errors = 0
_sched_started_at = 0.0


async def _push(next_ts: float, cid: str) -> None:
    async with _heap_lock:
        heapq.heappush(_heap, (next_ts, cid))
        _tracked_cids.add(cid)


async def _pop_due(now: float) -> _Optional[str]:
    """Pop the earliest due entry. Returns None if the heap head is in the future."""
    async with _heap_lock:
        while _heap:
            next_ts, cid = _heap[0]
            if cid not in _tracked_cids:
                heapq.heappop(_heap)
                continue
            if next_ts > now:
                return None
            heapq.heappop(_heap)
            return cid
        return None


async def _peek_next_ts() -> _Optional[float]:
    async with _heap_lock:
        while _heap:
            next_ts, cid = _heap[0]
            if cid not in _tracked_cids:
                heapq.heappop(_heap)
                continue
            return next_ts
        return None


async def _sync_schedule() -> None:
    """Reconcile heap against the current scanner cache.

    New markets get scheduled for an immediate first poll. Markets dropped from
    the scanner cache are tombstoned via the _tracked_cids set so they get
    filtered on pop instead of forcing a heap rebuild.
    """
    markets = geo_markets_snapshot()
    current = {m.condition_id for m in markets if m.condition_id}
    now = time.time()

    async with _heap_lock:
        added = 0
        for cid in current - _tracked_cids:
            heapq.heappush(_heap, (now, cid))
            _tracked_cids.add(cid)
            added += 1
        removed = _tracked_cids - current
        if removed:
            _tracked_cids.difference_update(removed)

    if added or removed:
        logger.info(
            f"[scheduler] sync: +{added} -{len(removed)}  tracked={len(_tracked_cids)}"
        )


async def _worker(
    worker_id: int,
    queue: "asyncio.Queue[GeoMarket]",
    client: httpx.AsyncClient,
) -> None:
    """Worker coroutine: consumes GeoMarket entries from the queue, polls each,
    and reschedules based on the current priority score."""
    global _sched_polls, _sched_analyzed, _sched_errors

    while True:
        market = await queue.get()
        try:
            analyzed = await _poll_market(client, market)
            _sched_polls += 1
            _sched_analyzed += analyzed
        except Exception as exc:
            _sched_errors += 1
            logger.debug(
                f"[scheduler/w{worker_id}] poll err cid={market.condition_id[:12]}: "
                f"{error_message(exc)}"
            )
        finally:
            queue.task_done()

        # Reschedule at whatever the *current* priority says, in case the
        # market's time-to-close, volume, or liquidity shifted since last poll.
        current = get_geo_market(market.condition_id) or market
        interval = compute_poll_interval(current)
        await _push(time.time() + interval, market.condition_id)


async def run_market_activity_poller() -> None:
    """Priority-queue scheduler: polls hot markets faster than cold ones.

    Replaces the old flat "iterate every market on a fixed interval" loop.
    The heap head is always the most-urgent market; workers pull via a bounded
    queue so we never accumulate unlimited task objects even under slow polls.
    """
    global _sched_started_at, _sched_polls, _sched_analyzed, _sched_errors
    _sched_started_at = time.time()
    _sched_polls = 0
    _sched_analyzed = 0
    _sched_errors = 0

    queue: "asyncio.Queue[GeoMarket]" = asyncio.Queue(maxsize=_SCHEDULER_QUEUE_CAPACITY)

    async with httpx.AsyncClient(timeout=12.0) as client:
        workers = [
            asyncio.create_task(_worker(i, queue, client))
            for i in range(_SCHEDULER_WORKER_COUNT)
        ]

        last_sync = 0.0
        last_telemetry = time.time()

        try:
            while True:
                now = time.time()

                # Periodic reconcile against scanner cache
                if now - last_sync >= _SYNC_INTERVAL_S:
                    try:
                        await _sync_schedule()
                    except Exception as exc:
                        logger.warn(f"[scheduler] sync err: {error_message(exc)}")
                    last_sync = now

                # Heartbeat log so operators can see the queue is alive
                if now - last_telemetry >= _TELEMETRY_INTERVAL_S:
                    uptime = now - _sched_started_at
                    rate = _sched_polls / uptime if uptime > 0 else 0.0
                    logger.info(
                        f"[scheduler] heartbeat: polls={_sched_polls} "
                        f"analyzed={_sched_analyzed} errors={_sched_errors} "
                        f"rate={rate:.1f}/s tracked={len(_tracked_cids)} "
                        f"queue={queue.qsize()}"
                    )
                    last_telemetry = now

                # What is the next due market?
                cid = await _pop_due(now)
                if cid is None:
                    # Either the heap is empty or the head is in the future.
                    next_ts = await _peek_next_ts()
                    if next_ts is None:
                        # No markets tracked yet — wait briefly and re-sync.
                        await asyncio.sleep(1.0)
                        continue
                    wait = max(0.0, next_ts - time.time())
                    # Never sleep past the next sync tick, so new markets land quickly.
                    max_wait = max(0.5, _SYNC_INTERVAL_S - (time.time() - last_sync))
                    await asyncio.sleep(min(wait, max_wait))
                    continue

                market = get_geo_market(cid)
                if market is None:
                    # Dropped from scanner cache between pop and lookup.
                    continue

                # put() blocks when the queue is full, which naturally throttles
                # the scheduler so we don't over-commit when all workers are busy.
                await queue.put(market)
        finally:
            for w in workers:
                w.cancel()


def _reset_scheduler_state() -> None:
    """Test helper — clears the heap and tracking set."""
    _heap.clear()
    _tracked_cids.clear()
