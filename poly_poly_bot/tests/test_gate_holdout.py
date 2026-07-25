"""Tests for the gate self-calibration holdout (ROADMAP P1-5 / P1-5b, Phase 1).

When the shortlist gate says "skip", a small fraction of those wallets are
admitted ANYWAY (holdout) and flagged, so a later job can compare their paper
outcomes against the admitted wallets — the counterfactual the gate's +EV can't
be measured without. The holdout is deterministic here via an injected ``rand``.
"""

from __future__ import annotations

from src.copy_trading import gate_history
from src.copy_trading.discovery import DiscoveryConfig, Eval
from src.copy_trading.discovery_runner import DiscoveryRunner, _confidence_band
from src.copy_trading.llm_review import LLMVerdict

CFG = DiscoveryConfig(min_capture_cents=1.5, min_tstat=10.0,
                      drop_capture_cents=1.0, watchlist_cap=10, auto_remove=True)


def _ev(w, theory="1e"):
    return Eval(wallet=w, capture_cents=0.0, lead_cents=0.0, hit_rate=0.0, n=0,
                roi=0.5, tstat=12.0, flagged_by=(theory,))


def _runner(tmp_path, *, seq, holdout_frac=0.0, rand=None, verdict_fn=None):
    calls = {"i": 0}

    def fake_eval(config, **kw):
        d = seq[min(calls["i"], len(seq) - 1)]
        calls["i"] += 1
        return d

    if verdict_fn is None:
        def verdict_fn(dossier, model=None):
            return LLMVerdict("skip", "high", False, 0.9, "variance artifact")

    return DiscoveryRunner(
        config=CFG,
        watchlist_path=str(tmp_path / "wl.json"),
        state_path=str(tmp_path / "state.json"),
        evaluate=fake_eval, llm_review=verdict_fn, llm_review_enabled=True,
        holdout_frac=holdout_frac, holdout_max_per_sweep=2,
        now=lambda: 100.0, rand=rand,
    )


def test_band_helper():
    assert _confidence_band(0.95) == "high"
    assert _confidence_band(0.7) == "medium"
    assert _confidence_band(0.4) == "low"


def test_skip_is_dropped_without_holdout(tmp_path):
    seq = [{"0xk": _ev("0xk", "1b")},
           {"0xk": _ev("0xk", "1b"), "0xskip": _ev("0xskip")}]
    r = _runner(tmp_path, seq=seq, holdout_frac=0.0)
    r.run_once(); r.run_once()
    rows = {row["wallet"]: row for row in gate_history.load(r.gate_history_path)}
    assert rows["0xskip"]["admitted"] is False
    assert rows["0xskip"]["holdout"] is False
    assert rows["0xskip"]["confidence_band"] == "high"


def test_holdout_admits_would_be_skip(tmp_path):
    seq = [{"0xk": _ev("0xk", "1b")},
           {"0xk": _ev("0xk", "1b"), "0xskip": _ev("0xskip")}]
    # rand() well below frac => holdout fires
    r = _runner(tmp_path, seq=seq, holdout_frac=1.0, rand=lambda: 0.0)
    r.run_once(); result = r.run_once()
    rows = {row["wallet"]: row for row in gate_history.load(r.gate_history_path)}
    assert rows["0xskip"]["admitted"] is True         # admitted despite skip
    assert rows["0xskip"]["holdout"] is True
    assert rows["0xskip"]["verdict"] == "skip"        # original verdict preserved
    # and it actually reached the watchlist (the counterfactual is live)
    assert any(e.wallet == "0xskip" for e in result.watchlist)


def test_holdout_ping_is_labelled(tmp_path):
    # a holdout-admitted wallet's Telegram ping must say it's a holdout, so the
    # "Claude: skip" line doesn't read as the gate contradicting itself.
    seq = [{"0xk": _ev("0xk", "1b")},
           {"0xk": _ev("0xk", "1b"), "0xskip": _ev("0xskip")}]
    sent = []
    r = _runner(tmp_path, seq=seq, holdout_frac=1.0, rand=lambda: 0.0)
    r._notify = lambda m: sent.append(m)
    r.run_once(); r.run_once()
    holdout_msgs = [m for m in sent if "Gate holdout" in m]
    assert len(holdout_msgs) == 1 and "0xskip" in holdout_msgs[0]


def test_holdout_respects_frac(tmp_path):
    seq = [{"0xk": _ev("0xk", "1b")},
           {"0xk": _ev("0xk", "1b"), "0xskip": _ev("0xskip")}]
    # rand() above frac => no holdout, wallet is dropped
    r = _runner(tmp_path, seq=seq, holdout_frac=0.1, rand=lambda: 0.5)
    r.run_once(); result = r.run_once()
    rows = {row["wallet"]: row for row in gate_history.load(r.gate_history_path)}
    assert rows["0xskip"]["admitted"] is False
    assert not any(e.wallet == "0xskip" for e in result.watchlist)


def test_holdout_capped_per_sweep(tmp_path):
    # three new skip candidates, cap = 2 -> at most two holdouts admitted.
    seq = [
        {"0xk": _ev("0xk", "1b")},
        {"0xk": _ev("0xk", "1b"), "0xs1": _ev("0xs1"),
         "0xs2": _ev("0xs2"), "0xs3": _ev("0xs3")},
    ]
    r = _runner(tmp_path, seq=seq, holdout_frac=1.0, rand=lambda: 0.0)
    r.run_once(); r.run_once()
    rows = gate_history.load(r.gate_history_path)
    held = [x for x in rows if x.get("holdout")]
    assert len(held) == 2                             # capped, not 3
