"""Tests for the 2026-07-25 instrumentation-for-trust build (ROADMAP P0-2/3/4).

Covers: the clean-era state marker (era_state), the fill-health witness
(copy_paper.drag_stats/fill_health), the at-their-price dual ROI and the
divergence tripwire (pnl_unified), split-half wallet persistence
(promotion_gate.split_half_corr), the re-baseline computation + CLI
(rebaseline / scripts/rebaseline_ledger.py), the era-floored race comparison
(strategy_compare), and the /pnl trust block (telegram_bot._trust_lines).
"""

from __future__ import annotations

import json
import os

import pytest

from src.copy_trading import era_state, pnl_unified as u, rebaseline
from src.copy_trading.copy_paper import (
    PaperCopyLedger, PaperPosition, drag_stats, fill_health, fill_health_suspect)
from src.copy_trading.promotion_gate import (
    FALSIFY_MIN_N, FALSIFY_MIN_WALLETS, split_half_corr)
from src.copy_trading.strategy_compare import compare, format_snapshot, format_verdict


def _pos(copy_id, target="0xw", *, their=0.50, entry=0.50, spent=50.0,
         opened=1000.0, won=None, closed_ts=0.0, drag=0):
    shares = spent / entry
    p = PaperPosition(
        copy_id=copy_id, target=target, condition_id="c-" + copy_id,
        token_id="t-" + copy_id, outcome_index=0, category="x",
        their_price=their, entry_price=entry, shares=shares, spent=spent,
        drag_bps=drag or int(round((entry - their) / their * 10000)),
        opened_ts=opened,
    )
    if won is not None:
        p.realize(won=won, now=closed_ts or opened + 100)
    return p


def _wallet_pattern(wallet, first_wins, second_wins, *, n=10, base_ts=1000.0,
                    their=0.50, entry=0.50):
    """n settled copies for one wallet: n//2 in each chronological half."""
    out = []
    half = n // 2
    for i in range(n):
        won = (i < first_wins) if i < half else (i - half < second_wins)
        out.append(_pos(f"{wallet}-{i}", target=wallet, their=their,
                        entry=entry, opened=base_ts + i,
                        closed_ts=base_ts + i + 1, won=won))
    return out


# --------------------------------------------------------------------------- #
# era_state
# --------------------------------------------------------------------------- #

def test_era_state_roundtrip_and_floor(tmp_path):
    path = str(tmp_path / "ab_race_state.json")
    assert era_state.load(path) == {}                    # missing file
    assert era_state.era_floor_ts(path) is None
    floor = era_state.seed_era_floor(path, now=1234.5)
    assert floor == 1234.5
    assert era_state.era_floor_ts(path) == 1234.5
    # seeding never moves an existing floor — the era's start is a fact
    assert era_state.seed_era_floor(path, now=9999.0) == 1234.5


