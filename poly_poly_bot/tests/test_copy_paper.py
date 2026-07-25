"""Tests for the forward paper-copy harness (Strategy 1b execution measurement)."""

from __future__ import annotations

import os
import tempfile

from src.copy_trading.copy_paper import (
    CopyPaperEngine,
    PaperCopyLedger,
    PaperPosition,
    format_resolution_telegram,
    report,
    simulate_copy_fill,
)


# --------------------------------------------------------------------------- #
# simulate_copy_fill
# --------------------------------------------------------------------------- #

def test_fill_at_book_with_drag():
    # target bought at 0.50; current best ask is 0.52 -> we pay up, drag +400bps
    # (needs a slippage cap >= 400bps to allow the chase)
    fill = simulate_copy_fill(0.50, [(0.52, 1000)], copy_usd=52, max_slippage_bps=500)
    assert fill.shares > 0
    assert abs(fill.avg_price - 0.52) < 1e-9
    assert fill.drag_bps == 400


def test_fill_respects_slippage_cap():
    # best ask 0.61 is beyond 0.50*(1+200bps)=0.51 -> unfilled
    fill = simulate_copy_fill(0.50, [(0.61, 1000)], copy_usd=50, max_slippage_bps=200)
    assert fill.shares == 0
    assert fill.spent == 0


def test_fill_walks_levels_within_budget():
    # 0.50 has 20 shares ($10), then 0.505 deeper; copy $20 -> spans two levels
    fill = simulate_copy_fill(0.50, [(0.50, 20), (0.505, 1000)], copy_usd=20,
                              max_slippage_bps=300)
    assert abs(fill.spent - 20) < 1e-6
    assert 0.50 <= fill.avg_price <= 0.505


def test_fill_empty_book_unfilled():
    fill = simulate_copy_fill(0.50, [], copy_usd=50)
    assert fill.shares == 0


def test_fill_depth_limited():
    # only $5 of asks available within slippage, want $50
    fill = simulate_copy_fill(0.50, [(0.50, 10)], copy_usd=50, max_slippage_bps=10)
    assert abs(fill.spent - 5.0) < 1e-6  # 10 shares * 0.50


def test_fill_skips_stale_dust_ask_below_floor():
    # Regression for the "drag $-30950" blow-up: a dust ask at 0.001 sitting
    # under a 0.62 market is stale data. It must be skipped, not swept — else a
    # $50 budget buys ~50k shares and the drag metric explodes.
    fill = simulate_copy_fill(
        0.62, [(0.001, 1_000_000), (0.63, 1000)], copy_usd=50, max_slippage_bps=200,
    )
    assert fill.avg_price >= 0.62 * 0.97        # filled on the credible level, not the dust
    assert fill.shares < 200                    # ~80 shares, not ~50k
    assert abs(fill.drag_bps) < 500             # bounded, not -9984bps


def test_fill_unfilled_when_only_sub_floor_liquidity():
    # If the *only* liquidity is non-credible deep-discount dust, treat as unfilled.
    fill = simulate_copy_fill(0.62, [(0.001, 1_000_000)], copy_usd=50)
    assert fill.shares == 0 and fill.spent == 0


def test_fill_refuses_deep_favourable_move_p0_1():
    # ROADMAP P0-1 (2026-07-25): a 0.40 ask under a 0.62 target price is NOT a
    # genuine pullback we get to keep — it is stale/erroneous book data, and
    # sweeping it is exactly how the old 0.5 floor manufactured Book A's
    # apparent edge (39% of copies filled >2% better than the target; that gift
    # was the book's entire profit). The 0.97 floor refuses it: unfilled.
    fill = simulate_copy_fill(0.62, [(0.40, 10000)], copy_usd=50, max_slippage_bps=200)
    assert fill.shares == 0 and fill.spent == 0


