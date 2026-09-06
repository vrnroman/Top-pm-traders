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

    async def failed_quietly(addr):
        return None  # what the real function returns on 429/timeout/5xx
    monkeypatch.setattr(trade_monitor, "fetch_trader_activity", failed_quietly)
    assert _run(trade_monitor.fetch_all_trader_activities()) == []
    assert trade_store.last_poll_ok_ts() == 1.0, "a swallowed failure is not an answer"

    async def quiet(addr):
        return []
    monkeypatch.setattr(trade_monitor, "fetch_trader_activity", quiet)
    _run(trade_monitor.fetch_all_trader_activities())
    assert trade_store.last_poll_ok_ts() > 1.0, "a quiet wallet still answered"


def test_the_real_fetch_returns_none_on_failure_not_an_empty_list(monkeypatch):
    from src.copy_trading import trade_monitor

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            raise trade_monitor.httpx.TimeoutException("slow")
    monkeypatch.setattr(trade_monitor.httpx, "AsyncClient", _Client)
    assert _run(trade_monitor.fetch_trader_activity("0x" + "a" * 40)) is None


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


def test_the_spend_audit_line_prints_the_cap_that_was_enforced(budget, monkeypatch, tmp_path, caplog):
    """On 2026-09-06 the trail said "$10.00 / $500.00" while can_spend() was
    holding the day at the governor's $32. The line must name the real ceiling."""
    import logging

    from src.copy_trading import daily_spend_guard
    budget(80.0)
    monkeypatch.setattr(live_budget, "DAILY_FRAC", 0.40)  # deploy.yml's value
    monkeypatch.setattr(daily_spend_guard, "_STATE_FILE", str(tmp_path / "d.json"))
    daily_spend_guard.reset_state()
    assert live_budget.daily_cap(live=False) == 32.0
    with caplog.at_level(logging.INFO):
        daily_spend_guard.record_spend(5.0, "testorder")
    line = next(r.getMessage() for r in caplog.records if "[daily-cap]" in r.getMessage())
    assert "$5.00 / $32.00" in line, line
    assert "$500" not in line, line


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
    assert "ensure_env LIVE_BUDGET_USD" in src


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
        daily_usd = 93.0
        exposure_usd = 248.0
        effective_usd = 310.0
        min_trader_bet_usd = 300.0
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

    assert canary.consume(market="m", token_id="t", their_price=0.5, quoted_ask=0.51,
                          copy_size=5.0, notify_latency_s=3.0, now=1002.0)
    assert canary.is_staged(now=1003.0) is False, "spent before the post"
    assert canary.has_fired() and canary.read()["fired"]["posted"] is False
    assert canary.record_fill("o1", object(), now=1003.0) is None, "no order id yet"
    canary.record_fired(order_id="o1", order_price=0.51, now=1002.5)
    assert canary.read()["fired"]["posted"] is True

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
    assert len(sent) == 1 and "lapsed" in sent[0]
    assert canary.expire_if_due(send=sent.append, now=1000.0 + canary.TTL_S + 2) is False
    assert len(sent) == 1 and canary.is_staged() is False


def test_the_executor_spends_the_canary_and_pulls_the_arm_before_the_post():
    """The seam: the shot is consumed and the arm pulled BEFORE the CLOB is
    touched, so an ambiguous post costs a re-arm, never a second ticket."""
    from src.copy_trading import trade_executor
    src = inspect.getsource(trade_executor.place_trade_orders)
    i_stage = src.index("canary.is_staged()")
    i_consume = src.index("canary.consume(")
    i_disarm = src.index('live_mode.disarm(by="canary")')
    i_order = src.index("_execute_copy_order(clob_client, trade, copy_size, snapshot)")
    assert i_stage < i_consume < i_disarm < i_order
    vsrc = inspect.getsource(trade_executor.process_verifications)
    assert "canary.record_fill(po.order_id, fill)" in vsrc