def test_era_state_load_tolerates_corrupt(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json")
    assert era_state.load(str(path)) == {}
    path.write_text('["a list"]')
    assert era_state.load(str(path)) == {}
    path.write_text('{"era_floor_ts": "not-a-number"}')
    assert era_state.era_floor_ts(str(path)) is None


def test_era_state_save_preserves_other_keys(tmp_path):
    path = str(tmp_path / "s.json")
    era_state.save(path, {"verdict_sent": True, "verdict_ts": 7.0})
    era_state.seed_era_floor(path, now=100.0)
    st = era_state.load(path)
    assert st["verdict_sent"] is True and st["era_floor_ts"] == 100.0


# --------------------------------------------------------------------------- #
# fill-health witness
# --------------------------------------------------------------------------- #

def test_drag_stats_empty_and_basic():
    assert drag_stats([]) == {"n": 0, "avg_drag_bps": 0.0, "min_drag_bps": 0,
                              "pct_better": 0.0, "n_deep_gift": 0}
    s = drag_stats([100, -50, -4000])
    assert s["n"] == 3
    assert s["avg_drag_bps"] == pytest.approx((100 - 50 - 4000) / 3, abs=0.1)
    assert s["min_drag_bps"] == -4000
    assert s["pct_better"] == pytest.approx(2 / 3, abs=1e-3)
    assert s["n_deep_gift"] == 1                        # the -4000 fill


def test_fill_health_scopes_to_clean_era():
    old = [_pos("o1", won=True, opened=100.0, drag=-5000),
           _pos("o2", won=False, opened=200.0, drag=-4000)]
    new = [_pos("n1", won=True, opened=5000.0, drag=120),
           _pos("n2", won=False, opened=6000.0, drag=-100)]
    all_time = fill_health(old + new)
    assert all_time["n"] == 4 and all_time["n_deep_gift"] == 2
    era = fill_health(old + new, min_opened_ts=1000.0)
    assert era["n"] == 2 and era["n_deep_gift"] == 0
    assert era["avg_drag_bps"] == pytest.approx(10.0)   # (120 - 100) / 2
    assert era["min_drag_bps"] == -100


def test_fill_health_suspect_rules():
    assert fill_health_suspect({"n": 1, "avg_drag_bps": 0.0, "n_deep_gift": 1})
    # deep gift flags at ANY n — it is impossible by construction post-P0-1
    assert not fill_health_suspect({"n": 3, "avg_drag_bps": -10.0, "n_deep_gift": 0})
    # negative avg needs a non-trivial sample
    assert fill_health_suspect({"n": 10, "avg_drag_bps": -5.0, "n_deep_gift": 0})
    assert not fill_health_suspect({"n": 10, "avg_drag_bps": 30.0, "n_deep_gift": 0})


# --------------------------------------------------------------------------- #
# pnl_unified: at-their-price ROI + divergence tripwire
# --------------------------------------------------------------------------- #

def test_at_price_roi_accumulates_through_aggregation():
    # 2 settled copies: one clean (entry=their), one gifted (entry 0.25 vs 0.50)
    clean = _pos("c1", won=True, their=0.50, entry=0.50, spent=50.0)
    gifted = _pos("c2", won=True, their=0.50, entry=0.25, spent=50.0)
    wallets = u.aggregate_system_b([clean, gifted])
    w = wallets[0]
    # clean: shares=100, pnl = ideal = +50.
    # gifted: shares=200, pnl=+150, ideal = 200 - 200*0.50 = +100.
    assert w.realized_pnl == pytest.approx(200.0)
    assert w.ideal_pnl == pytest.approx(150.0)
    assert w.closed_cost == pytest.approx(100.0)
    assert w.realized_roi_closed == pytest.approx(2.0)
    assert w.at_price_roi == pytest.approx(1.5)         # realized flatters by 50%
    unified = u.build_unified([], wallets)
    sp = [s for s in unified.strategies][0]
    assert sp.at_price_roi == pytest.approx(1.5)
    assert sp.ideal_pnl == pytest.approx(150.0)


def test_at_price_roi_none_without_closed_capital():
    open_only = _pos("o1", won=None)                    # never resolved
    w = u.aggregate_system_b([open_only])[0]
    assert w.at_price_roi is None and w.realized_roi_closed is None


def test_divergence_suspect_tripwire():
    # 12 gifted winners: realized +300%, at-price +100% -> SUSPECT
    gifted = [_pos(f"g{i}", won=True, their=0.50, entry=0.25, spent=50.0)
              for i in range(12)]
    unified = u.build_unified([], u.aggregate_system_b(gifted))
    assert u.divergence_suspect(unified.strategies[0]) is True
    # clean fills: realized == at-price -> not suspect
    clean = [_pos(f"c{i}", won=True, their=0.50, entry=0.50, spent=50.0)
             for i in range(12)]
    unified = u.build_unified([], u.aggregate_system_b(clean))
    assert u.divergence_suspect(unified.strategies[0]) is False
    # small sample: same gap, only 4 settled -> noise, not suspect
    few = [_pos(f"f{i}", won=True, their=0.50, entry=0.25, spent=50.0)
           for i in range(4)]
    unified = u.build_unified([], u.aggregate_system_b(few))
    assert u.divergence_suspect(unified.strategies[0]) is False


# --------------------------------------------------------------------------- #
# split-half persistence
# --------------------------------------------------------------------------- #

def _three_wallet_flip():
    """The 2026-07 signature: every wallet's second half is its first half
    inverted — first [+1.0, +0.6, +0.2], second [-1.0, -0.6, -0.2] -> corr -1."""
    positions = []
    for j, (fw, sw) in enumerate(((5, 0), (4, 1), (3, 2))):
        positions += _wallet_pattern(f"0xw{j}", fw, sw, base_ts=1000.0 + j * 100)
    return positions


def test_split_half_corr_negative_persistence():
    corr, n = split_half_corr(_three_wallet_flip())
    assert n == 3
    assert corr == pytest.approx(-1.0, abs=1e-9)


def test_split_half_corr_positive_persistence():
    positions = []
    for j, (fw, sw) in enumerate(((3, 5), (2, 4), (1, 3))):
        positions += _wallet_pattern(f"0xw{j}", fw, sw, base_ts=1000.0 + j * 100)
    corr, n = split_half_corr(positions)
    assert n == 3 and corr == pytest.approx(1.0, abs=1e-9)


def test_split_half_corr_reports_measurability():
    # fewer than min_wallets qualify -> corr None, but the count is honest
    corr, n = split_half_corr(_wallet_pattern("0xsolo", 5, 0))
    assert corr is None and n == 1
    # wallets below min_n don't qualify at all
    thin = _wallet_pattern("0xthin", 3, 1, n=6)
    corr, n = split_half_corr(thin)
    assert corr is None and n == 0


def test_split_half_corr_ideal_variant_and_era_scope():
    # gifted entries: realized halves differ from at-their-price halves
    positions = []
    for j, (fw, sw) in enumerate(((5, 0), (4, 1), (3, 2))):
        positions += _wallet_pattern(f"0xw{j}", fw, sw, base_ts=1000.0 + j * 100,
                                     their=0.50, entry=0.45)
    realized, _ = split_half_corr(positions, pnl_attr="pnl")
    ideal, _ = split_half_corr(positions, pnl_attr="ideal_pnl")
    assert realized == pytest.approx(-1.0, abs=1e-9)
    assert ideal == pytest.approx(-1.0, abs=1e-9)   # sign preserved on both
    # era floor: exclude every wallet's rows -> unmeasurable
    corr, n = split_half_corr(positions, min_opened_ts=10_000.0)
    assert corr is None and n == 0


def test_split_half_corr_zero_variance_is_undefined_not_zero():
    # all wallets identical -> zero variance -> None (never a fake 0.0)
    positions = []
    for j in range(3):
        positions += _wallet_pattern(f"0xsame{j}", 5, 0, base_ts=1000.0 + j * 100)
    corr, n = split_half_corr(positions)
    assert corr is None and n == 3


def test_split_half_corr_never_fabricates_from_float_noise():
    # Verifier catch (2026-07-25): three flat-stake wallets whose four half
    # ROIs are bit-identical at 0.1 — 0.1+0.1+0.1 = 0.30000000000000004, so a
    # naive vx > 0 check slips and noise/noise returned a maximal +1.0. This
    # is the §7 kill-criterion number: it must report unmeasurable, never a
    # manufactured correlation. pnl=+5.0 on spent=50.0 for every row.
    positions = []
    for j in range(3):
        for i in range(10):
            p = _pos(f"flat{j}-{i}", target=f"0xflat{j}", their=0.50,
                     entry=0.50, spent=50.0, opened=100.0 + i,
                     closed_ts=200.0 + i, won=None)
            p.closed, p.won, p.pnl, p.ideal_pnl = True, True, 5.0, 5.0
            positions.append(p)
    corr, n = split_half_corr(positions)
    assert corr is None and n == 3
    # the second variant from the repro: bit-identical FIRST halves (variance
    # is float-summation noise only) with genuinely VARIED second halves —
    # without the guard, cov/sqrt(vx*vy) = noise/real returned a garbage 0.0
    # that would have counted TOWARD the kill bar. Must be unmeasurable.
    mixed = []
    for j, sw in enumerate((5, 3, 1)):          # second-half wins vary
        mixed += _wallet_pattern(f"0xmix{j}", 3, sw, base_ts=500.0 + j * 100)
    corr, n = split_half_corr(mixed)
    assert corr is None and n == 3


def test_falsification_bar_constants_are_public():
    # The ROADMAP §7 bar, pinned as constants so surfaces and tests agree.
    assert FALSIFY_MIN_WALLETS == 15
    assert FALSIFY_MIN_N == 10


# --------------------------------------------------------------------------- #
# rebaseline
# --------------------------------------------------------------------------- #

def test_rebaseline_book_stats_and_per_wallet():
    positions = (
        [_pos(f"a{i}", target="0xgifted", won=True, their=0.50, entry=0.25,
              spent=50.0) for i in range(3)]
        + [_pos(f"b{i}", target="0xhonest", won=False, their=0.50, entry=0.50,
                spent=50.0) for i in range(2)]
    )
    s = rebaseline.book_stats(positions)
    assert s["n"] == 5 and s["spent"] == pytest.approx(250.0)
    # gifted: shares=200 each, pnl +150, ideal 200-100=+100; honest losses: -50 both
    assert s["pnl"] == pytest.approx(3 * 150 - 100)
    assert s["ideal_pnl"] == pytest.approx(3 * 100 - 100)
    assert s["gifted"] == pytest.approx(s["pnl"] - s["ideal_pnl"])
    assert s["roi"] == pytest.approx(s["pnl"] / 250.0)
    assert s["ideal_roi"] == pytest.approx(s["ideal_pnl"] / 250.0)
    pw = rebaseline.per_wallet(positions)
    assert pw[0]["wallet"] == "0xgifted"             # biggest fill gift first
    assert pw[0]["gifted"] == pytest.approx(150.0)   # 3 * (150 - 100)
    assert pw[1]["gifted"] == pytest.approx(0.0)


def test_rebaseline_era_scope():
    positions = [_pos("old", won=True, opened=100.0),
                 _pos("new", won=False, opened=5000.0)]
    s = rebaseline.book_stats(positions, min_opened_ts=1000.0)
    assert s["n"] == 1


# --------------------------------------------------------------------------- #
# strategy_compare: era floor + honest witnesses
# --------------------------------------------------------------------------- #

def _write_ledger(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _row(copy_id, target, *, opened, closed=True, won=True, spent=50.0,
         pnl=25.0, ideal=25.0, drag=100):
    return {"copy_id": copy_id, "target": target, "condition_id": "c" + copy_id,
            "token_id": "t" + copy_id, "outcome_index": 0, "category": "x",
            "their_price": 0.5, "entry_price": 0.5, "shares": 100.0,
            "spent": spent, "drag_bps": drag, "opened_ts": opened,
            "closed": closed, "won": won, "pnl": pnl, "ideal_pnl": ideal,
            "closed_ts": opened + 50}


def test_compare_era_floor_restarts_the_race(tmp_path):
    a_path, b_path = str(tmp_path / "a.jsonl"), str(tmp_path / "b.jsonl")
    _write_ledger(a_path, [_row("a-old", "0xw1", opened=100.0),
                           _row("a-new", "0xw1", opened=7000.0)])
    _write_ledger(b_path, [_row("b-old", "0xw1", opened=200.0),
                           _row("b-new", "0xw1", opened=6000.0)])
    unfloored = compare(a_path, b_path, now=10_000.0)
    assert unfloored["era_start"] == 200.0               # B's first-ever open
    assert unfloored["b"]["n_settled"] == 2
    floored = compare(a_path, b_path, now=10_000.0, era_floor=1000.0)
    assert floored["era_floor"] == 1000.0
    assert floored["era_start"] == 6000.0                # first POST-FIX B open
    assert floored["b"]["n_settled"] == 1                # b-old excluded
    assert floored["a"]["n_settled"] == 1                # a-new only (in-era)
    assert floored["a_all_time"]["n_settled"] == 2       # witness keeps history


def test_compare_era_floor_with_no_post_floor_opens_has_no_era(tmp_path):
    a_path, b_path = str(tmp_path / "a.jsonl"), str(tmp_path / "b.jsonl")
    _write_ledger(a_path, [_row("a1", "0xw1", opened=100.0)])
    _write_ledger(b_path, [_row("b1", "0xw1", opened=200.0)])
    cmp_ = compare(a_path, b_path, now=10_000.0, era_floor=9000.0)
    assert cmp_["era_start"] is None
    assert cmp_["validity"]["valid"] is False            # "no era yet" — verdict waits
    assert cmp_["b"]["n_settled"] == 0
    assert cmp_["a_all_time"]["n_settled"] == 1          # witness intact


def test_book_stats_ideal_and_suspect(tmp_path):
    a_path, b_path = str(tmp_path / "a.jsonl"), str(tmp_path / "b.jsonl")
    # 12 gifted winners: realized +$37.5/copy vs ideal +$12.5/copy -> SUSPECT
    _write_ledger(a_path, [_row(f"a{i}", "0xw1", opened=100.0 + i, pnl=37.5,
                                ideal=12.5, drag=-2500) for i in range(12)])
    _write_ledger(b_path, [_row("b1", "0xw1", opened=100.0)])
    cmp_ = compare(a_path, b_path, now=10_000.0)
    a = cmp_["a"]
    assert a["ideal_pnl"] == pytest.approx(150.0)
    assert a["ideal_roi"] == pytest.approx(150.0 / 600.0)
    assert a["gifted"] == pytest.approx(300.0)
    assert a["fill_suspect"] is True
    assert cmp_["b"]["fill_suspect"] is False


def test_snapshot_carries_trust_witnesses(tmp_path):
    a_path, b_path = str(tmp_path / "a.jsonl"), str(tmp_path / "b.jsonl")
    _write_ledger(a_path, [_row("a1", "0xw1", opened=300.0, drag=120)])
    _write_ledger(b_path, [_row("b1", "0xw1", opened=200.0)])
    # era_floor set: the persistence block renders even while the era is too
    # young to measure (that honesty is the point of P0-4's surface).
    snap = format_snapshot(compare(a_path, b_path, now=10_000.0, era_floor=50.0))
    assert "@price" in snap                              # P0-3: verdict can't be won on fills
    assert "fills A: avg drag +120bps" in snap           # P0-1 witness, standing
    assert "persistence" in snap and "kill bar" in snap  # P0-4 + ROADMAP §7 bar
    assert "SUSPECT" not in snap                         # clean books stay quiet


def test_verdict_names_fill_suspect(tmp_path):
    a_path, b_path = str(tmp_path / "a.jsonl"), str(tmp_path / "b.jsonl")
    _write_ledger(a_path, [_row(f"a{i}", "0xw1", opened=100.0 + i, pnl=37.5,
                                ideal=12.5, drag=-2500) for i in range(12)])
    _write_ledger(b_path, [_row("b1", "0xw1", opened=100.0)])
    verdict = format_verdict(compare(a_path, b_path, now=10_000.0))
    assert "FILL-SUSPECT A" in verdict
    assert "do not call a winner on realized" in verdict


# --------------------------------------------------------------------------- #
# rebaseline CLI
# --------------------------------------------------------------------------- #

def test_rebaseline_cli_matches_pnl_computation(tmp_path, capsys):
    a_path, b_path = str(tmp_path / "a.jsonl"), str(tmp_path / "b.jsonl")
    a_positions = [_pos(f"a{i}", target="0xw1", won=(i % 2 == 0), their=0.50,
                        entry=0.45, spent=50.0, opened=100.0 + i)
                   for i in range(6)]
    b_positions = [_pos(f"b{i}", target="0xw2", won=False, their=0.50,
                        entry=0.50, spent=50.0, opened=200.0 + i)
                   for i in range(4)]
    for path, positions in ((a_path, a_positions), (b_path, b_positions)):
        led = PaperCopyLedger(path)
        for p in positions:
            led.add(p)
        led.save()

    import scripts.rebaseline_ledger as cli
    assert cli.main([a_path, b_path]) == 0
    out = capsys.readouterr().out
    sa = rebaseline.book_stats(a_positions)
    assert "REBASELINE" in out and "COMBINED" in out
    assert f"ROI {sa['roi'] * 100:+.2f}%" in out                 # realized
    assert f"ROI {sa['ideal_roi'] * 100:+.2f}%" in out           # at-their-price
    assert "PER-WALLET" in out and "split-half corr" in out
    assert "kill bar" in out

    # --since scopes the read (the P0-1 48h acceptance is a one-command check)
    assert cli.main([a_path, b_path, "--since", "5000.0"]) == 0
    out = capsys.readouterr().out
    assert "0 settled" in out                                    # nothing post-floor


# --------------------------------------------------------------------------- #
# /pnl trust block
# --------------------------------------------------------------------------- #

def test_trust_lines_with_and_without_era_floor(monkeypatch, tmp_path):
    from src import telegram_bot

    positions = _three_wallet_flip()
    state = tmp_path / "ab_race_state.json"

    monkeypatch.setattr(telegram_bot, "_ab_race_state_path", lambda: str(state))
    lines = telegram_bot._trust_lines({"near": positions, "b": []})
    text = "\n".join(lines)
    assert "Fill health A (all-time)" in text              # no floor yet
    assert "persistence" in text
    assert "-1.00 (3w)" in text                            # the flip signature
    assert "kill bar" in text and "15" in text

    state.write_text(json.dumps({"era_floor_ts": 100_000.0}))
    lines = telegram_bot._trust_lines({"near": positions, "b": []})
    text = "\n".join(lines)
    assert "Fill health A (post-fix)" in text
    assert "post-fix n/a (0w)" in text                     # era too young to measure


def test_trust_lines_empty_when_no_paper_books():
    from src import telegram_bot
    assert telegram_bot._trust_lines({"near": [], "b": []}) == []


def test_pnl_shows_dual_roi_and_suspect(monkeypatch, capsys):
    from src import telegram_bot
    from src.copy_trading import pnl_unified as u

    gifted = [_pos(f"g{i}", target="0xgift", won=True, their=0.50, entry=0.25,
                   spent=50.0) for i in range(12)]
    b = u.aggregate_system_b(gifted)

    def fixture():
        return u.build_unified([], b), [], b, 0, {"near": gifted, "b": []}

    sent = []
    monkeypatch.setattr(telegram_bot, "_compute_unified", fixture)
    monkeypatch.setattr(telegram_bot, "send_message", lambda text, **_kw: sent.append(text))
    monkeypatch.setattr(telegram_bot, "_ab_race_state_path",
                        lambda: "/nonexistent/ab_race_state.json")
    monkeypatch.setattr(telegram_bot.CONFIG, "preview_mode", True)
    telegram_bot._handle_command("/pnl")
    out = sent[-1]
    assert "Paper at-target-price:" in out                 # TOTAL twin (P0-2)
    assert "@price" in out                                 # per-strategy twin
    assert "SUSPECT-fills" in out                          # divergence tripwire