def test_fill_allows_small_favourable_move_within_floor():
    # A modest pullback INSIDE the 3% credibility band still fills: 0.605 vs a
    # 0.62 target (-2.4%, above the 0.6014 floor) is a plausible same-side ask.
    fill = simulate_copy_fill(0.62, [(0.605, 10000)], copy_usd=50, max_slippage_bps=200)
    assert fill.shares > 0
    assert abs(fill.avg_price - 0.605) < 1e-9
    assert fill.drag_bps < 0                    # genuinely (slightly) better price
    assert fill.drag_bps > -300                 # ...but never better than the floor allows


# --------------------------------------------------------------------------- #
# PaperPosition.realize
# --------------------------------------------------------------------------- #

def _pos(**kw):
    base = dict(
        copy_id="tx1-TOK", target="0xT", condition_id="0xC", token_id="TOK",
        outcome_index=0, category="sports", their_price=0.50, entry_price=0.52,
        shares=100.0, spent=52.0, drag_bps=400, opened_ts=1000.0,
    )
    base.update(kw)
    return PaperPosition(**base)


def test_realize_win_counts_drag():
    p = _pos()
    p.realize(won=True, now=2000.0)
    assert p.closed and p.won
    assert abs(p.pnl - (100 - 52)) < 1e-9          # our PnL with drag
    assert abs(p.ideal_pnl - (100 - 50)) < 1e-9    # drag-free PnL
    # execution drag cost = ideal - actual = 2.0
    assert abs(p.ideal_pnl - p.pnl - 2.0) < 1e-9


def test_realize_loss():
    p = _pos()
    p.realize(won=False, now=2000.0)
    assert p.pnl == -52.0
    assert p.ideal_pnl == -50.0


# --------------------------------------------------------------------------- #
# Ledger persistence & dedup
# --------------------------------------------------------------------------- #

def test_ledger_roundtrip_and_dedup():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "ledger.jsonl")
        led = PaperCopyLedger(path)
        led.add(_pos(copy_id="a"))
        assert led.has("a")
        # reload from disk
        led2 = PaperCopyLedger(path)
        assert led2.has("a")
        assert len(led2.open_positions()) == 1


def test_ledger_persists_closed_state():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "l.jsonl")
        led = PaperCopyLedger(path)
        p = _pos(copy_id="x")
        led.add(p)
        p.realize(won=True, now=1.0)
        led.save()
        led2 = PaperCopyLedger(path)
        assert led2.closed_positions()[0].won is True


# --------------------------------------------------------------------------- #
# Engine cycle with injected fakes
# --------------------------------------------------------------------------- #

def _trade(copy_id, token, oi=0, their_price=0.50, their_usd=1000):
    return dict(copy_id=copy_id, target="0xT", condition_id="0xC", token_id=token,
                outcome_index=oi, category="sports", their_price=their_price,
                their_usd=their_usd)


def test_engine_opens_dedups_and_resolves():
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        feed = [[_trade("t1", "TOK", oi=0)], [_trade("t1", "TOK", oi=0)]]  # same trade twice
        books = {"TOK": [(0.51, 10000)]}
        resolved = {}  # condition -> winner

        eng = CopyPaperEngine(
            led, detector=lambda: feed[cycle[0]],
            book_fetcher=lambda t: books.get(t, []),
            resolver=lambda c: resolved.get(c),
            max_copy_usd=50,
        )
        cycle = [0]
        s1 = eng.run_cycle(now=100)
        assert s1.opened == 1 and len(led.open_positions()) == 1

        cycle[0] = 1
        s2 = eng.run_cycle(now=200)   # same copy_id -> deduped, no new open
        assert s2.opened == 0

        # now resolve in favour of outcome 0
        resolved["0xC"] = 0
        s3 = eng.run_cycle(now=300)
        assert s3.resolved == 1
        assert len(led.closed_positions()) == 1
        assert led.closed_positions()[0].won is True


def test_engine_fill_gate_skips_chase():
    # fill at 0.52 vs their 0.50 = +400bps; gate at 100bps -> don't chase.
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        eng = CopyPaperEngine(
            led, detector=lambda: [_trade("t1", "TOK", their_price=0.50)],
            book_fetcher=lambda t: [(0.52, 10000)],
            resolver=lambda c: None, max_slippage_bps=500, fill_gate_bps=100,
        )
        s = eng.run_cycle(now=1)
        assert s.opened == 0 and s.skipped_fill_gate == 1


