#!/usr/bin/env python3
"""Re-baseline both paper ledgers on at-their-price economics (ROADMAP P0-2).

    python scripts/rebaseline_ledger.py [A_LEDGER] [B_LEDGER] [--since EPOCH | --era]

Defaults to the configured ledgers, all-time scope. ``--era`` scopes to the
clean post-P0-1 era (the floor recorded in ab_race_state.json); ``--since``
scopes to an explicit epoch. Prints, per book and per wallet: realized ROI
next to the at-their-price ROI the fill model cannot inflate, the fill-gift
delta between them, the drag witness (P0-1's acceptance numbers), and the
split-half persistence correlation (P0-4). The at-their-price figures are the
same computation /pnl renders, so the two always reconcile.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.config import CONFIG  # noqa: E402
from src.copy_trading import era_state, rebaseline  # noqa: E402
from src.copy_trading.copy_paper import PaperCopyLedger  # noqa: E402
from src.copy_trading.promotion_gate import (  # noqa: E402
    FALSIFY_MIN_N, FALSIFY_MIN_WALLETS, split_half_corr,
)


def _pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:+.2f}%"


def _usd(x) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.1f}"


def _corr_line(positions, since) -> str:
    """Split-half persistence, realized and at-their-price variants."""
    def _fmt(res):
        corr, n = res
        if corr is None:
            return f"n/a ({n}w)"
        return f"{corr:+.2f} ({n}w)"
    r = split_half_corr(positions, pnl_attr="pnl", min_opened_ts=since)
    i = split_half_corr(positions, pnl_attr="ideal_pnl", min_opened_ts=since)
    return f"split-half corr {_fmt(r)} · @price {_fmt(i)}"


def _book_block(name: str, positions: list, since) -> list[str]:
    s = rebaseline.book_stats(positions, min_opened_ts=since)
    d = s["drag"]
    lines = [f"BOOK {name}: {s['n']} settled · ${s['spent']:,.0f} deployed"]
    lines.append(f"  realized        {_usd(s['pnl']):>10}   ROI {_pct(s['roi'])}")
    lines.append(f"  at-their-price  {_usd(s['ideal_pnl']):>10}   ROI {_pct(s['ideal_roi'])}")
    lines.append(f"  fill gift       {_usd(s['gifted']):>10}   "
                 f"(what the fills added; + = flattered)")
    lines.append(
        f"  drag: avg {d['avg_drag_bps']:+.0f}bps · min {d['min_drag_bps']:+d} · "
        f"{d['pct_better'] * 100:.0f}% better-than-target · "
        f"deep-gift(<-300bps) {d['n_deep_gift']}")
    lines.append(f"  persistence: {_corr_line(positions, since)}")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("a_ledger", nargs="?", default=CONFIG.copy_paper_ledger)
    ap.add_argument("b_ledger", nargs="?", default=CONFIG.copy_paper_b_ledger)
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--since", type=float, default=None,
                       help="epoch floor on opened_ts (clean-era acceptance read)")
    scope.add_argument("--era", action="store_true",
                       help="floor at the era_floor_ts recorded in ab_race_state.json")
    ap.add_argument("--state", default=os.path.join(CONFIG.data_dir, "ab_race_state.json"),
                    help="race/era state file for --era")
    ap.add_argument("--top", type=int, default=12, help="per-wallet rows to show")
    args = ap.parse_args(argv)

    since = args.since
    if args.era:
        since = era_state.era_floor_ts(args.state)
        if since is None:
            print(f"no era_floor_ts in {args.state} — showing all-time")

    books = []
    for label, path in (("A (lagged)", args.a_ledger), ("B (instant)", args.b_ledger)):
        # A missing path loads as an EMPTY ledger by design (PaperCopyLedger);
        # say so loudly — a typo'd path must not read as a vacuously-passing
        # "0 settled" acceptance check (2026-07-25 review).
        if not os.path.exists(path):
            print(f"⚠ WARNING: {path} not found — {label} scored as an empty book")
        positions = list(PaperCopyLedger(path).positions.values())
        books.append((label, positions))

    print("=" * 72)
    print("REBASELINE — realized vs at-their-price (settled rows, dust-quarantine applied)")
    print("scope: " + ("all-time" if since is None else f"opened >= {since:.0f}"))
    print("=" * 72)
    tot = {"spent": 0.0, "pnl": 0.0, "ideal": 0.0}
    for label, positions in books:
        print()
        for line in _book_block(label, positions, since):
            print(line)
        s = rebaseline.book_stats(positions, min_opened_ts=since)
        tot["spent"] += s["spent"]
        tot["pnl"] += s["pnl"]
        tot["ideal"] += s["ideal_pnl"]

    print()
    if tot["spent"] > 0:
        # A+B race books only — /pnl's "Paper at-target-price" line also folds
        # in S4 when that book has settled rows, so with S4 live the two totals
        # differ by exactly S4's contribution (2026-07-25 review).
        print(f"COMBINED (A+B): realized {_usd(tot['pnl'])} ({_pct(tot['pnl'] / tot['spent'])})"
              f" · at-their-price {_usd(tot['ideal'])} ({_pct(tot['ideal'] / tot['spent'])})"
              f" on ${tot['spent']:,.0f} deployed")
    print(f"kill bar (ROADMAP §7): clean-era at-price ROI < 0 AND split-half corr <= 0 "
          f"across >= {FALSIFY_MIN_WALLETS} wallets (n>={FALSIFY_MIN_N}) "
          f"=> wallet-copying falsified")

    for label, positions in books:
        pw = rebaseline.per_wallet(positions, min_opened_ts=since)
        if not pw:
            continue
        print()
        print(f"PER-WALLET {label} (top {args.top} by fill gift):")
        print(f"  {'wallet':<12} {'n':>4} {'spent':>8} {'realized':>9} {'@price':>9} {'gift':>8}")
        for s in pw[: args.top]:
            w = s["wallet"]
            w = f"{w[:6]}…{w[-4:]}" if len(w) > 12 else w
            print(f"  {w:<12} {s['n']:>4} {s['spent']:>8,.0f} "
                  f"{_pct(s['roi']):>9} {_pct(s['ideal_roi']):>9} {_usd(s['gifted']):>8}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
