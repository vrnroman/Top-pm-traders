"""Tests for the priority-queue scheduler in market_activity_poller.py.

Covers:
  - compute_poll_interval for each tier (imminent / near / warm / cool / cold)
  - volume boost tier upgrades
  - liquidity floor penalty
  - _sync_schedule add + remove reconciliation
  - _pop_due tombstone filtering
"""

import asyncio
import time

import pytest

from src.copy_trading import geo_market_scanner as scanner
from src.copy_trading import market_activity_poller as mp
from src.copy_trading.geo_market_scanner import GeoMarket


def _mk_market(
    cid: str,
    hours_to_close: float = 1000.0,
    volume_1w: float = 0.0,
    liquidity: float = 10_000.0,
) -> GeoMarket:
    return GeoMarket(
        condition_id=cid,
        slug=cid,
        title="test",
        tags=["geopolitics"],
        end_ts=int(time.time() + hours_to_close * 3600),
        liquidity_usd=liquidity,
        volume_1w_usd=volume_1w,
    )


@pytest.fixture(autouse=True)
def _clean_state():
    scanner._by_cid.clear()
    scanner._by_slug.clear()
    mp._reset_scheduler_state()
    yield
    scanner._by_cid.clear()
    scanner._by_slug.clear()
    mp._reset_scheduler_state()


class TestComputePollInterval:
    def test_imminent_tier(self):
        m = _mk_market("cid-a", hours_to_close=3)
        assert mp.compute_poll_interval(m) == mp._TIER_IMMINENT_S

    def test_near_tier(self):
        m = _mk_market("cid-b", hours_to_close=12)
        assert mp.compute_poll_interval(m) == mp._TIER_NEAR_S

    def test_warm_tier(self):
        m = _mk_market("cid-c", hours_to_close=48)
        assert mp.compute_poll_interval(m) == mp._TIER_WARM_S

    def test_cool_tier(self):
        m = _mk_market("cid-d", hours_to_close=24 * 5)
        assert mp.compute_poll_interval(m) == mp._TIER_COOL_S

    def test_cold_tier(self):
        m = _mk_market("cid-e", hours_to_close=24 * 30)
        assert mp.compute_poll_interval(m) == mp._TIER_COLD_S

    def test_unknown_end_ts_is_cold(self):
        m = GeoMarket(
            condition_id="cid-f",
            slug="cid-f",
            title="no end",
            end_ts=0,
            liquidity_usd=10_000,
        )
        assert mp.compute_poll_interval(m) == mp._TIER_COLD_S

    def test_past_close_is_cold(self):
        m = _mk_market("cid-past", hours_to_close=-5)
        assert mp.compute_poll_interval(m) == mp._TIER_COLD_S

    def test_hot_volume_boosts_two_tiers(self):
        # Cool baseline (5 days) → volume 200k should boost by 2 → near (60s)
        m = _mk_market("cid-hot", hours_to_close=24 * 5, volume_1w=200_000)
        assert mp.compute_poll_interval(m) == mp._TIER_NEAR_S

    def test_warm_volume_boosts_one_tier(self):
        # Warm baseline (48h) → volume 15k should boost by 1 → near
        m = _mk_market("cid-wb", hours_to_close=48, volume_1w=15_000)
        assert mp.compute_poll_interval(m) == mp._TIER_NEAR_S

    def test_boost_does_not_go_below_imminent(self):
        m = _mk_market("cid-ultra", hours_to_close=3, volume_1w=500_000)
        assert mp.compute_poll_interval(m) == mp._TIER_IMMINENT_S

    def test_dead_liquidity_demotes_to_cold(self):
        # Near baseline (12h) with $5 liquidity → floor to cold
        m = _mk_market("cid-dead", hours_to_close=12, liquidity=5)
        assert mp.compute_poll_interval(m) == mp._TIER_COLD_S


class TestScheduleSync:
    def test_sync_adds_new_markets(self):
        m1 = _mk_market("0xaaa")
        m2 = _mk_market("0xbbb")
        scanner._by_cid[m1.condition_id] = m1
        scanner._by_cid[m2.condition_id] = m2

        asyncio.run(mp._sync_schedule())

        assert len(mp._tracked_cids) == 2
        assert "0xaaa" in mp._tracked_cids
        assert "0xbbb" in mp._tracked_cids
        assert len(mp._heap) == 2

    def test_sync_removes_dropped_markets(self):
        m = _mk_market("0xaaa")
        scanner._by_cid[m.condition_id] = m
        asyncio.run(mp._sync_schedule())
        assert "0xaaa" in mp._tracked_cids

        scanner._by_cid.clear()
        asyncio.run(mp._sync_schedule())
        assert "0xaaa" not in mp._tracked_cids

    def test_sync_idempotent(self):
        m = _mk_market("0xabc")
        scanner._by_cid[m.condition_id] = m
        asyncio.run(mp._sync_schedule())
        size_1 = len(mp._heap)
        asyncio.run(mp._sync_schedule())
        size_2 = len(mp._heap)
        assert size_1 == size_2 == 1


class TestPopDue:
    def test_returns_due_head(self):
        now = time.time()

        async def run():
            await mp._push(now - 10, "0xaaa")
            await mp._push(now + 100, "0xbbb")
            return await mp._pop_due(now)

        cid = asyncio.run(run())
        assert cid == "0xaaa"

    def test_skips_tombstoned_entry(self):
        now = time.time()

        async def run():
            await mp._push(now - 10, "0xaaa")  # due but tombstoned
            await mp._push(now - 5, "0xbbb")   # due and live
            mp._tracked_cids.discard("0xaaa")  # tombstone
            return await mp._pop_due(now)

        cid = asyncio.run(run())
        assert cid == "0xbbb"

    def test_returns_none_when_head_in_future(self):
        now = time.time()

        async def run():
            await mp._push(now + 100, "0xaaa")
            return await mp._pop_due(now)

        result = asyncio.run(run())
        assert result is None