def test_engine_fill_gate_allows_no_drag_fill():
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        eng = CopyPaperEngine(
            led, detector=lambda: [_trade("t1", "TOK", their_price=0.50)],
            book_fetcher=lambda t: [(0.50, 10000)],   # zero drag
            resolver=lambda c: None, fill_gate_bps=100,
        )
        s = eng.run_cycle(now=1)
        assert s.opened == 1 and s.skipped_fill_gate == 0


def test_engine_fill_gate_two_sided_rejects_gifted_fill():
    # ROADMAP P0-1: the gate is TWO-SIDED. A fill 180bps BETTER than the target
    # (0.491 ask vs their 0.50 — inside the 0.97 floor, so the simulator takes
    # it) is the same evidence corruption as a chase: the old one-sided gate
    # kept exactly these, and they were the book's entire apparent edge.
    # (Deeper gifts, >300bps, never reach the gate — the 0.97 floor refuses
    # them as non-credible book data, covered above.)
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        eng = CopyPaperEngine(
            led, detector=lambda: [_trade("t1", "TOK", their_price=0.50)],
            book_fetcher=lambda t: [(0.491, 10000)],   # -180bps "gift"
            resolver=lambda c: None, max_slippage_bps=500, fill_gate_bps=100,
        )
        s = eng.run_cycle(now=1)
        assert s.opened == 0 and s.skipped_fill_gate == 1


def test_engine_fill_gate_two_sided_allows_small_favourable():
    # A small favourable fill inside the band (-40bps < gate 100) still opens.
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        eng = CopyPaperEngine(
            led, detector=lambda: [_trade("t1", "TOK", their_price=0.50)],
            book_fetcher=lambda t: [(0.498, 10000)],
            resolver=lambda c: None, max_slippage_bps=500, fill_gate_bps=100,
        )
        s = eng.run_cycle(now=1)
        assert s.opened == 1 and s.skipped_fill_gate == 0


def test_is_dust_fill_decoupled_from_sim_floor():
    # Manager-mandated boundary regression (P0-1): is_dust_fill's quarantine
    # threshold is DUST_FILL_FRAC (0.5), NOT the simulator's MIN_FILL_FRAC
    # (raised 0.5 -> 0.97). Bumping the sim floor must never reclassify the
    # legacy fills in [0.5x, 0.97x) whose honest economics the re-baseline
    # needs visible in the settled sample.
    from src.copy_trading import copy_paper as cp
    assert cp.MIN_FILL_FRAC == 0.97      # the P0-1 simulator floor
    assert cp.DUST_FILL_FRAC == 0.5      # the quarantine, pinned at its legacy value
    their = 0.60
    assert cp.is_dust_fill(_pos(their_price=their, entry_price=0.29)) is True   # 0.48x: absurd
    assert cp.is_dust_fill(_pos(their_price=their, entry_price=0.31)) is False  # 0.52x: legacy gift, KEPT
    assert cp.is_dust_fill(_pos(their_price=their, entry_price=0.57)) is False  # 0.95x: legacy gift, KEPT
    assert cp.is_dust_fill(_pos(their_price=their, entry_price=0.60)) is False  # at price: clean


def test_engine_first_entry_only_skips_readd():
    # two BUYs (different tx) into the SAME target+market -> only the first opens.
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        eng = CopyPaperEngine(
            led, detector=lambda: [_trade("t1", "TOK"), _trade("t2", "TOK")],
            book_fetcher=lambda t: [(0.51, 10000)],
            resolver=lambda c: None, first_entry_only=True,
        )
        s = eng.run_cycle(now=1)
        assert s.opened == 1 and s.skipped_not_first_entry == 1


