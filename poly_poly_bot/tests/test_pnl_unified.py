"""Unified per-strategy + per-wallet P&L aggregation (pure)."""

from __future__ import annotations

import pytest

from src.copy_trading.copy_paper import PaperPosition
from src.copy_trading.pnl import OpenPositionPnl
from src.copy_trading.pnl_unified import (
    LEGACY_A,
    MATURITY_READY,
    STRATEGY4_LABEL,
    UNTAGGED_A,
    UNTAGGED_B,
    aggregate_strategy4,
    aggregate_system_a,
    aggregate_system_b,
    best_worst,
    build_unified,
    maturity_tag,
    promotion_verdict,
    strategy_highlights,
    top_wallets,
    wilson_lower_bound,
)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def _open(token, trader, tier, shares, avg, cur):
    cost = shares * avg
    upnl = None if cur is None else shares * cur - cost
    return OpenPositionPnl(
        token_id=token, market="M", shares=shares, avg_price=avg, cur_price=cur,
        cost=cost, value=(0.0 if cur is None else shares * cur),
        unrealized_pnl=upnl, unrealized_pct=(None if cur is None else upnl / cost),
        tier=tier, trader_address=trader,
    )


def _paper(target, *, flagged_by=(), closed=False, won=None, pnl=0.0, spent=50.0,
           their_price=0.50, entry_price=0.50, copy_id="c"):
    return PaperPosition(
        copy_id=copy_id, target=target, condition_id="0xC", token_id="TOK",
        outcome_index=0, category="research", their_price=their_price,
        entry_price=entry_price, shares=spent / entry_price, spent=spent,
        drag_bps=0, opened_ts=0.0, flagged_by=flagged_by,
        closed=closed, won=won, pnl=pnl,
    )


# --------------------------------------------------------------------------- #
# System A
# --------------------------------------------------------------------------- #

def test_system_a_groups_realized_by_stamped_tier_and_wallet():
    rows = [
        {"trader_address": "0xAaa", "tier": "1a", "pnl": 10.0, "cost_basis": 40.0, "won": True},
        {"trader_address": "0xAaa", "tier": "1a", "pnl": -4.0, "cost_basis": 20.0, "won": False},
        {"trader_address": "0xBbb", "tier": "1b", "pnl": 6.0, "cost_basis": 30.0, "won": True},
    ]
    wallets = aggregate_system_a(rows, [])
    by_addr = {w.wallet: w for w in wallets}
    assert by_addr["0xaaa"].realized_pnl == 6.0
    assert by_addr["0xaaa"].wins == 1 and by_addr["0xaaa"].losses == 1
    assert by_addr["0xaaa"].strategies == ("A:1a",)
    assert by_addr["0xaaa"].roi == pytest.approx(6.0 / 60.0)
    assert by_addr["0xbbb"].strategies == ("A:1b",)


def test_system_a_falls_back_to_tier_of_then_legacy():
    rows = [
        {"trader_address": "0xCcc", "pnl": 5.0, "cost_basis": 10.0, "won": True},   # no tier -> fallback
        # no attribution at all AND pre-cutoff timestamp -> legacy backlog
        {"trader_address": "", "pnl": 2.0, "cost_basis": 5.0, "won": True,
         "timestamp": "2026-07-11T23:05:39+00:00"},
    ]
    wallets = aggregate_system_a(rows, [], tier_of=lambda a: "1c" if a == "0xccc" else None)
    by_addr = {w.wallet: w for w in wallets}
    assert by_addr["0xccc"].strategies == ("A:1c",)
    # 2026-07-16: rows with NO tier and NO wallet are pre-schema debris and get
    # their own legacy track (and their own "(legacy)" accumulator, so a mixed
    # empty-wallet population can never leak tier rows into the legacy split).
    assert by_addr["(legacy)"].strategies == (LEGACY_A,)


def test_system_a_open_positions_add_unrealized_and_cost():
    opens = [_open("t1", "0xAaa", "1a", shares=100, avg=0.40, cur=0.60)]   # +20 unrealized
    wallets = aggregate_system_a([], opens)
    w = wallets[0]
    assert w.unrealized_pnl == pytest.approx(20.0)
    assert w.open_cost == pytest.approx(40.0)
    assert w.n_open == 1
    assert w.roi == pytest.approx(20.0 / 40.0)   # priced open cost feeds ROI


