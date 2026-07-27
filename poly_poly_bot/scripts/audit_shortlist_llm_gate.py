#!/usr/bin/env python3
"""Retroactively run the LLM shortlist gate over the CURRENT paper watchlist.

Why: the Claude wallet gate (Strategy 1c) only runs on *newly* qualified wallets.
Every wallet already on ``data/copy_watchlist.json`` when the gate went live was
admitted before the gate existed — never vetted. This one-off audit asks the
counterfactual: *if the gate had been active when each shortlisted wallet
qualified, would it still have been added?* It rebuilds each wallet's dossier
from the watchlist row (the same shape ``discovery_runner._dossier_from_eval``
feeds the live gate), runs ``review_wallet``, and cross-references the verdict
against the wallet's realized paper PnL.

This is an AUDIT, not an auto-purge: a ``skip`` on a wallet that is currently
paper-profitable is a flag to investigate, not a removal. The output is a table
and a skip-list; nothing is mutated.

Usage:
  python -m scripts.audit_shortlist_llm_gate                 # audit the live watchlist
  python -m scripts.audit_shortlist_llm_gate --limit 5       # first 5 wallets only
  python -m scripts.audit_shortlist_llm_gate --dry-run       # build dossiers, no LLM calls
  python -m scripts.audit_shortlist_llm_gate --json out.json # also write a JSON report
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import CONFIG
from src.copy_trading.discovery_runner import _dossier_from_eval
from src.copy_trading.llm_review import RATE_LIMITED, review_wallet

# The Eval fields _dossier_from_eval reads; anything absent from a watchlist row
# defaults so build_dossier's defensive getattr simply omits it.
_EVAL_DEFAULTS = dict(
    roi=0.0, tstat=0.0, n=0, capture_cents=0.0, lead_cents=0.0, hit_rate=0.0,
    copy_roi=0.0, copy_n=0, copy_hit=0.0, exit_roi=0.0, tail_ratio=0.0,
    copyable_ratio=None, net_pnl=None, curve_drawdown=None, curve_sharpe=None,
    n_closed=None, closed_hit_rate=None, capital=None, concentration=None,
    mean_entry=None, up_ratio=None,
    flagged_by=(), reason="",
)


def eval_like_from_row(row: dict) -> SimpleNamespace:
    """A minimal Eval-shaped object from a watchlist ``targets[]`` row, so the
    live dossier builder can be reused verbatim."""
    d = dict(_EVAL_DEFAULTS)
    for k in _EVAL_DEFAULTS:
        if k in row and row[k] is not None:
            d[k] = row[k]
    d["wallet"] = row.get("wallet", "")
    d["flagged_by"] = tuple(row.get("flagged_by") or ())
    return SimpleNamespace(**d)


def load_watchlist(path: str) -> list[dict]:
    with open(path) as f:
        data = json.load(f)
    return list(data.get("targets") or [])


def paper_pnl_by_wallet(ledger_path: str) -> dict[str, dict]:
    """wallet(lower) -> {roi, n_closed, net_pnl} from the paper-copy ledger, for
    cross-referencing each gate verdict against realized outcomes. Empty when the
    ledger is missing (audit still runs, just without the paper column)."""
    try:
        from src.copy_trading.copy_paper import PaperCopyLedger
        from src.copy_trading.pnl_unified import aggregate_system_b
    except Exception:
        return {}
    if not os.path.exists(ledger_path):
        return {}
    ledger = PaperCopyLedger(ledger_path)
    out: dict[str, dict] = {}
    for w in aggregate_system_b(list(ledger.positions.values())):
        out[w.wallet.lower()] = {
            "roi": w.roi, "n_closed": w.n_closed, "net_pnl": w.net_pnl}
    return out


def run_audit(rows, paper, *, review_fn, model, dry_run=False, limit=None):
    """Build a dossier per watchlist row, gate it, join to paper PnL. Returns a
    list of report dicts (pure aside from the injected ``review_fn`` calls)."""
    report = []
    for i, row in enumerate(rows):
        if limit is not None and i >= limit:
            break
        wallet = row.get("wallet", "")
        dossier = _dossier_from_eval(eval_like_from_row(row))
        raw = None if dry_run else review_fn(dossier, model=model)
        rate_limited = raw is RATE_LIMITED
        verdict = None if rate_limited else raw     # sentinel isn't a verdict
        pp = paper.get((wallet or "").lower(), {})
        report.append({
            "wallet": wallet,
            "rank": row.get("rank"),
            "flagged_by": list(row.get("flagged_by") or []),
            "rate_limited": rate_limited,
            "verdict": getattr(verdict, "verdict", None),
            "confidence": getattr(verdict, "confidence", None),
            "copyable": getattr(verdict, "copyable", None),
            "reasoning": getattr(verdict, "reasoning", None),
            "paper_roi": pp.get("roi"),
            "paper_n": pp.get("n_closed"),
            "paper_net_pnl": pp.get("net_pnl"),
        })
    return report


def _fmt_pct(v):
    return f"{v * 100:+.0f}%" if isinstance(v, (int, float)) else "—"


def print_report(report, dry_run=False):
    print(f"\nShortlist LLM-gate backfill audit — {len(report)} wallet(s)\n")
    header = f"{'wallet':<14} {'rank':>4} {'verdict':>8} {'conf':>5} " \
             f"{'paperROI':>9} {'paperN':>6}  theories / reasoning"
    print(header)
    print("-" * len(header))
    skips = []
    rate_limited = [r for r in report if r.get("rate_limited")]
    for r in report:
        w = (r["wallet"] or "")[:12]
        verdict = r["verdict"] or ("(dry)" if dry_run else
                                   ("LIMIT" if r.get("rate_limited") else "?"))
        conf = f"{r['confidence']:.0%}" if isinstance(r["confidence"], (int, float)) else "—"
        theories = ",".join(r["flagged_by"])
        tail = theories if dry_run else f"{theories}  {(r['reasoning'] or '')[:70]}"
        print(f"{w:<14} {str(r['rank']):>4} {verdict:>8} {conf:>5} "
              f"{_fmt_pct(r['paper_roi']):>9} {str(r['paper_n'] or '—'):>6}  {tail}")
        if r["verdict"] == "skip":
            skips.append(r)

    if skips:
        print(f"\n⚠️  Would-SKIP list ({len(skips)}) — investigate before any demote:")
        for r in skips:
            flag = ""
            if isinstance(r["paper_roi"], (int, float)):
                flag = ("  ← but paper-POSITIVE (gate may be wrong / recovered)"
                        if r["paper_roi"] > 0 else "  ← paper-negative (confirms the skip)")
            print(f"  {r['wallet']}  ({','.join(r['flagged_by'])}){flag}")
    else:
        print("\nNo would-skip wallets." if not dry_run else "\n(dry run — no verdicts)")

    if rate_limited:
        print(f"\n⚠️  INCOMPLETE: {len(rate_limited)} wallet(s) could not be checked "
              "(claude -p spend/rate-limited). Re-run this audit once the "
              "subscription restores — these were NOT vetted:")
        for r in rate_limited:
            print(f"  {r['wallet']}  ({','.join(r['flagged_by'])})")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Backfill the LLM shortlist gate over the watchlist.")
    ap.add_argument("--watchlist", default=CONFIG.copy_paper_watchlist)
    ap.add_argument("--ledger", default=CONFIG.copy_paper_ledger)
    ap.add_argument("--model", default=CONFIG.wallet_discovery_llm_model)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="build + show dossiers without calling Claude")
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args(argv)

    try:
        rows = load_watchlist(args.watchlist)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Could not read watchlist {args.watchlist}: {e}", file=sys.stderr)
        return 1
    if not rows:
        print(f"No wallets on {args.watchlist}.")
        return 0

    paper = paper_pnl_by_wallet(args.ledger)
    report = run_audit(rows, paper, review_fn=review_wallet, model=args.model,
                       dry_run=args.dry_run, limit=args.limit)
    print_report(report, dry_run=args.dry_run)
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nWrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