def test_engine_slate_cap_per_wallet_day():
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        feed = [_trade("t1", "A"), _trade("t2", "B"), _trade("t3", "C")]  # same target
        eng = CopyPaperEngine(
            led, detector=lambda: feed,
            book_fetcher=lambda t: [(0.51, 10000)],
            resolver=lambda c: None, max_copies_per_wallet_day=2,
        )
        s = eng.run_cycle(now=1)
        assert s.opened == 2 and s.skipped_slate_cap == 1


def test_engine_slate_cap_per_category_day():
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))

        def tr(cid, tok, cat):
            return dict(copy_id=cid, target="0x" + tok, condition_id="0xC" + tok,
                        token_id=tok, outcome_index=0, category=cat,
                        their_price=0.50, their_usd=1000)
        feed = [tr("t1", "A", "sports"), tr("t2", "B", "sports"), tr("t3", "C", "crypto")]
        eng = CopyPaperEngine(
            led, detector=lambda: feed,
            book_fetcher=lambda t: [(0.51, 10000)],
            resolver=lambda c: None, max_copies_per_category_day=1,
        )
        s = eng.run_cycle(now=1)
        # one sports + one crypto open; the 2nd sports is capped
        assert s.opened == 2 and s.skipped_slate_cap == 1


def test_engine_slate_cap_persists_across_cycles_same_day():
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        feed = [[_trade("t1", "A")], [_trade("t2", "B")]]  # same target, two cycles
        cycle = [0]
        eng = CopyPaperEngine(
            led, detector=lambda: feed[cycle[0]],
            book_fetcher=lambda t: [(0.51, 10000)],
            resolver=lambda c: None, max_copies_per_wallet_day=1,
        )
        s1 = eng.run_cycle(now=100)            # day 0
        assert s1.opened == 1
        cycle[0] = 1
        s2 = eng.run_cycle(now=200)            # same UTC day -> cap already hit
        assert s2.opened == 0 and s2.skipped_slate_cap == 1


def test_engine_skips_unfilled():
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        eng = CopyPaperEngine(
            led, detector=lambda: [_trade("t1", "TOK", their_price=0.50)],
            book_fetcher=lambda t: [(0.99, 1000)],  # way beyond slippage
            resolver=lambda c: None, max_slippage_bps=200,
        )
        s = eng.run_cycle(now=1)
        assert s.opened == 0 and s.skipped_unfilled == 1


def test_report_aggregates_drag_and_roi():
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        p1 = _pos(copy_id="w", shares=100, spent=52, their_price=0.50)
        p1.realize(won=True, now=1.0)
        p2 = _pos(copy_id="l", shares=100, spent=52, their_price=0.50)
        p2.realize(won=False, now=1.0)
        led.add(p1)
        led.add(p2)
        r = report(led)
        assert r["closed"] == 2
        assert abs(r["realized_pnl"] - ((100 - 52) + (-52))) < 1e-6  # -4
        assert abs(r["execution_drag_cost"] - 4.0) < 1e-6  # 2 per trade * 2
        assert r["hit_rate"] == 0.5


# --------------------------------------------------------------------------- #
# Resolution context + Telegram formatting
# --------------------------------------------------------------------------- #

def test_resolved_positions_carry_market_context():
    # detector context (title/slug) must survive onto the closed position so the
    # notification can name what resolved instead of only counting it.
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        tr = _trade("t1", "TOK", oi=0)
        tr["title"] = "Will BTC hit $100k in 2025?"
        tr["slug"] = "btc-100k-2025"
        eng = CopyPaperEngine(
            led, detector=lambda: [tr],
            book_fetcher=lambda t: [(0.51, 10000)],
            resolver=lambda c: 0,          # resolves to outcome 0 immediately
            max_copy_usd=50,
        )
        s = eng.run_cycle(now=100)
        assert s.opened == 1 and s.resolved == 1
        assert len(s.resolved_positions) == 1
        p = s.resolved_positions[0]
        assert p.title == "Will BTC hit $100k in 2025?"
        assert p.slug == "btc-100k-2025"
        assert p.won is True


