"""Set Z, the fast prober, the live guard, and the pentest regressions.

The through-line: the only thing standing between this repo and real money is
that a wallet cannot get into set Z without passing the gate, and the order
path cannot fire without two keys. Everything here attacks one of those two
claims.
"""

import json
import time

import pytest

from src.copy_trading import fast_prober, live_guard, live_mode, zset


class P:
    """Minimal PaperPosition stand-in."""

    def __init__(self, spent=50.0, ideal=5.0, opened=1000.0, closed=True):
        self.spent, self.ideal_pnl = spent, ideal
        self.opened_ts, self.closed = opened, closed


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Never touch the real stores."""
    monkeypatch.setattr(zset.promotion_state.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(live_guard.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(live_mode.CONFIG, "data_dir", str(tmp_path))
    zset.promotion_state.clear_cache()
    yield
    zset.promotion_state.clear_cache()


# --------------------------------------------------------------------------- #
# Set Z admission: the gate is the only writer
# --------------------------------------------------------------------------- #

def test_a_wallet_the_gate_refused_cannot_be_admitted():
    """The whole point of Z. `admit` has no force flag on purpose."""
    ok, _checks = zset.admit("0xdead", ready=False,
                             checks=[("fake", False, "")], settled=[P()] * 40)
    assert ok is False
    assert zset.wallets() == []


def test_a_passing_wallet_is_admitted():
    ok, checks = zset.admit("0xGOOD", ready=True, checks=[("gate", True, "")],
                            settled=[P(ideal=6.0) for _ in range(30)])
    assert ok is True
    assert [w.lower() for w in zset.wallets()] == ["0xgood"]
    assert any("best 3" in lab for lab, _o, _d in checks)


def test_the_concentration_rail_rejects_a_three_ticket_record():
    """A record that is three jackpots and a pile of losers is not an edge.
    One real candidate had 61% of its book-B profit and 115% of its book-A
    profit in three tickets."""
    jackpot = [P(50, 400) for _ in range(3)] + [P(50, -20) for _ in range(7)]
    ok, detail = zset.concentration_check(jackpot)
    assert ok is False, detail
    # and it blocks admission even when the gate said ready
    admitted, _ = zset.admit("0xJACKPOT", ready=True,
                             checks=[("gate", True, "")], settled=jackpot)
    assert admitted is False
    assert zset.wallets() == []


def test_a_broad_record_survives_the_rail():
    broad = [P(50, 8) for _ in range(20)]
    ok, _ = zset.concentration_check(broad)
    assert ok is True


def test_a_record_shorter_than_the_rail_fails_closed():
    ok, detail = zset.concentration_check([P(50, 10), P(50, 10)])
    assert ok is False
    assert "no record left" in detail


def test_trimmed_roi_deletes_the_best_not_the_worst():
    rows = [P(50, 100), P(50, 100), P(50, 100), P(50, -10), P(50, -10)]
    roi, kept, dropped = zset.trimmed_roi(rows)
    assert (kept, dropped) == (2, 3)
    assert roi == pytest.approx(-0.2)   # only the two losers remain


def test_eviction_is_always_allowed():
    zset.admit("0xGOOD", ready=True, checks=[], settled=[P(ideal=6.0)] * 30)
    assert zset.evict("0xGOOD", reason="drill") is True
    assert zset.wallets() == []


def test_the_live_watch_list_reads_set_z_not_the_legacy_store():
    """The bug this whole run exists to kill: the live list was
    `user_addresses + promoted_wallets()`, and that store held two wallets
    with 2 and 7 clean-era copies."""
    import inspect

    from src.copy_trading import strategy_config, trade_monitor
    for mod in (trade_monitor, strategy_config):
        src = inspect.getsource(mod)
        assert "zset.wallets()" in src, mod.__name__
        assert "promotion_state.promoted_wallets()" not in src, (
            f"{mod.__name__} still reads the hand-writable store")


# --------------------------------------------------------------------------- #
# The fast prober: measurement path, never the frozen paper sample
# --------------------------------------------------------------------------- #

def test_prober_emits_the_same_shape_the_detectors_do():
    now = 1_000_000.0
    acts = [{"type": "TRADE", "side": "BUY", "timestamp": now - 5,
             "price": 0.5, "usdcSize": 900, "transactionHash": "0xtx",
             "asset": "tok", "conditionId": "0xc", "title": "T",
             "outcomeIndex": 0}]
    out = fast_prober.poll_once(["0xW"], set(), fetch=lambda w: acts, now=now)
    assert len(out) == 1
    r = out[0]
    assert r["copy_id"] == "0xtx-tok"
    assert r["their_ts"] == now - 5
    assert r["detected_at"] == now
    assert r["age_s"] == 5
    assert r["actionable"] is True


def test_prober_marks_stale_history_unactionable():
    """After a restart the endpoint returns the wallet's recent history.
    Copying an hour-old entry at today's book buys the move already missed."""
    now = 1_000_000.0
    acts = [{"type": "TRADE", "side": "BUY", "timestamp": now - 3600,
             "price": 0.5, "usdcSize": 900, "transactionHash": "0xold",
             "asset": "tok"}]
    out = fast_prober.poll_once(["0xW"], set(), fetch=lambda w: acts, now=now)
    assert out[0]["actionable"] is False


