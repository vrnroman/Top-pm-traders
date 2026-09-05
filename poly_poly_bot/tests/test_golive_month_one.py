"""Run s-yr3unh (2026-09-05): the month-one go-live batch.

The guard's clock, its silence while unarmed, the redeemer's retry, and the
bankroll governor. Each test names the defect it pins, in the style of
test_zset_and_guard.py: assert on the production seam, not on a helper.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from unittest.mock import patch

import pytest

from src.config import CONFIG
from src.copy_trading import live_budget, live_guard, live_mode, trade_store


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --------------------------------------------------------------------------- #
# The guard's stale-feed clock
# --------------------------------------------------------------------------- #

def test_the_guard_clock_is_the_live_poller_not_the_shadow_log():
    """255 flaps in 20 days: 'newest shadow-quote row' measured the market's
    quiet, not the pipeline's health. The production loop must read the
    poller's own liveness stamp."""
    import main
    src = inspect.getsource(main._live_guard_loop)
    assert "last_poll_ok_ts" in src
    assert "shadow_quote.load_rows" not in src, "still reading the market's clock"
    assert "guard_started" in src, "a poller that never succeeds after boot must still trip it"


def test_poll_ok_round_trip():
    trade_store.record_poll_ok(123.0)
    assert trade_store.last_poll_ok_ts() == 123.0


def test_fetch_all_stamps_the_clock_only_when_a_wallet_answered(monkeypatch):
    """A poll where every fetch failed is the outage the trigger exists for;
    it must not look like a heartbeat."""
    from src.copy_trading import trade_monitor, zset

    monkeypatch.setattr(zset, "wallets", lambda: ["0x" + "a" * 40])
    trade_store.record_poll_ok(1.0)

    async def boom(addr):
        raise RuntimeError("429")
    monkeypatch.setattr(trade_monitor, "fetch_trader_activity", boom)
    assert _run(trade_monitor.fetch_all_trader_activities()) == []
    assert trade_store.last_poll_ok_ts() == 1.0, "all fetches failed, clock must not move"

    async def quiet(addr):
        return []
    monkeypatch.setattr(trade_monitor, "fetch_trader_activity", quiet)
    _run(trade_monitor.fetch_all_trader_activities())
    assert trade_store.last_poll_ok_ts() > 1.0, "a quiet wallet still answered"


def test_an_empty_z_is_a_completed_poll_not_a_dead_pipeline(monkeypatch):
    from src.copy_trading import trade_monitor, zset
    monkeypatch.setattr(zset, "wallets", lambda: [])
    trade_store.record_poll_ok(1.0)
    _run(trade_monitor.fetch_all_trader_activities())
    assert trade_store.last_poll_ok_ts() > 1.0


# --------------------------------------------------------------------------- #
# Silence while unarmed
# --------------------------------------------------------------------------- #

def test_self_disarm_is_detected_but_not_messaged_while_unarmed(tmp_path, monkeypatch):
    """The owner received ~12 'Self-disarmed, back to paper' messages a day
    from a session that was never off paper."""
    monkeypatch.setattr(live_guard.CONFIG, "data_dir", str(tmp_path))
    sent: list = []
    out = live_guard.run_once(feed_stale_s=3600, send=sent.append)
    assert out["disarm_reason"], "the condition is still detected"
    assert out["self_disarmed"] is False
    assert sent == [], "and nothing is sent while there is no arm to pull"
    assert live_guard._read_state().get("self_disarm") is True, "the edge is still logged"


