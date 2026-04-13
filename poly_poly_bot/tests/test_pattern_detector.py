"""Tests for Strategy 1c pattern detector."""

import time
from unittest.mock import patch, AsyncMock

import pytest

from src.models import DetectedTrade
from src.copy_trading.pattern_detector import (
    is_geopolitical_market,
    _check_new_account_geo,
    _check_first_ever_bet_geo,
    _check_dormant_reactivation_from_history,
    _check_same_funder_cluster,
    _check_late_geo_bet,
    _check_thin_market_dominance,
    _reference_price_for_market,
    _find_cluster,
    _add_recent_bet,
    _get_or_create_wallet,
    _update_wallet_activity,
    _reset_pattern_detector,
    _extract_tx_hash,
    RecentBet,
    WalletActivity,
)
from src.copy_trading.strategy_config import TIER_1C


@pytest.fixture(autouse=True)
def reset_detector():
    """Reset all module-level state before each test."""
    _reset_pattern_detector()
    yield
    _reset_pattern_detector()


def _make_trade(
    market: str = "Will Russia invade Ukraine?",
    side: str = "BUY",
    size: float = 10000.0,
    price: float = 0.50,
    trader: str = "0x" + "a" * 40,
) -> DetectedTrade:
    return DetectedTrade(
        id="trade-1",
        trader_address=trader,
        timestamp="2026-04-08T12:00:00Z",
        market=market,
        side=side,
        size=size,
        price=price,
    )


class TestIsGeopoliticalMarket:
    def test_detects_war(self):
        assert is_geopolitical_market("Will war break out in 2026?") is True

    def test_detects_invasion(self):
        assert is_geopolitical_market("Russia invasion of Ukraine") is True

    def test_detects_nuclear(self):
        assert is_geopolitical_market("Nuclear weapons usage in conflict") is True

    def test_detects_election(self):
        assert is_geopolitical_market("US Presidential Election 2028") is True

    def test_detects_sanctions(self):
        assert is_geopolitical_market("New sanctions on Iran") is True

    def test_detects_china(self):
        assert is_geopolitical_market("Will China invade Taiwan?") is True

    def test_case_insensitive(self):
        assert is_geopolitical_market("NUCLEAR MISSILE LAUNCH") is True

    def test_rejects_non_geo(self):
        assert is_geopolitical_market("Will Bitcoin hit $100k?") is False

    def test_rejects_sports(self):
        assert is_geopolitical_market("Super Bowl 2027 winner") is False

    def test_rejects_weather(self):
        assert is_geopolitical_market("NYC temperature above 80F?") is False


class TestNewAccountLargeGeoAlert:
    def test_new_account_large_geo_bet(self):
        trader = "0x" + "b" * 40
        wa = _get_or_create_wallet(trader)

        trade = _make_trade(
            market="Will Russia use nuclear weapons?",
            size=10000.0,
            trader=trader,
        )

        alert = _check_new_account_geo(trade, wa, age_days=1.0)
        assert alert is not None
        assert alert.pattern == "new_account_geo"
        assert alert.severity == "high"

    def test_old_account_no_alert(self):
        trader = "0x" + "c" * 40
        wa = _get_or_create_wallet(trader)

        trade = _make_trade(
            market="Will Russia use nuclear weapons?",
            size=10000.0,
            trader=trader,
        )
        alert = _check_new_account_geo(trade, wa, age_days=60.0)
        assert alert is None

    def test_small_bet_no_alert(self):
        trader = "0x" + "d" * 40
        wa = _get_or_create_wallet(trader)

        trade = _make_trade(
            market="Will Russia use nuclear weapons?",
            size=100.0,
            trader=trader,
        )
        alert = _check_new_account_geo(trade, wa, age_days=1.0)
        assert alert is None

    def test_unknown_age_no_alert(self):
        trader = "0x" + "e" * 40
        wa = _get_or_create_wallet(trader)
        trade = _make_trade(
            market="Will Russia use nuclear weapons?",
            size=10000.0,
            trader=trader,
        )
        # Unknown age → fail closed, no alert
        alert = _check_new_account_geo(trade, wa, age_days=None)
        assert alert is None

    def test_scalper_with_many_observed_trades_no_alert(self):
        trader = "0x" + "f" * 40
        wa = _get_or_create_wallet(trader)
        wa.trade_count = TIER_1C.max_lifetime_trades_for_new + 1
        trade = _make_trade(
            market="Will Russia use nuclear weapons?",
            size=10000.0,
            trader=trader,
        )
        alert = _check_new_account_geo(trade, wa, age_days=1.0)
        assert alert is None

    def test_polymarket_count_gate_overrides_in_memory(self):
        trader = "0x" + "1" * 40
        wa = _get_or_create_wallet(trader)
        # Bot-observed count is zero, but Polymarket says this wallet has
        # placed many fills — we should NOT fire the new_account pattern.
        wa.trade_count = 0
        trade = _make_trade(
            market="Will Russia use nuclear weapons?",
            size=10000.0,
            trader=trader,
        )
        alert = _check_new_account_geo(
            trade, wa,
            age_days=1.0,
            polymarket_trade_count=TIER_1C.max_lifetime_trades_for_new + 20,
        )
        assert alert is None

    def test_polymarket_count_truncated_overrides(self):
        trader = "0x" + "2" * 40
        wa = _get_or_create_wallet(trader)
        trade = _make_trade(size=10000.0, trader=trader)
        alert = _check_new_account_geo(
            trade, wa,
            age_days=1.0,
            polymarket_trade_count=50,
            polymarket_count_truncated=True,
        )
        assert alert is None