def test_format_resolution_telegram_win_names_market_and_links():
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        p = _pos(copy_id="w", title="Will BTC hit $100k in 2025?",
                 slug="btc-100k-2025", shares=100, spent=50,
                 their_price=0.50, entry_price=0.52, drag_bps=400)
        p.realize(won=True, now=1.0)
        led.add(p)
        msg = format_resolution_telegram([p], report(led))
        assert "Will BTC hit $100k in 2025?" in msg          # what resolved
        assert "polymarket.com/event/btc-100k-2025" in msg   # dig deeper
        assert "✅ <b>WON</b>" in msg
        assert "+400bps drag" in msg                         # per-position drag
        assert "<b>Ledger:</b>" in msg                       # cumulative footer


def test_format_resolution_telegram_names_outcome_when_resolver_given():
    from src.copy_trading.outcome_names import OutcomeNameResolver
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        p = _pos(copy_id="w", title="Will X win?", outcome_index=0,
                 shares=100, spent=50, their_price=0.50, entry_price=0.50)
        p.realize(won=True, now=1.0)
        led.add(p)
        resolver = OutcomeNameResolver(fetcher=lambda cid: ["Yes", "No"])
        msg = format_resolution_telegram([p], report(led), resolver=resolver)
        assert "“Yes”" in msg            # names which side settled (index 0)


def test_report_quarantines_pre_fix_dust_positions():
    # A pre-fix dust fill (entry far below their price) must not pollute the
    # cumulative stats; it is excluded and counted under "quarantined".
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        good = _pos(copy_id="g", shares=100, spent=50, their_price=0.50,
                    entry_price=0.52)
        good.realize(won=True, now=1.0)
        dust = _pos(copy_id="x", shares=50000, spent=50, their_price=0.62,
                    entry_price=0.001)           # the blown-up fill
        dust.realize(won=False, now=1.0)
        led.add(good)
        led.add(dust)
        r = report(led)
        assert r["closed"] == 1                   # dust excluded from closed
        assert r["quarantined"] == 1
        assert abs(r["realized_pnl"] - (100 - 50)) < 1e-6   # only the good one
        assert r["realized_roi"] > 0              # not dragged to -100%/-30k


def test_format_resolution_skips_dust_block_but_notes_it():
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        good = _pos(copy_id="g", title="Real market?", slug="real-mkt",
                    shares=100, spent=50, their_price=0.50, entry_price=0.52)
        good.realize(won=True, now=1.0)
        dust = _pos(copy_id="x", title="garbage", shares=50000, spent=50,
                    their_price=0.62, entry_price=0.001)
        dust.realize(won=False, now=1.0)
        led.add(good)
        led.add(dust)
        msg = format_resolution_telegram([good, dust], report(led))
        assert "Real market?" in msg
        assert "garbage" not in msg               # dust block suppressed
        assert "1 market resolved" in msg         # only the credible one counted
        assert "1 stale dust-fill position excluded" in msg


def test_format_resolution_telegram_loss_and_titleless_fallback():
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        p = _pos(copy_id="l", title="", category="politics",
                 shares=100, spent=50, their_price=0.50)
        p.realize(won=False, now=1.0)
        led.add(p)
        msg = format_resolution_telegram([p], report(led))
        assert "❌ <b>LOST</b>" in msg
        assert "(politics market)" in msg     # falls back to category when untitled
        assert "1 market resolved" in msg     # singular


# --------------------------------------------------------------------------- #
# Bet-horizon routing (Strategy 1 near-term vs 4 long-horizon) + mark-to-market
# --------------------------------------------------------------------------- #

def _trade_h(copy_id, token, horizon_days, their_price=0.50, their_usd=1000):
    t = _trade(copy_id, token, their_price=their_price, their_usd=their_usd)
    t["condition_id"] = copy_id  # distinct condition per bet for the resolver
    t["horizon_days"] = horizon_days
    return t


