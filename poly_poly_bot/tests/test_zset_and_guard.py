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
                             checks=[("fake", False, "")], settled=[P()] * 40, rails_supplied=True)
    assert ok is False
    assert zset.wallets() == []


def test_a_passing_wallet_is_admitted():
    ok, checks = zset.admit("0xGOOD", ready=True, checks=[("gate", True, "")],
                            settled=[P(ideal=6.0) for _ in range(30)], rails_supplied=True)
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
                             checks=[("gate", True, "")], settled=jackpot, rails_supplied=True)
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
    zset.admit("0xGOOD", ready=True, checks=[], settled=[P(ideal=6.0)] * 30, rails_supplied=True)
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
    fires, _ = live_guard.should_self_disarm(crash_streak=0,
                                             unredeemed=0, feed_stale_s=10)
    assert fires is False


@pytest.mark.parametrize("kw", [
    dict(crash_streak=live_guard.CRASH_LOOP_N),
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
                           settled=[P(ideal=9.0)] * 40, rails_supplied=True)
        assert ok is False
    assert zset.wallets() == []


def test_pentest_wash_trade_shaped_record_is_rejected():
    """A wallet that fakes an edge by making a few enormous winners is exactly
    what the concentration rail exists to stop."""
    wash = [P(50, 5000) for _ in range(3)] + [P(50, -25) for _ in range(60)]
    ok, _ = zset.concentration_check(wash)
    assert ok is False
    admitted, _ = zset.admit("0xWASH", ready=True, checks=[], settled=wash, rails_supplied=True)
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
        zset.admit("0xGOOD", ready=True, checks=[], settled=[P(ideal=6.0)] * 30, rails_supplied=True)
    assert len(zset.wallets()) == 1


def test_pentest_z_survives_a_corrupt_store(tmp_path):
    """A corrupt Z file must read as empty, never as "everything allowed"."""
    from src.copy_trading import promotion_state
    promotion_state.clear_cache()
    open(promotion_state.promoted_path("z"), "w").write("{not json")
    promotion_state.clear_cache()
    assert zset.wallets() == []


# --------------------------------------------------------------------------- #
# The detector label: fast and slow rows must never pool
# --------------------------------------------------------------------------- #

def test_the_detection_source_is_persisted(tmp_path, monkeypatch):
    """The prober sets `source`; `_build_row` dropped it, so ~2 wallets
    detected in 1s pooled with ~500 detected in 5 minutes into one headline,
    and a row written unlabelled can never be split afterwards."""
    from src.copy_trading import shadow_quote
    monkeypatch.setattr(shadow_quote.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(shadow_quote, "SECOND_SAMPLE_DELAY_S", 0.0)
    from src.models import MarketSnapshot
    snap = MarketSnapshot(best_bid=0.60, best_ask=0.62, midpoint=0.61,
                          spread=0.02, spread_bps=328, fetched_at=1.0)
    monkeypatch.setattr("src.copy_trading.market_price.fetch_market_snapshot",
                        lambda c, t: snap)
    now = time.time()
    monkeypatch.setattr(shadow_quote, "PROCESS_START_TS", now - 3600)

    fast = shadow_quote.sample_trade(object(), {
        "copy_id": "f1", "token_id": "t", "their_price": 0.5,
        "their_ts": now - 1, "detected_at": now, "source": "fast-prober"})
    slow = shadow_quote.sample_trade(object(), {
        "copy_id": "s1", "token_id": "t", "their_price": 0.5,
        "their_ts": now - 300, "detected_at": now})

    assert fast["source"] == "fast-prober"
    assert slow["source"] == "feed", "an unlabelled row defaults to the feed"


def test_the_two_detectors_are_reportable_separately():
    from src.copy_trading import shadow_quote
    rows = ([{"source": "fast-prober", "notify_latency_s": 1.0,
              "penalty_bps": 50, "quote_lag_s": 1.0, "boot_flush": False,
              "token_id": f"t{i}", "book_ts": float(i)} for i in range(6)]
            + [{"notify_latency_s": 300.0, "penalty_bps": 900,
                "quote_lag_s": 1.0, "boot_flush": False,
                "token_id": f"s{i}", "book_ts": float(100 + i)}
               for i in range(6)])
    fast = shadow_quote.by_source(rows, shadow_quote.FAST_SOURCE)
    feed = shadow_quote.by_source(rows, "feed")
    assert len(fast) == 6 and len(feed) == 6
    assert shadow_quote.summarize(fast)["latency_p50_s"] == 1.0
    assert shadow_quote.summarize(feed)["latency_p50_s"] == 300.0
    # pooled would be ~150s, describing neither
    pooled = shadow_quote.summarize(rows)["latency_p50_s"]
    assert 1.0 < pooled < 300.0


def test_the_z_slice_is_withheld_while_thin(tmp_path, monkeypatch):
    """A two-wallet slice starts at n=0; it must say so, not print a median."""
    from unittest.mock import patch

    import src.telegram_bot as tb
    from src.copy_trading import shadow_quote
    monkeypatch.setattr(shadow_quote.CONFIG, "data_dir", str(tmp_path))
    now = time.time()
    rows = [{"copy_id": f"c{i}", "target": "0xw", "token_id": f"t{i}",
             "category": "sports", "their_price": 0.5, "our_price": 0.55,
             "penalty_bps": 1000, "penalty_bps_t1": 1100, "t1_stale": False,
             "quote_lag_s": 1.0, "boot_flush": False, "book_ts": float(i),
             "notify_latency_s": 300.0, "detected_at": now - 10,
             "source": "feed"} for i in range(8)]
    with open(tmp_path / "shadow-quotes.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    sent = []
    with patch.object(tb, "_send_chunked", lambda x: sent.append(x)), \
         patch.object(tb, "send_message", lambda x, **k: sent.append(x)):
        tb._handle_speed("/speed 7")
    out = sent[0]
    assert "Set Z, detected per-wallet" in out
    assert "Too thin to report" in out
    assert "—" not in out and "–" not in out


# --------------------------------------------------------------------------- #
# Round-1 verification: five reproduced defects, pinned
# --------------------------------------------------------------------------- #

def test_a_z_wallet_resolves_to_a_tier(tmp_path, monkeypatch):
    """D1, the inversion. `get_wallet_tier` read the LEGACY store, so Z
    wallets returned None and `trade_executor` skipped them before pricing,
    while the 21 env wallets kept their tiers and were the only ones that
    would have traded. Exactly backwards."""
    from src.copy_trading import strategy_config
    zset.admit("0xZWALLET", ready=True, checks=[], settled=[P(ideal=6.0)] * 30, rails_supplied=True)
    assert strategy_config.get_wallet_tier("0xZWALLET") == "1b"


def test_a_non_z_wallet_has_no_tier_even_if_env_lists_it(monkeypatch):
    """The env tier lists must not grant live access on their own."""
    from src.copy_trading import strategy_config
    monkeypatch.setattr(strategy_config, "_wallet_tier_map", {"0xenvonly": "1a"})
    assert strategy_config.get_wallet_tier("0xENVONLY") is None


def test_the_live_universe_is_z_only():
    """D1b. `user_addresses` resolved to 21 ungated env wallets; leaving them
    in the live list made Z decorative."""
    import ast
    import inspect

    from src.copy_trading import strategy_config, trade_monitor

    def _reads(fn, dotted):
        """Does the function actually READ this attribute (not a comment)?"""
        tree = ast.parse(inspect.getsource(fn).lstrip())
        want = dotted.split(".")[-1]
        return any(isinstance(n, ast.Attribute) and n.attr == want
                   for n in ast.walk(tree))

    assert not _reads(trade_monitor.fetch_all_trader_activities, "user_addresses")
    assert _reads(trade_monitor.fetch_all_trader_activities, "wallets")
    src = inspect.getsource(strategy_config.get_all_tiered_wallets)
    assert "TIER_1A.wallets" not in src and "TIER_1B.wallets" not in src


def test_a_blacklisted_wallet_cannot_enter_z(tmp_path, monkeypatch):
    """D2. 0x4a3f86ed was admitted while carrying an ACTIVE auto-demote
    (n=15, roi -6.4%, until 2026-08-18). It slipped the contradiction rail
    because the demotion had already removed its clean-era rows, so the rail
    saw 'not enough evidence to contradict'. Admitting on headline ROI over
    the system's own recorded verdict is the failure."""
    from src.copy_trading import promotion_state
    promotion_state.add_blacklist("0xDEMOTED", until=time.time() + 86400,
                                  reason="auto-demote")
    ok, checks = zset.admit("0xDEMOTED", ready=True, checks=[],
                            settled=[P(ideal=9.0)] * 40, rails_supplied=True)
    assert ok is False
    assert any("auto-demote" in lab and not good for lab, good, _d in checks)
    assert zset.wallets() == []


def test_the_contradiction_rail_is_a_property_of_z_not_a_script():
    """It used to live only in `scripts/seed_zset.py`, so it protected one
    script run rather than the set."""
    ok, detail = zset.contradiction_check(-0.19, 15)
    assert ok is False and "-19%" in detail
    ok2, _ = zset.contradiction_check(-0.19, 3)      # too thin to contradict
    assert ok2 is True
    # and admit enforces it
    admitted, _ = zset.admit("0xCONTRA", ready=True, checks=[],
                             settled=[P(ideal=6.0)] * 30,
                             other_book_roi=-0.19, other_book_n=15, rails_supplied=True)
    assert admitted is False


def test_a_hand_written_z_entry_is_ignored(tmp_path):
    """Secondary finding: the store is a file on a box. A hand-added record
    lacks the gate source and must be inert, not tradable."""
    from src.copy_trading import promotion_state
    # Admit through the gate first: that initialises the eviction history, so
    # this test isolates the SOURCE filter rather than tripping the
    # missing-history rule.
    zset.admit("0xLEGIT", ready=True, checks=[], settled=[P(ideal=6.0)] * 30,
               rails_supplied=True)
    promotion_state.add_promoted("0xATTACKER", tier="1a", source="telegram",
                                 scope=zset.SCOPE)
    assert [w.lower() for w in zset.wallets()] == ["0xlegit"], (
        "a non-gate record reached the live list")


def test_the_guard_loop_reads_functions_that_exist():
    """D3. The loop imported `inventory.get_open_positions`, which does not
    exist; the ImportError was swallowed and every detector became
    structurally incapable of firing while the log said the guard was up."""
    import inspect

    from src.copy_trading import live_guard, trade_queue
    assert hasattr(trade_queue, "peek_pending_orders")
    assert hasattr(live_guard, "redeemable_positions")

    import main
    src = inspect.getsource(main._live_guard_loop)
    assert "get_open_positions" not in src, "imports a function that does not exist"
    assert "peek_pending_orders" in src
    # The redeemer's own source, not the inventory store: inventory holds OPEN
    # positions and knows nothing about resolution, so it could never answer
    # "what failed to redeem".
    assert "redeemable_positions" in src
    # a failed read must be logged AND must count toward the crash streak,
    # or the guard keeps passing on empty inputs forever
    assert "could not read pending orders" in src
    assert "read_failed" in src


def test_the_guard_reads_dict_positions(monkeypatch, tmp_path):
    """`inventory.Position` is a dict alias, so `getattr(p, "closed")` was
    always False and three of four triggers could never fire on real state."""
    now = time.time()
    dict_rows = [{"closed": True, "redeemed": False, "closed_ts": now - 48 * 3600}
                 for _ in range(3)]
    assert len(live_guard.find_unredeemed(dict_rows, now=now)) == 3
    dict_orders = [{"order_id": "o1", "placed_at": now - 3600}]
    assert len(live_guard.find_stuck_orders(dict_orders, now=now)) == 1


def test_chain_redeemable_rows_count_as_unredeemed(tmp_path, monkeypatch):
    """A row from the redeemable API has no closed/closed_ts to test: being in
    that list at all IS the finding."""
    monkeypatch.setattr(live_guard.CONFIG, "data_dir", str(tmp_path))
    out = live_guard.run_once(redeemable=[{"asset": "a"}, {"asset": "b"},
                                          {"asset": "c"}])
    assert out["unredeemed"] == 3
    assert out["disarm_reason"], "3 stuck redemptions must trip self-disarm"


def test_the_drills_do_not_write_the_real_guard_state():
    """D5. Running the drills flipped the live `live_guard.json` edge state,
    so the next production pass sent the owner 'resolved' messages for
    conditions that never happened. A drill that alerts the owner is worse
    than no drill."""
    import inspect
    src = open("scripts/golive_drills.py", encoding="utf-8").read()
    assert "tempfile.mkdtemp" in src
    assert "live_guard.CONFIG.data_dir = sandbox" in src
    assert "CONFIG.data_dir = real_data_dir" in src


def test_the_drills_never_call_arm():
    """Inert today only because the env key is unset; it would arm the bot
    the day that key is set, against the script's own banner."""
    import ast
    tree = ast.parse(open("scripts/golive_drills.py", encoding="utf-8").read())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "arm"]
    assert calls == [], "the drills must not be able to arm the bot"


def test_every_disarm_trigger_is_actually_fed_by_production():
    """The class that survived three verification rounds.

    Every guard test asserted on `run_once`'s kwargs, and none asserted on the
    PRODUCTION call site, so a trigger the loop forgot to supply stayed green
    forever while the drills fed it by hand. Three rounds running, the fix
    closed one instance and the next round found another. This asserts the
    contract at the seam instead: every parameter `should_self_disarm` accepts
    must actually be passed by `_live_guard_loop`.
    """
    import ast
    import inspect

    import main

    trigger_params = {
        p.name for p in
        inspect.signature(live_guard.should_self_disarm).parameters.values()
        if p.kind is inspect.Parameter.KEYWORD_ONLY
    }
    assert trigger_params, "no triggers found; the signature changed shape"

    tree = ast.parse(inspect.getsource(main._live_guard_loop).lstrip())
    supplied = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run_once"):
            supplied |= {kw.arg for kw in node.keywords if kw.arg}
    # `unredeemed` is derived inside run_once from `redeemable`/`positions`.
    derived = {"unredeemed": {"redeemable", "positions"}}
    missing = []
    for t in sorted(trigger_params):
        if t in supplied:
            continue
        if t in derived and (derived[t] & supplied):
            continue
        missing.append(t)
    assert not missing, (
        f"live_guard advertises trigger(s) {missing} that the production loop "
        f"never supplies. Wire them or delete them: a trigger nothing can feed "
        f"is a false green on the safety net itself.")


def test_neg_risk_positions_do_not_disarm_the_session(monkeypatch, tmp_path):
    """The redeemer SKIPS neg-risk positions by design, so they sit in the
    redeemable list forever. Counting presence alone meant three of them would
    permanently self-disarm a live session for something that was never going
    to be redeemed."""
    monkeypatch.setattr(live_guard.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(
        "src.copy_trading.auto_redeemer._fetch_redeemable_positions",
        None, raising=False)
    negs = [{"negRisk": True, "title": "n"} for _ in range(5)]
    assert all(live_guard._is_neg_risk(p) for p in negs)
    # run_once counts what it is given; the exclusion happens at the source,
    # so assert the source filter rather than the counter
    import inspect
    src = inspect.getsource(live_guard.redeemable_positions)
    assert "_is_neg_risk" in src


def test_the_drills_themselves_still_run(tmp_path, monkeypatch, capsys):
    """The drills are a script, so CI never ran them and they rotted: deleting
    a guard kwarg left the suite green while `golive_drills.py` died with a
    TypeError on the deployed image. A check that nothing gates is a check
    that decays. This gates it.
    """
    import importlib
    import sys

    sys.path.insert(0, "scripts")
    drills = importlib.import_module("golive_drills")
    importlib.reload(drills)
    monkeypatch.setattr(drills.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(drills, "_results", [])
    rc = drills.main()
    out = capsys.readouterr().out
    assert "drills passed" in out
    assert rc == 0, out


# --------------------------------------------------------------------------- #
# Round-3 manager gate: the expiry bomb and the fail-open door
# --------------------------------------------------------------------------- #

def test_eviction_is_sticky_past_the_demote_window(tmp_path, monkeypatch):
    """B1, the dated regression. The bot's auto-demote is TIME-BOXED:
    0x4a3f86ed's block expires 2026-08-18. Without a durable eviction record a
    re-run of the seeding script after that date would re-admit it on the very
    evidence the bot demoted it for, days before the flip."""
    from src.copy_trading import promotion_state

    # demoted, evicted, then the demote window expires
    promotion_state.add_blacklist("0xDEMOTED", until=time.time() + 60,
                                  reason="auto-demote")
    zset.admit("0xDEMOTED", ready=True, checks=[], settled=[P(ideal=9.0)] * 40, rails_supplied=True)
    assert zset.wallets() == []
    zset.evict("0xDEMOTED", reason="active auto-demote")

    # fast-forward past the demote expiry: the blacklist no longer blocks
    monkeypatch.setattr(promotion_state, "is_blacklisted",
                        lambda w, now=None, scope="": False)
    ok, checks = zset.admit("0xDEMOTED", ready=True, checks=[],
                            settled=[P(ideal=9.0)] * 40, rails_supplied=True)
    assert ok is False, "the expiry bomb: it walked back in"
    assert any("evicted" in lab and not good for lab, good, _d in checks)


def test_readmission_is_explicit(tmp_path):
    zset.evict("0xGONE", reason="test")
    ok, _ = zset.admit("0xGONE", ready=True, checks=[], settled=[P(ideal=6.0)] * 30, rails_supplied=True)
    assert ok is False
    assert zset.readmit("0xGONE", reason="reviewed") is True
    ok2, _ = zset.admit("0xGONE", ready=True, checks=[], settled=[P(ideal=6.0)] * 30, rails_supplied=True)
    assert ok2 is True


def test_an_unreadable_demote_list_refuses_rather_than_passes(tmp_path):
    """B2. `promotion_state._read` returns {} for corrupt JSON, which would
    turn 'no active demote' into a pass for every wallet. A blacklist we
    cannot read is not an empty blacklist."""
    from src.copy_trading import promotion_state
    with open(promotion_state.blacklist_path(""), "w") as f:
        f.write("{not json")
    promotion_state.clear_cache()
    blocked = zset._blacklist_block("0xANY")
    assert blocked is not None and "unreadable" in blocked
    ok, _ = zset.admit("0xANY", ready=True, checks=[], settled=[P(ideal=6.0)] * 30, rails_supplied=True)
    assert ok is False


def test_unreadable_eviction_history_closes_the_set(tmp_path, monkeypatch):
    # Both files present, so the probe gets past the missing-file branch and
    # exercises the read failure itself.
    zset.admit("0xANY0", ready=True, checks=[], settled=[P(ideal=6.0)] * 30,
               rails_supplied=True)
    monkeypatch.setattr(zset.promotion_state, "retired_map",
                        lambda scope="": (_ for _ in ()).throw(OSError("boom")))
    assert zset.evicted_set() == {"*"}
    ok, _ = zset.admit("0xANY", ready=True, checks=[], settled=[P(ideal=6.0)] * 30, rails_supplied=True)
    assert ok is False, "unreadable eviction history must close Z, not open it"


def test_admit_refuses_a_caller_that_omits_the_rail_evidence():
    """The rails defaulted to PASS: `other_book_*` at (None, 0) printed the
    contradiction check as PASS for a caller who simply omitted it, and
    `era_floor=None` computed the concentration rail over the pre-clean-era
    rows this repo spent months invalidating. A caller who does not supply the
    evidence now gets a refusal, not a free pass."""
    ok, checks = zset.admit("0xLAZY", ready=True, checks=[],
                            settled=[P(ideal=9.0)] * 40)
    assert ok is False
    assert any("rail evidence" in lab for lab, _o, _d in checks)
    assert zset.wallets() == []


def test_a_stuck_order_is_detected_in_the_unit_production_writes():
    """`trade_executor` stamps `placed_at = time.time() * 1000`. Comparing
    that against epoch SECONDS made a real resting order read as decades in
    the future, so the detector and the armed cancel were both dead on
    production state while the drill stayed green on its own seconds shape."""
    now = time.time()
    ms_order = {"order_id": "prod", "placed_at": (now - 3600) * 1000}
    s_order = {"order_id": "drill", "placed_at": now - 3600}
    assert len(live_guard.find_stuck_orders([ms_order], now=now)) == 1
    assert len(live_guard.find_stuck_orders([s_order], now=now)) == 1
    fresh_ms = {"order_id": "fresh", "placed_at": (now - 5) * 1000}
    assert live_guard.find_stuck_orders([fresh_ms], now=now) == []


def test_a_failed_redeemable_read_is_not_reported_as_resolved(tmp_path,
                                                              monkeypatch):
    """[] means nothing is stuck; a failed read means we do not know. Reporting
    the second as the first sends a 'resolved' message for a condition nobody
    confirmed cleared, and blinds the trigger."""
    monkeypatch.setattr(live_guard.CONFIG, "data_dir", str(tmp_path))
    sent = []
    # first: a real finding
    live_guard.run_once(redeemable=[{"a": 1}], send=sent.append)
    assert sent, "the finding should have alerted"
    sent.clear()
    # then the read FAILS: no clearance message may be sent
    out = live_guard.run_once(redeemable=None, send=sent.append)
    assert sent == [], "a failed read must not announce a clearance"
    assert out["unredeemed"] == 0


def test_the_executor_checks_set_z_outside_the_tiered_branch():
    """Two live bypasses existed while the check sat inside `if TIERED_MODE`:
    `onchain_source` builds its own list from CONFIG.user_addresses, and the
    legacy non-tiered branch consults no wallet set at all."""
    import inspect

    from src.copy_trading import trade_executor
    src = inspect.getsource(trade_executor.place_trade_orders)
    zi = src.index("not in set Z")
    ti = src.index("if TIERED_MODE:")
    assert zi < ti, "the Z check must come BEFORE the tiered branch"


def test_wallet_set_is_derived_from_the_filtered_list(tmp_path):
    """`wallet_set()` read the store raw, so it was the forgeable twin of
    `wallets()` and the more natural name for 'is this wallet in Z'."""
    from src.copy_trading import promotion_state
    promotion_state.add_promoted("0xHAND", tier="1a", source="telegram",
                                 scope=zset.SCOPE)
    assert zset.wallet_set() == set()


# --------------------------------------------------------------------------- #
# Round-4: the kill switch must not lie, and the gate needs a BEHAVIOURAL test
# --------------------------------------------------------------------------- #

def test_zset_drop_reports_a_failed_eviction_as_a_failure(tmp_path, monkeypatch):
    """The owner's only manual kill switch. `evict()` returns False when the
    durable record cannot be written, and the handler threw that away and sent
    'Real money can no longer follow it' while the wallet was still in Z: the
    headline and the wallet count contradicting each other in one message."""
    from unittest.mock import patch

    import src.telegram_bot as tb
    zset.admit("0xAAAABBBB", ready=True, checks=[], settled=[P(ideal=6.0)] * 30,
               rails_supplied=True)
    assert len(zset.wallets()) == 1

    monkeypatch.setattr(zset.promotion_state, "add_retired",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("ENOSPC")))
    sent = []
    with patch.object(tb, "send_message", lambda x, **k: sent.append(x)), \
         patch.object(tb, "_send_chunked", lambda x: sent.append(x)):
        tb._handle_zset("/zset drop 0xAAAABBBB")
    out = "\n".join(sent)
    assert "EVICTION FAILED" in out, out
    assert "can no longer follow" not in out
    assert len(zset.wallets()) == 1, "and it really is still in Z"


def test_a_deleted_eviction_history_closes_the_set(tmp_path):
    """Corrupt already failed closed; MISSING did not, which left the
    2026-08-18 expiry live on its third failure mode. `seed_zset` draws
    candidates from the ledger every run, so the demoted wallet stays a
    candidate forever."""
    import os

    from src.copy_trading import promotion_state
    zset.admit("0xKEEP", ready=True, checks=[], settled=[P(ideal=6.0)] * 30,
               rails_supplied=True)
    zset.evict("0xGONE", reason="test")
    os.remove(promotion_state.retired_path(zset.SCOPE))
    promotion_state.clear_cache()
    assert zset.evicted_set() == {"*"}
    assert zset.wallets() == [], "a missing history must close Z, not open it"
    ok, _ = zset.admit("0xGONE", ready=True, checks=[],
                       settled=[P(ideal=9.0)] * 40, rails_supplied=True)
    assert ok is False


def test_an_evicted_wallet_is_off_the_money_path_even_if_removal_failed(tmp_path,
                                                                        monkeypatch):
    """`evict` removes AND records; the removal half can fail on its own."""
    from src.copy_trading import promotion_state
    zset.admit("0xSTUCK", ready=True, checks=[], settled=[P(ideal=6.0)] * 30,
               rails_supplied=True)
    monkeypatch.setattr(promotion_state, "remove_promoted",
                        lambda w, scope="": False)
    zset.evict("0xSTUCK", reason="record only")
    assert zset.wallets() == [], "the eviction record must subtract it anyway"


@pytest.mark.asyncio
async def test_a_non_z_wallet_places_no_order_even_with_tiering_off(tmp_path,
                                                                    monkeypatch):
    """BEHAVIOURAL, not textual. The only previous guard on this gate asserted
    source ORDER, and a plausible re-indent into the `TIERED_MODE and
    TIER_1C.enabled` block above it re-arms 21 env wallets with the whole
    suite green. This exercises the money path instead."""
    from src.copy_trading import trade_executor
    from src.models import DetectedTrade, QueuedTrade

    class Boom:
        def __getattr__(self, _n):
            raise AssertionError("the executor reached the CLOB for a non-Z wallet")

    monkeypatch.setattr(trade_executor.live_mode, "is_preview", lambda: False)
    monkeypatch.setattr(trade_executor, "_strategy_config",
                        lambda: (False, lambda a: None, None))  # TIERED_MODE off

    t = DetectedTrade(id="t1", trader_address="0xDEADBEEF",
                      timestamp="2026-08-17T00:00:00+00:00", market="m",
                      token_id="tok", side="BUY", size=100.0, price=0.5)
    monkeypatch.setattr(trade_executor, "_trade_queue",
                        lambda: (lambda *a, **k: None, lambda *a, **k: None,
                                 lambda *a, **k: 0))
    seen = set()
    monkeypatch.setattr(trade_executor, "_trade_store",
                        lambda: (lambda i: False, lambda i: seen.add(i),
                                 lambda i: 0, lambda i: False,
                                 lambda r: None, lambda k, s: 0))
    assert zset.wallets() == [], "Z must be empty for this test to mean anything"
    await trade_executor.place_trade_orders(
        [QueuedTrade(trade=t, enqueued_at=0.0, source_detected_at=0.0)], Boom())
    assert t.id in seen, "the trade should have been marked seen and skipped"