class TestFirstEverBetGeo:
    def test_fires_when_no_prior_and_count_is_one(self):
        trader = "0x" + "3" * 40
        trade = _make_trade(
            market="Will Russia use nuclear weapons?",
            size=10000.0,
            trader=trader,
        )
        alert = _check_first_ever_bet_geo(
            trade, prior_trade_ts=None, polymarket_trade_count=1
        )
        assert alert is not None
        assert alert.pattern == "first_ever_bet_geo"
        assert alert.severity == "high"

    def test_fires_when_no_prior_and_count_is_zero(self):
        trader = "0x" + "4" * 40
        trade = _make_trade(size=10000.0, trader=trader)
        alert = _check_first_ever_bet_geo(
            trade, prior_trade_ts=None, polymarket_trade_count=0
        )
        assert alert is not None

    def test_does_not_fire_when_prior_exists(self):
        trader = "0x" + "5" * 40
        trade = _make_trade(size=10000.0, trader=trader)
        alert = _check_first_ever_bet_geo(
            trade, prior_trade_ts=int(time.time()) - 86400, polymarket_trade_count=5
        )
        assert alert is None

    def test_fail_closed_on_unknown_count(self):
        trader = "0x" + "6" * 40
        trade = _make_trade(size=10000.0, trader=trader)
        alert = _check_first_ever_bet_geo(
            trade, prior_trade_ts=None, polymarket_trade_count=None
        )
        assert alert is None

    def test_small_bet_no_alert(self):
        trader = "0x" + "7" * 40
        trade = _make_trade(size=100.0, trader=trader)
        alert = _check_first_ever_bet_geo(
            trade, prior_trade_ts=None, polymarket_trade_count=0
        )
        assert alert is None

    def test_non_geo_market_no_alert(self):
        trader = "0x" + "8" * 40
        trade = _make_trade(market="Bitcoin up or down today?", size=10000.0, trader=trader)
        alert = _check_first_ever_bet_geo(
            trade, prior_trade_ts=None, polymarket_trade_count=0
        )
        assert alert is None


class TestDormantReactivationFromHistory:
    def test_fires_on_long_gap(self):
        trader = "0x" + "9" * 40
        trade = _make_trade(size=10000.0, trader=trader)
        long_ago = int(time.time()) - int((TIER_1C.dormant_days + 5) * 86400)
        alert = _check_dormant_reactivation_from_history(trade, prior_trade_ts=long_ago)
        assert alert is not None
        assert alert.pattern == "dormant_reactivation"

    def test_does_not_fire_on_short_gap(self):
        trader = "0x" + "a" * 40
        trade = _make_trade(size=10000.0, trader=trader)
        recent = int(time.time()) - 3600  # 1h ago
        alert = _check_dormant_reactivation_from_history(trade, prior_trade_ts=recent)
        assert alert is None

    def test_does_not_fire_on_no_prior(self):
        trader = "0x" + "b" * 40
        trade = _make_trade(size=10000.0, trader=trader)
        # No prior → first-ever-bet territory, not dormant
        alert = _check_dormant_reactivation_from_history(trade, prior_trade_ts=None)
        assert alert is None


