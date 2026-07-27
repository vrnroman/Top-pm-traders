"""Tests for the ROADMAP P1 batch (2026-07-28) — actually search for edge.

P1-1's cache resurrection lives in its own commit + test file (test_p1_cache.py)
so rolling back the 10x widen never touches the safety fix.
P1-2: the skill screen excludes wallets already proven-negative under our copy
      action (persisted copy stats) and demotes the hit-rate-scooper signature;
      copy evidence accrues rank over copy_n=0 in the watchlist order.
P1-3: the gate dossier ships no explicit nulls and carries the full skill /
      entry / curve blocks (the "+613% ROI" claim is legible next to capital).
P1-4: gate fail-open is visible: Langfuse ERROR trace, /gate unvetted split,
      and a Telegram alert when a sweep fails open past the alert fraction.
P1-5: every holdout roll is logged (DEBUG per roll + INFO per sweep).
P1-6: book-evidence gates block UNSTAMPED wallets from slices this book's own
      ledger proves losing (category and entry-price bucket).
P1-7: opened rows carry modeled costs; the surfaces expose net-of-costs ROI.
I4:   the cull histogram persists one JSONL row per sweep (trendable).
"""

from __future__ import annotations

import logging
import os
import tempfile
from types import SimpleNamespace

import pytest