def test_engine_long_horizon_book_takes_only_long_bets():
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        feed = [_trade_h("short", "TOKS", horizon_days=10),
                _trade_h("long", "TOKL", horizon_days=300)]
        eng = CopyPaperEngine(
            led, detector=lambda: feed, book_fetcher=lambda t: [(0.50, 10000)],
            resolver=lambda c: None, max_copy_usd=25,
            min_horizon_days=180, strategy="4",
        )
        s = eng.run_cycle(now=1)
        assert s.opened == 1 and s.skipped_horizon == 1
        pos = led.open_positions()[0]
        assert pos.copy_id == "long" and pos.strategy == "4" and pos.horizon_days == 300


def test_engine_near_term_book_skips_long_bets():
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        feed = [_trade_h("short", "TOKS", horizon_days=10),
                _trade_h("long", "TOKL", horizon_days=300)]
        eng = CopyPaperEngine(
            led, detector=lambda: feed, book_fetcher=lambda t: [(0.50, 10000)],
            resolver=lambda c: None, max_horizon_days=180,
        )
        s = eng.run_cycle(now=1)
        assert s.opened == 1 and s.skipped_horizon == 1
        pos = led.open_positions()[0]
        assert pos.copy_id == "short" and pos.strategy == "1"


def test_engine_undated_bet_is_treated_near_term():
    # No horizon_days (endDate unknown): the near-term book still copies it, the
    # long-horizon book skips it — a missing date never opens a months-long bet.
    with tempfile.TemporaryDirectory() as d:
        s4 = CopyPaperEngine(
            PaperCopyLedger(os.path.join(d, "s4.jsonl")),
            detector=lambda: [_trade("u", "TOK")],   # no horizon_days key
            book_fetcher=lambda t: [(0.50, 10000)],
            resolver=lambda c: None, min_horizon_days=180)
        assert s4.run_cycle(now=1).opened == 0
        s1 = CopyPaperEngine(
            PaperCopyLedger(os.path.join(d, "s1.jsonl")),
            detector=lambda: [_trade("u", "TOK")],
            book_fetcher=lambda t: [(0.50, 10000)],
            resolver=lambda c: None, max_horizon_days=180)
        assert s1.run_cycle(now=1).opened == 1


def test_engine_marks_open_positions_to_market():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "l.jsonl")
        led = PaperCopyLedger(path)
        eng = CopyPaperEngine(
            led,
            detector=lambda: [_trade_h("long", "TOK", horizon_days=300, their_price=0.40)],
            book_fetcher=lambda t: [(0.40, 10000)],   # fill at 0.40 -> ~100 shares
            resolver=lambda c: None, max_copy_usd=40,
            min_horizon_days=180, strategy="4",
            mark_fetcher=lambda t: 0.60,              # mid moved up to 0.60
        )
        s = eng.run_cycle(now=1)
        assert s.opened == 1 and s.marked == 1
        pos = led.open_positions()[0]
        assert pos.mark_price == 0.60
        assert abs(pos.unrealized_pnl - 20.0) < 1e-6   # 100 * (0.60 - 0.40)
        # marking persists the ledger so a restart keeps the live mark
        assert PaperCopyLedger(path).open_positions()[0].mark_price == 0.60


def test_engine_mark_skipped_on_empty_book():
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        eng = CopyPaperEngine(
            led, detector=lambda: [_trade_h("long", "TOK", horizon_days=300)],
            book_fetcher=lambda t: [(0.50, 10000)], resolver=lambda c: None,
            min_horizon_days=180, strategy="4",
            mark_fetcher=lambda t: None,              # no live quote
        )
        s = eng.run_cycle(now=1)
        assert s.opened == 1 and s.marked == 0
        assert led.open_positions()[0].mark_price == 0.0


# --------------------------------------------------------------------------- #
# Winning-markets-only category gate (item A)
# --------------------------------------------------------------------------- #

def test_category_gate_skips_unapproved_category():
    # wallet 0xT approved only in crypto; its sports BUY must be skipped.
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        eng = CopyPaperEngine(
            led, detector=lambda: [_trade("t1", "TOK")],  # category="sports"
            book_fetcher=lambda t: [(0.51, 10000)],
            resolver=lambda c: None, max_copy_usd=50,
            allowed_categories={"0xt": {"crypto"}},
        )
        s = eng.run_cycle(now=1)
        assert s.opened == 0 and s.skipped_category_gate == 1