def test_system_a_unpriced_open_excluded_from_roi_denominator():
    opens = [_open("t1", "0xAaa", "1a", shares=10, avg=0.30, cur=None)]
    wallets = aggregate_system_a([], opens)
    w = wallets[0]
    assert w.unrealized_pnl == 0.0
    assert w.open_cost == pytest.approx(3.0)
    assert w.cost_basis == 0.0       # unpriced -> not measured
    assert w.roi is None


# --------------------------------------------------------------------------- #
# System B
# --------------------------------------------------------------------------- #

def test_system_b_multi_theory_wallet_listed_under_each_theory_once_in_total():
    positions = [
        _paper("0xT", flagged_by=("1b", "1f"), closed=True, won=True, pnl=30.0, spent=50.0),
    ]
    wallets = aggregate_system_b(positions)
    assert len(wallets) == 1
    w = wallets[0]
    assert set(w.strategies) == {"B:1b", "B:1f"}
    assert w.realized_pnl == 30.0

    unified = build_unified([], wallets)
    labels = {s.label: s for s in unified.strategies}
    # the wallet's PnL shows in BOTH theory blocks ...
    assert labels["B:1b"].realized_pnl == 30.0
    assert labels["B:1f"].realized_pnl == 30.0
    # ... but the grand total counts it once.
    assert unified.total_realized == 30.0


def test_system_b_excludes_dust_fills():
    # entry far below their_price -> dust (pre-fix stale book sweep)
    dust = _paper("0xT", closed=True, won=False, pnl=-49.0, spent=50.0,
                  their_price=0.60, entry_price=0.001)
    wallets = aggregate_system_b([dust])
    assert wallets == []


def test_system_b_flagged_by_fallback_to_current_watchlist_then_untagged():
    p_fallback = _paper("0xT", flagged_by=(), closed=True, won=True, pnl=5.0, copy_id="a")
    p_orphan = _paper("0xZ", flagged_by=(), closed=True, won=True, pnl=7.0, copy_id="b")
    wallets = aggregate_system_b([p_fallback, p_orphan], flagged_by_now={"0xt": ["1d"]})
    by = {w.wallet: w for w in wallets}
    assert by["0xt"].strategies == ("B:1d",)
    assert by["0xz"].strategies == (UNTAGGED_B,)


def test_system_b_unmarked_open_positions_count_but_dont_feed_roi():
    # no live mark (mark_price defaults to 0) -> open capital shows but doesn't
    # drag ROI or add unrealized. This is the unhappy path (mark fetch failed).
    positions = [_paper("0xT", flagged_by=("1b",), closed=False, spent=50.0)]
    wallets = aggregate_system_b(positions)
    w = wallets[0]
    assert w.n_open == 1 and w.n_closed == 0
    assert w.open_cost == 50.0
    assert w.cost_basis == 0.0
    assert w.n_open_marked == 0
    assert w.roi is None
    assert w.net_pnl == 0.0


def test_system_b_marked_open_positions_contribute_unrealized_and_roi():
    # a near-term copy marked to market: shares * mid - spent feeds unrealized,
    # and its capital feeds the ROI denominator (mirrors System A / S4).
    p = _paper("0xT", flagged_by=("1b",), closed=False, spent=50.0)
    p.entry_price = 0.50
    p.shares = 100.0            # 100 * 0.50 = $50 spent
    p.mark(0.60)               # mid 0.60 -> value 60, unrealized +10
    wallets = aggregate_system_b([p])
    w = wallets[0]
    assert w.n_open == 1 and w.n_open_marked == 1
    assert w.unrealized_pnl == pytest.approx(10.0)
    assert w.cost_basis == pytest.approx(50.0)
    assert w.net_pnl == pytest.approx(10.0)
    assert w.roi == pytest.approx(0.20)


# --------------------------------------------------------------------------- #
# Unification + ranking
# --------------------------------------------------------------------------- #