from src.copy_trading import discovery_data as dd
from src.copy_trading import gate_history
from src.copy_trading.copy_cost import CostModel
from src.copy_trading.copy_paper import (
    CopyPaperEngine,
    PaperCopyLedger,
    PaperPosition,
    price_bucket,
)
from src.copy_trading.discovery import DiscoveryConfig, DiscoveryState, Eval, _rank_key
from src.copy_trading.discovery_runner import DiscoveryRunner, _dossier_from_eval
from src.copy_trading.llm_review import LLMVerdict, build_dossier
from src.copy_trading.trader_scoring import WalletMetrics, select_targets


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _nulls(obj) -> int:
    if obj is None:
        return 1
    if isinstance(obj, dict):
        return sum(_nulls(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return sum(_nulls(v) for v in obj)
    return 0


def _metrics(*, tstat_pnls, wins, n_closed, capital=10_000.0) -> WalletMetrics:
    m = WalletMetrics()
    m.capital = capital
    m.pnls = list(tstat_pnls)
    m.pnl = sum(tstat_pnls)
    m.wins = wins
    m.n_closed = n_closed
    return m


CFG = DiscoveryConfig(min_capture_cents=1.5, min_tstat=10.0,
                      drop_capture_cents=1.0, watchlist_cap=10, auto_remove=True)


def _ev(w, theory="1e", **kw):
    base = dict(capture_cents=0.0, lead_cents=0.0, hit_rate=0.0, n=0,
                roi=0.5, tstat=12.0, flagged_by=(theory,))
    base.update(kw)
    return Eval(wallet=w, **base)


def _runner(tmp_path, *, seq, verdict_fn=None, rand=None, holdout_frac=0.0,
            llm_review_fn=None):
    calls = {"i": 0}

    def fake_eval(config, **kw):
        d = seq[min(calls["i"], len(seq) - 1)]
        calls["i"] += 1
        return d

    if llm_review_fn is None:
        def llm_review_fn(dossier, model=None):
            return verdict_fn(dossier) if verdict_fn else None

    return DiscoveryRunner(
        config=CFG,
        watchlist_path=str(tmp_path / "wl.json"),
        state_path=str(tmp_path / "state.json"),
        evaluate=fake_eval, llm_review=llm_review_fn, llm_review_enabled=True,
        holdout_frac=holdout_frac, holdout_max_per_sweep=2,
        now=lambda: 100.0, rand=rand,
    )


# --------------------------------------------------------------------------- #
# P1-2 — copy-replay-informed skill screen
# --------------------------------------------------------------------------- #

def test_screen_excluded_only_when_proven_negative_and_fresh():
    cfg = DiscoveryConfig(min_copy_replay_n=12, min_copy_replay_roi=0.02)
    now = 1_000_000.0
    stats = {"0xbad": {"copy_n": 15, "copy_roi": -0.05, "ts": now - 100}}
    assert dd._screen_excluded("0xBAD", stats, cfg, now) is True
    # too little evidence -> not excluded
    assert dd._screen_excluded("0xBAD", {"0xbad": {"copy_n": 3, "copy_roi": -0.5,
                                                   "ts": now}}, cfg, now) is False
    # proven POSITIVE -> not excluded
    assert dd._screen_excluded("0xOK", {"0xok": {"copy_n": 20, "copy_roi": 0.10,
                                                 "ts": now}}, cfg, now) is False
    # stale evidence (> re-eval window) -> re-evaluate, don't keep punishing
    old = {"0xbad": {"copy_n": 15, "copy_roi": -0.05,
                     "ts": now - dd.COPY_STAT_REEVAL_S - 1}}
    assert dd._screen_excluded("0xBAD", old, cfg, now) is False
    # gate off -> screen inert
    cfg_off = DiscoveryConfig(copy_replay_gate=False)
    assert dd._screen_excluded("0xBAD", stats, cfg_off, now) is False
    # no record -> not excluded
    assert dd._screen_excluded("0xNEW", stats, cfg, now) is False


def test_merge_topk_excludes_persisted_proven_negative():
    cfg = DiscoveryConfig(skill_pool=10, min_copy_replay_n=12,
                          min_copy_replay_roi=0.02)
    good = _metrics(tstat_pnls=[10.0] * 20, wins=12, n_closed=20)
    bad = _metrics(tstat_pnls=[20.0] * 20, wins=15, n_closed=20)  # higher t-stat
    prior = {"0xbad": {"copy_n": 15, "copy_roi": -0.05, "ts": 1e12}}
    pool = dd._merge_topk([], {"0xGOOD": good, "0xBAD": bad}, cfg,
                          prior_copy_stats=prior, now=1e12)
    picked = {rm.address for rm in pool}
    assert "0xGOOD" in picked and "0xBAD" not in picked
    # without prior stats the higher-t-stat scooper would have won the slot
    pool2 = dd._merge_topk([], {"0xGOOD": good, "0xBAD": bad}, cfg)
    assert {rm.address for rm in pool2} == {"0xGOOD", "0xBAD"}


def test_select_targets_demotes_scooper_signature():
    # scooper: 100% hit rate on low-variance near-$1 wins -> huge t-stat
    scooper = _metrics(tstat_pnls=[5.0] * 18 + [4.0, 6.0], wins=20, n_closed=20)
    normal = _metrics(tstat_pnls=[8.0] * 18 + [-5.0, 15.0], wins=12, n_closed=20)
    assert scooper.hit_rate >= 0.95 and scooper.tstat > normal.tstat
    scored = {"0xSCOOP": scooper, "0xNORM": normal}
    # legacy: the scooper's t-stat wins the top slot
    assert select_targets(scored, method="robust", top_k=1)[0].address == "0xSCOOP"
    # with the demotion wired, it ranks below everyone else
    out = select_targets(scored, method="robust", top_k=2,
                         demote_hit_rate=0.95, demote_min_closed=15)
    assert [rm.address for rm in out] == ["0xNORM", "0xSCOOP"]
    # thin books are never demoted on hit rate alone (n < demote_min_closed)
    thin = _metrics(tstat_pnls=[5.0, 4.9, 5.1] * 3 + [5.0], wins=10, n_closed=10,
                    capital=10_000)
    assert thin.hit_rate >= 0.95 and thin.tstat > normal.tstat
    out2 = select_targets({"0xTHIN": thin, "0xNORM": normal}, method="robust",
                          top_k=2, demote_hit_rate=0.95, demote_min_closed=15)
    assert out2[0].address == "0xTHIN"


def test_rank_key_copy_evidence_outranks_no_data():
    cfg = DiscoveryConfig()  # copy_replay_gate on, min_n=12, min_roi=0.0
    e_none = _ev("0xNONE", copy_n=0, copy_roi=0.0)
    e_accruing = _ev("0xACC", copy_n=5, copy_roi=0.01)      # below min_n -> unproven
    e_proven = _ev("0xPRO", copy_n=15, copy_roi=0.10)
    assert _rank_key(cfg, e_accruing) > _rank_key(cfg, e_none)
    assert _rank_key(cfg, e_proven) > _rank_key(cfg, e_accruing)


def test_discovery_state_copy_stats_roundtrip():
    st = DiscoveryState(on_watchlist={}, last_run=1.0, initialized=True,
                        copy_stats={"0xw": {"copy_n": 15, "copy_roi": -0.05, "ts": 99.0}})
    assert DiscoveryState.from_json(st.to_json()).copy_stats == st.copy_stats
    # legacy state files (no copy_stats key) load as {}
    assert DiscoveryState.from_json({"on_watchlist": {}}).copy_stats == {}


def test_discovery_state_copy_stats_shape_checked():
    """Review finding (MEDIUM): a corrupt copy_stats row must be dropped at the
    boundary — the readers call .get() on every record, and an AttributeError
    there killed EVERY sweep at the same spot with no self-heal."""
    st = DiscoveryState.from_json({
        "copy_stats": {"0xbad": "junk", "0xok": {"copy_n": 15, "ts": 1.0}}})
    assert st.copy_stats == {"0xok": {"copy_n": 15, "ts": 1.0}}
    # copy_stats itself being a bare string must not raise either
    assert DiscoveryState.from_json({"copy_stats": "junk"}).copy_stats == {}


def test_screen_excluded_tolerates_garbage_rows():
    cfg = DiscoveryConfig(min_copy_replay_n=12, min_copy_replay_roi=0.02)
    assert dd._screen_excluded("0xW", {"0xw": "junk"}, cfg, 1e12) is False
    assert dd._screen_excluded("0xW", {"0xw": None}, cfg, 1e12) is False


def test_updated_copy_stats_drops_garbage_rows(tmp_path):
    r = _runner(tmp_path, seq=[{}])
    r._now = lambda: 1_000_000.0
    prior = {"0xjunk": "not-a-dict", "0xok": {"copy_n": 15, "ts": 1_000_000.0}}
    out = r._updated_copy_stats(prior, {})
    assert out == {"0xok": {"copy_n": 15, "ts": 1_000_000.0}}  # junk dropped, no raise


def test_updated_copy_stats_merges_keeps_max_n_and_prunes(tmp_path):
    r = _runner(tmp_path, seq=[{}])
    r._now = lambda: 1_000_000.0
    s1 = r._updated_copy_stats({}, {"0xW": _ev("0xW", copy_n=15, copy_roi=-0.05)})
    assert s1["0xw"]["copy_n"] == 15 and s1["0xw"]["ts"] == 1_000_000.0
    # a later sweep with LESS replay evidence must not downgrade the record
    s2 = r._updated_copy_stats(s1, {"0xW": _ev("0xW", copy_n=3, copy_roi=0.0)})
    assert s2["0xw"]["copy_n"] == 15 and s2["0xw"]["copy_roi"] == -0.05
    # equal-or-more evidence refreshes
    s3 = r._updated_copy_stats(s1, {"0xW": _ev("0xW", copy_n=18, copy_roi=-0.02)})
    assert s3["0xw"]["copy_n"] == 18
    # ancient entries are pruned
    old = {"0xold": {"copy_n": 15, "copy_roi": -0.05,
                     "ts": 1_000_000.0 - 31 * 86400.0}}
    assert "0xold" not in r._updated_copy_stats(old, {})


def test_runner_passes_prior_copy_stats_to_evaluate(tmp_path):
    """The sweep's screen reads last sweep's persisted stats (P1-2 wiring)."""
    seen = {}

    def fake_eval(config, **kw):
        seen["prior"] = kw.get("prior_copy_stats")
        return {}

    r = DiscoveryRunner(config=CFG, watchlist_path=str(tmp_path / "wl.json"),
                        state_path=str(tmp_path / "state.json"),
                        evaluate=fake_eval, now=lambda: 100.0)
    # seed a prior state with copy stats
    r._save_state(DiscoveryState(initialized=True,
                                 copy_stats={"0xw": {"copy_n": 15, "ts": 1.0}}))
    r.run_once()
    assert seen["prior"] == {"0xw": {"copy_n": 15, "ts": 1.0}}


# --------------------------------------------------------------------------- #
# P1-3 — the dossier the gate sees
# --------------------------------------------------------------------------- #

def test_dossier_from_eval_has_no_null_fields():
    """The acceptance test (P1-3): a fully-evaluated wallet's dossier ships
    ZERO null fields — the gate used to referee six explicit nulls."""
    e = _ev("0xFULL", n_closed=455, capital=125.0, closed_hit_rate=1.0,
            concentration=0.31, mean_entry=0.62, up_ratio=0.44,
            tail_ratio=0.1, copyable_ratio=0.8, net_pnl=767.0,
            curve_drawdown=0.2, curve_sharpe=1.1)
    d = _dossier_from_eval(e)
    assert _nulls(d) == 0
    assert d["skill"]["n_closed"] == 455
    assert d["skill"]["capital"] == 125
    assert d["skill"]["hit_rate"] == 1.0
    assert d["skill"]["concentration"] == 0.31
    assert d["entry_profile"]["mean_entry_price"] == 0.62
    assert d["pnl_curve"]["up_ratio"] == 0.44


def test_build_dossier_drops_none_fields():
    d = build_dossier(
        "0xw",
        metrics=SimpleNamespace(roi=0.5, tstat=None, n_closed=None, capital=None,
                                hit_rate=None, concentration=None),
        curve=SimpleNamespace(net_pnl=100.0, max_drawdown_frac=None,
                              up_ratio=None, sharpe=None),
    )
    assert d["skill"] == {"roi": 0.5}
    assert d["pnl_curve"] == {"net_pnl": 100}


# --------------------------------------------------------------------------- #
# P1-4 — fail-open visibility
# --------------------------------------------------------------------------- #

def test_langfuse_error_rides_the_trace(monkeypatch):
    """§1.7a: ~13-15% of gate calls failed open with ZERO error traces — the
    level only rode the generation. The trace itself must carry it."""
    from src.copy_trading import langfuse_telemetry as lt

    batches = []

    class _Resp:
        status_code = 207

    monkeypatch.setattr(lt.requests, "post",
                        lambda url, json=None, headers=None, timeout=None: batches.append(json) or _Resp())
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("LANGFUSE_HOST", "https://langfuse.example")

    lt.record_generation(name="wallet-gate", input="p", output="", model="m",
                         start=1.0, end=2.0, error="unparseable verdict")
    (batch,) = batches
    trace = next(e for e in batch["batch"] if e["type"] == "trace-create")
    gen = next(e for e in batch["batch"] if e["type"] == "generation-create")
    assert trace["body"]["level"] == "ERROR"
    assert trace["body"]["statusMessage"] == "unparseable verdict"
    assert gen["body"]["level"] == "ERROR"

    batches.clear()
    lt.record_generation(name="wallet-gate", input="p", output="o", model="m",
                         start=1.0, end=2.0)
    (batch,) = batches
    trace = next(e for e in batch["batch"] if e["type"] == "trace-create")
    assert trace["body"]["level"] == "DEFAULT"
    assert "statusMessage" not in trace["body"]


def test_failopen_alert_fires_above_threshold(tmp_path):
    """3 of 3 gate calls fail open (100% > 20%, >= 3 calls) -> Telegram alert."""
    seq = [{"0xk": _ev("0xk", "1b")},
           {"0xk": _ev("0xk", "1b"), "0xf1": _ev("0xf1"), "0xf2": _ev("0xf2"),
            "0xf3": _ev("0xf3")}]
    sent = []
    r = _runner(tmp_path, seq=seq)      # llm_review_fn default -> None (fail-open)
    r._notify = lambda m: sent.append(m)
    r.run_once()
    r.run_once()
    alerts = [m for m in sent if "fail-open" in m]
    assert len(alerts) == 1 and "3/3" in alerts[0]


def test_failopen_alert_quiet_below_min_calls(tmp_path):
    """Two fail-opens in a 2-call sweep must NOT page (100% of 2 is noise)."""
    seq = [{"0xk": _ev("0xk", "1b")},
           {"0xk": _ev("0xk", "1b"), "0xf1": _ev("0xf1"), "0xf2": _ev("0xf2")}]
    sent = []
    r = _runner(tmp_path, seq=seq)
    r._notify = lambda m: sent.append(m)
    r.run_once()
    r.run_once()
    assert not [m for m in sent if "fail-open" in m]


# --------------------------------------------------------------------------- #
# P1-5 — the holdout roll is observable
# --------------------------------------------------------------------------- #

def test_holdout_roll_logged_debug_and_summary(tmp_path, caplog):
    seq = [{"0xk": _ev("0xk", "1b")},
           {"0xk": _ev("0xk", "1b"), "0xskip": _ev("0xskip")}]

    def skip_fn(dossier, model=None):
        return LLMVerdict("skip", "high", False, 0.9, "variance artifact")

    r = _runner(tmp_path, seq=seq, holdout_frac=0.1, rand=lambda: 0.5,
                llm_review_fn=skip_fn)
    with caplog.at_level(logging.DEBUG, logger="poly_poly_bot"):
        r.run_once()
        r.run_once()
    msgs = [rec.message for rec in caplog.records]
    assert any("holdout roll" in m and "0xskip" in m and "no-holdout" in m
               for m in msgs)                                   # per-roll DEBUG
    assert any("holdout rolls this sweep: 1 skip(s) rolled, 0 holdout(s)" in m
               for m in msgs)                                   # per-sweep INFO


# --------------------------------------------------------------------------- #
# P1-6 — book-evidence gates
# --------------------------------------------------------------------------- #

def _losing_rows(led, n, *, category="sports", their_price=0.5, pnl_sign=-1,
                 opened_ts=100.0):
    for i in range(n):
        p = PaperPosition(
            copy_id=f"loss{i}", target="0xOLD", condition_id=f"c{i}",
            token_id=f"t{i}", outcome_index=0, category=category,
            their_price=their_price, entry_price=their_price, shares=100.0,
            spent=50.0, drag_bps=0, opened_ts=opened_ts)
        p.realize(won=(pnl_sign > 0), now=opened_ts + 1)
        led.add(p)


def _evidence_engine(d, *, rows=15, row_category="sports", row_price=0.5,
                     row_pnl=-1, min_n=15, floor=0.0, allowed=None,
                     trade_category="sports", trade_price=0.5):
    led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
    _losing_rows(led, rows, category=row_category, their_price=row_price,
                 pnl_sign=row_pnl)
    eng = CopyPaperEngine(
        led,
        detector=lambda: [dict(copy_id="new1", target="0xT", condition_id="0xC",
                               token_id="TOK", outcome_index=0,
                               category=trade_category, their_price=trade_price,
                               their_usd=1000)],
        book_fetcher=lambda t: [(trade_price, 10000)],
        resolver=lambda c: None,
        max_copy_usd=50,
        category_evidence_min_n=min_n,
        category_evidence_floor_ts=floor,
        allowed_categories=allowed,
    )
    return eng


def test_evidence_gate_blocks_proven_losing_category():
    with tempfile.TemporaryDirectory() as d:
        s = _evidence_engine(d).run_cycle(now=1)
        assert s.opened == 0 and s.skipped_category_evidence == 1


def test_evidence_gate_exempts_stamped_wallet():
    with tempfile.TemporaryDirectory() as d:
        s = _evidence_engine(d, allowed={"0xt": {"sports"}}).run_cycle(now=1)
        assert s.opened == 1 and s.skipped_category_evidence == 0


def test_evidence_gate_open_below_min_n():
    with tempfile.TemporaryDirectory() as d:
        s = _evidence_engine(d, rows=5).run_cycle(now=1)
        assert s.opened == 1 and s.skipped_category_evidence == 0


def test_evidence_gate_open_when_slice_wins():
    with tempfile.TemporaryDirectory() as d:
        s = _evidence_engine(d, row_pnl=+1).run_cycle(now=1)
        assert s.opened == 1 and s.skipped_category_evidence == 0


def test_evidence_gate_blocks_proven_losing_price_bucket():
    with tempfile.TemporaryDirectory() as d:
        # losers live in "other" at 0.30 (bucket 0.2-0.4); the new trade is a
        # different category at the same price — the bucket gate still binds.
        s = _evidence_engine(d, row_category="other", row_price=0.30,
                             trade_category="crypto", trade_price=0.30).run_cycle(now=1)
        assert s.opened == 0 and s.skipped_price_bucket_evidence == 1
        assert s.skipped_category_evidence == 0


def test_evidence_gate_floor_excludes_old_rows():
    with tempfile.TemporaryDirectory() as d:
        # all the losing rows predate the floor -> the gate is inert
        s = _evidence_engine(d, floor=500.0).run_cycle(now=1)
        assert s.opened == 1 and s.skipped_category_evidence == 0


def test_price_bucket_labels():
    assert price_bucket(0.05) == "0.0-0.2"
    assert price_bucket(0.1999) == "0.0-0.2"
    assert price_bucket(0.2) == "0.2-0.4"
    assert price_bucket(0.55) == "0.4-0.6"
    assert price_bucket(0.94) == "0.8-1.0"


# --------------------------------------------------------------------------- #
# P1-7 — modeled fees and gas
# --------------------------------------------------------------------------- #

def test_costs_stamped_on_open():
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        eng = CopyPaperEngine(
            led,
            detector=lambda: [dict(copy_id="t1", target="0xT", condition_id="0xC",
                                   token_id="TOK", outcome_index=0,
                                   category="sports", their_price=0.50,
                                   their_usd=1000)],
            book_fetcher=lambda t: [(0.50, 10000)],
            resolver=lambda c: None,
            max_copy_usd=50,
            cost_model=CostModel(category_cost={"sports": 0.12}, fallback=0.10),
            gas_usd_per_trade=0.02,
        )
        s = eng.run_cycle(now=1)
        assert s.opened == 1
        (p,) = led.open_positions()
        assert p.cost_usd == pytest.approx(0.02)
        assert p.ideal_cost_usd == pytest.approx(0.02 + 50.0 * 0.12)


def test_costs_default_zero_without_model():
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        eng = CopyPaperEngine(
            led,
            detector=lambda: [dict(copy_id="t1", target="0xT", condition_id="0xC",
                                   token_id="TOK", outcome_index=0,
                                   category="sports", their_price=0.50,
                                   their_usd=1000)],
            book_fetcher=lambda t: [(0.50, 10000)],
            resolver=lambda c: None,
            max_copy_usd=50,
        )
        eng.run_cycle(now=1)
        (p,) = led.open_positions()
        assert p.cost_usd == 0.0 and p.ideal_cost_usd == 0.0


def test_pnl_unified_net_roi_aggregates_costs():
    from src.copy_trading.pnl_unified import aggregate_paper_b

    p = PaperPosition(copy_id="t1", target="0xW", condition_id="0xC",
                      token_id="TOK", outcome_index=0, category="sports",
                      their_price=0.50, entry_price=0.50, shares=100.0,
                      spent=50.0, drag_bps=0, opened_ts=1.0,
                      cost_usd=0.02, ideal_cost_usd=6.02)
    p.realize(won=True, now=2.0)          # pnl = ideal = +50 on 50 spent
    (wp,) = aggregate_paper_b([p])
    assert wp.cost_sum == pytest.approx(0.02)
    assert wp.ideal_cost_sum == pytest.approx(6.02)
    assert wp.realized_roi_closed == pytest.approx(1.0)
    assert wp.realized_net_roi == pytest.approx((50 - 0.02) / 50)
    assert wp.at_price_roi == pytest.approx(1.0)
    assert wp.at_price_net_roi == pytest.approx((50 - 6.02) / 50)


def test_strategy_compare_book_stats_carries_net():
    from src.copy_trading.strategy_compare import _book_stats

    rows = [{"closed": True, "won": True, "pnl": 50.0, "spent": 50.0,
             "ideal_pnl": 50.0, "drag_bps": 0,
             "cost_usd": 0.02, "ideal_cost_usd": 6.02}]
    s = _book_stats(rows)
    assert s["roi_net"] == pytest.approx((50 - 0.02) / 50)
    assert s["ideal_roi_net"] == pytest.approx((50 - 6.02) / 50)
    assert s["cost_stamped"] == 1
    # pre-P1-7 rows (no cost keys) net == gross and aren't counted stamped
    s2 = _book_stats([{"closed": True, "won": True, "pnl": 50.0, "spent": 50.0,
                       "ideal_pnl": 50.0, "drag_bps": 0}])
    assert s2["roi_net"] == pytest.approx(1.0)
    assert s2["cost_stamped"] == 0


def test_rebaseline_book_stats_net_on_the_fly():
    from src.copy_trading import rebaseline

    p = PaperPosition(copy_id="t1", target="0xW", condition_id="0xC",
                      token_id="TOK", outcome_index=0, category="sports",
                      their_price=0.50, entry_price=0.50, shares=100.0,
                      spent=50.0, drag_bps=0, opened_ts=1.0)
    p.realize(won=True, now=2.0)
    s = rebaseline.book_stats(
        [p], cost_model=CostModel(category_cost={"sports": 0.12}, fallback=0.10),
        gas_usd=0.02)
    assert s["roi_net"] == pytest.approx((50 - 0.02) / 50)
    assert s["ideal_roi_net"] == pytest.approx((50 - 6.02) / 50)
    # without a cost model the legacy shape is untouched (no net keys)
    assert "roi_net" not in rebaseline.book_stats([p])


# --------------------------------------------------------------------------- #
# I2 + I4 — funnel telemetry + persistent cull histogram
# --------------------------------------------------------------------------- #

def test_cull_histogram_persisted_and_funnel_logged(tmp_path, caplog):
    bad = _ev("0xBad", copy_n=15, copy_roi=-0.05)   # proven-negative -> culled
    r = _runner(tmp_path, seq=[{"0xBad": bad}])
    with caplog.at_level(logging.INFO, logger="poly_poly_bot"):
        r.run_once()
    rows = gate_history.load(r.cull_histogram_path)
    assert len(rows) == 1
    assert rows[0]["histogram"] == {"replay-proven-negative": 1}
    assert rows[0]["swept"] == 1
    assert any("funnel:" in rec.message for rec in caplog.records)


def test_funnel_line_counts_every_gate_disposition(tmp_path, caplog):
    """Review finding: gate_in must count every wallet the gate disposed (LLM
    calls + over-cap ungated), new= pre-gate, admitted= post-gate — the first
    cut undercounted exactly the wallets the vetting-backlog watch reads."""
    seq = [{"0xk": _ev("0xk", "1b")},
           {"0xk": _ev("0xk", "1b"), "0xA": _ev("0xA"), "0xB": _ev("0xB"),
            "0xC": _ev("0xC")}]
    calls = {"i": 0}

    def fake_eval(config, **kw):
        d = seq[min(calls["i"], len(seq) - 1)]
        calls["i"] += 1
        return d

    def verdict_fn(dossier, model=None):
        if dossier.get("wallet") == "0xA":
            return LLMVerdict("skip", "high", False, 0.9, "artifact")
        return LLMVerdict("follow", "low", True, 0.8, "ok")

    r = DiscoveryRunner(config=CFG, watchlist_path=str(tmp_path / "wl.json"),
                        state_path=str(tmp_path / "state.json"),
                        evaluate=fake_eval, llm_review=verdict_fn,
                        llm_review_enabled=True, llm_review_top_n=2,
                        now=lambda: 100.0)
    with caplog.at_level(logging.INFO, logger="poly_poly_bot"):
        r.run_once()
        r.run_once()
    line = [rec.message for rec in caplog.records if "funnel:" in rec.message][-1]
    # 3 new: 2 called (0xA skip, 0xB follow) + 0xC over the top_n=2 cap.
    assert "new=3" in line
    assert "gate_in=3" in line          # 2 calls + 1 admit-cap, not just verdicts
    assert "admitted=2" in line         # 0xB (follow) + 0xC (cap); 0xA dropped


def test_price_cache_bounded_during_deep_eval(monkeypatch):
    """Review finding: the shared per-token price cache must not grow without
    bound across a 400-wallet deep-eval loop — clear past PRICE_CACHE_MAX."""
    universe = ["0xw1", "0xw2", "0xw3"]
    monkeypatch.setattr(dd, "build_universe", lambda target, **kw: list(universe))
    monkeypatch.setattr(dd, "fetch_all_activity",
                        lambda wallets, *a, **k: {w: [] for w in wallets})
    monkeypatch.setattr(dd, "compute_wallet_metrics",
                        lambda a, **kw: SimpleNamespace(tstat=5.0, roi=0.1))
    monkeypatch.setattr(dd, "select_targets",
                        lambda scored, **kw: [SimpleNamespace(address=w, metrics=m)
                                              for w, m in scored.items()])
    # every wallet has enough buys on the SAME 3 tokens (dedup-friendly)
    buys = [{"token": t, "ts": 1, "price": 0.5, "usd": 600.0} for t in "ABC" * 2]
    monkeypatch.setattr(dd, "fetch_recent_buys", lambda *a, **k: list(buys))
    monkeypatch.setattr(dd, "wallet_curve_metrics", lambda *a, **k: dd.CurveMetrics())
    monkeypatch.setattr(dd, "build_wallet_context",
                        lambda w, *a, **k: dd.WalletContext(wallet=w, now=0.0))
    monkeypatch.setattr(dd, "fetch_resolutions", lambda cids, cache_dir=None, **k: {})
    fetches = []
    monkeypatch.setattr(dd, "fetch_price_series",
                        lambda token, cache: fetches.append(token) or [(1, 0.5)])
    monkeypatch.setattr(dd, "PRICE_CACHE_MAX", 2)
    monkeypatch.setenv("WALLET_DISCOVERY_BATCH_PAUSE_S", "0")

    dd.evaluate_sweep(DiscoveryConfig(min_ll_trades=4))

    # unbounded: 3 fetches total (shared tokens served from cache for w2/w3).
    # bounded at 2: each wallet's loop-top clear forces a refetch -> 9.
    assert len(fetches) == 9