class TestSameFunderCluster:
    def test_fires_on_three_wallets_sharing_funder(self):
        funder = "0x" + "f" * 40
        market = "Will Russia use nuclear weapons?"
        _add_recent_bet("0x" + "1" * 40, market, "BUY", 5000.0, funder=funder)
        _add_recent_bet("0x" + "2" * 40, market, "BUY", 6000.0, funder=funder)

        current = _make_trade(market=market, size=7000.0, trader="0x" + "3" * 40)
        alert = _check_same_funder_cluster(current, current_funder=funder)
        assert alert is not None
        assert alert.pattern == "same_funder_cluster"

    def test_skips_when_current_funder_unknown(self):
        market = "Will Russia use nuclear weapons?"
        _add_recent_bet("0x" + "1" * 40, market, "BUY", 5000.0, funder="0x" + "f" * 40)
        current = _make_trade(market=market, size=7000.0, trader="0x" + "3" * 40)
        alert = _check_same_funder_cluster(current, current_funder="")
        assert alert is None

    def test_different_funders_no_alert(self):
        market = "Will Russia use nuclear weapons?"
        _add_recent_bet("0x" + "1" * 40, market, "BUY", 5000.0, funder="0x" + "a" * 40)
        _add_recent_bet("0x" + "2" * 40, market, "BUY", 6000.0, funder="0x" + "b" * 40)
        current = _make_trade(market=market, size=7000.0, trader="0x" + "3" * 40)
        alert = _check_same_funder_cluster(current, current_funder="0x" + "c" * 40)
        assert alert is None

    def test_small_bets_ignored(self):
        funder = "0x" + "f" * 40
        market = "Will Russia use nuclear weapons?"
        _add_recent_bet("0x" + "1" * 40, market, "BUY", 10.0, funder=funder)
        _add_recent_bet("0x" + "2" * 40, market, "BUY", 20.0, funder=funder)
        current = _make_trade(market=market, size=5000.0, trader="0x" + "3" * 40)
        alert = _check_same_funder_cluster(current, current_funder=funder)
        assert alert is None

    def test_current_trade_size_gate(self):
        funder = "0x" + "f" * 40
        market = "Will Russia use nuclear weapons?"
        _add_recent_bet("0x" + "1" * 40, market, "BUY", 5000.0, funder=funder)
        _add_recent_bet("0x" + "2" * 40, market, "BUY", 6000.0, funder=funder)
        current = _make_trade(market=market, size=10.0, trader="0x" + "3" * 40)
        alert = _check_same_funder_cluster(current, current_funder=funder)
        assert alert is None


