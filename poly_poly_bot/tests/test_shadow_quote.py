"""Pre-flip measurement: shadow quotes, the latency clock, and the live interlock.

The point of these tests is that the two numbers the owner asked for are
measured the same way the live executor would actually price, and that the
real-money door cannot open by accident.
"""

import json
import os

import pytest

from src.copy_trading import shadow_quote
from src.copy_trading.order_executor import (
    entry_penalty_bps, quote_copy_order, shares_for)


# --------------------------------------------------------------------------- #
# The pricing rule — one function, shared with the live executor
# --------------------------------------------------------------------------- #

def test_buy_lifts_the_ask_not_a_capped_price():
    # The dead code this replaced capped at trader_price * 1.02, which would
    # have answered "how much worse is my entry" with a number the live path
    # never produces. BUY takes the ask, whatever it is.
    assert quote_copy_order("BUY", 0.50, {"best_ask": 0.62, "best_bid": 0.60}) == 0.62


def test_sell_hits_the_bid():
    assert quote_copy_order("SELL", 0.50, {"best_ask": 0.62, "best_bid": 0.40}) == 0.40


def test_no_snapshot_falls_back_to_their_price():
    assert quote_copy_order("BUY", 0.37, None) == 0.37


def test_unusable_prices_return_none_not_a_silent_zero():
    assert quote_copy_order("BUY", 0.0, None) is None
    assert quote_copy_order("BUY", 0.5, {"best_ask": 1.0}) is None
    assert quote_copy_order("BUY", 0.5, {"best_ask": -0.2}) == 0.5  # bad ask -> fallback


def test_price_is_rounded_to_whole_cents():
    assert quote_copy_order("BUY", 0.5, {"best_ask": 0.6249}) == 0.62


def test_shares_and_penalty_math():
    assert shares_for(50.0, 0.50) == 100.0
    # we paid 0.62 where they paid 0.50 -> 24% worse
    assert entry_penalty_bps(0.62, 0.50) == 2400
    # cheaper than them is negative, not clamped away
    assert entry_penalty_bps(0.45, 0.50) == -1000
    # a missing input must never read as "no penalty"
    assert entry_penalty_bps(0.62, 0.0) is None


def test_live_executor_and_shadow_use_the_same_function():
    # Guards the whole premise: if the executor stops importing this, the
    # measurement silently stops describing the thing it claims to measure.
    import inspect

    from src.copy_trading import trade_executor
    src = inspect.getsource(trade_executor._execute_copy_order)
    assert "quote_copy_order" in src


# --------------------------------------------------------------------------- #
# The latency clock
# --------------------------------------------------------------------------- #

def test_queued_trade_separates_their_time_from_ours():
    from src.models import DetectedTrade, QueuedTrade
    t = DetectedTrade(id="x", trader_address="0xa", timestamp="2026-08-16T00:00:00+00:00",
                      market="m")
    q = QueuedTrade(trade=t, enqueued_at=1000.0, source_detected_at=1000.0,
                    received_at_ms=125000.0)
    # The gap is the number that did not exist before.
    assert q.received_at_ms - q.source_detected_at == 124000.0


def test_queued_trade_still_loads_without_the_new_field():
    # Old queue rows must keep validating, or a deploy drops in-flight trades.
    from src.models import DetectedTrade, QueuedTrade
    t = DetectedTrade(id="x", trader_address="0xa", timestamp="2026-08-16T00:00:00+00:00",
                      market="m")
    q = QueuedTrade(trade=t, enqueued_at=1.0, source_detected_at=1.0)
    assert q.received_at_ms is None


def test_detector_stamps_both_clocks(monkeypatch):
    from src.copy_trading import copy_paper_live

    activity = [{
        "type": "TRADE", "side": "BUY", "timestamp": 1_000_000.0, "price": 0.5,
        "usdcSize": 5000.0, "transactionHash": "0xtx", "asset": "tok",
        "conditionId": "0xc", "title": "T", "outcomeIndex": 0,
    }]
    monkeypatch.setattr(copy_paper_live, "_get", lambda *a, **k: activity)
    monkeypatch.setattr(copy_paper_live.time, "time", lambda: 1_000_060.0)

    det = copy_paper_live.make_detector(["0xw"], max_age_s=3600, min_usd=100)
    out = det()
    assert len(out) == 1
    # 60s between their trade and our detection, and both are recorded.
    assert out[0]["their_ts"] == 1_000_000.0
    assert out[0]["detected_at"] == 1_000_060.0


# --------------------------------------------------------------------------- #
# The shadow-quote row and its summary
# --------------------------------------------------------------------------- #

