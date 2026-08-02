#!/usr/bin/env python3
"""Offline discovery-funnel + gate digest — the RCA evidence harness, maintained.

Why this exists: Langfuse and ``/gate`` only see wallets that reached the LLM
shortlist gate, i.e. they are blind to the *statistical* funnel upstream that does
the real culling. The 2026-07 RCA showed the discovery pre-filter was selecting
near-$1 settlement-lag scoopers (great headline stats, losing money curves) which
the LLM then rejected ~93% of, so ~0 net good candidates were found. This script
reconstructs the whole picture from the operational logs so any criteria change
can be judged against wallets already swept, without waiting on new sweeps.

It prints, over the given ``bot-*.log`` / ``signals-*.log`` files:
  * the discovery funnel (swept / qualified / new / removed / watchlist trend),
  * the paper-harness guardrail-skip breakdown (deduped — every COPY-PAPER line is
    logged twice; slate-cap vs fill-gate vs category-gate vs first-entry),
  * the LLM gate outcome mix (rejected / fail-open / deferred),
  * the reject-reason taxonomy over the distinct rejected wallets,
  * a COUNTERFACTUAL: how many of the LLM's rejects a cheap money-curve pre-filter
    would line up with. This is a CIRCULAR LOWER BOUND (metrics are read from the
    LLM's own rejection prose), it measures agreement / recall on rejects only and
    says nothing about false positives on good wallets — it is a pointer, not a
    backtest.

With ``--gate-history data/gate-history.jsonl`` it also prints the structured gate
summary via ``gate_history.summarize``, including the vetted-vs-fail-open split
(``admitted`` conflates real follow/watch verdicts with ungated fail-open / cap
admits — the split shows how much of the watchlist was never actually vetted).

Usage:
  python -m scripts.funnel_digest "/tmp/poly-logs/*.log"
  python -m scripts.funnel_digest /tmp/poly-logs --gate-history data/gate-history.jsonl
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.copy_trading import gate_history

GUARDS = ("fill-gate", "first-entry", "slate-cap", "category-gate")
_REJECT_KEYWORDS = (
    "drawdown", "sharpe", "settlement-lag", "scooping", "picking-pennies",
    "near $1", "near-$1", "artifact", "net loss", "net pnl -", "net -", "spiky",
    "variance", "-100%", "too thin", "too small", "copy_replay", "copy-and-hold",
)


def _iter_lines(patterns: list[str]):
    files: list[str] = []
    for p in patterns:
        if os.path.isdir(p):
            files.extend(glob.glob(os.path.join(p, "*.log")))
        else:
            files.extend(glob.glob(p))
    for f in sorted(set(files)):
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    yield ln.rstrip("\n")
        except OSError:
            continue


def _extract_metrics(reason: str) -> dict:
    """Best-effort structured metrics from an LLM rejection reason (prose)."""
    out: dict = {}
    rl = reason.lower()
    m = (re.search(r"([0-9]+(?:\.[0-9]+)?)\s*x\s*max\s*draw", rl)
         or re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%\s*max\s*draw", rl)
         or re.search(r"max\s*draw\w*\s*([0-9]+(?:\.[0-9]+)?)\s*%", rl))
    if m:
        near = rl[max(0, m.start() - 3):m.end()]
        out["maxdd_pct"] = float(m.group(1)) * 100 if "x" in near else float(m.group(1))
    m = re.search(r"sharpe[^0-9+\-]*([+\-]?[0-9]+(?:\.[0-9]+)?)", rl)
    if m:
        out["sharpe"] = float(m.group(1))
    elif "negative sharpe" in rl:
        out["sharpe"] = -0.01
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%\s*hit", rl)
    if m:
        out["hit_pct"] = float(m.group(1))
    m = (re.search(r"net[ _]?pnl[^0-9+\-]*([+\-]?\$?[0-9,]+)", rl)
         or re.search(r"net\s+([+\-]\$?[0-9,]+)", rl))
    if m:
        raw = m.group(1).replace("$", "").replace(",", "")
        # reject prose sometimes carries an empty/currency-only amount
        # ("net pnl -$,") — without a digit there is nothing to parse
        if any(ch.isdigit() for ch in raw):
            out["net_pnl"] = float(raw)
    return out


def digest(patterns: list[str]) -> dict:
    sweeps: list[tuple] = []
    guard = {g: 0 for g in GUARDS}
    opened = 0
    rejected: dict[str, str] = {}
    deferred_reject: dict[str, str] = {}
    failopen: set = set()
    rate_limited: set = set()
    dropped: list[tuple] = []
    seen: set = set()

    culled: dict[str, str] = {}
    cull_hist: dict[str, int] = {}
    paper_proven: set = set()
    pp_rejected: dict[str, str] = {}
    pp_reacquire_failed: dict[str, str] = {}

    # strategy B (the borrowed-clock instant-copy book, 2026-07 A-vs-B race)
    guard_b = {g: 0 for g in GUARDS}
    opened_b = 0
    cap_binds_b: dict[str, int] = {}      # "0x12345678… (wallet-day)" -> count
    cross_routed: dict[str, str] = {}     # wallet -> why

    for ln in _iter_lines(patterns):
        m = re.search(r"swept=(\d+) qualified=(\d+) new=(\d+) removed=(\d+) watchlist=(\d+)", ln)
        if m:
            sweeps.append(tuple(int(x) for x in m.groups()))
            continue
        # gate autopsy (2026-07): every removal now logs an attributable reason —
        # "[DISCOVERY] cull: 0x… — curve-drawdown (2.31 > 1.50 @ n_closed=44)"
        m = re.search(r"\[DISCOVERY\] cull: (0x[0-9a-fA-F]+) [—-] (.+)$", ln)
        if m:
            w = m.group(1).lower()
            reason = m.group(2).strip()
            key = ("c", ln[:19], w)
            if key not in seen:
                seen.add(key)
                culled[w] = reason
                gate = reason.split(" (", 1)[0]
                cull_hist[gate] = cull_hist.get(gate, 0) + 1
            continue
        m = re.search(r"paper-proven \(realized-ledger override\): (.+)$", ln)
        if m:
            paper_proven.update(w.strip().lower()
                                for w in m.group(1).split(",") if w.strip())
            continue
        m = re.search(r"LLM gate REJECTED paper-proven (0x[0-9a-fA-F]+) \((.+?)\): (.+)$", ln)
        if m:
            pp_rejected[m.group(1).lower()] = f"{m.group(2)} | {m.group(3).strip()}"
            continue
        # "[DISCOVERY] paper-proven reacquire FAILED: 0x… — replay-proven-negative (…)
        #  (realized: 7 settled, ROI +80.5%, $+246.24)"
        m = re.search(r"paper-proven reacquire FAILED: (0x[0-9a-fA-F]+) [—-] (.+)$", ln)
        if m:
            pp_reacquire_failed[m.group(1).lower()] = m.group(2).strip()
            continue
        # strategy-B lines FIRST — the generic "guardrail skips" pattern below
        # would otherwise swallow them into A's counters.
        is_b = "[COPY-PAPER-B]" in ln
        mg = re.search(r"guardrail skips: (.+)$", ln)
        if mg:
            key = ("gb" if is_b else "g", ln[:19], mg.group(1).strip())
            if key not in seen:
                seen.add(key)
                tgt = guard_b if is_b else guard
                for g in GUARDS:
                    mm = re.search(rf"{g}=(\d+)", mg.group(1))
                    if mm:
                        tgt[g] += int(mm.group(1))
            continue
        m = re.search(r"\[COPY-PAPER-B\] cap-bind: (.+)$", ln)
        if m:
            key = ("cb", ln[:19], m.group(1).strip())
            if key not in seen:
                seen.add(key)
                for part in m.group(1).split(","):
                    mm = re.search(r"(0x[0-9a-fA-F]+)…?×(\d+) \((\S+)\)", part.strip())
                    if mm:
                        k = f"{mm.group(1)} ({mm.group(3)})"
                        cap_binds_b[k] = cap_binds_b.get(k, 0) + int(mm.group(2))
            continue
        m = re.search(r"cross-routed (0x[0-9a-fA-F]+) to strategy B \((.+)\)$", ln)
        if m:
            cross_routed[m.group(1).lower()] = m.group(2).strip()
            continue
        mo = re.search(r"\[COPY-PAPER(-B)?\] opened=(\d+) resolved=\d+ open=\d+ closed=\d+", ln)
        if mo:
            key = ("o", ln[:19], mo.group(0))
            if key not in seen:
                seen.add(key)
                if mo.group(1):
                    opened_b += int(mo.group(2))
                else:
                    opened += int(mo.group(2))
            continue
        m = re.search(r"LLM gate REJECTED (0x[0-9a-fA-F]+)(?: \(conf \d+%\))?: (.+)$", ln)
        if m:
            rejected[m.group(1).lower()] = m.group(2).strip()
            continue
        m = re.search(r"deferred gate re-check REJECTED (0x[0-9a-fA-F]+): (.+)$", ln)
        if m:
            deferred_reject[m.group(1).lower()] = m.group(2).strip()
            continue
        m = re.search(r"LLM gate unavailable for (0x[0-9a-fA-F]+)", ln)
        if m:
            failopen.add(m.group(1).lower())
            continue
        m = re.search(r"gate rate-limited for (0x[0-9a-fA-F]+)", ln)
        if m:
            rate_limited.add(m.group(1).lower())
            continue
        m = re.search(r"LLM gate dropped (\d+)/(\d+)", ln)
        if m:
            dropped.append((int(m.group(1)), int(m.group(2))))

    uniq_sweeps = list(dict.fromkeys(sweeps))
    all_rejected = {**deferred_reject, **rejected, **pp_rejected}
    metrics = {w: _extract_metrics(r) for w, r in all_rejected.items()}
    return {
        "sweeps": uniq_sweeps, "guard": guard, "opened": opened,
        "rejected": rejected, "deferred_reject": deferred_reject,
        "failopen": failopen, "rate_limited": rate_limited, "dropped": dropped,
        "all_rejected": all_rejected, "metrics": metrics,
        "culled": culled, "cull_hist": cull_hist,
        "paper_proven": paper_proven, "pp_rejected": pp_rejected,
        "pp_reacquire_failed": pp_reacquire_failed,
        "guard_b": guard_b, "opened_b": opened_b,
        "cap_binds_b": cap_binds_b, "cross_routed": cross_routed,
    }


def _print_report(d: dict) -> None:
    sweeps = d["sweeps"]
    guard = d["guard"]
    metrics = d["metrics"]
    n = len(d["all_rejected"])

    print("=" * 70)
    print("DISCOVERY FUNNEL + GATE DIGEST")
    print("=" * 70)

    print("\n[1] DISCOVERY FUNNEL  (distinct sweeps: %d)" % len(sweeps))
    if sweeps:
        tot_new = sum(s[2] for s in sweeps)
        wls = [s[4] for s in sweeps]
        print("  swept/sweep   : min %d  median %d  max %d" % (
            min(s[0] for s in sweeps),
            int(statistics.median([s[0] for s in sweeps])),
            max(s[0] for s in sweeps)))
        print("  NET NEW added : %d over %d sweeps (mean %.2f/sweep)" % (
            tot_new, len(sweeps), tot_new / len(sweeps)))
        print("  removed       : %d" % sum(s[3] for s in sweeps))
        print("  watchlist size: min %d  max %d" % (min(wls), max(wls)))

    if d.get("cull_hist") or d.get("paper_proven"):
        print("\n[1b] CULL AUTOPSY + PAPER-PROVEN  (post 2026-07 instrumentation)")
        for gate, c in sorted(d["cull_hist"].items(), key=lambda kv: -kv[1]):
            print("  cull %-28s %3d" % (gate, c))
        if d["paper_proven"]:
            print("  paper-proven on watchlist   : %d  (%s)" % (
                len(d["paper_proven"]),
                ", ".join(sorted(w[:10] + "…" for w in d["paper_proven"]))))
        for w, why in sorted(d.get("pp_rejected", {}).items()):
            print("  ⚠ gate REJECTED paper-proven %s — %s" % (w[:10] + "…", why[:90]))
        for w, why in sorted(d.get("pp_reacquire_failed", {}).items()):
            print("  ⚠ reacquire FAILED paper-proven %s — %s" % (w[:10] + "…", why[:120]))

    print("\n[2] PAPER-HARNESS GUARDRAIL SKIPS  (deduped)")
    gtot = sum(guard.values())
    for g, v in sorted(guard.items(), key=lambda kv: -kv[1]):
        print("  %-14s %8d  (%.1f%%)" % (g, v, 100 * v / gtot if gtot else 0))
    print("  %-14s %8d   positions opened: %d" % ("TOTAL", gtot, d["opened"]))

    if (d.get("opened_b") or any(d.get("guard_b", {}).values())
            or d.get("cross_routed") or d.get("cap_binds_b")):
        print("\n[2b] STRATEGY-B BOOK  (borrowed-clock instant-copy — A-vs-B race)")
        gb = d.get("guard_b", {})
        gbtot = sum(gb.values())
        for g, v in sorted(gb.items(), key=lambda kv: -kv[1]):
            if v:
                print("  %-14s %8d  (%.1f%%)" % (g, v, 100 * v / gbtot if gbtot else 0))
        print("  %-14s %8d   B positions opened: %d" % ("TOTAL", gbtot, d.get("opened_b", 0)))
        # NB: loop vars must not shadow `n` (the [4] rejected-wallet count above)
        for bind_key, bind_n in sorted(d.get("cap_binds_b", {}).items(),
                                       key=lambda kv: -kv[1]):
            print("  ⚠ cap-bind %-28s ×%d  — a binding B cap re-creates A's censoring"
                  % (bind_key, bind_n))
        for w, why in sorted(d.get("cross_routed", {}).items()):
            print("  → cross-routed %s — %s" % (w[:10] + "…", why[:80]))

    print("\n[3] LLM GATE OUTCOMES  (distinct wallets)")
    print("  REJECTED (skip)            : %d" % len(d["rejected"]))
    print("  deferred re-check REJECTED : %d" % len(d["deferred_reject"]))
    print("  fail-open admitted UNVETTED: %d" % len(d["failopen"]))
    print("  rate-limited (deferred)    : %d" % len(d["rate_limited"]))
    if d["dropped"]:
        dd = sum(a for a, _ in d["dropped"])
        tt = sum(b for _, b in d["dropped"])
        print("  'dropped X/Y' new wallets  : %d of %d (%.0f%%)" % (
            dd, tt, 100 * dd / tt if tt else 0))

    print("\n[4] REJECT-REASON TAXONOMY  (distinct rejected wallets: %d)" % n)
    tax: dict[str, int] = defaultdict(int)
    for r in d["all_rejected"].values():
        rl = r.lower()
        for k in _REJECT_KEYWORDS:
            if k in rl:
                tax[k] += 1
    for k, c in sorted(tax.items(), key=lambda kv: -kv[1]):
        print("  %-16s %3d  (%.0f%%)" % (k, c, 100 * c / n if n else 0))

    print("\n[5] COUNTERFACTUAL  (circular lower bound — NOT a backtest)")
    print("    Metrics are read from the LLM's own rejection prose, so this")
    print("    measures agreement / recall-on-rejects only; false positives on")
    print("    good wallets are UNMEASURED. A pointer to move filtering upstream,")
    print("    validate the FP side on real swept wallets before committing.")
    filters = {
        "max-drawdown > 100%": lambda m: m.get("maxdd_pct", 0) > 100,
        "Sharpe <= 0": lambda m: "sharpe" in m and m["sharpe"] <= 0,
        "hit-rate >= 97% (scooper)": lambda m: m.get("hit_pct", 0) >= 97,
        "net PnL < 0": lambda m: "net_pnl" in m and m["net_pnl"] < 0,
        "ANY of the above": lambda m: (
            m.get("maxdd_pct", 0) > 100 or ("sharpe" in m and m["sharpe"] <= 0)
            or m.get("hit_pct", 0) >= 97 or ("net_pnl" in m and m["net_pnl"] < 0)),
    }
    for name, pred in filters.items():
        c = sum(1 for m in metrics.values() if pred(m))
        print("  %-28s lines up with %2d/%d  (>= %.0f%%)" % (
            name, c, n, 100 * c / n if n else 0))


def _print_gate_history(path: str) -> None:
    rows = gate_history.load(path)
    if not rows:
        print("\n[6] GATE HISTORY: no rows at %s" % path)
        return
    s = gate_history.summarize(rows)
    print("\n[6] GATE HISTORY SUMMARY  (%s)" % path)
    print("  total decided     : %d" % s["total"])
    print("  admitted          : %d  (vetted %d / UNVETTED %d)" % (
        s["admitted"], s.get("admitted_vetted", 0), s.get("admitted_unvetted", 0)))
    if s.get("unvetted_by_reason"):
        for reason, c in sorted(s["unvetted_by_reason"].items(), key=lambda kv: -kv[1]):
            print("      unvetted: %-26s %d" % (reason, c))
    print("  rejected          : %d" % s["rejected"])
    print("  deferred          : %d" % s["deferred"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Offline discovery-funnel + gate digest.")
    ap.add_argument("logs", nargs="*", default=["/tmp/poly-logs"],
                    help="log files, dirs, or globs (default /tmp/poly-logs)")
    ap.add_argument("--gate-history", default=None,
                    help="path to gate-history.jsonl for the structured gate summary")
    args = ap.parse_args(argv)
    _print_report(digest(args.logs))
    if args.gate_history:
        _print_gate_history(args.gate_history)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