class TestLateGeoBet:
    """Uses geo_market_scanner module state — must seed it in the test."""

    def test_fires_within_close_proximity(self):
        from src.copy_trading import geo_market_scanner as gm
        cid = "0x" + "a" * 64
        gm._by_cid[cid] = gm.GeoMarket(
            condition_id=cid,
            slug="test-market",
            title="Will Iran strike Israel in the next 12 hours?",
            tags=["geopolitics"],
            end_ts=int(time.time()) + 3600 * 6,  # 6h from now
        )
        try:
            from src.models import DetectedTrade
            trade = DetectedTrade(
                id="tx-tok-BUY",
                trader_address="0x" + "1" * 40,
                timestamp="2026-04-12T00:00:00Z",
                market="Will Iran strike Israel in the next 12 hours?",
                condition_id=cid,
                side="BUY",
                size=15_000.0,
                price=0.35,
            )
            alert = _check_late_geo_bet(trade)
            assert alert is not None
            assert alert.pattern == "late_geo_bet"
        finally:
            gm._by_cid.pop(cid, None)

    def test_does_not_fire_far_from_close(self):
        from src.copy_trading import geo_market_scanner as gm
        cid = "0x" + "b" * 64
        gm._by_cid[cid] = gm.GeoMarket(
            condition_id=cid,
            slug="far-market",
            title="Will Iran strike Israel by 2027?",
            tags=["geopolitics"],
            end_ts=int(time.time()) + 3600 * 24 * 60,  # 60 days
        )
        try:
            from src.models import DetectedTrade
            trade = DetectedTrade(
                id="tx-tok-BUY",
                trader_address="0x" + "1" * 40,
                timestamp="2026-04-12T00:00:00Z",
                market="Will Iran strike Israel by 2027?",
                condition_id=cid,
                side="BUY",
                size=15_000.0,
                price=0.35,
            )
            alert = _check_late_geo_bet(trade)
            assert alert is None
        finally:
            gm._by_cid.pop(cid, None)

    def test_small_bet_no_alert(self):
        from src.copy_trading import geo_market_scanner as gm
        cid = "0x" + "c" * 64
        gm._by_cid[cid] = gm.GeoMarket(
            condition_id=cid,
            slug="small-market",
            title="Will Iran strike Israel soon?",
            tags=["geopolitics"],
            end_ts=int(time.time()) + 3600 * 6,
        )
        try:
            from src.models import DetectedTrade
            trade = DetectedTrade(
                id="tx-tok-BUY",
                trader_address="0x" + "1" * 40,
                timestamp="2026-04-12T00:00:00Z",
                market="Will Iran strike Israel soon?",
                condition_id=cid,
                side="BUY",
                size=100.0,
                price=0.35,
            )
            alert = _check_late_geo_bet(trade)
            assert alert is None
        finally:
            gm._by_cid.pop(cid, None)

    def test_unknown_market_no_alert(self):
        from src.models import DetectedTrade
        trade = DetectedTrade(
            id="tx-tok-BUY",
            trader_address="0x" + "1" * 40,
            timestamp="2026-04-12T00:00:00Z",
            market="random non-geo market",
            condition_id="0x" + "0" * 64,
            side="BUY",
            size=15000.0,
            price=0.35,
        )
        alert = _check_late_geo_bet(trade)
        assert alert is None


class TestLateGeoBetEdgeDetection:
    def _seed_market(self, cid, close_hours, bid=0.0, ask=0.0, last=0.0):
        from src.copy_trading import geo_market_scanner as gm
        gm._by_cid[cid] = gm.GeoMarket(
            condition_id=cid,
            slug="edge-market",
            title="Will Iran strike Israel soon?",
            tags=["geopolitics"],
            end_ts=int(time.time()) + int(close_hours * 3600),
            best_bid=bid,
            best_ask=ask,
            last_price=last,
        )

    def _make_trade(self, cid, price, size=15_000.0, trader="0x" + "1" * 40):
        from src.models import DetectedTrade
        return DetectedTrade(
            id="tx-tok-BUY",
            trader_address=trader,
            timestamp="2026-04-12T00:00:00Z",
            market="Will Iran strike Israel soon?",
            condition_id=cid,
            side="BUY",
            size=size,
            price=price,
        )

    def test_alert_details_flag_edge_when_price_diverges(self):
        from src.copy_trading import geo_market_scanner as gm
        cid = "0x" + "e" * 64
        self._seed_market(cid, close_hours=6, bid=0.60, ask=0.62)
        try:
            trade = self._make_trade(cid, price=0.40)
            alert = _check_late_geo_bet(trade)
            assert alert is not None
            assert "EDGE" in alert.details
            assert "0.400" in alert.details or "0.4" in alert.details
        finally:
            gm._by_cid.pop(cid, None)

    def test_alert_details_no_edge_when_price_matches(self):
        from src.copy_trading import geo_market_scanner as gm
        cid = "0x" + "ee" * 32
        self._seed_market(cid, close_hours=6, bid=0.60, ask=0.62)
        try:
            trade = self._make_trade(cid, price=0.61)
            alert = _check_late_geo_bet(trade)
            assert alert is not None
            assert "EDGE" not in alert.details
        finally:
            gm._by_cid.pop(cid, None)

    def test_alert_fires_without_reference_price(self):
        """No bid/ask/last → base late-bet alert still fires."""
        from src.copy_trading import geo_market_scanner as gm
        cid = "0x" + "fa" * 32
        self._seed_market(cid, close_hours=6)  # all prices zero
        try:
            trade = self._make_trade(cid, price=0.40)
            alert = _check_late_geo_bet(trade)
            assert alert is not None
            assert "EDGE" not in alert.details
        finally:
            gm._by_cid.pop(cid, None)