def test_prober_dedupes_and_skips_sells():
    now = 1_000_000.0
    acts = [
        {"type": "TRADE", "side": "BUY", "timestamp": now, "price": 0.5,
         "usdcSize": 900, "transactionHash": "0xa", "asset": "t"},
        {"type": "TRADE", "side": "SELL", "timestamp": now, "price": 0.5,
         "usdcSize": 900, "transactionHash": "0xb", "asset": "t"},
    ]
    seen = set()
    first = fast_prober.poll_once(["0xW"], seen, fetch=lambda w: acts, now=now)
    second = fast_prober.poll_once(["0xW"], seen, fetch=lambda w: acts, now=now)
    assert len(first) == 1 and first[0]["copy_id"] == "0xa-t"
    assert second == []


def test_prober_survives_a_broken_endpoint():
    def boom(w):
        raise RuntimeError("data api down")
    assert fast_prober.poll_once(["0xW"], set(), fetch=boom) == []


def test_prober_does_not_touch_the_paper_books():
    """The 08-22 freeze: nothing here may change what the books admit."""
    import inspect
    src = inspect.getsource(fast_prober)
    for forbidden in ("copy_paper_runner", "CopyPaperEngine", "PaperCopyLedger"):
        assert forbidden not in src, f"prober references {forbidden}"


# --------------------------------------------------------------------------- #
# The live guard
# --------------------------------------------------------------------------- #

def test_detectors_fire_but_actions_do_not_while_unarmed(monkeypatch):
    monkeypatch.setattr(live_mode.CONFIG, "preview_mode", True)
    monkeypatch.setattr(live_mode.CONFIG, "live_arm_enabled", False)
    now = time.time()
    cancelled = []

    class O:
        def __init__(self, oid, age):
            self.order_id, self.placed_at = oid, now - age

    class Pos:
        def __init__(self, age):
            self.closed, self.redeemed, self.closed_ts = True, False, now - age

    out = live_guard.run_once(
        pending_orders=[O("stuck", 3600)], positions=[Pos(48 * 3600)],
        cancel_order=lambda oid: cancelled.append(oid), now=now)
    assert out["stuck_orders"] == 1
    assert out["unredeemed"] == 1
    assert cancelled == [], "an unarmed guard must never cancel a real order"


def test_a_losing_session_is_not_a_disarm_condition():
    """Losing money is a strategy question. Not knowing what you hold is a
    trust question. Only the second one disarms."""
    fires, _ = live_guard.should_self_disarm(crash_streak=0, drift=0.0,
                                             unredeemed=0, feed_stale_s=10)
    assert fires is False


@pytest.mark.parametrize("kw", [
    dict(crash_streak=live_guard.CRASH_LOOP_N),
    dict(drift=0.5),
    dict(unredeemed=3),
    dict(feed_stale_s=3600),
])
def test_each_trust_failure_disarms(kw):
    fires, why = live_guard.should_self_disarm(**kw)
    assert fires is True and why


def test_self_disarm_actually_disarms_when_armed(monkeypatch, tmp_path):
    monkeypatch.setattr(live_mode.CONFIG, "preview_mode", False)
    monkeypatch.setattr(live_mode.CONFIG, "live_arm_enabled", True)
    live_mode.arm(reason="test")
    assert live_mode.is_armed() is True
    out = live_guard.run_once(crash_streak=live_guard.CRASH_LOOP_N)
    assert out["self_disarmed"] is True
    assert live_mode.is_preview() is True


def test_the_guard_alerts_on_the_edge_only():
    sent = []
    st = {}
    for _ in range(3):
        live_guard.edge("k", True, "condition", sent.append, st)
    assert len(sent) == 1, "a watcher that repeats itself is one you ignore"
    live_guard.edge("k", False, "condition", sent.append, st)
    assert len(sent) == 2