def test_build_unified_orders_strategies_and_sums_total_over_unique_wallets():
    a = aggregate_system_a(
        [{"trader_address": "0xA", "tier": "1a", "pnl": 10.0, "cost_basis": 20.0, "won": True}], []
    )
    b = aggregate_system_b([_paper("0xB", flagged_by=("1c",), closed=True, won=True, pnl=5.0)])
    unified = build_unified(a, b)
    labels = [s.label for s in unified.strategies]
    assert labels == ["A:1a", "B:1c"]          # A before B
    assert unified.total_realized == 15.0
    assert unified.total_net == 15.0


def test_best_worst_ranks_by_pnl_and_roi():
    a = aggregate_system_a(
        [
            {"trader_address": "0x1", "tier": "1a", "pnl": 100.0, "cost_basis": 1000.0, "won": True},  # +10% ROI
            {"trader_address": "0x2", "tier": "1a", "pnl": -50.0, "cost_basis": 100.0, "won": False},  # -50% ROI
            {"trader_address": "0x3", "tier": "1a", "pnl": 5.0, "cost_basis": 10.0, "won": True},   # +50% ROI
            {"trader_address": "0x4", "tier": "1a", "pnl": 1.0, "cost_basis": 100.0, "won": True},  # +1% ROI
        ],
        [],
    )
    bw = best_worst(a, k=2)
    assert [w.wallet for w in bw.by_pnl_best] == ["0x1", "0x3"]
    assert [w.wallet for w in bw.by_pnl_worst] == ["0x2", "0x4"]
    assert bw.by_roi_best[0].wallet == "0x3"     # highest ROI
    assert bw.by_roi_worst[0].wallet == "0x2"    # most negative ROI


def test_best_worst_roi_excludes_undefined_roi():
    b = aggregate_system_b([_paper("0xOpen", flagged_by=("1b",), closed=False, spent=50.0)])
    bw = best_worst(b, k=3)
    assert bw.by_roi_best == [] and bw.by_roi_worst == []
    assert [w.wallet for w in bw.by_pnl_best] == ["0xopen"]


# --------------------------------------------------------------------------- #
# Deduped per-strategy highlights + cross-strategy top list (/wallets layout)
# --------------------------------------------------------------------------- #

def test_strategy_highlights_lists_each_wallet_once_with_combined_tags():
    # A strategy where the same wallets top BOTH PnL and ROI — the old four-list
    # layout repeated each wallet up to 4×; highlights lists each once, tagged.
    a = aggregate_system_a(
        [
            {"trader_address": "0x1", "tier": "1a", "pnl": 100.0, "cost_basis": 100.0, "won": True},  # +100% ROI
            {"trader_address": "0x2", "tier": "1a", "pnl": -50.0, "cost_basis": 100.0, "won": False},  # -50% ROI
            {"trader_address": "0x3", "tier": "1a", "pnl": 5.0, "cost_basis": 10.0, "won": True},   # +50% ROI
            {"trader_address": "0x4", "tier": "1a", "pnl": 1.0, "cost_basis": 100.0, "won": True},  # +1% ROI
        ],
        [],
    )
    hl = strategy_highlights(a, k=2)
    wallets = [h.wallet.wallet for h in hl]
    assert wallets == ["0x1", "0x3", "0x4", "0x2"]   # each once, by net PnL desc
    assert len(wallets) == len(set(wallets))         # no duplicates
    top = hl[0]
    assert top.pnl_best and top.roi_best             # leader carries both tags
    assert top.tags == ["▲PnL", "▲ROI"]
    bottom = hl[-1]
    assert bottom.pnl_worst and bottom.roi_worst
    assert bottom.tags == ["▼PnL", "▼ROI"]


def test_strategy_highlights_drops_contradictory_tags_in_tiny_strategy():
    # A lone wallet is both top and bottom of its strategy — a meaningless
    # ranking, so it carries no tags.
    a = aggregate_system_a(
        [{"trader_address": "0xSolo", "tier": "1a", "pnl": 5.0, "cost_basis": 10.0, "won": True}], []
    )
    hl = strategy_highlights(a, k=3)
    assert len(hl) == 1
    assert hl[0].tags == []