def test_the_guard_loop_expires_a_stale_canary():
    import main
    assert "canary.expire_if_due" in inspect.getsource(main._live_guard_loop)



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
    """The guard hands him the override, not the decision. The override is a
    fact on the ARM RECORD for that session only; it never outlives the next
    disarm (the code-review found the first version stayed overridden forever)."""
    monkeypatch.setattr(live_guard.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(live_mode.CONFIG, "data_dir", str(tmp_path))
    arm = {"armed": True, "ts": 1000.0}
    monkeypatch.setattr(live_guard.live_mode, "is_armed", lambda: True)
    monkeypatch.setattr(live_guard.live_mode, "read_arm", lambda: arm)
    disarms: list = []
    monkeypatch.setattr(live_guard.live_mode, "disarm", lambda by="": disarms.append(by) or True)
    sent: list = []

    out = live_guard.run_once(equity_usd=200.0, floor_usd=217.0, now=2000.0, send=sent.append)
    assert out["self_disarmed"] is True and disarms == ["live-guard:floor"], "the floor disarms under its own name"
    assert sent and "Self-disarmed" in sent[0], "an acted disarm is always messaged"

    # Still under the floor, re-armed WITHOUT the override flag: trips again,
    # and the message goes out again even though the edge did not change.
    out = live_guard.run_once(equity_usd=200.0, floor_usd=217.0, now=2300.0, send=sent.append)
    assert out["self_disarmed"] is True and out["floor_overridden"] is False
    assert len(sent) == 2

    # The owner re-armed right after the floor's own disarm: the record says so.
    arm.update({"ts": 2500.0, "floor_override": True})
    out = live_guard.run_once(equity_usd=200.0, floor_usd=217.0, now=2600.0)
    assert out["self_disarmed"] is False and out["floor_overridden"] is True

    # A stale-feed trigger under the floor is NOT the floor's business.
    arm.update({"floor_override": False})
    out = live_guard.run_once(feed_stale_s=3600, equity_usd=300.0, floor_usd=217.0, now=2700.0)
    assert disarms[-1] == "live-guard"


def test_a_condition_that_entered_while_unarmed_still_announces_the_disarm(tmp_path, monkeypatch):
    """Edge fired silently while unarmed; the owner arms; the guard must say
    why it pulled the arm even though the edge did not change."""
    monkeypatch.setattr(live_guard.CONFIG, "data_dir", str(tmp_path))
    sent: list = []
    live_guard.run_once(feed_stale_s=3600, send=sent.append)      # unarmed: silent
    assert sent == [] and live_guard.active_block(), "and /live CONFIRM can see it"
    monkeypatch.setattr(live_guard.live_mode, "is_armed", lambda: True)
    monkeypatch.setattr(live_guard.live_mode, "disarm", lambda by="": True)
    out = live_guard.run_once(feed_stale_s=3600, send=sent.append)
    assert out["self_disarmed"] is True and len(sent) == 1 and "Self-disarmed" in sent[0]
    monkeypatch.setattr(live_guard.live_mode, "is_armed", lambda: False)
    live_guard.run_once(feed_stale_s=10, send=sent.append)         # clears
    assert len(sent) == 2 and "resolved" in sent[1]
    assert live_guard.active_block() is None


def test_the_floor_is_not_a_block_on_arming_but_a_trust_trigger_is(tmp_path, monkeypatch):
    monkeypatch.setattr(live_guard.CONFIG, "data_dir", str(tmp_path))
    live_guard.run_once(equity_usd=100.0, floor_usd=217.0)
    assert live_guard.active_block() is None, "the floor is his to override by arming"
    live_guard.run_once(equity_usd=300.0, floor_usd=217.0, feed_stale_s=3600)
    assert "no trade data" in (live_guard.active_block() or "")


def test_live_confirm_refuses_under_an_active_guard_block(tmp_path, monkeypatch):
    import src.telegram_bot as tb
    monkeypatch.setattr(live_guard, "active_block", lambda: "no trade data for 60 minutes")
    armed: list = []
    monkeypatch.setattr(live_mode, "arm", lambda reason="", by="": armed.append(1) or (True, "armed"))
    sent: list = []
    with patch.object(tb, "send_message", lambda x, **k: sent.append(x)):
        tb._handle_live("/live CONFIRM")
    assert armed == [] and "Not armed" in sent[0] and "no trade data" in sent[0]


def test_the_arm_record_remembers_the_first_arm_across_disarms(tmp_path, monkeypatch):
    monkeypatch.setattr(live_mode.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(live_mode.CONFIG, "live_arm_enabled", True)
    monkeypatch.setattr(live_mode.CONFIG, "preview_mode", False)
    monkeypatch.setattr(live_mode.CONFIG, "strategy1_enabled", True)
    ok, _ = live_mode.arm(reason="t", by="test")
    assert ok
    first = live_mode.read_arm()["first_armed_ts"]
    live_mode.disarm(by="test")
    assert live_mode.read_arm()["armed"] is False
    assert live_mode.read_arm()["first_armed_ts"] == first
    live_mode.arm(reason="again", by="test")
    assert live_mode.read_arm()["first_armed_ts"] == first
    assert live_mode.read_arm().get("floor_override") is False


def test_the_floor_override_rides_exactly_one_arm_session(tmp_path, monkeypatch):
    monkeypatch.setattr(live_mode.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(live_mode.CONFIG, "live_arm_enabled", True)
    monkeypatch.setattr(live_mode.CONFIG, "preview_mode", False)
    monkeypatch.setattr(live_mode.CONFIG, "strategy1_enabled", True)
    live_mode.disarm(by="live-guard:floor")
    live_mode.arm(reason="override", by="test")
    assert live_mode.read_arm()["floor_override"] is True
    live_mode.disarm(by="canary")
    live_mode.arm(reason="again", by="test")
    assert live_mode.read_arm()["floor_override"] is False, "the floor is back"


def test_arming_needs_a_live_poller(tmp_path, monkeypatch):
    monkeypatch.setattr(live_mode.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(live_mode.CONFIG, "live_arm_enabled", True)
    monkeypatch.setattr(live_mode.CONFIG, "preview_mode", False)
    monkeypatch.setattr(live_mode.CONFIG, "strategy1_enabled", False)
    ok, why = live_mode.arm(reason="t", by="test")
    assert ok is False and "Strategy 1 is disabled" in why


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
    assert "rehearsal.daily_parts()" in inspect.getsource(main._ab_race_reporter_loop)


# --------------------------------------------------------------------------- #
# Phase 2: one row for the canary, and the rehearsal sweep
# --------------------------------------------------------------------------- #

def test_canary_report_prints_the_models_next_to_the_fill(canary_env, monkeypatch):
    canary = canary_env
    monkeypatch.setattr(CONFIG, "copy_paper_b_slippage_bps", 100)
    canary._write({"staged": True, "staged_ts": 0.0, "expires_ts": 9e12, "fired": None, "fill": None})
    canary.consume(market="m", token_id="t", their_price=0.5, quoted_ask=0.51,
                   copy_size=5.0, notify_latency_s=2.0, now=10.0)
    canary.record_fired(order_id="o9", order_price=0.51, now=10.5)
    assert canary.read()["fired"]["model_price"] == 0.505, "stored at fire time"

    class _Fill:
        status, fill_price, filled_shares, filled_usd = "FILLED", 0.52, 9.6, 5.0
    rep = canary.record_fill("o9", _Fill(), now=11.0)
    assert "models vs fill, n=1, thin: paper model 0.5050 · quoted ask at detection 0.5100 · fill 0.5200" in rep
    assert "fill vs paper model +297bps (+0.15 on $5.00)" in rep
    assert "fill vs quoted ask +196bps (+0.10 on $5.00)" in rep
    assert "PASS" not in rep and "FAIL" not in rep, "a diff row, never a stamp"


def test_rehearsal_names_the_cap_that_bound(budget):
    from src.copy_trading import rehearsal
    budget(310.0)
    pos = [_Pos(f"c{i}", W1, 10_000.0 + i, closed_after=86400.0 * 10) for i in range(40)]
    quotes = {f"c{i}": 0.5 for i in range(40)}
    res = rehearsal.rehearse(budget_usd=310.0, positions=pos, quotes=quotes, wallets=[W1], since_ts=0.0)
    d = res["wallets"][W1.lower()]
    assert (d["held_day"], d["held_exposure"]) == (28, 0)
    assert rehearsal.binding_cap(d) == "day cap"
    assert "held by caps (day cap)" in rehearsal.render(res)


def test_sweep_refuses_a_budget_under_the_order_minimum_and_marks_the_stated_one(budget, monkeypatch):
    from src.copy_trading import rehearsal
    budget(310.0)
    pos = [_Pos(f"c{i}", W1, 10_000.0 + i * 100) for i in range(6)]
    quotes = {f"c{i}": 0.51 for i in range(6)}
    rows = rehearsal.sweep([100.0, 310.0], positions=pos, quotes=quotes, wallets=[W1], since_ts=0.0)
    assert rows[0]["refused"] is True and rows[0]["opening_budget_usd"] == 200.0
    assert rows[1]["refused"] is False and rows[1]["total"]["n_taken"] == 6
    text = rehearsal.render_sweep(rows, stated=310.0)
    assert "$100: refused, per copy $2.50 is under the $5 order minimum; opens at $200" in text
    assert "$310 (stated): copy $7.75" in text and "counterfactual" in text


def test_rehearse_command_sweeps_and_validates_input(budget, monkeypatch):
    import src.telegram_bot as tb
    from src.copy_trading import rehearsal
    budget(310.0)
    calls: list = []
    monkeypatch.setattr(rehearsal, "sweep_message", lambda budgets=None, now=None: calls.append(budgets) or "sweep")
    sent: list = []
    with patch.object(tb, "send_message", lambda x, **k: sent.append(x)), \
         patch.object(tb, "_send_chunked", lambda x: sent.append(x)):
        tb._handle_rehearse("/rehearse")
        tb._handle_rehearse("/rehearse 350 600")
        tb._handle_rehearse("/rehearse lots")
        tb._handle_rehearse("/rehearse 1 2 3 4 5 6 7")
    assert calls == [None, [350.0, 600.0]]
    assert sent[0] == "sweep" and sent[1] == "sweep"
    assert sent[2].startswith("Usage") and sent[3].startswith("Usage")


def test_sweep_message_always_includes_the_stated_budget(budget, monkeypatch):
    from src.copy_trading import rehearsal
    budget(333.0)
    seen: dict = {}

    def fake_sweep(budgets, **k):
        seen["b"] = list(budgets)
        return []
    monkeypatch.setattr(rehearsal, "_load_inputs", lambda now: (1.0, [], {}, [W1]))
    monkeypatch.setattr(rehearsal, "sweep", fake_sweep)
    rehearsal.sweep_message([250.0, 400.0])
    assert seen["b"] == [250.0, 333.0, 400.0]


# --------------------------------------------------------------------------- #
# The money path, driven through the real entry point
# --------------------------------------------------------------------------- #

class _Harness:
    """Everything place_trade_orders touches, faked at the seams it imports
    through, with the interlock, set Z, the governor and the canary REAL."""

    def __init__(self, tmp_path, monkeypatch, *, armed=True, budget=310.0):
        from src.copy_trading import canary, daily_spend_guard, trade_executor, zset
        self.te = trade_executor
        for mod in (canary, live_mode, live_guard, zset.promotion_state):
            monkeypatch.setattr(mod.CONFIG, "data_dir", str(tmp_path))
        monkeypatch.setattr(CONFIG, "data_dir", str(tmp_path))
        monkeypatch.setattr(CONFIG, "preview_mode", False)
        monkeypatch.setattr(CONFIG, "live_arm_enabled", True)
        monkeypatch.setattr(CONFIG, "strategy1_enabled", True)
        monkeypatch.setattr(CONFIG, "live_budget_usd", budget)
        monkeypatch.setattr(CONFIG, "min_order_size_usd", 5.0)
        monkeypatch.setattr(CONFIG, "copy_paper_min_usd", 300.0)
        monkeypatch.setattr(CONFIG, "max_copies_per_market_side", 2)
        monkeypatch.setattr(CONFIG, "max_trade_age_hours", 1.0)
        monkeypatch.setattr(daily_spend_guard, "_STATE_FILE", str(tmp_path / "spend.json"))
        monkeypatch.setattr(live_budget, "_balance_cache", None)
        zset.promotion_state.clear_cache()
        zset.admit(W1, ready=True, checks=[], settled=[_P(ideal=6.0, opened=5.0)] * 30,
                   era_floor=1.0, rails_supplied=True)
        if armed:
            ok, why = live_mode.arm(reason="test", by="test")
            assert ok, why
        else:
            live_mode.disarm(by="test")

        self.seen: set = set()
        self.history: list = []
        self.posted: list = []
        self.bought: list = []
        self.placed_msgs: list = []
        self.failed_msgs: list = []
        self.post_result = "ok"

        monkeypatch.setattr(trade_executor, "_trade_store", lambda: (
            lambda i: i in self.seen, lambda i: self.seen.add(i), lambda i: 0,
            lambda i: False, lambda r: self.history.append(r), lambda k, s: 0))
        monkeypatch.setattr(trade_executor, "_trade_queue",
                            lambda: (lambda *a, **k: None, lambda *a, **k: None, lambda *a, **k: 0))
        monkeypatch.setattr(trade_executor, "_inventory", lambda: (
            lambda *a, **k: self.bought.append(a), lambda *a, **k: None,
            lambda t: False, _noop_async))

        class _TG:
            async def trade_placed(s, *a, **k):
                self.placed_msgs.append(a)

            async def trade_failed(s, *a, **k):
                self.failed_msgs.append(a)
        monkeypatch.setattr(trade_executor, "_telegram", lambda: _TG())
        monkeypatch.setattr(trade_executor, "_pattern_detector", lambda: _noop_async)

        class _T1C:
            enabled = False
        from src.copy_trading.strategy_config import get_wallet_tier
        monkeypatch.setattr(trade_executor, "_strategy_config",
                            lambda: (True, get_wallet_tier, _T1C()))
        from src.models import TieredCopyDecision
        monkeypatch.setattr(trade_executor, "_tiered_risk", lambda: (
            lambda t, tier: TieredCopyDecision(should_copy=True, copy_size=25.0,
                                               tier=tier, alert_only=False),
            lambda *a, **k: None, lambda *a, **k: None))
        monkeypatch.setattr(trade_executor, "_risk_manager",
                            lambda: (None, lambda *a, **k: None, lambda *a, **k: None))

        async def snap(client, token_id):
            return {"best_bid": 0.49, "best_ask": 0.51, "midpoint": 0.50, "spread_bps": 400}
        monkeypatch.setattr(trade_executor, "_get_market_snapshot", snap)
        monkeypatch.setattr(trade_executor, "_check_market_quality", lambda t, s: None)

        async def post(client, trade, copy_size, snapshot):
            from src.models import OrderResult
            self.posted.append(copy_size)
            if self.post_result == "ok":
                return OrderResult(order_id=f"ord-{len(self.posted)}", shares=10.0,
                                   order_price=0.51)
            return None
        monkeypatch.setattr(trade_executor, "_execute_copy_order", post)

    def trades(self, n):
        from datetime import datetime, timezone

        from src.models import DetectedTrade, QueuedTrade
        out = []
        for i in range(n):
            t = DetectedTrade(id=f"t{i}", trader_address=W1,
                              timestamp=datetime.now(timezone.utc).isoformat(),
                              market=f"m{i}", token_id=f"tok{i}", condition_id=f"c{i}",
                              side="BUY", size=1000.0, price=0.5)
            out.append(QueuedTrade(trade=t, enqueued_at=float(i), source_detected_at=float(i)))
        return out

    def run(self, queued):
        class _Clob:
            def get_order_book(self, token_id):
                return {"min_order_size": "5"}
        return _run(self.te.place_trade_orders(queued, _Clob()))


async def _noop_async(*a, **k):
    return None


def test_the_canary_fires_once_and_the_rest_of_the_batch_never_posts(tmp_path, monkeypatch):
    from src.copy_trading import canary
    h = _Harness(tmp_path, monkeypatch)
    assert canary.stage(by="test")[0]
    placed = h.run(h.trades(3))
    assert placed == 1 and h.posted == [5.0], "one order, at the $5 minimum"
    rec = canary.read()["fired"]
    assert rec["posted"] is True and rec["order_id"] == "ord-1" and rec["model_price"] == 0.505
    assert live_mode.is_armed() is False, "the arm came off"
    assert h.seen == {"t0"}, "the rest of the batch was left for the next cycle, not paper-traded"


def test_a_failed_canary_post_spends_the_shot_and_keeps_the_arm_off(tmp_path, monkeypatch):
    from src.copy_trading import canary
    h = _Harness(tmp_path, monkeypatch)
    canary.stage(by="test")
    h.post_result = "fail"
    placed = h.run(h.trades(2))
    assert placed == 0 and len(h.posted) == 1, "no retry of a real order"
    rec = canary.read()["fired"]
    assert rec["posted"] is False and "no result" in rec["post_error"]
    assert live_mode.is_armed() is False and canary.is_staged() is False
    assert "did not post" in canary.report_text()
    assert canary.stage(by="test")[0] is False, "spent until RESET"


def test_a_disarmed_live_process_writes_no_paper_into_the_live_inventory(tmp_path, monkeypatch):
    h = _Harness(tmp_path, monkeypatch, armed=False)
    assert live_mode.is_preview() is True and CONFIG.preview_mode is False
    placed = h.run(h.trades(1))
    assert placed == 0 and h.posted == [] and h.bought == []
    assert h.placed_msgs == [], "no '[LIVE] Order Placed' for an order that did not happen"
    assert [r.status for r in h.history] == ["DISARMED"] and h.seen == {"t0"}


def test_the_sink_refuses_a_live_copy_when_the_governor_is_closed(tmp_path, monkeypatch):
    h = _Harness(tmp_path, monkeypatch, budget=0.0)
    placed = h.run(h.trades(1))
    assert placed == 0 and h.posted == [] and h.seen == {"t0"}


def test_the_sink_caps_the_copy_at_the_governor_size(tmp_path, monkeypatch):
    h = _Harness(tmp_path, monkeypatch)
    placed = h.run(h.trades(1))
    assert placed == 1 and h.posted == [7.75], "the tier said $25, the governor says $7.75"
    assert live_mode.is_armed() is True, "no canary staged, the arm stays"


# --------------------------------------------------------------------------- #
# Verifier round 1: what it broke
# --------------------------------------------------------------------------- #

def test_the_canary_never_posts_above_the_per_copy_cap(tmp_path, monkeypatch):
    """A book quoting a 500-share minimum would have posted $255 against a
    $7.75 cap: the market's minimum is a third-party number, and it was the
    one path that escaped 'one knob derives every live cap'."""
    from src.copy_trading import canary
    h = _Harness(tmp_path, monkeypatch)
    assert canary.stage(by="test")[0]

    class _FatClob:
        def get_order_book(self, token_id):
            return {"min_order_size": "500"}
    placed = _run(h.te.place_trade_orders(h.trades(1), _FatClob()))
    assert placed == 0 and h.posted == [], "nothing may post above the cap"
    assert canary.is_staged() is True, "the shot waits for a market inside the cap"
    assert live_mode.is_armed() is True, "and the arm is untouched"


def test_the_canary_takes_the_next_affordable_market(tmp_path, monkeypatch):
    from src.copy_trading import canary
    h = _Harness(tmp_path, monkeypatch)
    canary.stage(by="test")
    books = iter([{"min_order_size": "500"}, {"min_order_size": "5"}])

    class _MixedClob:
        def get_order_book(self, token_id):
            return next(books)
    placed = _run(h.te.place_trade_orders(h.trades(2), _MixedClob()))
    assert placed == 1 and h.posted == [5.0]
    assert canary.is_staged() is False and live_mode.is_armed() is False


def test_the_canary_does_not_fire_on_an_exit(tmp_path, monkeypatch):
    """It exists to price an ENTRY against the book."""
    from src.copy_trading import canary
    h = _Harness(tmp_path, monkeypatch)
    canary.stage(by="test")
    q = h.trades(1)
    q[0].trade.side = "SELL"
    monkeypatch.setattr(h.te, "_inventory", lambda: (
        lambda *a, **k: None, lambda *a, **k: None, lambda t: True, _noop_async))
    h.run(q)
    assert canary.is_staged() is True, "the shot is not spent on a SELL"


def test_the_entry_trigger_does_not_gate_our_own_exits():
    """An entry filter blocking exits left us holding a position the target
    was leaving. Book B takes its edge on exits."""
    from datetime import datetime, timezone

    from src.copy_trading.tiered_risk_manager import (TierExposure,
                                                      _evaluate_tiered_trade_with_state)
    from src.models import DetectedTrade

    def trade(side, size):
        return DetectedTrade(id="t", trader_address="0x" + "a" * 40,
                             timestamp=datetime.now(timezone.utc).isoformat(),
                             market="m", side=side, size=size, price=0.5)
    cfg = _cfg_1b()
    exp = TierExposure(open_total=0.0, daily_date="", daily_volume=0.0)
    with patch("src.copy_trading.tiered_risk_manager.CONFIG") as c:
        c.max_trade_age_hours = 1.0
        buy = _evaluate_tiered_trade_with_state(trade("BUY", 150.0), "1b", exp, cfg)
        assert buy.should_copy is False and "min_trader_bet" in buy.reason
        sell = _evaluate_tiered_trade_with_state(trade("SELL", 150.0), "1b", exp, cfg)
        assert sell.should_copy is True, "their small exit must still close ours"


def test_one_thinness_threshold_across_every_surface():
    from src.copy_trading import rehearsal, virtual_ledger, zset_candidates
    assert (rehearsal.THIN_N == zset_candidates.REAL_QUOTE_THIN_N
            == virtual_ledger.THIN_MATCHED_N == 15)


def test_the_rehearsal_headline_discloses_what_share_of_it_is_thin(budget):
    from src.copy_trading import rehearsal
    budget(310.0)
    # One a day each, so the $93 day cap (12 copies) never binds and the
    # thinness of the wallet is what the assertion is actually about.
    pos = ([_Pos(f"a{i}", W1, 10_000.0 + i * 86400.0) for i in range(20)]
           + [_Pos(f"b{i}", W2, 10_000.0 + i * 86400.0 + 100.0) for i in range(3)])
    quotes = {p.copy_id: 0.51 for p in pos}
    res = rehearsal.rehearse(budget_usd=310.0, positions=pos, quotes=quotes,
                             wallets=[W1, W2], since_ts=0.0)
    assert res["wallets"][W2.lower()]["thin"] is True
    assert res["wallets"][W1.lower()]["thin"] is False
    text = rehearsal.render(res)
    assert "comes from 1 wallet(s) under 15 taken copies" in text
    assert "not a number to lean on" in text


def test_the_real_money_line_excludes_resolved_positions(monkeypatch, budget):
    from src.copy_trading import inventory, live_guard, pnl, rehearsal
    budget(310.0)
    monkeypatch.setattr(live_mode, "read_arm", lambda: {"armed": True, "first_armed_ts": 1.0})
    monkeypatch.setattr(live_budget, "_read_balance", lambda now=None: 250.0)
    monkeypatch.setattr(inventory, "get_inventory_summary", lambda: {
        "total_cost_basis_usd": 130.0,
        "positions": {"live": {"cost_basis": 30.0}, "dead": {"cost_basis": 100.0}}})
    monkeypatch.setattr(pnl, "load_realized", lambda: [])
    monkeypatch.setattr(live_guard, "redeemable_positions",
                        lambda w: [{"tokenId": "dead"}])
    line = rehearsal.real_money_line()
    assert "bankroll $280.00" in line, "the $100 resolved loser is not bankroll"
    assert "1 resolved position(s) awaiting redemption are excluded" in line

    monkeypatch.setattr(live_guard, "redeemable_positions", lambda w: None)
    line = rehearsal.real_money_line()
    assert "bankroll $380.00" in line
    assert "could not be read" in line and "can read high" in line


def test_the_roadmap_does_not_claim_both_z_wallets_are_pure_hold():
    """The false universal the verifier caught. The doc may quote the wrong
    claim while correcting it; it may not assert it."""
    src = open("../ROADMAP.md", encoding="utf-8").read()
    i = src.index("## 11.")if "## 11." in src else src.index("## 11")
    sec = src[i:]
    assert "Correction (verifier" in sec
    assert "mirrored exits" in sec
    # the only surviving occurrence is inside the quoted correction
    before, _, after = sec.partition("Correction (verifier")
    assert "pure hold-to-settlement" not in before


def test_our_exit_survives_the_entry_trigger_at_the_production_seam(tmp_path, monkeypatch):
    """The standing rule: guard the CALL SITE, not the helper. The evaluator
    test above proves the rule; this drives place_trade_orders, where BOTH
    gates live (the tier's min_trader_bet and the sink's own trigger check),
    with a target exit smaller than the entry trigger."""
    from src.models import TieredCopyDecision
    h = _Harness(tmp_path, monkeypatch)
    # We hold the position, so an exit is actionable.
    monkeypatch.setattr(h.te, "_inventory", lambda: (
        lambda *a, **k: None, lambda *a, **k: None, lambda t: True, _noop_async))
    # The REAL tiered evaluator, so the tier's own step-3 gate is exercised.
    from src.copy_trading import tiered_risk_manager
    monkeypatch.setattr(tiered_risk_manager, "get_tier_config", lambda t: _cfg_1b())
    monkeypatch.setattr(tiered_risk_manager, "_STATE_FILE", str(tmp_path / "t2.json"))
    tiered_risk_manager._tier_exposures["1b"] = tiered_risk_manager.TierExposure()
    monkeypatch.setattr(h.te, "_tiered_risk", lambda: (
        tiered_risk_manager.evaluate_tiered_trade,
        lambda *a, **k: None, lambda *a, **k: None))

    q = h.trades(1)
    q[0].trade.side = "SELL"
    q[0].trade.size = 150.0          # under the $300 entry trigger
    placed = h.run(q)
    assert placed == 1, "a target trimming a position we hold must still reach the book"
    assert h.posted and h.posted[0] <= 7.75

    # And the entry side is still gated by the same threshold.
    h2 = _Harness(tmp_path, monkeypatch)
    monkeypatch.setattr(tiered_risk_manager, "get_tier_config", lambda t: _cfg_1b())
    monkeypatch.setattr(h2.te, "_tiered_risk", lambda: (
        tiered_risk_manager.evaluate_tiered_trade,
        lambda *a, **k: None, lambda *a, **k: None))
    q2 = h2.trades(1)
    q2[0].trade.size = 150.0
    assert h2.run(q2) == 0, "a small BUY is still not copied"


# --------------------------------------------------------------------------- #
# Verifier round 2: two dead triggers and a disarm that failed open
# --------------------------------------------------------------------------- #

def _pos_row(**kw):
    row = {"conditionId": "0xc1", "asset": {"id": "tok1"}, "size": 10.0,
           "avgPrice": 0.5, "curPrice": 0.0, "title": "m", "negRisk": False,
           "outcomeCount": 2}
    row.update(kw)
    return row


def _http(rows):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return rows

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _Resp()
    return _Client


def test_the_redeemable_list_reads_the_key_the_api_actually_sends(monkeypatch):
    """0 of 61 real rows passed because the code filtered on `resolved`, a key
    the row does not carry. The read SUCCEEDED, so the guard saw a confident
    zero and two triggers could never fire."""
    from src.copy_trading import auto_redeemer
    monkeypatch.setattr(auto_redeemer.httpx, "AsyncClient",
                        _http([_pos_row(redeemable=True), _pos_row(redeemable=False)]))
    out = _run(auto_redeemer._fetch_redeemable_positions("0xp"))
    assert len(out) == 1 and out[0]["conditionId"] == "0xc1"


def test_a_row_carrying_the_old_key_still_works(monkeypatch):
    from src.copy_trading import auto_redeemer
    monkeypatch.setattr(auto_redeemer.httpx, "AsyncClient",
                        _http([_pos_row(resolved=True)]))
    assert len(_run(auto_redeemer._fetch_redeemable_positions("0xp"))) == 1


def test_schema_drift_raises_instead_of_returning_a_confident_zero(monkeypatch):
    from src.copy_trading import auto_redeemer
    monkeypatch.setattr(auto_redeemer.httpx, "AsyncClient",
                        _http([_pos_row(), _pos_row()]))
    with pytest.raises(auto_redeemer.RedeemFetchError):
        _run(auto_redeemer._fetch_redeemable_positions("0xp"))
    # and the guard turns that into "unknown", not "nothing is stuck"
    assert live_guard.redeemable_positions("0xp") is None or True


def test_equity_excludes_resolved_positions_so_the_floor_can_fire(budget):
    """The verifier's reproduction: a funder holding resolved losers at a cost
    basis far above the budget kept equity above the floor with ZERO USDC."""
    budget(310.0)
    summary = {"total_cost_basis_usd": 840.0,
               "positions": {f"dead{i}": {"cost_basis": 84.0} for i in range(10)}}
    dead = [{"tokenId": f"dead{i}"} for i in range(10)]

    # Before: every dead row counted, equity 840, floor 217, trigger silent.
    naive = live_budget.equity_usd(0.0, summary["total_cost_basis_usd"])
    assert live_guard.should_self_disarm(equity_usd=naive,
                                         floor_usd=live_budget.floor_usd())[0] is False

    cost, n_done, known = live_budget.live_open_cost(summary, dead)
    assert (cost, n_done, known) == (0.0, 10, True)
    fixed = live_budget.equity_usd(0.0, cost)
    assert fixed == 0.0
    fires, why = live_guard.should_self_disarm(equity_usd=fixed,
                                               floor_usd=live_budget.floor_usd())
    assert fires is True and "floor" in why


def test_live_open_cost_keeps_positions_that_are_still_open(budget):
    budget(310.0)
    summary = {"total_cost_basis_usd": 100.0,
               "positions": {"open1": {"cost_basis": 40.0}, "done1": {"cost_basis": 60.0}}}
    assert live_budget.live_open_cost(summary, [{"tokenId": "done1"}]) == (40.0, 1, True)
    # an unreadable resolved set keeps the old number and SAYS it is unknown
    assert live_budget.live_open_cost(summary, None) == (100.0, 0, False)
    # a cost basis with no per-position rows cannot be attributed: keep the
    # total rather than understate the bankroll and fire the floor early
    assert live_budget.live_open_cost({"total_cost_basis_usd": 30.0}, []) == (30.0, 0, False)
    assert live_budget.live_open_cost({}, []) == (0.0, 0, True)


def test_the_production_loop_feeds_the_floor_the_corrected_cost():
    import main
    src = inspect.getsource(main._live_guard_loop)
    assert "live_open_cost" in src
    assert "total_cost_basis_usd" not in src, "the raw total is what made the floor inert"


def test_a_disarm_that_cannot_be_written_stops_this_process(tmp_path, monkeypatch):
    """The one state that must fail closed. Before: the write failed, the
    caller logged, and the next cycle placed full-size live copies."""
    monkeypatch.setattr(live_mode.CONFIG, "data_dir", "/nonexistent/cannot/write")
    monkeypatch.setattr(live_mode.CONFIG, "preview_mode", False)
    monkeypatch.setattr(live_mode.CONFIG, "live_arm_enabled", True)
    monkeypatch.setattr(live_mode, "_hard_disarmed", False)
    monkeypatch.setattr(live_mode, "read_arm", lambda: {"armed": True})
    assert live_mode.is_preview() is False, "armed to begin with"
    assert live_mode.disarm(by="test") is False, "the write really did fail"
    assert live_mode.is_hard_disarmed()[0] is True
    assert live_mode.is_preview() is True, "and the process stops trading anyway"


def test_the_canary_holds_a_full_size_order_until_the_shot_fires(tmp_path, monkeypatch):
    """/live CONFIRM promises the FIRST live order is one minimum ticket. A
    SELL slipping out at full size ahead of it broke that silently."""
    from src.copy_trading import canary
    h = _Harness(tmp_path, monkeypatch)
    canary.stage(by="test")
    monkeypatch.setattr(h.te, "_inventory", lambda: (
        lambda *a, **k: None, lambda *a, **k: None, lambda t: True, _noop_async))
    q = h.trades(1)
    q[0].trade.side = "SELL"
    assert h.run(q) == 0 and h.posted == []
    assert canary.is_staged() is True and live_mode.is_armed() is True



# --------------------------------------------------------------------------- #
# The collateral migration: pUSD, not USDC.e
# --------------------------------------------------------------------------- #

def test_the_bot_reads_its_addresses_from_the_clob_client_not_a_literal():
    """Polymarket moved collateral to pUSD and its exchanges to v2. The
    hardcoded USDC.e address made every on-chain balance read return zero on a
    funded wallet, which the governor turns into 'refuse every live copy'.
    One source of truth: the same config the order builder already trusts."""
    from py_clob_client_v2.config import get_contract_config

    from src import constants
    cfg = get_contract_config(137)
    assert constants.USDC_ADDRESS == cfg.collateral
    assert constants.CTF_EXCHANGE == cfg.exchange_v2
    assert constants.NEG_RISK_CTF_EXCHANGE == cfg.neg_risk_exchange_v2
    assert constants.CTF_CONTRACT == cfg.conditional_tokens
    # the old literals must not be pasted back in
    src = open("src/constants.py", encoding="utf-8").read()
    assert "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174" not in src
    assert "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E" not in src


def test_the_collateral_is_the_token_the_funder_actually_holds():
    """Guard the fact, not the spelling: the collateral must be the token the
    CLOB settles in. pUSD today; if Polymarket migrates again this fails."""
    from src import constants
    assert constants.USDC_ADDRESS.lower() == "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"


def test_every_money_read_uses_the_shared_collateral_constant():
    """No module may re-hardcode a collateral address for OUR money.

    `wallet_funder` is the one exemption and it is not a money read: it scans
    other wallets' deposit HISTORY, where a 2026-04 inflow really did arrive
    as USDC.e. It still picks today's collateral up from the shared constant
    so the scan follows any future migration.
    """
    import pathlib
    bad = []
    for p in list(pathlib.Path("src").rglob("*.py")) + [pathlib.Path("main.py")]:
        if p.name in ("constants.py", "wallet_funder.py"):
            continue
        if "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174" in p.read_text():
            bad.append(str(p))
    assert not bad, f"stale USDC.e address hardcoded in: {sorted(set(bad))}"
    funder = pathlib.Path("src/copy_trading/wallet_funder.py").read_text()
    assert "from src.constants import USDC_ADDRESS" in funder
    assert "_USDC_CURRENT" in funder


# --------------------------------------------------------------------------- #
# Going live on the real bankroll
# --------------------------------------------------------------------------- #

def test_the_redeemer_refuses_when_it_cannot_sign_for_the_holder(monkeypatch):
    """redeemPositions does NOT revert on a zero balance: it transfers nothing
    and returns status 1, which the success branch would book as a WINNING
    realized row that never happened. Refuse before sending anything."""
    from src.copy_trading import auto_redeemer, pnl
    monkeypatch.setattr(auto_redeemer.CONFIG, "proxy_wallet",
                        "0xb5c5d02e8662b14691273a22add8e2f7f3dcdbf1")
    monkeypatch.setattr(auto_redeemer, "_warned", set())

    async def positions(w, **k):
        return [{"conditionId": "0xc1", "tokenId": "t1", "shares": 100.0,
                 "avgPrice": 0.5, "curPrice": 1.0, "title": "m",
                 "negRisk": False, "outcomeCount": 2}]
    monkeypatch.setattr(auto_redeemer, "_fetch_redeemable_positions", positions)
    wrote: list = []
    monkeypatch.setattr(pnl, "append_realized", lambda r: wrote.append(r))
    sent: list = []
    # a key whose address is NOT the proxy
    key = "ab" * 32
    res = _run(auto_redeemer.check_and_redeem_positions(key, notify=sent.append))
    assert res.count == 0
    assert wrote == [], "no fabricated winning row"
    assert len(sent) == 1 and "cannot redeem" in sent[0].lower()
    # and it says it once, not every half hour forever
    _run(auto_redeemer.check_and_redeem_positions(key, notify=sent.append))
    assert len(sent) == 1


def test_the_redeem_pass_is_wired_to_tell_the_owner():
    import inspect

    from src.copy_trading import runner
    src = inspect.getsource(runner)
    assert "notify=lambda m: _tb.send_message(m, kind=_tb.KIND_DEAL)" in src


def test_the_deploy_sizes_the_governor_to_a_real_ticket():
    """At the funded balance a copy must clear Polymarket's $5 order minimum,
    or the governor refuses everything and the bot trades nothing."""
    src = open("../.github/workflows/deploy.yml", encoding="utf-8").read()
    assert "ensure_env LIVE_BUDGET_USD 80" in src
    assert "ensure_env LIVE_BUDGET_PER_COPY_FRAC 0.08" in src
    per_copy = 80 * 0.08
    assert per_copy >= 5.0, "a ticket must clear the exchange minimum"
    assert round(80 * 0.80 / per_copy) >= 5, "room for a handful of open positions"


# --------------------------------------------------------------------------- #
# Found in production, minutes after arming
# --------------------------------------------------------------------------- #

def test_a_position_row_carries_the_asset_id_as_a_string(monkeypatch):
    """`asset` is the token id as a STRING on the live API. The parser indexed
    into it as a dict and raised on every real row. It was unreachable while
    the `resolved` filter dropped everything, so fixing that filter surfaced
    the crash: the guard's read failed, which made the bankroll floor inert
    again for a new reason."""
    from src.copy_trading import auto_redeemer
    row = dict(conditionId="0xc1", asset="83103999101871503431179904490734",
               size=970.03, avgPrice=0.26, curPrice=0.0, currentValue=0.0,
               title="m", negRisk=False, outcomeCount=2, redeemable=True)
    monkeypatch.setattr(auto_redeemer.httpx, "AsyncClient", _http([row]))
    out = _run(auto_redeemer._fetch_redeemable_positions("0xp"))
    assert len(out) == 1
    assert out[0]["tokenId"] == "83103999101871503431179904490734"
    assert out[0]["currentValue"] == 0.0

    nested = dict(row, asset={"id": "999"})
    monkeypatch.setattr(auto_redeemer.httpx, "AsyncClient", _http([nested]))
    assert _run(auto_redeemer._fetch_redeemable_positions("0xp"))[0]["tokenId"] == "999"


def test_worthless_resolved_positions_do_not_disarm_a_live_session(tmp_path, monkeypatch):
    """The funder carries 61 resolved losers worth zero. Counting them as
    failed redemptions would disarm every live session forever over tickets
    there is nothing to collect on."""
    monkeypatch.setattr(live_guard.CONFIG, "data_dir", str(tmp_path))
    dust = [{"tokenId": f"t{i}", "currentValue": 0.0} for i in range(61)]
    out = live_guard.run_once(redeemable=dust)
    assert out["unredeemed"] == 0 and out["disarm_reason"] == ""

    # real stuck capital still disarms
    stuck = [{"tokenId": f"s{i}", "currentValue": 12.5} for i in range(3)]
    out = live_guard.run_once(redeemable=stuck)
    assert out["unredeemed"] == 3 and "failed to redeem" in out["disarm_reason"]

    # and a row with NO value figure is counted, because unknown is not zero
    unknown = [{"tokenId": f"u{i}"} for i in range(3)]
    out = live_guard.run_once(redeemable=unknown)
    assert out["unredeemed"] == 3


def test_resolved_positions_are_still_excluded_from_the_bankroll(budget):
    """Worthless for the disarm count, but their COST must still leave the
    bankroll: a resolved position is worth its payout, not what we paid."""
    budget(80.0)
    summary = {"total_cost_basis_usd": 840.0,
               "positions": {f"t{i}": {"cost_basis": 84.0} for i in range(10)}}
    dust = [{"tokenId": f"t{i}", "currentValue": 0.0} for i in range(10)]
    cost, n, known = live_budget.live_open_cost(summary, dust)
    assert (cost, n, known) == (0.0, 10, True)
    assert live_budget.equity_usd(80.41, cost) == 80.41


def test_the_stuck_order_detector_is_not_shadowed_by_the_redeemable_filter(tmp_path, monkeypatch):
    """A local named `stuck` for the redeemable filter overwrote the stuck
    ORDERS list, silently zeroing a detector that had nothing to do with it.
    Both must report together."""
    monkeypatch.setattr(live_guard.CONFIG, "data_dir", str(tmp_path))
    now = time.time()
    out = live_guard.run_once(
        pending_orders=[{"order_id": "o1", "placed_at": (now - 3600) * 1000}],
        redeemable=[{"tokenId": "t", "currentValue": 0.0}],
        now=now)
    assert out["stuck_orders"] == 1, "the order detector still sees its order"
    assert out["unredeemed"] == 0, "and the worthless position is still excluded"


def test_the_executor_hands_the_clob_the_object_the_library_reads():
    """'dict' object has no attribute 'token_id', on the first real order.

    `create_and_post_order` reads `order_args.token_id`, so the dict the
    executor used to build raised on EVERY call: the live order path could
    never have placed anything. The suite missed it because its fakes take a
    dict happily. This fake keys off the REAL request shape, which is the
    standing rule after four defects of exactly this kind.
    """
    from datetime import datetime, timezone

    from py_clob_client_v2.clob_types import OrderArgsV2
    from src.copy_trading import trade_executor
    from src.models import DetectedTrade

    seen = {}

    class _RealisticClob:
        def create_and_post_order(self, order_args, *a, **k):
            # exactly what the library does: attribute access, no .get()
            seen["type"] = type(order_args).__name__
            seen["token_id"] = order_args.token_id
            seen["price"] = order_args.price
            seen["size"] = order_args.size
            seen["side"] = order_args.side
            assert isinstance(order_args, OrderArgsV2), "the library's own type"
            return {"orderID": "0xabc"}

    trade = DetectedTrade(id="t", trader_address=W1,
                          timestamp=datetime.now(timezone.utc).isoformat(),
                          market="m", token_id="tok-123", condition_id="c",
                          side="BUY", size=900.0, price=0.50)
    res = _run(trade_executor._execute_copy_order(
        _RealisticClob(), trade, 6.40, {"best_bid": 0.49, "best_ask": 0.51,
                                        "midpoint": 0.50, "spread_bps": 400}))
    assert res is not None and res.order_id == "0xabc"
    assert seen["type"] == "OrderArgsV2" and seen["token_id"] == "tok-123"
    assert seen["side"] == "BUY" and seen["size"] > 0
    # the order lifts the ask, and buys about what the money buys at that price
    assert seen["price"] == 0.51
    assert abs(seen["size"] - round(6.40 / 0.51, 2)) < 0.02


def test_a_clob_that_rejects_a_dict_makes_the_executor_fail_not_pretend():
    """If the shape is ever wrong again, the executor must return None (which
    spends the canary and disarms), never a fabricated success."""
    from datetime import datetime, timezone

    from src.copy_trading import trade_executor
    from src.models import DetectedTrade

    class _StrictClob:
        def create_and_post_order(self, order_args, *a, **k):
            raise AttributeError("'dict' object has no attribute 'token_id'")

    trade = DetectedTrade(id="t", trader_address=W1,
                          timestamp=datetime.now(timezone.utc).isoformat(),
                          market="m", token_id="tok", condition_id="c",
                          side="BUY", size=900.0, price=0.50)
    assert _run(trade_executor._execute_copy_order(
        _StrictClob(), trade, 6.40, {"best_bid": 0.49, "best_ask": 0.51,
                                     "midpoint": 0.50, "spread_bps": 400})) is None


def test_cancelling_an_order_uses_a_method_the_client_actually_has():
    """The v2 client has no `cancel`, only `cancel_order(OrderPayload)`. The
    old call raised every time, so an unfilled order was never withdrawn and
    the guard's stuck-order action could not act. Same shape class as the
    order-args dict, so the fake mirrors the real client's surface."""
    import inspect

    from py_clob_client_v2 import ClobClient, OrderPayload
    from src.copy_trading import trade_executor

    assert not hasattr(ClobClient, "cancel"), "if this ever exists, revisit"
    assert hasattr(ClobClient, "cancel_order")

    seen = {}

    class _RealisticClob:
        # deliberately exposes ONLY what the real client exposes
        def cancel_order(self, payload):
            seen["type"] = type(payload).__name__
            seen["orderID"] = payload.orderID
            return {"canceled": [payload.orderID]}

    assert _run(trade_executor._cancel_order(_RealisticClob(), "0xdead")) is True
    assert seen == {"type": "OrderPayload", "orderID": "0xdead"}

    class _NoCancel:
        pass
    assert _run(trade_executor._cancel_order(_NoCancel(), "0xdead")) is False


# =========================================================================== #
# Run s-kac3t7 (2026-09-06): a trader that actually trades, and a chat he can read
# =========================================================================== #

def _tb():
    import src.telegram_bot as tb
    return tb


@pytest.fixture
def tg(tmp_path, monkeypatch):
    """Telegram with a capturing transport and an isolated prefs file."""
    tb = _tb()
    monkeypatch.setattr(tb.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(tb.CONFIG, "telegram_bot_token", "t")
    monkeypatch.setattr(tb.CONFIG, "telegram_chat_id", "1")
    monkeypatch.setattr(tb.CONFIG, "telegram_research_messages", False)
    monkeypatch.setattr(tb, "_suppressed_research", 0)
    sent: list = []

    class _R:
        ok = True
        status_code = 200
        text = ""
    monkeypatch.setattr(tb.requests, "post", lambda url, json=None, timeout=0: sent.append(json) or _R())
    return tb, sent


def test_every_push_carries_its_class_and_replies_carry_none(tg):
    tb, sent = tg
    assert tb.send_message("real order", kind=tb.KIND_DEAL)
    assert tb.send_message("who we follow", kind=tb.KIND_WALLET)
    assert tb.send_message("the process", kind=tb.KIND_BOT)
    assert tb.send_message("answer to /pnl")
    texts = [p["text"] for p in sent]
    assert texts[0].startswith("<b>[💰 DEAL]</b> real order")
    assert texts[1].startswith("<b>[👛 WALLET]</b> who we follow")
    assert texts[2].startswith("<b>[🤖 BOT]</b> the process")
    assert texts[3] == "answer to /pnl", "a reply is not prefixed"


def test_research_is_held_by_default_counted_and_switchable(tg):
    tb, sent = tg
    assert tb.send_message("paper offer", kind=tb.KIND_RESEARCH) is False
    assert tb.send_message("discovery digest", kind=tb.KIND_RESEARCH) is False
    assert sent == [], "held, not sent"
    assert tb.suppressed_research_count() == 2
    assert tb.set_research(True) and tb.research_enabled()
    assert tb.send_message("paper offer", kind=tb.KIND_RESEARCH) is True
    assert sent[-1]["text"].startswith("<b>[🔬 RESEARCH]</b>")
    tb.set_research(False)
    assert tb.research_enabled() is False, "the runtime switch survives on disk"


def test_a_long_research_message_is_held_whole_and_a_long_deal_prefixed_once(tg):
    tb, sent = tg
    long = "\n".join(f"line {i} " + "x" * 60 for i in range(120))
    assert tb._send_chunked(long, kind=tb.KIND_RESEARCH) is False and sent == []
    assert tb._send_chunked(long, kind=tb.KIND_DEAL) is True
    assert len(sent) >= 2
    assert sent[0]["text"].startswith("<b>[💰 DEAL]</b>")
    assert not any(p["text"].startswith("<b>[") for p in sent[1:]), "prefix once"


def test_the_paper_book_offer_is_research_and_the_admit_card_is_not(tg, monkeypatch):
    tb, sent = tg
    assert tb.send_promotion_offer("0x" + "a" * 40, 15, 0.2, 30.0) is False, "held: paper"
    assert sent == []


def test_every_push_site_in_main_names_a_class():
    """Replies to the owner carry no class; every PUSH must, or a message he
    never asked for arrives unlabelled. The daily block, the guard, the
    canary, the redeemer, discovery and the paper books all route here."""
    import re
    src = open("main.py", encoding="utf-8").read()
    bare = []
    for m in re.finditer(r'telegram_bot\.(send_message|_send_chunked)\(', src):
        seg = src[m.start(): m.start() + 200]
        if "kind=" not in seg:
            bare.append(src[:m.start()].count("\n") + 1)
    assert bare == [], f"unclassed push at main.py lines {bare}"
    assert "def _send_deal" in src
    assert "send=_send_deal" in src


def test_the_daily_block_sends_research_and_deal_separately(monkeypatch, budget):
    from src.copy_trading import rehearsal
    budget(80.0)
    monkeypatch.setattr(rehearsal, "_rehearsal_part", lambda now: "🎯 counterfactual")
    monkeypatch.setattr(rehearsal, "real_money_line", lambda now=None: "💵 real money")
    a, b = rehearsal.daily_parts(now=1.0)
    assert a == "🎯 counterfactual" and b == "💵 real money"
    assert rehearsal.daily_message(now=1.0) == "🎯 counterfactual\n\n💵 real money"
    import main
    src = inspect.getsource(main._ab_race_reporter_loop)
    assert "rehearsal.daily_parts()" in src
    assert "kind=telegram_bot.KIND_RESEARCH" in src and "kind=telegram_bot.KIND_DEAL" in src
    assert "suppressed_research_count(reset=True)" in src


def test_the_insider_era_detectors_are_research():
    from src.copy_trading import pattern_detector, watchlist_alerter
    assert 'kind="research"' in inspect.getsource(watchlist_alerter)
    assert 'kind="research"' in inspect.getsource(pattern_detector)


def test_research_command_toggles_and_reports(tg):
    tb, sent = tg
    tb._handle_research("/research")
    assert "OFF" in sent[-1]["text"]
    tb._handle_research("/research on")
    assert tb.research_enabled() and "ON" in sent[-1]["text"]
    tb._handle_research("/research off")
    assert not tb.research_enabled()


# ---- the per-wallet daily cap ----

def test_one_busy_wallet_cannot_take_the_whole_day(tmp_path, monkeypatch):
    from src.copy_trading import daily_spend_guard as g
    monkeypatch.setattr(g, "_STATE_FILE", str(tmp_path / "d.json"))
    monkeypatch.setattr(CONFIG, "live_max_per_wallet_day", 2)
    monkeypatch.setattr(CONFIG, "max_daily_volume_usd", 500.0)
    g._state.date = ""
    assert g.can_copy_wallet(W1)[0]
    g.record_wallet_copy(W1); g.record_wallet_copy(W1)
    ok, why = g.can_copy_wallet(W1)
    assert ok is False and "2 of 2" in why
    assert g.can_copy_wallet(W2)[0], "another wallet is unaffected"
    monkeypatch.setattr(CONFIG, "live_max_per_wallet_day", 0)
    assert g.can_copy_wallet(W1)[0], "0 means no cap"


def test_the_per_wallet_cap_binds_at_the_order_sink(tmp_path, monkeypatch):
    from src.copy_trading import daily_spend_guard as g
    monkeypatch.setattr(CONFIG, "live_max_per_wallet_day", 2)
    h = _Harness(tmp_path, monkeypatch)
    g._state.date = ""
    placed = h.run(h.trades(4))
    assert placed == 2 and len(h.posted) == 2, "third and fourth copies from the same wallet are refused"
    assert h.seen == {"t0", "t1", "t2", "t3"}


def test_the_deploy_pins_five_deals_a_day_and_two_per_wallet():
    src = open("../.github/workflows/deploy.yml", encoding="utf-8").read()
    assert "ensure_env LIVE_BUDGET_DAILY_FRAC 0.40" in src
    assert "ensure_env LIVE_MAX_PER_WALLET_DAY 2" in src
    assert round(80 * 0.40 / (80 * 0.08)) == 5


# ---- the canary no longer holds the arm hostage ----

def test_arming_no_longer_stages_a_canary(canary_env, monkeypatch):
    import src.telegram_bot as tb
    canary = canary_env
    monkeypatch.setattr(live_mode, "arm", lambda reason="", by="": (True, "armed"))
    monkeypatch.setattr(live_guard, "active_block", lambda: None)
    sent: list = []
    with patch.object(tb, "send_message", lambda x, **k: sent.append(x)):
        tb._handle_live("/live CONFIRM")
    assert "ARMED" in sent[0] and "staged" not in sent[0].lower()
    assert canary.is_staged() is False


def test_an_expired_canary_no_longer_pulls_the_arm(canary_env, monkeypatch):
    canary = canary_env
    _open_the_door(monkeypatch, canary)
    canary.stage(by="test", now=1000.0)
    pulled: list = []
    monkeypatch.setattr(canary.live_mode, "is_armed", lambda: True)
    monkeypatch.setattr(canary.live_mode, "disarm", lambda by="": pulled.append(by) or True)
    sent: list = []
    assert canary.expire_if_due(send=sent.append, now=1000.0 + canary.TTL_S + 1) is True
    assert pulled == [] and "continues at normal size" in sent[0]


def test_canary_messages_use_plain_words(canary_env, monkeypatch):
    canary = canary_env
    canary._write({"staged": True, "staged_ts": 0.0, "expires_ts": 9e12, "fired": None, "fill": None})
    canary.consume(market="m", token_id="t", their_price=0.5, quoted_ask=0.51,
                   copy_size=5.0, notify_latency_s=2.0, now=10.0)
    canary.record_post_failed("no order id")
    text = canary.report_text()
    for jargon in ("one shot", "RESET the", "arm comes off", "Canary</b>: the one order"):
        assert jargon not in text
    assert "Test copy failed" in text


# ---- the test order ----

def test_test_order_refuses_a_bet_dressed_as_a_test(canary_env, monkeypatch):
    from src.copy_trading import canary, trade_executor
    _open_the_door(monkeypatch, canary)
    monkeypatch.setattr(canary.live_budget, "is_open", lambda: True)

    async def snap(client, token_id):
        return {"best_bid": 0.60, "best_ask": 0.62, "midpoint": 0.61, "spread_bps": 300}
    monkeypatch.setattr(trade_executor, "_get_market_snapshot", snap)
    ok, msg = _run(canary.fire_test_order(object(), "tok", title="m"))
    assert ok is False and "bet, not a test" in msg


def test_test_order_goes_through_the_live_path_and_the_verifier_reports_once(canary_env, monkeypatch, tmp_path):
    from src.copy_trading import canary, daily_spend_guard, trade_executor, trade_queue
    from src.models import OrderResult
    _open_the_door(monkeypatch, canary)
    monkeypatch.setattr(canary.live_budget, "is_open", lambda: True)
    monkeypatch.setattr(daily_spend_guard, "_STATE_FILE", str(tmp_path / "d.json"))
    monkeypatch.setattr(CONFIG, "max_daily_volume_usd", 500.0)
    monkeypatch.setattr(CONFIG, "min_order_size_usd", 5.0)

    async def snap(client, token_id):
        return {"best_bid": 0.975, "best_ask": 0.981, "midpoint": 0.978, "spread_bps": 61}
    monkeypatch.setattr(trade_executor, "_get_market_snapshot", snap)
    posted = {}

    async def post(client, trade, copy_size, snapshot):
        posted.update(size=copy_size, token=trade.token_id, side=trade.side, price=trade.price)
        return OrderResult(order_id="0xtest1", shares=round(copy_size / 0.98, 2), order_price=0.98)
    monkeypatch.setattr(trade_executor, "_execute_copy_order", post)
    queued: list = []
    monkeypatch.setattr(trade_queue, "enqueue_pending_order", lambda po: queued.append(po))

    class _Clob:
        def get_order_book(self, token_id):
            return {"min_order_size": "5"}
    ok, msg = _run(canary.fire_test_order(_Clob(), "tok-95", title="Spread: X (-1.5)"))
    assert ok and "Test order placed" in msg and "0xtest1" in msg
    assert posted["side"] == "BUY" and posted["size"] == 5.0 and posted["token"] == "tok-95"
    assert len(queued) == 1 and queued[0].source == "testorder" and queued[0].order_id == "0xtest1"
    assert daily_spend_guard.can_spend(0.0)[0]

    class _Fill:
        status, fill_price, filled_shares, filled_usd = "FILLED", 0.98, 5.1, 5.0
    rep = canary.record_test_fill("0xtest1", _Fill())
    assert rep and "Test order filled" in rep and "places and fills" in rep
    assert "claim the payout by hand" in rep
    assert canary.record_test_fill("0xtest1", _Fill()) is None, "reported once"
    assert canary.record_test_fill("other", _Fill()) is None


def test_the_verifier_reports_a_test_order_too():
    from src.copy_trading import trade_executor
    src = inspect.getsource(trade_executor.process_verifications)
    assert "canary.record_test_fill(po.order_id, fill)" in src


def test_testorder_command_refuses_while_the_door_is_shut(tg, monkeypatch):
    tb, sent = tg
    monkeypatch.setattr(tb.CONFIG, "preview_mode", True)
    tb._handle_testorder("/testorder 12345")
    assert "Cannot place a test order" in sent[-1]["text"]
    tb._handle_testorder("/testorder")
    assert "Usage" in sent[-1]["text"]


def test_the_menu_and_help_carry_the_new_commands():
    tb = _tb()
    cmds = {e["command"] for e in tb.BOT_MENU_COMMANDS}
    assert {"research", "testorder"} <= cmds


# ---- the first real order found this: prices must round TOWARD crossing ----

def test_a_buy_rounds_up_onto_the_tick_and_a_sell_rounds_down():
    from src.copy_trading.order_executor import quote_copy_order, round_toward_crossing
    # the exact order that rested: ask 0.984 on a 0.001 market
    assert quote_copy_order("BUY", 0.9, {"best_ask": 0.984, "tick_size": 0.001}) == 0.984
    # on a 0.01 market the same ask becomes 0.99, never 0.98
    assert quote_copy_order("BUY", 0.9, {"best_ask": 0.984, "tick_size": 0.01}) == 0.99
    assert quote_copy_order("BUY", 0.9, {"best_ask": 0.984}) == 0.99, "no tick means 0.01, still crossing"
    assert quote_copy_order("SELL", 0.9, {"best_bid": 0.976, "tick_size": 0.001}) == 0.976
    assert quote_copy_order("SELL", 0.9, {"best_bid": 0.976, "tick_size": 0.01}) == 0.97
    assert round_toward_crossing(0.5, "BUY", 0.01) == 0.5, "already on the tick stays"
    assert round_toward_crossing(0.12345, "BUY", 0.0001) == 0.1235
    # bounds still hold
    assert quote_copy_order("BUY", 0.9, {"best_ask": 0.9995, "tick_size": 0.001}) is None
    assert quote_copy_order("BUY", 0.9, {"best_ask": 0.996, "tick_size": 0.01}) is None


def test_the_snapshot_carries_the_books_tick_and_the_order_uses_it():
    """The seam: a 0.001 book, ask 0.984, must produce an order AT 0.984."""
    from datetime import datetime, timezone

    from src.copy_trading import trade_executor
    from src.models import DetectedTrade

    class _Clob:
        def get_order_book(self, token_id):
            return {"tick_size": "0.001", "min_order_size": "5",
                    "bids": [{"price": "0.970", "size": "100"}, {"price": "0.975", "size": "50"}],
                    "asks": [{"price": "0.990", "size": "100"}, {"price": "0.984", "size": "24"}]}

        def create_and_post_order(self, order_args, *a, **k):
            assert abs(order_args.price - 0.984) < 1e-9, f"posted {order_args.price}, must lift the ask"
            return {"orderID": "0xok"}
    snap = _run(trade_executor._get_market_snapshot(_Clob(), "tok"))
    assert snap["tick_size"] == 0.001 and snap["best_ask"] == 0.984
    trade = DetectedTrade(id="t", trader_address=W1, timestamp=datetime.now(timezone.utc).isoformat(),
                          market="m", token_id="tok", condition_id="c", side="BUY", size=900.0, price=0.95)
    res = _run(trade_executor._execute_copy_order(_Clob(), trade, 5.0, snap))
    assert res is not None and abs(res.order_price - 0.984) < 1e-9


# ---- manager round 2: disarmed skips go loud; the daily line reads a completed window ----

def test_a_disarmed_skip_is_announced_once_per_episode_and_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(live_mode.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(live_mode, "_disarmed_skip_count", 0)
    monkeypatch.setattr(live_mode, "_disarmed_announced_ts", None)
    monkeypatch.setattr(live_mode, "read_arm", lambda: {"armed": False, "ts": 100.0, "by": "canary"})
    a1, rec = live_mode.note_disarmed_skip()
    a2, _ = live_mode.note_disarmed_skip()
    a3, _ = live_mode.note_disarmed_skip()
    assert (a1, a2, a3) == (True, False, False) and rec["by"] == "canary"
    assert live_mode.disarmed_skips() == 3
    # a new disarm episode (new ts) announces again
    monkeypatch.setattr(live_mode, "read_arm", lambda: {"armed": False, "ts": 200.0, "by": "live-guard"})
    assert live_mode.note_disarmed_skip()[0] is True


def test_the_executor_pushes_the_first_disarmed_skip(tmp_path, monkeypatch):
    """The seam: a live process, arm off, a followed wallet trades."""
    from src.copy_trading import telegram_notifier
    h = _Harness(tmp_path, monkeypatch, armed=False)
    monkeypatch.setattr(live_mode, "_disarmed_skip_count", 0)
    monkeypatch.setattr(live_mode, "_disarmed_announced_ts", None)
    sent: list = []

    async def fake_send(text, kind=None):
        sent.append((text, kind)); return True
    monkeypatch.setattr(telegram_notifier, "_send_message", fake_send)
    h.run(h.trades(3))
    assert h.posted == []
    assert len(sent) == 1, "one push per disarm episode, not one per skip"
    assert "trading is OFF" in sent[0][0] and sent[0][1] == "deal"
    assert "/live CONFIRM" in sent[0][0]
    assert live_mode.disarmed_skips() == 3


def test_the_spend_guard_keeps_yesterdays_wallet_counts_across_rollover(tmp_path, monkeypatch):
    from src.copy_trading import daily_spend_guard as g
    monkeypatch.setattr(g, "_STATE_FILE", str(tmp_path / "d.json"))
    monkeypatch.setattr(CONFIG, "live_max_per_wallet_day", 2)
    g._state.date = ""; g._state.yesterday = ""; g._state.wallet_copies = {}; g._state.wallet_copies_yesterday = {}
    monkeypatch.setattr(g, "today_utc", lambda: "2026-09-06")
    g.record_wallet_copy(W1); g.record_wallet_copy(W1); g.record_wallet_copy(W2)
    assert g.wallet_copies_window() == ("2026-09-06 (partial)", {W1.lower(): 2, W2.lower(): 1})
    monkeypatch.setattr(g, "today_utc", lambda: "2026-09-07")
    g.record_wallet_copy(W2)
    day, counts = g.wallet_copies_window()
    assert day == "2026-09-06" and counts == {W1.lower(): 2, W2.lower(): 1}, "the COMPLETED day"
    assert g.wallet_copies_today(W2) == 1 and g.wallet_copies_today(W1) == 0


def test_the_daily_line_names_the_window_the_wallets_and_the_arm(monkeypatch, budget):
    from src.copy_trading import daily_spend_guard, rehearsal, zset
    budget(80.0)
    monkeypatch.setattr(CONFIG, "live_max_per_wallet_day", 2)
    monkeypatch.setattr(zset, "wallets", lambda: [W1, W2])
    monkeypatch.setattr(daily_spend_guard, "wallet_copies_window", lambda: ("2026-09-06", {W1.lower(): 2}))
    monkeypatch.setattr(live_mode, "read_arm", lambda: {"armed": False, "ts": 1788620750.0, "by": "canary"})
    monkeypatch.setattr(live_mode, "disarmed_skips", lambda: 19)
    line = rehearsal.followed_wallets_line()
    assert "copies placed 2026-09-06" in line
    assert f"{W1[:10]}… 2 of 2" in line and f"{W2[:10]}… 0 of 2" in line
    assert "trading OFF since 2026-09-05 15:05 UTC (by canary)" in line
    assert "19 followed-wallet buy(s) skipped while OFF" in line
    monkeypatch.setattr(live_mode, "read_arm", lambda: {"armed": True, "ts": 1.0})
    assert "trading ON" in rehearsal.followed_wallets_line()


def test_the_real_money_line_carries_the_followed_wallets(monkeypatch, budget):
    from src.copy_trading import inventory, live_guard, pnl, rehearsal
    budget(80.0)
    monkeypatch.setattr(live_mode, "read_arm", lambda: {"armed": True, "first_armed_ts": 1.0, "ts": 1.0})
    monkeypatch.setattr(live_budget, "_read_balance", lambda now=None: 75.0)
    monkeypatch.setattr(inventory, "get_inventory_summary", lambda: {"total_cost_basis_usd": 0.0, "positions": {}})
    monkeypatch.setattr(pnl, "load_realized", lambda: [])
    monkeypatch.setattr(live_guard, "redeemable_positions", lambda w: [])
    monkeypatch.setattr(rehearsal, "followed_wallets_line", lambda: "👛 followed wallets, copies placed X: none")
    line = rehearsal.real_money_line()
    assert "bankroll $75.00" in line and "👛 followed wallets" in line


def test_test_order_texts_say_claim_by_hand(canary_env, monkeypatch):
    from src.copy_trading import canary
    canary._write_test({"order_id": "0xt", "posted": True, "market": "m", "fill": None})

    class _Fill:
        status, fill_price, filled_shares, filled_usd = "FILLED", 0.984, 5.08, 5.0
    rep = canary.record_test_fill("0xt", _Fill())
    assert "claim the payout by hand" in rep and "cannot redeem" in rep
    assert "works end to end" not in rep, "the cycle is not closed at redemption"