def test_category_gate_allows_approved_category():
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        eng = CopyPaperEngine(
            led, detector=lambda: [_trade("t1", "TOK")],  # category="sports"
            book_fetcher=lambda t: [(0.51, 10000)],
            resolver=lambda c: None, max_copy_usd=50,
            allowed_categories={"0xt": {"sports", "crypto"}},
        )
        s = eng.run_cycle(now=1)
        assert s.opened == 1 and s.skipped_category_gate == 0


def test_category_gate_absent_wallet_is_unrestricted():
    # a wallet with no entry in the map is NOT blocked (no category data yet).
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        eng = CopyPaperEngine(
            led, detector=lambda: [_trade("t1", "TOK")],
            book_fetcher=lambda t: [(0.51, 10000)],
            resolver=lambda c: None, max_copy_usd=50,
            allowed_categories={"0xother": {"crypto"}},
        )
        s = eng.run_cycle(now=1)
        assert s.opened == 1


def test_category_gate_off_by_default():
    with tempfile.TemporaryDirectory() as d:
        led = PaperCopyLedger(os.path.join(d, "l.jsonl"))
        eng = CopyPaperEngine(
            led, detector=lambda: [_trade("t1", "TOK")],
            book_fetcher=lambda t: [(0.51, 10000)],
            resolver=lambda c: None, max_copy_usd=50,
        )  # allowed_categories None -> gate off
        s = eng.run_cycle(now=1)
        assert s.opened == 1


# --------------------------------------------------------------------------- #
# Conviction sizing (item C)
# --------------------------------------------------------------------------- #

def test_conviction_size_scales_with_bet_vs_median():
    # base 20, median 1000; a 2000 bet = 2x median -> winsorized to 2x -> $40.
    eng = CopyPaperEngine(
        PaperCopyLedger.__new__(PaperCopyLedger), detector=lambda: [],
        book_fetcher=lambda t: [], resolver=lambda c: None, max_copy_usd=50,
        wallet_median_usd={"0xt": 1000.0}, conviction_base_usd=20.0,
    )
    assert eng._copy_size("0xT", 2000.0) == 40.0     # 2x median, capped at 2x base
    assert eng._copy_size("0xT", 1000.0) == 20.0     # at median -> base
    assert eng._copy_size("0xT", 100.0) == 5.0       # 0.1x -> winsorized to 0.25x*20


def test_conviction_size_capped_at_max():
    eng = CopyPaperEngine(
        PaperCopyLedger.__new__(PaperCopyLedger), detector=lambda: [],
        book_fetcher=lambda t: [], resolver=lambda c: None, max_copy_usd=30,
        wallet_median_usd={"0xt": 1000.0}, conviction_base_usd=20.0,
    )
    assert eng._copy_size("0xT", 5000.0) == 30.0     # 2x*20=40 capped to max 30


def test_conviction_size_unknown_wallet_uses_base():
    eng = CopyPaperEngine(
        PaperCopyLedger.__new__(PaperCopyLedger), detector=lambda: [],
        book_fetcher=lambda t: [], resolver=lambda c: None, max_copy_usd=50,
        wallet_median_usd={"0xt": 1000.0}, conviction_base_usd=20.0,
    )
    # no median for this wallet -> multiplier 1.0 -> base
    assert eng._copy_size("0xUNKNOWN", 5000.0) == 20.0


def test_legacy_sizing_when_conviction_off():
    eng = CopyPaperEngine(
        PaperCopyLedger.__new__(PaperCopyLedger), detector=lambda: [],
        book_fetcher=lambda t: [], resolver=lambda c: None, max_copy_usd=50,
        copy_pct=1.0,
    )  # no conviction config -> legacy min(max_copy_usd, their_usd*pct)
    assert eng._copy_size("0xT", 1000.0) == 50.0     # capped
    assert eng._copy_size("0xT", 30.0) == 30.0       # below cap