# --------------------------------------------------------------------------- #
# PENTEST: attacks on the two things that stand between here and real money
# --------------------------------------------------------------------------- #

def test_pentest_forged_gate_result_cannot_admit(monkeypatch):
    """An attacker (or a careless caller) who can call admit() still cannot
    get a wallet in without the gate."""
    for ready, checks in ((False, []), (False, [("x", True, "")])):
        ok, _ = zset.admit("0xEVIL", ready=ready, checks=checks,
                           settled=[P(ideal=9.0)] * 40)
        assert ok is False
    assert zset.wallets() == []


def test_pentest_wash_trade_shaped_record_is_rejected():
    """A wallet that fakes an edge by making a few enormous winners is exactly
    what the concentration rail exists to stop."""
    wash = [P(50, 5000) for _ in range(3)] + [P(50, -25) for _ in range(60)]
    ok, _ = zset.concentration_check(wash)
    assert ok is False
    admitted, _ = zset.admit("0xWASH", ready=True, checks=[], settled=wash)
    assert admitted is False


def test_pentest_arm_file_cannot_be_forged(monkeypatch, tmp_path):
    """Writing the arm file by hand must not arm the bot without the env key."""
    monkeypatch.setattr(live_mode.CONFIG, "preview_mode", False)
    monkeypatch.setattr(live_mode.CONFIG, "live_arm_enabled", False)
    (tmp_path / "live_arm.json").write_text(json.dumps({"armed": True}))
    assert live_mode.is_armed() is False
    assert live_mode.is_preview() is True


@pytest.mark.parametrize("payload", [
    {"armed": "true"}, {"armed": 1}, {"armed": [1]}, {"armed": {"y": 1}},
    {"armed": "True"}, {"armed": 1.0}, {"ARMED": True},
])
def test_pentest_truthy_arm_payloads_all_fail_closed(payload, monkeypatch, tmp_path):
    monkeypatch.setattr(live_mode.CONFIG, "preview_mode", False)
    monkeypatch.setattr(live_mode.CONFIG, "live_arm_enabled", True)
    (tmp_path / "live_arm.json").write_text(json.dumps(payload))
    assert live_mode.is_armed() is False, f"{payload} armed the bot"


def test_pentest_telegram_rejects_a_foreign_chat_id():
    """Command authorisation is a single allowlisted chat. Anything else is
    dropped before dispatch."""
    import inspect

    import src.telegram_bot as tb
    src = inspect.getsource(tb)
    assert "chat_id != CONFIG.telegram_chat_id" in src, (
        "the chat allowlist must gate command dispatch")


def test_pentest_a_hostile_market_title_cannot_break_the_panel(tmp_path,
                                                               monkeypatch):
    """Wallet-supplied text reaches Telegram. It must be escaped, not rendered."""
    from unittest.mock import patch

    import src.telegram_bot as tb
    from src.copy_trading import shadow_quote
    monkeypatch.setattr(shadow_quote.CONFIG, "data_dir", str(tmp_path))
    now = time.time()
    hostile = "<script>alert(1)</script> & <b>bold</b>"
    rows = [{"copy_id": f"c{i}", "target": hostile, "token_id": "t",
             "category": hostile, "their_price": 0.5, "our_price": 0.55,
             "penalty_bps": 100, "penalty_bps_t1": 110, "t1_stale": False,
             "quote_lag_s": 1.0, "boot_flush": False, "book_ts": float(i),
             "notify_latency_s": 60.0, "detected_at": now - 10}
            for i in range(6)]
    with open(tmp_path / "shadow-quotes.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    sent = []
    with patch.object(tb, "_send_chunked", lambda x: sent.append(x)), \
         patch.object(tb, "send_message", lambda x, **k: sent.append(x)):
        tb._handle_speed("/speed 7")
    out = "\n".join(sent)
    assert "<script>" not in out, "hostile title rendered as markup"
    assert "&lt;script&gt;" in out or "script" not in out


def test_pentest_admitting_the_same_wallet_twice_is_idempotent():
    for _ in range(3):
        zset.admit("0xGOOD", ready=True, checks=[], settled=[P(ideal=6.0)] * 30)
    assert len(zset.wallets()) == 1


def test_pentest_z_survives_a_corrupt_store(tmp_path):
    """A corrupt Z file must read as empty, never as "everything allowed"."""
    from src.copy_trading import promotion_state
    promotion_state.clear_cache()
    open(promotion_state.promoted_path("z"), "w").write("{not json")
    promotion_state.clear_cache()
    assert zset.wallets() == []