def test_strategy_highlights_dedup_across_system_a_and_b_same_address():
    # Same address tracked under both systems are distinct entries, kept separate.
    a = aggregate_system_a(
        [{"trader_address": "0xDup", "tier": "1a", "pnl": 5.0, "cost_basis": 10.0, "won": True}], []
    )
    b = aggregate_system_b([_paper("0xdup", flagged_by=("1b",), closed=True, won=True, pnl=3.0)])
    hl = strategy_highlights(a + b, k=3)
    keys = {(h.wallet.system, h.wallet.wallet) for h in hl}
    assert keys == {("A", "0xdup"), ("B", "0xdup")}
    assert len(hl) == 2


def test_top_wallets_ranks_unique_profitable_wallets_across_strategies():
    a = aggregate_system_a(
        [{"trader_address": "0xWin", "tier": "1a", "pnl": 80.0, "cost_basis": 100.0, "won": True}], []
    )
    # multi-theory winner appears once despite spanning two strategies
    b = aggregate_system_b(
        [
            _paper("0xMulti", flagged_by=("1b", "1f"), closed=True, won=True, pnl=40.0),
            _paper("0xLoss", flagged_by=("1c",), closed=True, won=False, pnl=-30.0),
        ]
    )
    top = top_wallets(a, b, k=3)
    wallets = [w.wallet for w in top]
    assert wallets == ["0xwin", "0xmulti"]      # by net PnL desc, losers dropped
    assert wallets.count("0xmulti") == 1        # listed once though it spans 1b & 1f


def test_top_wallets_positive_only_can_be_disabled():
    b = aggregate_system_b([_paper("0xLoss", flagged_by=("1c",), closed=True, won=False, pnl=-30.0)])
    assert top_wallets([], b, k=3) == []                       # nothing profitable
    assert [w.wallet for w in top_wallets([], b, k=3, positive_only=False)] == ["0xloss"]


# --------------------------------------------------------------------------- #
# Maturity / confidence annotations (promote-vs-noise observability)
# --------------------------------------------------------------------------- #

def test_wilson_lower_bound_is_honest_about_small_n():
    assert wilson_lower_bound(0, 0) is None              # no data
    # a 3/3 lucky streak must NOT look like a proven edge
    assert wilson_lower_bound(3, 3) < 0.5
    # the band tightens toward the point estimate as n grows
    lo_small = wilson_lower_bound(8, 10)
    lo_big = wilson_lower_bound(80, 100)
    assert lo_small < lo_big < 0.80
    # bounded to [0, point estimate]
    assert 0.0 <= wilson_lower_bound(1, 20) <= 0.05


def test_maturity_tag_bands_on_settled_count():
    assert maturity_tag(0) == maturity_tag(4) == "\U0001f9ca"     # 🧊 thin
    assert maturity_tag(5) == maturity_tag(14) == "\U0001f331"    # 🌱 building
    assert maturity_tag(MATURITY_READY) == maturity_tag(40) == "✅"


def test_promotion_verdict_gates_on_sample_size_then_pnl_not_hitrate():
    # too few resolved -> HOLD regardless of PnL
    v, reason = promotion_verdict(net_pnl=120.0, n_closed=6)
    assert v == "HOLD" and "resolved" in reason
    # enough resolved but not profitable -> HOLD
    v, reason = promotion_verdict(net_pnl=-3.0, n_closed=20)
    assert v == "HOLD" and "positive" in reason
    # enough resolved + positive paper PnL -> ready (even a sub-50% longshot
    # theory qualifies: the gate is PnL, not hit-rate)
    v, reason = promotion_verdict(net_pnl=40.0, n_closed=MATURITY_READY)
    assert v == "PROMOTE-READY"


# --------------------------------------------------------------------------- #
# Strategy 4 — long-horizon paper book (marked to market, single "S4" track)
# --------------------------------------------------------------------------- #