class _FakeClient:
    """Returns a book that has moved against us since the target traded."""

    def __init__(self, asks):
        self._asks = list(asks)

    def get_price(self, token_id, side):
        from py_clob_client_v2.order_builder.constants import BUY
        if side == BUY:
            return str(self._asks[0] if len(self._asks) == 1 else self._asks.pop(0))
        return "0.40"


def _patch_snapshot(monkeypatch, sequence):
    """Feed quote_once a scripted series of books, bypassing the 5s TTL cache."""
    calls = {"n": 0}

    def fake(clob_client, token_id):
        i = min(calls["n"], len(sequence) - 1)
        calls["n"] += 1
        bid, ask = sequence[i]
        from src.models import MarketSnapshot
        mid = (bid + ask) / 2
        return MarketSnapshot(best_bid=bid, best_ask=ask, midpoint=mid,
                              spread=ask - bid,
                              spread_bps=int((ask - bid) / mid * 10000),
                              fetched_at=0.0)

    monkeypatch.setattr("src.copy_trading.market_price.fetch_market_snapshot", fake)


def test_sample_trade_records_penalty_and_latency(monkeypatch, tmp_path):
    monkeypatch.setattr(shadow_quote.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(shadow_quote, "SECOND_SAMPLE_DELAY_S", 0.0)
    _patch_snapshot(monkeypatch, [(0.60, 0.62), (0.62, 0.65)])

    trade = {
        "copy_id": "c1", "target": "0xw", "token_id": "tok", "category": "sports",
        "title": "t", "their_price": 0.50, "their_usd": 900.0,
        "their_ts": 1_000_000.0, "detected_at": 1_000_045.0,
    }
    row = shadow_quote.sample_trade(object(), trade)

    assert row["notify_latency_s"] == 45.0
    assert row["our_price"] == 0.62
    assert row["penalty_bps"] == 2400          # 0.62 vs their 0.50
    assert row["penalty_bps_t1"] == 3000       # 0.65 vs their 0.50 — it drifted
    # and it landed on disk, so the panel can read it back
    written = (tmp_path / "shadow-quotes.jsonl").read_text().strip()
    assert json.loads(written)["copy_id"] == "c1"


def test_missing_timestamps_are_none_never_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(shadow_quote.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(shadow_quote, "SECOND_SAMPLE_DELAY_S", 0.0)
    _patch_snapshot(monkeypatch, [(0.60, 0.62)])
    row = shadow_quote.sample_trade(object(), {
        "copy_id": "c2", "token_id": "tok", "their_price": 0.5,
        "their_ts": 0, "detected_at": 0})
    # A missing clock must not be scored as an instantaneous copy.
    assert row["notify_latency_s"] is None


def test_summarize_percentiles_and_decay():
    rows = [
        {"notify_latency_s": 10, "penalty_bps": 100, "penalty_bps_t1": 120},
        {"notify_latency_s": 20, "penalty_bps": 200, "penalty_bps_t1": 210},
        {"notify_latency_s": 30, "penalty_bps": -50, "penalty_bps_t1": -40},
        {"notify_latency_s": 40, "penalty_bps": 400, "penalty_bps_t1": 400},
    ]
    s = shadow_quote.summarize(rows)
    assert s["n"] == 4
    assert s["latency_p50_s"] == 25
    assert s["penalty_p50_bps"] == 150
    assert s["penalty_worse_frac"] == 0.75      # 3 of 4 paid more
    assert s["decay_mean_bps"] == pytest.approx(10.0)


def test_summarize_survives_an_empty_and_a_partial_sample():
    assert shadow_quote.summarize([])["n"] == 0
    partial = shadow_quote.summarize([{"penalty_bps": 100}])
    assert partial["n_latency"] == 0
    assert partial["latency_p50_s"] is None
    assert partial["penalty_p50_bps"] == 100


def test_second_sample_delay_clears_the_snapshot_cache():
    # If the delay were inside market_price's TTL the second look would be
    # handed the identical prices and the decay term could only ever be zero.
    from src.copy_trading import market_price
    assert shadow_quote.SECOND_SAMPLE_DELAY_S > market_price.CACHE_TTL_S


def test_observer_quotes_each_trade_once(monkeypatch, tmp_path):
    monkeypatch.setattr(shadow_quote.CONFIG, "data_dir", str(tmp_path))
    seen = []
    monkeypatch.setattr(shadow_quote, "sample_trade",
                        lambda c, t: seen.append(t["copy_id"]))
    observer, stop = shadow_quote.make_observer(lambda: object())
    try:
        batch = [{"copy_id": "a"}, {"copy_id": "b"}]
        observer(batch)
        observer(batch)          # the detector re-emits until the trade ages out
        import time as _t
        for _ in range(50):
            if len(seen) >= 2:
                break
            _t.sleep(0.05)
    finally:
        stop()
    assert sorted(seen) == ["a", "b"]


def test_engine_observer_sees_refused_trades_and_cannot_break_the_cycle():
    """The measurement must see what the book rejects, and never raise."""
    from src.copy_trading.copy_paper import CopyPaperEngine, PaperCopyLedger
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        ledger = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        seen = {}

        def detector():
            return [{
                "copy_id": "x1", "target": "0xw", "condition_id": "0xc",
                "token_id": "tok", "outcome_index": 0, "category": "sports",
                "title": "t", "slug": "", "event_key": "", "flagged_by": (),
                "horizon_days": None, "their_price": 0.5, "their_usd": 100.0,
                "their_ts": 1.0, "detected_at": 2.0,
            }]

        def observer(trades):
            seen["n"] = len(trades)
            raise RuntimeError("a measurement must never break the book")

        engine = CopyPaperEngine(
            ledger, detector=detector,
            # empty book -> the engine refuses to open anything
            book_fetcher=lambda t: [], resolver=lambda t: None,
            observer=observer)
        summary = engine.run_cycle()

    assert seen["n"] == 1          # observed the trade the book then refused
    assert summary.detected == 1   # and the cycle completed anyway


# --------------------------------------------------------------------------- #
# The real-money interlock
# --------------------------------------------------------------------------- #

def test_preview_is_the_default_and_both_keys_are_required(monkeypatch, tmp_path):
    from src.copy_trading import live_mode
    monkeypatch.setattr(live_mode.CONFIG, "data_dir", str(tmp_path))

    # Shipping state: process in preview, no owner key.
    monkeypatch.setattr(live_mode.CONFIG, "preview_mode", True)
    monkeypatch.setattr(live_mode.CONFIG, "live_arm_enabled", False)
    assert live_mode.is_preview() is True
    ok, _ = live_mode.arm()
    assert ok is False

    # Owner key alone is not enough.
    monkeypatch.setattr(live_mode.CONFIG, "live_arm_enabled", True)
    assert live_mode.is_preview() is True
    ok, _ = live_mode.arm()
    assert ok is False, "must refuse to arm while the process is in preview"

    # Process live but not armed is still preview.
    monkeypatch.setattr(live_mode.CONFIG, "preview_mode", False)
    assert live_mode.is_preview() is True

    # Both keys turned.
    ok, _ = live_mode.arm(reason="pilot")
    assert ok is True
    assert live_mode.is_preview() is False

    # And it can always be shut off.
    live_mode.disarm()
    assert live_mode.is_preview() is True


def test_arm_does_not_survive_the_owner_key_being_pulled(monkeypatch, tmp_path):
    from src.copy_trading import live_mode
    monkeypatch.setattr(live_mode.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(live_mode.CONFIG, "preview_mode", False)
    monkeypatch.setattr(live_mode.CONFIG, "live_arm_enabled", True)
    live_mode.arm(reason="pilot")
    assert live_mode.is_preview() is False
    # Owner removes LIVE_ARM_ENABLED and restarts: the persisted arm is inert.
    monkeypatch.setattr(live_mode.CONFIG, "live_arm_enabled", False)
    assert live_mode.is_preview() is True


def test_is_preview_fails_closed_on_a_broken_arm_file(monkeypatch, tmp_path):
    from src.copy_trading import live_mode
    monkeypatch.setattr(live_mode.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(live_mode.CONFIG, "preview_mode", False)
    monkeypatch.setattr(live_mode.CONFIG, "live_arm_enabled", True)
    (tmp_path / "live_arm.json").write_text("{not json")
    assert live_mode.is_preview() is True


def test_the_order_gate_reads_the_interlock_not_the_raw_config():
    # The whole safety story rests on this one line staying wired.
    import inspect

    from src.copy_trading import trade_executor
    src = inspect.getsource(trade_executor._process_queue_once) if hasattr(
        trade_executor, "_process_queue_once") else inspect.getsource(trade_executor)
    assert "live_mode.is_preview()" in src
    assert "if CONFIG.preview_mode:" not in src, (
        "the order-placement gate must read the two-key interlock")


def test_blocking_reasons_are_plain_language(monkeypatch, tmp_path):
    from src.copy_trading import live_mode
    monkeypatch.setattr(live_mode.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(live_mode.CONFIG, "preview_mode", True)
    monkeypatch.setattr(live_mode.CONFIG, "live_arm_enabled", False)
    reasons = live_mode.blocking_reasons()
    assert len(reasons) == 3
    assert any("PREVIEW_MODE" in r for r in reasons)
    assert any("LIVE_ARM_ENABLED" in r for r in reasons)
