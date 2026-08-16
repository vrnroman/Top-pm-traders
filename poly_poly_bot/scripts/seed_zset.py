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
from src.copy_trading import era_state, promotion_gate, zset  # noqa: E402
from src.copy_trading.copy_paper import PaperCopyLedger, is_dust_fill  # noqa: E402

# A wallet needs at least this many clean-era rows in the OTHER book before its
# disagreement counts as evidence rather than noise.
CONTRADICTION_MIN_N = 10


def _num(x) -> float:
    try:
        return float(x or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _wallet_rows(positions, wallet: str):
    key = wallet.lower()
    settled, last_ts = [], 0.0
    for p in positions:
        if (getattr(p, "target", "") or "").lower() != key:
            continue
        last_ts = max(last_ts, _num(getattr(p, "opened_ts", 0.0)),
                      _num(getattr(p, "closed_ts", 0.0)))
        if getattr(p, "closed", False) and not is_dust_fill(p):
            settled.append(p)
    return settled, (last_ts or None)


def _clean_roi(settled, era):
    rows = [p for p in settled
            if _num(getattr(p, "opened_ts", 0.0)) >= (era or 0)
            and _num(getattr(p, "spent", 0.0)) > 0]
    if not rows:
        return (None, 0)
    spent = sum(_num(p.spent) for p in rows)
    ideal = sum(_num(getattr(p, "ideal_pnl", 0.0)) for p in rows)
    return ((ideal / spent) if spent else None, len(rows))


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

    honest = promotion_gate.honest_kwargs_from(CONFIG)
    floor_kwargs = promotion_gate.floor_kwargs_from(CONFIG)
    book_corr = promotion_gate.split_half_corr(b_positions, min_opened_ts=era)

    print(f"era floor: {era}   candidates: {len(candidates)}   "
          f"book-B split-half corr: {book_corr}")
    print(f"mode: {'APPLY (will write set Z)' if args.apply else 'DRY RUN'}\n")

    admitted, near = [], []
    for w in candidates:
        settled, last_ts = _wallet_rows(b_positions, w)
        if not settled:
            continue
        stats = promotion_gate.compute_stats(w, settled)
        ideal_roi, n_ideal = promotion_gate.ideal_roi_for(settled, min_opened_ts=era)
        ready, checks = promotion_gate.golive_check(
            stats, last_trade_ts=last_ts, now=now,
            min_settled=CONFIG.copy_golive_min_settled,
            max_idle_days=CONFIG.copy_golive_max_idle_days,
            min_roi=CONFIG.copy_golive_min_roi, floor_kwargs=floor_kwargs,
            ideal_roi=ideal_roi, n_ideal_settled=n_ideal,
            book_corr=book_corr, **honest)

        # Both extra rails now live in zset.admit, so this script cannot pass
        # a wallet that admit() would refuse. It only supplies the evidence.
        a_settled, _ = _wallet_rows(a_positions, w)
        a_roi, a_n = _clean_roi(a_settled, era)
        conc_ok, conc_detail = zset.concentration_check(settled, min_opened_ts=era)
        contra_ok, contra_detail = zset.contradiction_check(a_roi, a_n,
                                                            CONTRADICTION_MIN_N)
        bl = zset._blacklist_block(w)
        all_checks = list(checks) + [
            ("still positive with its best 3 copies deleted", conc_ok, conc_detail),
            ("the other book does not contradict it", contra_ok, contra_detail),
            ("not under the bot's own auto-demote", bl is None,
             bl or "no active demotion"),
        ]
        ok = bool(ready and conc_ok and contra_ok and bl is None)
        trimmed, _k, _d = zset.trimmed_roi(settled, min_opened_ts=era)
        n_fail = sum(1 for c in all_checks if not c[1])
        row = (w, ok, n_fail, len(all_checks), all_checks, ideal_roi, n_ideal,
               trimmed, settled)
        if ok:
            admitted.append(row)
        elif n_fail <= 2:
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
