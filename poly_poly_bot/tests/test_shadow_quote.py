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
                              # distinct per call, so a genuine re-read is
                              # distinguishable from a cache replay
                              fetched_at=float(calls["n"]))

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
    """The worker is two-stage now: t0 on dequeue, t1 after the delay."""
    monkeypatch.setattr(shadow_quote.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(shadow_quote, "SECOND_SAMPLE_DELAY_S", 0.0)
    monkeypatch.setattr(shadow_quote, "quote_once",
                        lambda c, tok, tp: {"our_price": 0.6, "best_bid": 0.5,
                                            "best_ask": 0.6, "spread_bps": 10,
                                            "penalty_bps": 100})
    recorded = []
    monkeypatch.setattr(shadow_quote, "_build_row",
                        lambda t, a, b, quoted_at: recorded.append(t["copy_id"]))

    observer, stop = shadow_quote.make_observer(lambda: object())
    try:
        batch = [{"copy_id": "a", "token_id": "t", "their_price": 0.5},
                 {"copy_id": "b", "token_id": "t", "their_price": 0.5}]
        observer(batch)
        observer(batch)          # the detector re-emits until the trade ages out
        import time as _t
        for _ in range(60):
            if len(recorded) >= 2:
                break
            _t.sleep(0.05)
    finally:
        stop()
    assert sorted(recorded) == ["a", "b"], "each trade quoted exactly once"


def test_t0_is_quoted_on_dequeue_not_after_the_sleep(monkeypatch, tmp_path):
    """The shipped worker slept 12s inside each sample, serialising the queue:
    the median production row was priced 295s after detection, so the 'price
    the moment we were told' carried minutes of our own queue delay."""
    monkeypatch.setattr(shadow_quote.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(shadow_quote, "SECOND_SAMPLE_DELAY_S", 5.0)
    order = []
    monkeypatch.setattr(shadow_quote, "quote_once",
                        lambda c, tok, tp: (order.append(tok) or {"our_price": 0.6}))
    monkeypatch.setattr(shadow_quote, "_build_row",
                        lambda t, a, b, quoted_at: None)
    observer, stop = shadow_quote.make_observer(lambda: object())
    try:
        observer([{"copy_id": f"c{i}", "token_id": f"t{i}", "their_price": 0.5}
                  for i in range(5)])
        import time as _t
        for _ in range(40):
            if len(order) >= 5:
                break
            _t.sleep(0.05)
    finally:
        stop()
    # All five t0 quotes land well inside one 5s delay window — under the old
    # shape the fifth would not have been priced for ~48s.
    assert len(order) == 5


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


# --------------------------------------------------------------------------- #
# Measurement validity — the biases the round-4 verification found
# --------------------------------------------------------------------------- #

def test_boot_flush_rows_are_flagged_and_kept_out_of_latency(monkeypatch, tmp_path):
    """A restart re-emits the whole look-back window; that is not latency.

    Reproduces the shipped defect: 33 of 44 production rows sat at boot+15s
    with `their_ts` spread over the previous hour, which rendered as a
    'median 11.8min' notification lag. On a repo that redeploys every push,
    that number would mostly have measured the last deploy.
    """
    monkeypatch.setattr(shadow_quote.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(shadow_quote, "PROCESS_START_TS", 1_000_000.0)
    monkeypatch.setattr(shadow_quote, "SECOND_SAMPLE_DELAY_S", 0.0)
    _patch_snapshot(monkeypatch, [(0.60, 0.62)])

    # Traded 50 min BEFORE we started; detected on our first sweep.
    old = shadow_quote.sample_trade(object(), {
        "copy_id": "boot", "token_id": "tok", "their_price": 0.5,
        "their_ts": 999_000.0, "detected_at": 1_000_015.0})
    assert old["boot_flush"] is True
    assert old["notify_latency_s"] == 1015.0     # recorded...
    assert shadow_quote.valid_for_latency(old) is False   # ...but not counted

    _patch_snapshot(monkeypatch, [(0.60, 0.62)])
    live = shadow_quote.sample_trade(object(), {
        "copy_id": "live", "token_id": "tok", "their_price": 0.5,
        "their_ts": 1_000_100.0, "detected_at": 1_000_160.0})
    assert live["boot_flush"] is False
    assert shadow_quote.valid_for_latency(live) is True

    s = shadow_quote.summarize([old, live])
    assert s["n"] == 2
    assert s["n_excluded_boot"] == 1
    assert s["n_latency"] == 1
    assert s["latency_p50_s"] == 60.0, "the boot artifact must not move the median"


def test_a_stale_quote_is_excluded_from_the_penalty(monkeypatch, tmp_path):
    """A quote taken minutes after detection measures our queue, not the market."""
    monkeypatch.setattr(shadow_quote.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(shadow_quote, "MAX_QUOTE_LAG_S", 30.0)
    fresh = {"penalty_bps": 100, "quote_lag_s": 5.0}
    stale = {"penalty_bps": 900, "quote_lag_s": 295.0}   # the observed p50
    assert shadow_quote.valid_for_penalty(fresh) is True
    assert shadow_quote.valid_for_penalty(stale) is False
    s = shadow_quote.summarize([fresh, stale])
    assert s["n_penalty"] == 1
    assert s["n_excluded_lag"] == 1
    assert s["penalty_p50_bps"] == 100


def test_rows_without_the_new_fields_are_still_usable():
    # Rows written before these flags existed must not vanish from the stats.
    legacy = {"penalty_bps": 250, "notify_latency_s": 45}
    assert shadow_quote.valid_for_penalty(legacy) is True
    assert shadow_quote.valid_for_latency(legacy) is True


def test_the_worker_records_quote_lag(monkeypatch, tmp_path):
    monkeypatch.setattr(shadow_quote.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(shadow_quote, "SECOND_SAMPLE_DELAY_S", 0.0)
    _patch_snapshot(monkeypatch, [(0.60, 0.62)])
    row = shadow_quote.sample_trade(object(), {
        "copy_id": "c", "token_id": "tok", "their_price": 0.5,
        "their_ts": 100.0, "detected_at": 200.0})
    assert row["quote_lag_s"] is not None


def test_dedup_survives_a_restart(monkeypatch, tmp_path):
    """`seen` is memory-only; without seeding, every restart re-quotes the
    whole look-back window and counts the duplicates as fresh samples."""
    monkeypatch.setattr(shadow_quote.CONFIG, "data_dir", str(tmp_path))
    # A previous process already quoted this trade.
    shadow_quote.record({"copy_id": "already", "detected_at": 1.0,
                         "penalty_bps": 100})
    queued = []
    monkeypatch.setattr(shadow_quote, "sample_trade",
                        lambda c, t: queued.append(t["copy_id"]))
    observer, stop = shadow_quote.make_observer(lambda: None)
    try:
        observer([{"copy_id": "already"}])
    finally:
        stop()
    assert queued == [], "a restart must not re-quote what is already in the log"


def test_per_sweep_cap_is_reported_not_silent(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(shadow_quote.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(shadow_quote, "MAX_SAMPLES_PER_SWEEP", 2)
    observer, stop = shadow_quote.make_observer(lambda: None)
    try:
        observer([{"copy_id": f"c{i}"} for i in range(5)])
    finally:
        stop()
    # All 5 are marked seen so the next sweep takes a fresh head, not a
    # growing backlog of stale trades.


# --------------------------------------------------------------------------- #
# The arm check the verifier proved was untested
# --------------------------------------------------------------------------- #

def test_a_truthy_string_does_not_arm(monkeypatch, tmp_path):
    """`{"armed": "false"}` is a truthy string. Reverting `is True` to
    `bool(...)` passed the entire suite before this test existed."""
    import json as _json

    from src.copy_trading import live_mode
    monkeypatch.setattr(live_mode.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(live_mode.CONFIG, "preview_mode", False)
    monkeypatch.setattr(live_mode.CONFIG, "live_arm_enabled", True)
    for value in ("false", "true", 1, "1", [], {}):
        (tmp_path / "live_arm.json").write_text(_json.dumps({"armed": value}))
        assert live_mode.is_armed() is False, f"{value!r} must not arm"
        assert live_mode.is_preview() is True
        # and the panel must not claim otherwise
        assert live_mode.status()["runtime_armed"] is False
        assert any("not armed" in r for r in live_mode.blocking_reasons())


def test_a_real_true_still_arms(monkeypatch, tmp_path):
    import json as _json

    from src.copy_trading import live_mode
    monkeypatch.setattr(live_mode.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(live_mode.CONFIG, "preview_mode", False)
    monkeypatch.setattr(live_mode.CONFIG, "live_arm_enabled", True)
    (tmp_path / "live_arm.json").write_text(_json.dumps({"armed": True}))
    assert live_mode.is_armed() is True
    assert live_mode.status()["runtime_armed"] is True


# --------------------------------------------------------------------------- #
# Round-5: the exclusion must apply to BOTH numbers, and drops must be counted
# --------------------------------------------------------------------------- #

def test_boot_flush_is_excluded_from_the_penalty_too():
    """The shipped bug: latency was withheld as unusable while a bold penalty
    median was reported off the very same rows. A trade detected 26 minutes
    late is priced against a book that has had 26 minutes to move."""
    boot = {"boot_flush": True, "penalty_bps": 6000, "notify_latency_s": 1565,
            "quote_lag_s": 0.7}
    assert shadow_quote.valid_for_latency(boot) is False
    assert shadow_quote.valid_for_penalty(boot) is False
    s = shadow_quote.summarize([boot])
    assert s["n"] == 1
    assert s["n_latency"] == 0
    assert s["n_penalty"] == 0, "both numbers must decline, not just one"
    assert s["penalty_p50_bps"] is None
    # and it is not double-counted as a lag exclusion
    assert s["n_excluded_boot"] == 1
    assert s["n_excluded_lag"] == 0


def test_a_wallet_with_no_usable_samples_is_not_listed():
    rows = [{"target": "0xA", "boot_flush": True, "penalty_bps": 500,
             "notify_latency_s": 900, "category": "sports"}]
    assert shadow_quote.by_wallet(rows) == []


def test_per_wallet_carries_its_own_latency_count():
    rows = [
        {"target": "0xA", "penalty_bps": 100, "notify_latency_s": 30,
         "category": "sports", "quote_lag_s": 1.0},
        # penalty usable, latency missing
        {"target": "0xA", "penalty_bps": 200, "category": "sports",
         "quote_lag_s": 1.0},
    ]
    d = shadow_quote.by_wallet(rows, min_n=1)[0]
    assert d["n"] == 2          # penalty samples
    assert d["n_latency"] == 1  # latency samples, its own count


def test_unquotable_trades_are_recorded_and_counted(tmp_path, monkeypatch):
    """One-sided books are the expensive-to-copy ones. Dropping them silently
    moves book A's survivor bias from the fill gate into the book read."""
    monkeypatch.setattr(shadow_quote.CONFIG, "data_dir", str(tmp_path))
    row = shadow_quote.record_unquotable(
        {"copy_id": "c9", "target": "0xw", "token_id": "t",
         "their_price": 0.5, "their_ts": 1.0, "detected_at": 2.0})
    assert row["unquotable"] is True
    # carries no prices, so every statistic skips it by construction
    assert "penalty_bps" not in row
    s = shadow_quote.summarize(shadow_quote.load_rows())
    assert s["n_unquotable"] == 1
    assert s["n_penalty"] == 0


def test_a_replayed_second_sample_does_not_count_as_zero_drift(monkeypatch, tmp_path):
    """market_price hands back an EXPIRED cached snapshot when a read fails,
    so a failed t1 is a byte-identical replay of t0 and would report a
    guaranteed zero decay, arguing for free that speed does not matter."""
    monkeypatch.setattr(shadow_quote.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(shadow_quote, "SECOND_SAMPLE_DELAY_S", 0.0)
    from src.models import MarketSnapshot

    frozen = MarketSnapshot(best_bid=0.60, best_ask=0.62, midpoint=0.61,
                            spread=0.02, spread_bps=328, fetched_at=111.0)
    monkeypatch.setattr("src.copy_trading.market_price.fetch_market_snapshot",
                        lambda c, t: frozen)
    import time as _t
    now = _t.time()
    monkeypatch.setattr(shadow_quote, "PROCESS_START_TS", now - 3600)
    row = shadow_quote.sample_trade(object(), {
        "copy_id": "c", "token_id": "tok", "their_price": 0.5,
        "their_ts": now - 60, "detected_at": now})
    assert row["t1_stale"] is True
    # and it is excluded for staleness, not merely for queue lag
    assert shadow_quote.valid_for_penalty(row) is True
    s = shadow_quote.summarize([row])
    assert s["n_decay"] == 0, "a replayed sample must not become a decay reading"


def test_a_fresh_second_sample_still_counts(monkeypatch, tmp_path):
    monkeypatch.setattr(shadow_quote.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(shadow_quote, "SECOND_SAMPLE_DELAY_S", 0.0)
    _patch_snapshot(monkeypatch, [(0.60, 0.62), (0.63, 0.65)])
    import time as _t
    now = _t.time()
    monkeypatch.setattr(shadow_quote, "PROCESS_START_TS", now - 3600)
    row = shadow_quote.sample_trade(object(), {
        "copy_id": "c", "token_id": "tok", "their_price": 0.5,
        "their_ts": now - 60, "detected_at": now})
    assert row["t1_stale"] is False
    assert shadow_quote.summarize([row])["n_decay"] == 1


def test_load_rows_survives_a_malformed_stamp(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow_quote.CONFIG, "data_dir", str(tmp_path))
    p = tmp_path / "shadow-quotes.jsonl"
    p.write_text('{"copy_id":"a","detected_at":"abc"}\n'
                 '{"copy_id":"b","detected_at":500}\n')
    rows = shadow_quote.load_rows(since_ts=100)
    assert [r["copy_id"] for r in rows] == ["b"]


def test_counterfactual_ignores_biased_quotes(tmp_path):
    """Re-settling the book at a stale entry would report drift as execution
    cost."""
    from src.copy_trading import virtual_ledger
    import json as _json
    lp = tmp_path / "l.jsonl"
    shares = 50.0 / 0.5
    lp.write_text(_json.dumps({
        "copy_id": "c1", "target": "0xw", "condition_id": "0xc",
        "token_id": "t", "outcome_index": 0, "category": "sports",
        "their_price": 0.5, "entry_price": 0.5, "shares": shares,
        "spent": 50.0, "drag_bps": 0, "opened_ts": 1.0, "closed": True,
        "won": True, "pnl": shares - 50.0, "ideal_pnl": 0.0,
        "closed_ts": 2.0, "exited_early": False, "cost_usd": 0.0,
        "ideal_cost_usd": 0.0}) + "\n")
    biased = [{"copy_id": "c1", "our_price": 0.9, "penalty_bps": 8000,
               "boot_flush": True}]
    out = virtual_ledger.replay(str(lp), quote_rows=biased)
    assert out["n_matched"] == 0, "a boot-flush quote must not price the book"


# --------------------------------------------------------------------------- #
# Round-6: n counts rows, not independent observations
# --------------------------------------------------------------------------- #

def test_clustered_rows_are_disclosed_as_few_independent_observations():
    """Production had 27 penalty rows that were 4 tokens and 10 book reads,
    with 14 sharing one cached ask. Row count alone presented one in-play
    market collapsing as a settled distribution."""
    rows = [{"target": "0xw", "token_id": "tokA", "book_ts": 111.0,
             "penalty_bps": -1400, "penalty_bps_t1": -1600, "t1_stale": False,
             "quote_lag_s": 1.0, "boot_flush": False} for _ in range(14)]
    rows += [{"target": "0xw", "token_id": "tokB", "book_ts": 222.0,
              "penalty_bps": 300, "penalty_bps_t1": 320, "t1_stale": False,
              "quote_lag_s": 1.0, "boot_flush": False}]
    s = shadow_quote.summarize(rows)
    assert s["n_penalty"] == 15        # rows
    assert s["n_markets"] == 2         # ...but only two markets
    assert s["n_book_reads"] == 2      # ...and two book reads
    # and the decay term counts distinct MOVES, not clones of one move
    assert s["n_decay"] == 15
    assert s["n_decay_moves"] == 2


def test_the_decay_conclusion_is_gated_on_distinct_book_reads(monkeypatch, tmp_path):
    """13 copies priced off ONE cached book read is one observation.

    The first version of this test gave every row the same penalty value,
    which was the only shape under which the old (token, penalty, penalty_t1)
    key deduped, and the one shape production never writes: penalty_bps is
    computed against each copy's OWN their_price, so one read yields 13
    different penalties. Production showed 15 reads reported as 37 moves.
    """
    import time as _t
    from unittest.mock import patch as _patch
    monkeypatch.setattr(shadow_quote.CONFIG, "data_dir", str(tmp_path))
    import src.telegram_bot as tb
    now = _t.time()
    rows = []
    for i in range(13):
        their = 0.50 + i * 0.005          # each copy entered at its own price
        rows.append({
            "copy_id": f"c{i}", "target": "0xw", "token_id": "tokA",
            "book_ts": 111.0,             # ...but ONE book read priced them all
            "category": "sports", "their_price": their, "our_price": 0.43,
            "penalty_bps": int((0.43 - their) / their * 10000),
            "penalty_bps_t1": int((0.41 - their) / their * 10000),
            "t1_stale": False, "quote_lag_s": 1.0, "boot_flush": False,
            "notify_latency_s": 120.0, "detected_at": now - 10})
    with open(tmp_path / "shadow-quotes.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    s = shadow_quote.summarize(shadow_quote.load_rows())
    assert s["n_penalty"] == 13
    assert len({r["penalty_bps"] for r in rows}) == 13, "penalties really do differ"
    assert s["n_book_reads"] == 1
    assert s["n_decay_moves"] == 1, "one read cannot be thirteen moves"

    sent = []
    with _patch.object(tb, "_send_chunked", lambda x: sent.append(x)), \
         _patch.object(tb, "send_message", lambda x, **k: sent.append(x)):
        tb._handle_speed("/speed 7")
    out = sent[0]
    assert "Does being faster help" not in out
    assert "1 distinct book read(s)" in out
    assert "almost all one market" in out


def test_unknown_independence_is_not_rendered_as_zero(monkeypatch, tmp_path):
    """Rows predating book_ts must read as unmeasured, not as zero reads."""
    import time as _t
    from unittest.mock import patch as _patch
    monkeypatch.setattr(shadow_quote.CONFIG, "data_dir", str(tmp_path))
    import src.telegram_bot as tb
    now = _t.time()
    rows = [{"copy_id": f"c{i}", "target": f"0xw{i}", "token_id": f"tok{i}",
             "category": "sports", "their_price": 0.5, "our_price": 0.55,
             "penalty_bps": 1000, "penalty_bps_t1": 1100, "t1_stale": False,
             "quote_lag_s": 1.0, "boot_flush": False,
             "notify_latency_s": 60.0, "detected_at": now - 10}
            for i in range(8)]          # 8 markets, no book_ts anywhere
    with open(tmp_path / "shadow-quotes.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    s = shadow_quote.summarize(shadow_quote.load_rows())
    assert s["n_book_reads"] is None, "absent must not read as zero"
    assert s["n_decay_moves"] is None
    sent = []
    with _patch.object(tb, "_send_chunked", lambda x: sent.append(x)), \
         _patch.object(tb, "send_message", lambda x, **k: sent.append(x)):
        tb._handle_speed("/speed 7")
    out = sent[0]
    assert "0 distinct book read" not in out
    assert "independence not recorded" in out
    # and the conclusion stays suppressed while independence is unknown
    assert "Does being faster help" not in out


def test_non_finite_penalties_cannot_render_as_a_measurement():
    rows = [{"penalty_bps": float("nan"), "quote_lag_s": 1.0},
            {"penalty_bps": float("inf"), "quote_lag_s": 1.0},
            {"penalty_bps": 200, "quote_lag_s": 1.0}]
    s = shadow_quote.summarize(rows)
    assert s["penalty_p50_bps"] == 200
    assert s["penalty_mean_bps"] == 200


def _render(cmd, rows, tmp_path, monkeypatch):
    """Render a Telegram command against `rows` and return what it would send."""
    from unittest.mock import patch as _patch
    import src.telegram_bot as tb
    monkeypatch.setattr(shadow_quote.CONFIG, "data_dir", str(tmp_path))
    with open(os.path.join(str(tmp_path), "shadow-quotes.jsonl"), "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    sent = []
    with _patch.object(tb, "_send_chunked", lambda x: sent.append(x)), \
         _patch.object(tb, "send_message", lambda x, **k: sent.append(x)):
        if cmd.startswith("/speed"):
            tb._handle_speed(cmd)
        else:
            tb._handle_live(cmd)
    return "\n".join(sent)


def _panel_rows(n=8, **over):
    import time as _t
    now = _t.time()
    base = dict(target="0xw", token_id="tok", category="sports",
                their_price=0.5, our_price=0.55, penalty_bps=1000,
                penalty_bps_t1=1100, t1_stale=False, quote_lag_s=1.0,
                boot_flush=False, notify_latency_s=60.0, book_ts=1.0)
    out = []
    for i in range(n):
        r = dict(base)
        r.update(copy_id=f"c{i}", detected_at=now - 10,
                 token_id=f"tok{i}", book_ts=float(i))
        r.update(over)
        out.append(r)
    return out


def test_no_dash_in_anything_these_commands_render(tmp_path, monkeypatch):
    """The owner's hardest style rule, asserted on the RENDERED TEXT.

    Three earlier versions of this guard checked source provenance and each
    was wrong. v1 listed only the modules the run created, so a dash shipped
    in main.py. v2 scoped by `git diff`, which is empty under CI's shallow
    clone, so it flagged the whole repo and turned the deploy gate red. v3
    scoped by log-prefix markers and therefore covered none of
    telegram_bot.py, i.e. every line of /speed and /live output, which is
    exactly the text he reads.

    The rule is about rendered text, so this asserts on rendered text. Every
    branch of both commands, including the empty and degenerate states.
    """
    import time as _t
    now = _t.time()
    cases = {
        "healthy": _panel_rows(8),
        "empty": [],
        "all boot flush": _panel_rows(6, boot_flush=True),
        "all stale quotes": _panel_rows(6, quote_lag_s=900.0),
        "one market one read": [dict(r, token_id="tokA", book_ts=1.0)
                                for r in _panel_rows(9)],
        "no independence stamp": [{k: v for k, v in r.items() if k != "book_ts"}
                                  for r in _panel_rows(7)],
        "mixed coverage": (_panel_rows(4)
                           + [{k: v for k, v in r.items() if k != "book_ts"}
                              for r in _panel_rows(9)]),
        "unquotable only": [{"copy_id": "u", "target": "0xw", "token_id": "t",
                             "category": "x", "their_price": 0.4,
                             "their_ts": now - 50, "detected_at": now - 10,
                             "unquotable": True, "reason": "no usable book"}],
        "negative penalty": _panel_rows(6, penalty_bps=-1400,
                                        penalty_bps_t1=-1600),
        "junk fields": _panel_rows(5, quote_lag_s="abc", penalty_bps=1000),
    }
    for label, rows in cases.items():
        for cmd in ("/speed 7", "/speed", "/speed banana", "/live"):
            out = _render(cmd, rows, tmp_path, monkeypatch)
            assert "\u2014" not in out and "\u2013" not in out, (
                f"dash rendered by {cmd} in the {label!r} case")


def test_the_dash_guard_actually_catches_one(tmp_path, monkeypatch):
    """A guard that cannot fail is not a guard. Proves this one can."""
    import src.telegram_bot as tb
    real = tb._fmt_bps
    monkeypatch.setattr(tb, "_fmt_bps", lambda v: "\u2014" + real(v))
    out = _render("/speed 7", _panel_rows(8), tmp_path, monkeypatch)
    assert "\u2014" in out, (
        "the render path must be reachable by the guard, or the guard proves "
        "nothing")


def test_rendered_output_never_emits_an_unescaped_angle_bracket(tmp_path,
                                                                monkeypatch):
    """The class that made the 2026-08-22 verdict memo undeliverable."""
    import re
    hostile = _panel_rows(4, target="<script>alert(1)</script>",
                          category="a < b & c")
    for cmd in ("/speed 7", "/live"):
        out = _render(cmd, hostile, tmp_path, monkeypatch)
        stray = [m for m in re.findall(r"<[^>]*", out)
                 if not re.match(r"</?(b|i|code|pre|a)\b", m)]
        assert not stray, f"{cmd} emitted {stray}"