def test_self_disarm_is_messaged_when_armed(tmp_path, monkeypatch):
    monkeypatch.setattr(live_guard.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(live_guard.live_mode, "is_armed", lambda: True)
    monkeypatch.setattr(live_guard.live_mode, "disarm", lambda by="": True)
    sent: list = []
    out = live_guard.run_once(feed_stale_s=3600, send=sent.append)
    assert out["self_disarmed"] is True
    assert sent and "Self-disarmed" in sent[0]


# --------------------------------------------------------------------------- #
# The redeemer's positions read
# --------------------------------------------------------------------------- #

class _Resp:
    def raise_for_status(self):
        return None

    def json(self):
        return []


def _client_factory(fail_times: int, calls: list):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            calls.append(1)
            if len(calls) <= fail_times:
                raise RuntimeError("429 Too Many Requests")
            return _Resp()
    return _Client


def test_redeemer_fetch_retries_with_backoff_then_raises(monkeypatch):
    """27 rate-limit failures in 20 days each read as 'nothing to redeem'."""
    from src.copy_trading import auto_redeemer
    calls: list = []
    monkeypatch.setattr(auto_redeemer.httpx, "AsyncClient", _client_factory(99, calls))
    sleeps: list = []

    async def fake_sleep(s):
        sleeps.append(s)
    with pytest.raises(auto_redeemer.RedeemFetchError):
        _run(auto_redeemer._fetch_redeemable_positions("0xp", sleep=fake_sleep))
    assert len(calls) == 4
    assert sleeps == [1.0, 3.0, 9.0]


def test_redeemer_fetch_recovers_on_a_later_attempt(monkeypatch):
    from src.copy_trading import auto_redeemer
    calls: list = []
    monkeypatch.setattr(auto_redeemer.httpx, "AsyncClient", _client_factory(2, calls))

    async def fake_sleep(s):
        return None
    out = _run(auto_redeemer._fetch_redeemable_positions("0xp", sleep=fake_sleep))
    assert out == [] and len(calls) == 3


def test_guard_reports_a_failed_positions_read_as_unknown(monkeypatch):
    """None, not []: the guard must not announce 'resolved' for a condition
    nobody confirmed cleared."""
    from src.copy_trading import auto_redeemer

    async def boom(w, **k):
        raise auto_redeemer.RedeemFetchError("429")
    monkeypatch.setattr(auto_redeemer, "_fetch_redeemable_positions", boom)
    assert live_guard.redeemable_positions("0xp") is None


def test_redeem_pass_survives_an_unreadable_positions_list(monkeypatch):
    from src.copy_trading import auto_redeemer
    monkeypatch.setattr(auto_redeemer.CONFIG, "proxy_wallet", "0xp")

    async def boom(w, **k):
        raise auto_redeemer.RedeemFetchError("429")
    monkeypatch.setattr(auto_redeemer, "_fetch_redeemable_positions", boom)
    res = _run(auto_redeemer.check_and_redeem_positions("ab" * 32))
    assert res.count == 0


# --------------------------------------------------------------------------- #
# The bankroll governor
# --------------------------------------------------------------------------- #

def _cfg_1b():
    from src.copy_trading.strategy_config import TierConfig
    return TierConfig(tier="1b", enabled=True, wallets=[], copy_percentage=5.0,
                      max_bet=25.0, min_bet=5.0, max_total_exposure=200.0,
                      max_price=0.90, min_price=0.10, min_trader_bet=10000.0,
                      hold_to_settlement=False, alert_only=False)


@pytest.fixture
def budget(monkeypatch):
    monkeypatch.setattr(CONFIG, "min_order_size_usd", 5.0)
    monkeypatch.setattr(CONFIG, "max_copies_per_market_side", 2)
    monkeypatch.setattr(CONFIG, "copy_paper_min_usd", 300.0)
    monkeypatch.setattr(CONFIG, "max_daily_volume_usd", 500.0)
    monkeypatch.setattr(live_budget, "_balance_cache", None)

    def _set(v):
        monkeypatch.setattr(CONFIG, "live_budget_usd", v)
    return _set


def test_governor_is_closed_live_and_transparent_in_preview_when_unset(budget):
    budget(0.0)
    cfg, why = live_budget.govern_tier(_cfg_1b(), live=True)
    assert why and "LIVE_BUDGET_USD" in why
    cfg, why = live_budget.govern_tier(_cfg_1b(), live=False)
    assert why is None and cfg.max_bet == 25.0, "preview rehearses on the tier's own caps"


def test_governor_caps_are_fractions_of_the_stated_budget(budget):
    budget(310.0)
    c = live_budget.caps(live=False)
    assert (c.per_copy_usd, c.per_market_usd, c.daily_usd, c.exposure_usd) == (7.75, 15.5, 93.0, 248.0)
    assert c.min_trader_bet_usd == 300.0 and c.balance_read is False
    cfg, why = live_budget.govern_tier(_cfg_1b(), live=False)
    assert why is None
    assert (cfg.max_bet, cfg.max_total_exposure, cfg.min_trader_bet) == (7.75, 200.0, 300.0)


def test_governor_never_exceeds_the_chain_balance_when_live(budget):
    budget(310.0)
    c = live_budget.caps(live=True, balance=120.0)
    assert c.effective_usd == 120.0 and c.balance_read is True
    c2 = live_budget.caps(live=True, balance=900.0)
    assert c2.effective_usd == 310.0, "the stated budget is the ceiling"


def test_governor_uses_the_stated_number_when_the_chain_is_unreadable(budget, monkeypatch):
    budget(310.0)
    monkeypatch.setattr(live_budget, "_read_balance", lambda now=None: None)
    c = live_budget.caps(live=True)
    assert c.effective_usd == 310.0 and c.balance_read is False
    assert any("unreadable" in ln for ln in live_budget.status_lines(live=True))


def test_governor_refuses_rather_than_rounding_up_a_tiny_budget(budget):
    budget(150.0)
    cfg, why = live_budget.govern_tier(_cfg_1b(), live=False)
    assert why and "$3.75" in why and "$200" in why, why


def test_daily_cap_is_the_lower_of_env_and_governor(budget):
    budget(310.0)
    assert live_budget.daily_cap(live=False) == 93.0
    budget(0.0)
    assert live_budget.daily_cap(live=False) == 500.0


def test_tiered_sizing_end_to_end_copies_from_300_at_2_5_percent(budget, monkeypatch, tmp_path):
    """The seam: evaluate_tiered_trade, not the helper. A $10,000 target trade
    sizes to $7.75, a $200 one is refused for the evidence base's reason, a
    $300 one is copied."""
    from datetime import datetime, timezone

    from src.copy_trading import daily_spend_guard, strategy_config, tiered_risk_manager
    from src.models import DetectedTrade
    budget(310.0)
    monkeypatch.setattr(CONFIG, "preview_mode", True)
    monkeypatch.setattr(CONFIG, "max_trade_age_hours", 1.0)
    monkeypatch.setattr(tiered_risk_manager, "_STATE_FILE", str(tmp_path / "t.json"))
    monkeypatch.setattr(daily_spend_guard, "_STATE_FILE", str(tmp_path / "d.json"))
    monkeypatch.setattr(tiered_risk_manager, "get_tier_config", lambda t: _cfg_1b())
    tiered_risk_manager._tier_exposures["1b"] = tiered_risk_manager.TierExposure()

    def trade(size):
        return DetectedTrade(id="t", trader_address="0x" + "a" * 40,
                             timestamp=datetime.now(timezone.utc).isoformat(),
                             market="m", side="BUY", size=size, price=0.5)
    d = tiered_risk_manager.evaluate_tiered_trade(trade(10000.0), "1b")
    assert d.should_copy and d.copy_size == 7.75
    d = tiered_risk_manager.evaluate_tiered_trade(trade(200.0), "1b")
    assert not d.should_copy and "300" in d.reason
    d = tiered_risk_manager.evaluate_tiered_trade(trade(300.0), "1b")
    assert d.should_copy and d.copy_size == 7.75


def test_live_panel_renders_the_governor(budget, monkeypatch):
    import src.telegram_bot as tb
    budget(310.0)
    monkeypatch.setattr(CONFIG, "preview_mode", True)
    sent: list = []
    with patch.object(tb, "send_message", lambda x, **k: sent.append(x)), \
         patch.object(tb, "_send_chunked", lambda x: sent.append(x)):
        tb._handle_live("/live")
    out = "\n".join(sent)
    assert "Bankroll governor" in out and "$7.75" in out and "from $300" in out


def test_deploy_sets_the_budget_default_only_when_absent():
    src = open("../.github/workflows/deploy.yml", encoding="utf-8").read()
    assert "ensure_env LIVE_BUDGET_USD 310" in src


# --------------------------------------------------------------------------- #
# /zset candidates: the gate proposes, the owner admits
# --------------------------------------------------------------------------- #

W1 = "0x" + "1" * 40
W2 = "0x" + "2" * 40


class _P:
    """Minimal PaperPosition stand-in for the rails."""

    def __init__(self, spent=50.0, ideal=5.0, opened=1000.0, closed=True):
        self.spent, self.ideal_pnl = spent, ideal
        self.opened_ts, self.closed = opened, closed


def _row(cid, target, *, won=True, price=0.5, spent=20.0, opened=5000.0,
         category="sports", exited=False):
    pnl = (spent / price - spent) if won else -spent
    return {"copy_id": cid, "target": target, "condition_id": "c" + cid,
            "token_id": "t" + cid, "outcome_index": 0, "category": category,
            "their_price": price, "entry_price": price, "shares": spent / price,
            "spent": spent, "drag_bps": 100, "opened_ts": opened, "title": "m",
            "slug": "", "event_key": "", "flagged_by": [], "strategy": "B",
            "horizon_days": 0.0, "mark_price": 0.0, "marked_ts": 0.0,
            "unrealized_pnl": 0.0, "closed": True, "won": won, "pnl": pnl,
            "ideal_pnl": pnl, "closed_ts": opened + 3600, "exited_early": exited,
            "cost_usd": 0.02, "ideal_cost_usd": 0.5}


@pytest.fixture
def books(tmp_path, monkeypatch):
    """Two paper books on disk, an era floor, and isolated stores."""
    import json as _json

    from src.copy_trading import zset
    b = tmp_path / "b.jsonl"
    with open(b, "w") as f:
        for i in range(6):
            f.write(_json.dumps(_row(f"b{i}", W1, won=(i != 5), opened=5000.0 + i)) + "\n")
        for i in range(6):
            f.write(_json.dumps(_row(f"w2-{i}", W2, won=True, opened=5000.0 + i)) + "\n")
    (tmp_path / "a.jsonl").write_text("")
    (tmp_path / "ab_race_state.json").write_text(_json.dumps({"era_floor_ts": 1.0}))
    for mod in (zset.promotion_state, live_guard, live_mode):
        monkeypatch.setattr(mod.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(CONFIG, "copy_paper_b_ledger", str(b))
    monkeypatch.setattr(CONFIG, "copy_paper_ledger", str(tmp_path / "a.jsonl"))
    zset.promotion_state.clear_cache()
    yield tmp_path
    zset.promotion_state.clear_cache()


def _passer(wallet, settled=None, ok=True):
    from src.copy_trading import zset_candidates as zc
    settled = settled if settled is not None else [_P(ideal=6.0, opened=5.0) for _ in range(30)]
    return zc.Candidate(wallet=wallet, ok=ok, gate_ready=ok,
                        checks=[("≥30 settled copies", ok, "30")],
                        gate_checks=[("≥30 settled copies", ok, "30")],
                        ideal_roi=0.12, n_ideal=30, paper_roi=0.11, trimmed_roi=0.07,
                        n_trimmed_kept=27, n_trimmed_dropped=3, settled=settled,
                        a_roi=None, a_n=0, last_ts=time.time() - 3600, n_open=1)


def test_candidates_render_one_card_per_passer_with_an_admit_button(books, monkeypatch):
    import src.telegram_bot as tb
    from src.copy_trading import shadow_quote
    from src.copy_trading import zset_candidates as zc

    monkeypatch.setattr(zc, "candidates", lambda *a, **k: ([_passer(W1)], [], None))
    quotes = [{"copy_id": f"b{i}", "target": W1, "source": "fast-prober",
               "our_price": 0.51, "penalty_bps": 200, "quote_lag_s": 1.0,
               "boot_flush": False, "notify_latency_s": 5.0} for i in range(6)]
    monkeypatch.setattr(shadow_quote, "load_rows", lambda *a, **k: quotes)
    sent: list = []
    with patch.object(tb, "send_message", lambda x, **k: sent.append((x, k))):
        tb._handle_zset("/zset candidates")
    header, card = sent[0][0], sent[1][0]
    assert "Admission is yours" in header and "ordered by real-quote sample size" in header
    assert "candidate" in card and W1 in card
    assert "real quotes: thin, 6 of 6 matched" in card, "9-of-58 must never render as a headline"
    assert "concentration rail: +7.0%" in card and "not used to order" in card
    assert "mirrored exits: 0 of 30 settled" in card
    assert "entry penalty: median 200bps" in card
    kb = sent[1][1]["reply_markup"]["inline_keyboard"][0][0]
    assert kb["callback_data"] == f"zadm:{W1}" and "Admit" in kb["text"]


def test_a_wallet_already_in_z_gets_a_card_but_no_button(books, monkeypatch):
    import src.telegram_bot as tb
    from src.copy_trading import shadow_quote, zset
    from src.copy_trading import zset_candidates as zc
    zset.admit(W2, ready=True, checks=[], settled=[_P(ideal=6.0, opened=5.0)] * 30,
               era_floor=1.0, rails_supplied=True)
    monkeypatch.setattr(zc, "candidates", lambda *a, **k: ([], [], None))
    monkeypatch.setattr(shadow_quote, "load_rows", lambda *a, **k: [])
    sent: list = []
    with patch.object(tb, "send_message", lambda x, **k: sent.append((x, k))):
        tb._handle_zset("/zset candidates")
    card, kw = sent[1]
    assert "in set Z" in card and W2 in card and "reply_markup" not in kw


def test_the_admit_tap_reruns_the_gate_and_writes_z_only_on_a_pass(books, monkeypatch):
    """The seam: callback -> zset_candidates.admit -> zset.admit, no force path."""
    import src.telegram_bot as tb
    from src.copy_trading import zset
    from src.copy_trading import zset_candidates as zc

    monkeypatch.setattr(zc, "evaluate", lambda w, *a, **k: _passer(w, ok=True))
    toast, edited = tb._handle_callback(f"zadm:{W1}")
    assert toast == "Admitted to set Z" and W1 in edited
    assert W1.lower() in zset.wallet_set()

    monkeypatch.setattr(zc, "evaluate", lambda w, *a, **k: _passer(w, ok=False))
    toast, edited = tb._handle_callback(f"zadm:{W2}")
    assert toast == "Not admitted" and "refused" in edited
    assert W2.lower() not in zset.wallet_set()


def test_the_admit_tap_cannot_bypass_the_concentration_rail(books, monkeypatch):
    import src.telegram_bot as tb
    from src.copy_trading import zset
    from src.copy_trading import zset_candidates as zc
    jackpot = [_P(ideal=400.0, opened=5.0) for _ in range(3)] + [_P(ideal=-20.0, opened=5.0) for _ in range(27)]
    monkeypatch.setattr(zc, "evaluate", lambda w, *a, **k: _passer(w, settled=jackpot, ok=True))
    toast, _ = tb._handle_callback(f"zadm:{W1}")
    assert toast == "Not admitted" and W1.lower() not in zset.wallet_set()


def test_evaluate_runs_the_real_gate_and_refuses_a_thin_record(books):
    from src.copy_trading import zset_candidates as zc
    era, b, a = zc.load_books()
    c = zc.evaluate(W1, b, a, era=era, now=time.time(), book_corr=(0.1, 20))
    assert c is not None and c.ok is False
    assert any("settled copies" in lab and not ok for lab, ok, _ in c.checks)


def test_the_seeding_script_and_the_cards_share_one_gate():
    src = open("scripts/seed_zset.py", encoding="utf-8").read()
    assert "zset_candidates.evaluate(" in src
    assert "golive_check(" not in src, "a second copy of the gate is a second gate"


# --------------------------------------------------------------------------- #
# The canary
# --------------------------------------------------------------------------- #

@pytest.fixture
def canary_env(tmp_path, monkeypatch):
    from src.copy_trading import canary, zset
    for mod in (canary, live_mode, live_guard, zset.promotion_state):
        monkeypatch.setattr(mod.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(CONFIG, "min_order_size_usd", 5.0)
    return canary


def _open_the_door(monkeypatch, canary):
    """Every interlock key turned and the governor open, as far as the
    canary can tell. Nothing here arms the real interlock."""
    monkeypatch.setattr(canary.live_mode, "blocking_reasons", lambda: [])
    monkeypatch.setattr(canary.live_budget, "is_open", lambda: True)

    class _C:
        tradeable = True
        per_copy_usd = 7.75
    monkeypatch.setattr(canary.live_budget, "caps", lambda **k: _C())


def test_canary_cannot_stage_in_preview(canary_env, monkeypatch):
    canary = canary_env
    monkeypatch.setattr(CONFIG, "preview_mode", True)
    ok, why = canary.stage(by="test")
    assert ok is False and "PREVIEW_MODE" in why
    assert canary.is_staged() is False


def test_canary_cannot_stage_with_the_governor_closed(canary_env, monkeypatch):
    canary = canary_env
    monkeypatch.setattr(canary.live_mode, "blocking_reasons", lambda: [])
    monkeypatch.setattr(canary.live_budget, "is_open", lambda: False)
    ok, why = canary.stage(by="test")
    assert ok is False and "LIVE_BUDGET_USD" in why


def test_canary_stages_fires_once_and_reports_once(canary_env, monkeypatch):
    canary = canary_env
    _open_the_door(monkeypatch, canary)
    ok, _ = canary.stage(by="test", now=1000.0)
    assert ok and canary.is_staged(now=1001.0)
    assert canary.expire_if_due(now=1001.0) is False

    class _Book:
        min_order_size = 5.0

    class _Clob:
        def get_order_book(self, token_id):
            return _Book()
    assert canary.size_for(_Clob(), "tok", 0.5) == 5.0, "5 shares at 0.5 is under the $5 floor"
    assert canary.size_for(_Clob(), "tok", 0.8) == 5.0
    assert canary.size_for(_Clob(), "tok", 2.0) == 10.0

    canary.record_fired(order_id="o1", market="m", token_id="t", their_price=0.5,
                        quoted_ask=0.51, order_price=0.51, copy_size=5.0,
                        notify_latency_s=3.0, now=1002.0)
    assert canary.is_staged(now=1003.0) is False, "one shot"
    assert canary.has_fired()

    class _Fill:
        status, fill_price, filled_shares, filled_usd = "FILLED", 0.52, 9.6, 5.0
    rep = canary.record_fill("o1", _Fill(), now=1004.0)
    assert rep and "entry penalty vs their price: +400bps" in rep
    assert canary.record_fill("o1", _Fill(), now=1005.0) is None, "reported once"
    assert canary.record_fill("other", _Fill()) is None

    ok2, why2 = canary.stage(by="test")
    assert ok2 is False and "RESET" in why2
    canary.reset(by="test")
    ok3, _ = canary.stage(by="test")
    assert ok3 is True


def test_canary_expires_unfired_and_says_so_once(canary_env, monkeypatch):
    canary = canary_env
    _open_the_door(monkeypatch, canary)
    canary.stage(by="test", now=1000.0)
    sent: list = []
    assert canary.expire_if_due(send=sent.append, now=1000.0 + canary.TTL_S + 1) is True
    assert len(sent) == 1 and "expired" in sent[0]
    assert canary.expire_if_due(send=sent.append, now=1000.0 + canary.TTL_S + 2) is False
    assert len(sent) == 1 and canary.is_staged() is False


def test_the_executor_sizes_the_canary_before_the_order_and_disarms_after():
    """The seam: the live path, not a helper."""
    from src.copy_trading import trade_executor
    src = inspect.getsource(trade_executor.place_trade_orders)
    i_stage = src.index("canary.is_staged()")
    i_order = src.index("_execute_copy_order(clob_client, trade, copy_size, snapshot)")
    i_disarm = src.index('live_mode.disarm(by="canary")')
    assert i_stage < i_order < i_disarm
    vsrc = inspect.getsource(trade_executor.process_verifications)
    assert "canary.record_fill(po.order_id, fill)" in vsrc


def test_the_guard_loop_expires_a_stale_canary():
    import main
    assert "canary.expire_if_due" in inspect.getsource(main._live_guard_loop)


def test_the_first_arm_stages_the_canary_by_default(canary_env, monkeypatch):
    import src.telegram_bot as tb
    canary = canary_env
    monkeypatch.setattr(tb.live_mode if hasattr(tb, "live_mode") else live_mode, "arm",
                        lambda reason="", by="": (True, "armed"))
    monkeypatch.setattr(live_mode, "arm", lambda reason="", by="": (True, "armed"))
    _open_the_door(monkeypatch, canary)
    sent: list = []
    with patch.object(tb, "send_message", lambda x, **k: sent.append(x)):
        tb._handle_live("/live CONFIRM")
    assert "ARMED" in sent[0] and "Canary staged" in sent[0]
    assert canary.is_staged()


def test_canary_status_renders_without_a_record(canary_env, monkeypatch):
    import src.telegram_bot as tb
    monkeypatch.setattr(CONFIG, "preview_mode", True)
    sent: list = []
    with patch.object(tb, "send_message", lambda x, **k: sent.append(x)), \
         patch.object(tb, "_send_chunked", lambda x: sent.append(x)):
        tb._handle_canary("/canary")
    assert "not staged" in sent[0] and "cannot stage now" in sent[0]


# --------------------------------------------------------------------------- #
# The floor under the bankroll
# --------------------------------------------------------------------------- #

def test_floor_is_a_fraction_of_the_stated_budget(budget):
    budget(310.0)
    assert live_budget.floor_usd() == 217.0
    budget(0.0)
    assert live_budget.floor_usd() is None
    assert live_budget.equity_usd(None, 50.0) is None, "unreadable is not zero"
    assert live_budget.equity_usd(100.0, 50.5) == 150.5


def test_a_bankroll_under_the_floor_disarms_and_a_losing_session_alone_does_not():
    fires, why = live_guard.should_self_disarm(equity_usd=200.0, floor_usd=217.0)
    assert fires and "floor" in why and "/live CONFIRM overrides" in why
    assert live_guard.should_self_disarm(equity_usd=300.0, floor_usd=217.0)[0] is False
    assert live_guard.should_self_disarm(equity_usd=None, floor_usd=217.0)[0] is False, "inert off paper"
    assert live_guard.should_self_disarm(crash_streak=0)[0] is False


def test_the_owner_rearming_after_a_trip_overrides_the_floor(tmp_path, monkeypatch):
    """The guard hands him the override, not the decision."""
    monkeypatch.setattr(live_guard.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(live_mode.CONFIG, "data_dir", str(tmp_path))
    arm = {"armed": True, "ts": 1000.0}
    monkeypatch.setattr(live_guard.live_mode, "is_armed", lambda: True)
    monkeypatch.setattr(live_guard.live_mode, "read_arm", lambda: arm)
    disarms: list = []
    monkeypatch.setattr(live_guard.live_mode, "disarm", lambda by="": disarms.append(by) or True)

    out = live_guard.run_once(equity_usd=200.0, floor_usd=217.0, now=2000.0)
    assert out["self_disarmed"] is True and disarms == ["live-guard"]
    assert live_guard._read_state().get("floor_trip_ts") == 2000.0

    # Still under the floor, and the owner has NOT re-armed: trips again.
    out = live_guard.run_once(equity_usd=200.0, floor_usd=217.0, now=2300.0)
    assert out["self_disarmed"] is True and out["floor_overridden"] is False

    # The owner re-arms after the trip: the floor steps aside for this session.
    arm["ts"] = 2500.0
    out = live_guard.run_once(equity_usd=200.0, floor_usd=217.0, now=2600.0)
    assert out["self_disarmed"] is False and out["floor_overridden"] is True


def test_the_arm_record_remembers_the_first_arm_across_disarms(tmp_path, monkeypatch):
    monkeypatch.setattr(live_mode.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(live_mode.CONFIG, "live_arm_enabled", True)
    monkeypatch.setattr(live_mode.CONFIG, "preview_mode", False)
    ok, _ = live_mode.arm(reason="t", by="test")
    assert ok
    first = live_mode.read_arm()["first_armed_ts"]
    live_mode.disarm(by="test")
    assert live_mode.read_arm()["armed"] is False
    assert live_mode.read_arm()["first_armed_ts"] == first
    live_mode.arm(reason="again", by="test")
    assert live_mode.read_arm()["first_armed_ts"] == first


# --------------------------------------------------------------------------- #
# The rehearsal ledger
# --------------------------------------------------------------------------- #

class _Pos:
    def __init__(self, cid, target, opened, *, won=True, price=0.5, spent=20.0,
                 closed_after=3600.0, exited=False):
        self.copy_id, self.target, self.opened_ts = cid, target, opened
        self.closed_ts = opened + closed_after
        self.closed, self.won, self.exited_early = True, won, exited
        self.their_price = self.entry_price = price
        self.spent = spent
        self.shares = spent / price
        self.pnl = (spent / price - spent) if won else -spent
        self.ideal_pnl = self.pnl
        self.cost_usd = 0.02


def test_rehearsal_sizes_at_the_caps_and_settles_at_real_quotes(budget):
    from src.copy_trading import rehearsal
    budget(310.0)
    pos = [_Pos(f"c{i}", W1, 10_000.0 + i * 10, won=(i % 2 == 0)) for i in range(4)]
    quotes = {f"c{i}": 0.51 for i in range(3)}   # the fourth has no real quote
    res = rehearsal.rehearse(budget_usd=310.0, positions=pos, quotes=quotes,
                             wallets=[W1], since_ts=0.0, era=1.0)
    d = res["wallets"][W1.lower()]
    assert (d["n_settled"], d["n_matched"], d["n_taken"], d["n_held"]) == (4, 3, 3, 0)
    assert res["per_copy_usd"] == 7.75
    # two wins at 0.51 on $7.75, one loss of $7.75
    assert d["real_pnl"] == round(2 * (7.75 / 0.51 - 7.75) - 7.75, 2)
    assert d["ideal_pnl"] == round(2 * 7.75 - 7.75, 2)
    assert d["thin"] is True and res["counterfactual"] is True


def test_rehearsal_holds_copies_the_exposure_cap_would_not_allow(budget):
    from src.copy_trading import rehearsal
    budget(310.0)
    # 40 copies opened within a minute, none closing. The day cap ($93,
    # twelve copies) binds first; spread over four days the exposure cap
    # ($248, thirty-two open) binds instead.
    pos = [_Pos(f"c{i}", W1, 10_000.0 + i, closed_after=86400.0 * 10) for i in range(40)]
    quotes = {f"c{i}": 0.5 for i in range(40)}
    res = rehearsal.rehearse(budget_usd=310.0, positions=pos, quotes=quotes,
                             wallets=[W1], since_ts=0.0)
    d = res["wallets"][W1.lower()]
    assert d["n_taken"] == 12 and d["n_held"] == 28, "the day cap binds"
    text = rehearsal.render(res)
    assert "counterfactual" in text and "12 taken, 28 held by caps" in text

    pos = [_Pos(f"c{i}", W1, 10_000.0 + (i // 10) * 86400.0 + i, closed_after=86400.0 * 10)
           for i in range(40)]
    res = rehearsal.rehearse(budget_usd=310.0, positions=pos, quotes=quotes,
                             wallets=[W1], since_ts=0.0)
    d = res["wallets"][W1.lower()]
    assert d["n_taken"] == 32 and d["n_held"] == 8, "the open-exposure cap binds"


def test_real_money_line_says_so_before_the_first_arm(tmp_path, monkeypatch):
    from src.copy_trading import rehearsal
    monkeypatch.setattr(live_mode.CONFIG, "data_dir", str(tmp_path))
    assert rehearsal.real_money_line().startswith("💵 real money: not armed yet")


def test_real_money_line_counts_only_redeemer_rows(tmp_path, monkeypatch, budget):
    from src.copy_trading import inventory, pnl, rehearsal
    budget(310.0)
    monkeypatch.setattr(live_mode, "read_arm", lambda: {"armed": True, "first_armed_ts": 1.0})
    monkeypatch.setattr(live_budget, "_read_balance", lambda now=None: 250.0)
    monkeypatch.setattr(inventory, "get_inventory_summary", lambda: {"total_cost_basis_usd": 30.0})
    now = time.time()
    today = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now))
    monkeypatch.setattr(pnl, "load_realized", lambda: [
        {"timestamp": today, "pnl": 4.0, "source": "redeemer"},
        {"timestamp": today, "pnl": -100.0},                       # preview row, no source
        {"timestamp": "2020-01-01T00:00:00+00:00", "pnl": 9.0, "source": "redeemer"},
    ])
    line = rehearsal.real_money_line(now=now)
    assert "bankroll $280.00" in line and "floor $217" in line
    assert "distance $+63.00" in line and "realized today $+4.00 (1 redeem(s))" in line


def test_the_daily_block_sends_the_rehearsal_line():
    import main
    assert "rehearsal.daily_message()" in inspect.getsource(main._ab_race_reporter_loop)
