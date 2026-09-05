#!/usr/bin/env python3
"""Run the go-live gate over candidate wallets and admit the passers to set Z.

    python scripts/seed_zset.py            # dry run, decides nothing
    python scripts/seed_zset.py --apply    # writes promoted_wallets_z.json

The point of this script is that it does NOT let anyone pick. It runs
``promotion_gate.golive_check`` (the same function `/golive` renders), plus set
Z's own two rails, and admits exactly the wallets that pass. If that is one
wallet, or zero, that is the answer: hand-forcing a second wallet to fill a
quota recreates the failure Z exists to prevent.

Evidence base is book B's ledger. Book B is the instant-copy regime, and after
the 2026-08-16 latency finding (per-wallet polling detects in seconds, not the
~5 minutes the 500-wallet global feed takes) it is the regime a small set is
actually able to trade. Book A is used only as a CONTRADICTION check: a wallet
whose two books disagree in sign on the same window is not one to put money
behind, whatever its headline says.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.config import CONFIG  # noqa: E402
from src.copy_trading import era_state, promotion_gate, zset, zset_candidates  # noqa: E402
from src.copy_trading.copy_paper import PaperCopyLedger  # noqa: E402

# One gate: the per-wallet evaluation lives in zset_candidates and is the same
# one the Telegram cards and the owner's admit button run.
CONTRADICTION_MIN_N = zset_candidates.CONTRADICTION_MIN_N
_wallet_rows = zset_candidates.wallet_rows
_clean_roi = zset_candidates.clean_roi


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="actually write set Z (default is a dry run)")
    ap.add_argument("--wallet", action="append", default=None,
                    help="restrict to these candidates (repeatable)")
    ap.add_argument("--top", type=int, default=None,
                    help="admit only the best N of the wallets that PASS, "
                         "ranked by trimmed ROI (the concentration-robust "
                         "number). The gate still decides eligibility; this "
                         "only caps how much of it goes live at once.")
    args = ap.parse_args(argv)

    era = era_state.era_floor_ts(os.path.join(CONFIG.data_dir, "ab_race_state.json"))
    now = time.time()
    b_positions = list(PaperCopyLedger(CONFIG.copy_paper_b_ledger).positions.values())
    a_positions = list(PaperCopyLedger(CONFIG.copy_paper_ledger).positions.values())

    candidates = ([w.lower() for w in args.wallet] if args.wallet else
                  sorted({(getattr(p, "target", "") or "").lower()
                          for p in b_positions if getattr(p, "target", None)}))

    book_corr = promotion_gate.split_half_corr(b_positions, min_opened_ts=era)

    print(f"era floor: {era}   candidates: {len(candidates)}   "
          f"book-B split-half corr: {book_corr}")
    print(f"mode: {'APPLY (will write set Z)' if args.apply else 'DRY RUN'}\n")

    admitted, near = [], []
    for w in candidates:
        c = zset_candidates.evaluate(w, b_positions, a_positions, era=era,
                                     now=now, book_corr=book_corr)
        if c is None:
            continue
        row = (w, c.ok, c.n_fail, len(c.checks), c.checks, c.ideal_roi,
               c.n_ideal, c.trimmed_roi, c.settled)
        if c.ok:
            admitted.append(row)
        elif c.n_fail <= 2:
            near.append(row)

    # Rank the PASSERS by their concentration-trimmed ROI, not the headline:
    # the headline is what jackpots inflate, and the whole reason the rail
    # exists is that the two orderings differ.
    admitted.sort(key=lambda r: -(r[7] if r[7] is not None else -9))
    held = []
    if args.top is not None and len(admitted) > args.top:
        held = admitted[args.top:]
        admitted = admitted[:args.top]

    if args.apply:
        for w, _ok, _nf, _nt, chks, _roi, _n, _tr, settled in admitted:
            gate_checks = [c for c in chks
                           if not c[0].startswith("still positive with its best")]
            a_settled, _ = _wallet_rows(a_positions, w)
            a_roi, a_n = _clean_roi(a_settled, era)
            zset.admit(w, ready=True, checks=gate_checks, settled=settled,
                       era_floor=era, other_book_roi=a_roi,
                       other_book_n=a_n, rails_supplied=True)

    print(f"=== ADMITTED TO SET Z: {len(admitted)} ===")
    for w, ok, nf, nt, checks, roi, n, trimmed, _s in admitted:
        print(f"\n  {w}   clean @price {(roi or 0) * 100:+.1f}% over {n} copies"
              f"   (trimmed {(trimmed or 0) * 100:+.1f}%)")
        for lab, good, detail in checks:
            print(f"    {'PASS' if good else 'FAIL'}  {lab}  ({detail})")

    if held:
        print(f"\n=== GATE-ELIGIBLE BUT HELD by --top {args.top}: {len(held)} ===")
        print("    These passed every check. They are not in Z only because "
              "you capped the initial set.")
        for w, _ok, _nf, _nt, _c, roi, n, trimmed, _s in held:
            print(f"  {w}  @price {(roi or 0) * 100:+.1f}% n={n}  "
                  f"trimmed {(trimmed or 0) * 100:+.1f}%")

    print(f"\n=== NEAR MISSES (1-2 failing checks): {len(near)} ===")
    for w, ok, nf, nt, checks, roi, n, _tr, _s in sorted(near, key=lambda r: r[2]):
        fails = [f"{lab} ({detail})" for lab, good, detail in checks if not good]
        print(f"  {w}  @price {(roi or 0) * 100:+.1f}% n={n}  "
              f"{nf}/{nt} failing")
        for f in fails:
            print(f"      FAIL {f}")

    if not admitted:
        print("\nSet Z is empty. That is a valid result, not a shortfall: "
              "nothing cleared the bar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