class TestReferencePriceVWAP:
    def test_local_vwap_used_when_enough_samples(self):
        market = "Will Russia invade soon?"
        _add_recent_bet("0x" + "1" * 40, market, "BUY", 1000, price=0.30)
        _add_recent_bet("0x" + "2" * 40, market, "BUY", 1000, price=0.32)
        _add_recent_bet("0x" + "3" * 40, market, "BUY", 1000, price=0.31)
        _add_recent_bet("0x" + "4" * 40, market, "BUY", 1000, price=0.29)
        _add_recent_bet("0x" + "5" * 40, market, "BUY", 1000, price=0.30)
        from src.models import DetectedTrade
        trade = DetectedTrade(
            id="tx-tok-BUY",
            trader_address="0x" + "9" * 40,
            timestamp="2026-04-12T00:00:00Z",
            market=market,
            condition_id="0x" + "c" * 64,
            side="BUY",
            size=5000,
            price=0.50,
        )
        ref, source = _reference_price_for_market(trade, gm=None)
        assert source == "local-vwap"
        assert ref is not None and abs(ref - 0.304) < 0.001

    def test_falls_back_to_gamma_mid(self):
        from src.copy_trading.geo_market_scanner import GeoMarket
        gm = GeoMarket(
            condition_id="0x" + "d" * 64,
            slug="x",
            title="x",
            best_bid=0.40,
            best_ask=0.44,
        )
        from src.models import DetectedTrade
        trade = DetectedTrade(
            id="tx-tok-BUY",
            trader_address="0x" + "1" * 40,
            timestamp="2026-04-12T00:00:00Z",
            market="x",
            condition_id="0x" + "d" * 64,
            side="BUY",
            size=5000,
            price=0.50,
        )
        ref, source = _reference_price_for_market(trade, gm=gm)
        assert source == "gamma-mid"
        assert ref is not None and abs(ref - 0.42) < 1e-9


class TestThinMarketDominance:
    def _make_trade(self, cid, size, market="Will Russia use nuclear weapons?"):
        from src.models import DetectedTrade
        return DetectedTrade(
            id="tx-tok-BUY",
            trader_address="0x" + "1" * 40,
            timestamp="2026-04-12T00:00:00Z",
            market=market,
            condition_id=cid,
            side="BUY",
            size=size,
            price=0.30,
        )

    def test_fires_on_liquidity_domination(self):
        from src.copy_trading import geo_market_scanner as gm
        cid = "0x" + "11" * 32
        gm._by_cid[cid] = gm.GeoMarket(
            condition_id=cid,
            slug="thin",
            title="Will Russia use nuclear weapons?",
            tags=["geopolitics"],
            liquidity_usd=10_000.0,
        )
        try:
            trade = self._make_trade(cid, size=6_000.0)  # 60% of liquidity
            alert = _check_thin_market_dominance(trade, wallet_is_novel=True)
            assert alert is not None
            assert "liquidity" in alert.details.lower()
        finally:
            gm._by_cid.pop(cid, None)

    def test_fires_on_weekly_volume_ratio(self):
        from src.copy_trading import geo_market_scanner as gm
        cid = "0x" + "22" * 32
        gm._by_cid[cid] = gm.GeoMarket(
            condition_id=cid,
            slug="low-vol",
            title="Will Russia use nuclear weapons?",
            tags=["geopolitics"],
            volume_1w_usd=8_000.0,
        )
        try:
            trade = self._make_trade(cid, size=6_000.0)  # 75% of weekly vol
            alert = _check_thin_market_dominance(trade, wallet_is_novel=True)
            assert alert is not None
            assert "weekly" in alert.details.lower()
        finally:
            gm._by_cid.pop(cid, None)

    def test_does_not_fire_on_liquid_market(self):
        """Weekly volume above max_weekly_volume_for_thin_usd disqualifies the market."""
        from src.copy_trading import geo_market_scanner as gm
        cid = "0x" + "33" * 32
        gm._by_cid[cid] = gm.GeoMarket(
            condition_id=cid,
            slug="liquid",
            title="Will Russia use nuclear weapons?",
            tags=["geopolitics"],
            liquidity_usd=50_000.0,  # 30% book ratio if it fired
            volume_1w_usd=250_000.0,  # disqualifying weekly volume
        )
        try:
            trade = self._make_trade(cid, size=15_000.0)
            alert = _check_thin_market_dominance(trade, wallet_is_novel=True)
            assert alert is None
        finally:
            gm._by_cid.pop(cid, None)

    def test_novelty_gate_blocks_known_whale(self):
        from src.copy_trading import geo_market_scanner as gm
        cid = "0x" + "66" * 32
        gm._by_cid[cid] = gm.GeoMarket(
            condition_id=cid,
            slug="thin",
            title="Will Russia use nuclear weapons?",
            tags=["geopolitics"],
            liquidity_usd=10_000.0,
        )
        try:
            trade = self._make_trade(cid, size=6_000.0)
            alert = _check_thin_market_dominance(trade, wallet_is_novel=False)
            assert alert is None
        finally:
            gm._by_cid.pop(cid, None)

    def test_small_bet_ignored(self):
        from src.copy_trading import geo_market_scanner as gm
        cid = "0x" + "44" * 32
        gm._by_cid[cid] = gm.GeoMarket(
            condition_id=cid,
            slug="thin",
            title="Will Russia use nuclear weapons?",
            tags=["geopolitics"],
            liquidity_usd=1_000.0,
        )
        try:
            trade = self._make_trade(cid, size=500.0)  # below min_thin_market_bet_usd
            alert = _check_thin_market_dominance(trade, wallet_is_novel=True)
            assert alert is None
        finally:
            gm._by_cid.pop(cid, None)

    def test_unknown_market_no_alert(self):
        trade = self._make_trade("0x" + "aa" * 32, size=10_000.0)
        alert = _check_thin_market_dominance(trade, wallet_is_novel=True)
        assert alert is None

    def test_missing_metrics_no_alert(self):
        """Market with zero liquidity AND zero volume → we have no data → skip."""
        from src.copy_trading import geo_market_scanner as gm
        cid = "0x" + "55" * 32
        gm._by_cid[cid] = gm.GeoMarket(
            condition_id=cid,
            slug="nodata",
            title="Will Russia use nuclear weapons?",
            tags=["geopolitics"],
        )
        try:
            trade = self._make_trade(cid, size=10_000.0)
            alert = _check_thin_market_dominance(trade, wallet_is_novel=True)
            assert alert is None
        finally:
            gm._by_cid.pop(cid, None)