def _s4_pos(target, *, closed=False, won=None, pnl=0.0, spent=25.0, shares=50.0,
            mark_price=0.0, copy_id="c"):
    unreal = shares * mark_price - spent if mark_price > 0 else 0.0
    return PaperPosition(
        copy_id=copy_id, target=target, condition_id="0xC", token_id="TOK",
        outcome_index=0, category="research", their_price=0.50,
        entry_price=spent / shares, shares=shares, spent=spent, drag_bps=0,
        opened_ts=0.0, strategy="4", closed=closed, won=won, pnl=pnl,
        mark_price=mark_price, unrealized_pnl=unreal,
    )


def test_strategy4_aggregates_under_s4_label_with_marked_unrealized():
    positions = [
        _s4_pos("0xL", closed=True, won=True, pnl=10.0, spent=25.0, copy_id="c1"),
        _s4_pos("0xL", closed=False, spent=25.0, shares=50.0, mark_price=0.70, copy_id="c2"),
    ]
    wallets = aggregate_strategy4(positions)
    assert len(wallets) == 1
    w = wallets[0]
    assert w.strategies == (STRATEGY4_LABEL,)
    assert w.realized_pnl == 10.0
    assert abs(w.unrealized_pnl - 10.0) < 1e-6        # 50 * 0.70 - 25
    assert w.n_closed == 1 and w.n_open == 1
    assert abs(w.cost_basis - 50.0) < 1e-6            # closed 25 + marked-open 25
    assert abs(w.net_pnl - 20.0) < 1e-6               # realized 10 + unrealized 10


def test_strategy4_unmarked_open_excluded_from_roi():
    w = aggregate_strategy4([_s4_pos("0xL", closed=False, spent=25.0, mark_price=0.0)])[0]
    assert w.n_open == 1 and w.cost_basis == 0.0 and w.unrealized_pnl == 0.0


def test_strategy4_track_shows_separately_from_near_term_in_unified():
    # the SAME wallet on both books shows two tracks: its near-term copier track
    # (B:1b) and its long-horizon track (S4) — the dual-membership P&L view.
    b = aggregate_system_b([_paper("0xboth", flagged_by=("1b",), closed=True, won=True, pnl=4.0)])
    s4 = aggregate_strategy4([_s4_pos("0xboth", closed=True, won=True, pnl=6.0)])
    unified = build_unified([], b + s4)
    labels = {sp.label for sp in unified.strategies}
    assert "B:1b" in labels and STRATEGY4_LABEL in labels
    assert unified.total_realized == 10.0             # 4 + 6, both tracks counted


def test_paper_b_costs_are_derived_not_stamped():
    """/pnl printed a book's @net twice in one message on two different bases:
    the per-strategy line summed the P1-7 cost stamps (absent on pre-P1-7 rows,
    so they read cost-free) while the cost block below it derived them. One
    basis now, matching strategy_compare and rebaseline (verifier r10)."""
    from src.copy_trading.copy_paper import PaperPosition
    from src.copy_trading.pnl_unified import _modeled_costs, aggregate_paper_b

    def _p(i, stamped):
        return PaperPosition(
            copy_id=f"c{i}", target="0xW", condition_id=f"0x{i}", token_id=f"T{i}",
            outcome_index=0, category="sports", their_price=0.5, entry_price=0.5,
            shares=100.0, spent=50.0, drag_bps=0, opened_ts=float(i),
            closed=True, won=True, pnl=5.0, ideal_pnl=5.0, closed_ts=float(i),
            cost_usd=(0.02 if stamped else 0.0),
            ideal_cost_usd=(6.0 if stamped else 0.0))

    # every row UNSTAMPED (pre-P1-7) — the case that used to read cost-free
    rows = [_p(i, stamped=False) for i in range(10)]
    out = aggregate_paper_b(rows)
    wp = out[0]
    exp = sum(_modeled_costs(p)[1] for p in rows)
    assert wp.ideal_cost_sum == pytest.approx(exp)
    assert wp.ideal_cost_sum > 0, "unstamped rows must still be charged"
    # and a stamped row is charged the SAME derived amount, not its stamp
    stamped = aggregate_paper_b([_p(i, stamped=True) for i in range(10)])[0]
    assert stamped.ideal_cost_sum == pytest.approx(exp)