class TestExtractTxHash:
    def test_extracts_from_canonical_id(self):
        from src.models import DetectedTrade
        t = DetectedTrade(
            id="0xdeadbeef-12345-BUY",
            trader_address="0x" + "0" * 40,
            timestamp="2026-04-12T00:00:00Z",
            market="x", side="BUY", size=1, price=0.5,
        )
        assert _extract_tx_hash(t) == "0xdeadbeef"

    def test_empty_on_malformed(self):
        from src.models import DetectedTrade
        t = DetectedTrade(
            id="nodash",
            trader_address="0x" + "0" * 40,
            timestamp="2026-04-12T00:00:00Z",
            market="x", side="BUY", size=1, price=0.5,
        )
        assert _extract_tx_hash(t) == ""


class TestClusterDetection:
    def test_cluster_with_three_wallets(self):
        market = "Will Ukraine join NATO?"
        side = "BUY"

        # Add bets from 2 other wallets
        _add_recent_bet("0x" + "1" * 40, market, side, 5000.0)
        _add_recent_bet("0x" + "2" * 40, market, side, 3000.0)

        # Third wallet triggers detection
        cluster = _find_cluster(market, side, "0x" + "3" * 40)
        assert len(cluster) >= 2  # 2 other wallets found

    def test_no_cluster_with_one_wallet(self):
        market = "Will Ukraine join NATO?"
        side = "BUY"

        _add_recent_bet("0x" + "1" * 40, market, side, 5000.0)

        cluster = _find_cluster(market, side, "0x" + "3" * 40)
        assert len(cluster) < 2

    def test_different_markets_not_clustered(self):
        side = "BUY"
        _add_recent_bet("0x" + "1" * 40, "Market A", side, 5000.0)
        _add_recent_bet("0x" + "2" * 40, "Market B", side, 5000.0)

        cluster = _find_cluster("Market A", side, "0x" + "3" * 40)
        assert len(cluster) < 2

    def test_different_sides_not_clustered(self):
        market = "Same market"
        _add_recent_bet("0x" + "1" * 40, market, "BUY", 5000.0)
        _add_recent_bet("0x" + "2" * 40, market, "SELL", 5000.0)

        cluster = _find_cluster(market, "BUY", "0x" + "3" * 40)
        assert len(cluster) < 2
