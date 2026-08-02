"""A-vs-B race comparison — the week's verdict machinery (2026-07).

Strategy A (lagged, censored, live-book fills) and strategy B (borrowed-clock
instant-copy at the target's price) run as parallel paper books. This module
computes the comparison both the daily snapshot and the day-7 verdict memo
render: headline realized dollars per book with the witnesses logged next to it
(ROI/copy, win rate, capital cycled — dollars decide, nothing gets dropped),
a per-wallet ROUTING TABLE (the deliverable is "which wallet routes through
which strategy", not a single winner), and a verdict-validity stamp — a week in
which either book sat starved for 48h+ must not produce a confident-sounding
winner.

The comparison window (the "era") starts at strategy B's first open: A existed
before the race, so only A copies opened inside the era count toward the race
(A's all-time record is shown as a witness, never the comparator).
"""

from __future__ import annotations

import datetime as _dt
import json
import time
from types import SimpleNamespace
from typing import Optional

from src.copy_trading import promotion_state
from src.copy_trading.copy_cost import CostModel
from src.copy_trading.copy_paper import (
    DUST_FILL_FRAC, drag_stats, fill_health_suspect)
from src.copy_trading.pnl_unified import (
    DIVERGENCE_MIN_CLOSED, DIVERGENCE_SUSPECT_BPS)
from src.copy_trading.promotion_gate import (
    FALSIFY_MIN_N, FALSIFY_MIN_WALLETS, split_half_corr)

STALL_VOID_HOURS = 48.0     # a zero-open window this long compromises the verdict
MIN_WALLET_N = 5            # per-wallet routing needs at least this many settled


def _load_rows(path: str) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict):
                    rows.append(d)
    except OSError:
        pass
    return rows


def _is_dust_row(r: dict) -> bool:
    """Dict-shaped mirror of copy_paper.is_dust_fill (this module reads raw
    JSONL rows, not PaperPosition objects)."""
    tp = float(r.get("their_price") or 0.0)
    ep = float(r.get("entry_price") or 0.0)
    return tp > 0 and ep > 0 and ep < DUST_FILL_FRAC * tp


def _book_stats(rows: list[dict]) -> dict:
    # Dust-quarantined, like every other trust surface (report, pnl_unified,
    # split_half_corr, ideal_roi_for, rebaseline._closed_clean). telegram_bot's
    # _stamped / _sensitivity still do not (ROADMAP §9.3); this one is the one
    # whose
    # ideal_roi_net section7_reading turns into a zero-crossing recommendation
    # — so a single unbounded dust win could move the memo's own advice.
    closed = [r for r in rows if r.get("closed") and not _is_dust_row(r)]
    open_rows = [r for r in rows if not r.get("closed")]
    pnl = sum(float(r.get("pnl") or 0.0) for r in closed)
    spent = sum(float(r.get("spent") or 0.0) for r in closed)
    ideal = sum(float(r.get("ideal_pnl") or 0.0) for r in closed)
    wins = sum(1 for r in closed if r.get("won"))
    roi = (pnl / spent) if spent else 0.0
    ideal_roi = (ideal / spent) if spent else 0.0
    # Drag witness (P0-1 acceptance, standing): how far fills landed from the
    # target's price — avg must be >= 0 and no fill below -300bps in the clean
    # era; the deep-gift count is impossible by construction post-fix.
    drag = drag_stats([int(r.get("drag_bps") or 0) for r in closed])
    # Modeled-cost pair (P1-7), DERIVED per row — never the stamps, which are
    # absent on pre-P1-7 rows and would flatter this book's @net (see
    # _row_costs). This is the number section7_reading turns into a
    # recommendation, so it must match rebaseline and the slice table.
    _cm, _gas, _fee = _cost_env()
    _pairs = [_row_costs(r, _cm, _gas, _fee) for r in closed]
    cost = sum(c for c, _ in _pairs)
    icost = sum(ic for _, ic in _pairs)
    # Divergence tripwire: realized vs at-their-price disagreeing past the bar
    # means the result is coming from the fill model, not the wallets.
    suspect = (len(closed) >= DIVERGENCE_MIN_CLOSED and spent > 0
               and abs(roi - ideal_roi) * 10000 > DIVERGENCE_SUSPECT_BPS)
    return {
        "n_settled": len(closed),
        "n_open": len(open_rows),
        "open_usd": round(sum(float(r.get("spent") or 0.0) for r in open_rows), 2),
        "pnl": round(pnl, 2),
        "spent": round(spent, 2),
        "roi": round(roi, 4),
        "ideal_pnl": round(ideal, 2),
        "ideal_roi": round(ideal_roi, 4),
        "roi_net": round((pnl - cost) / spent, 4) if spent else 0.0,
        "ideal_roi_net": round((ideal - icost) / spent, 4) if spent else 0.0,
        # INTENTIONALLY reads the raw stamp: a COUNT of stamped rows, kept as
        # a diagnostic. Never summed into a rendered figure — the @net render
        # no longer even gates on it, because cost derives for every row now.
        "cost_stamped": sum(1 for r in closed if float(r.get("ideal_cost_usd") or 0.0) > 0),
        "gifted": round(pnl - ideal, 2),
        "win_rate": round(wins / len(closed), 4) if closed else 0.0,
        "drag": drag,
        "fill_suspect": suspect or fill_health_suspect(drag),
    }


def _per_wallet(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        if not r.get("closed"):
            continue
        w = (r.get("target") or "").lower()
        p = out.setdefault(w, {"n": 0, "pnl": 0.0, "spent": 0.0, "wins": 0})
        p["n"] += 1
        p["pnl"] += float(r.get("pnl") or 0.0)
        p["spent"] += float(r.get("spent") or 0.0)
        p["wins"] += 1 if r.get("won") else 0
    for p in out.values():
        p["roi"] = p["pnl"] / p["spent"] if p["spent"] else 0.0
    return out


def _route_verdict(a: Optional[dict], b: Optional[dict]) -> str:
    """Per-wallet routing: which book does this wallet's evidence support?"""
    a_ok = a is not None and a["n"] >= MIN_WALLET_N
    b_ok = b is not None and b["n"] >= MIN_WALLET_N
    if not a_ok and not b_ok:
        return "dual-track (thin evidence)"
    if a_ok and not b_ok:
        return "A" if a["roi"] > 0 else "neither (A-negative, B thin)"
    if b_ok and not a_ok:
        return "B" if b["roi"] > 0 else "neither (B-negative, A thin)"
    a_pos, b_pos = a["roi"] > 0, b["roi"] > 0
    if a_pos and b_pos:
        return "both (A first)" if a["roi"] >= b["roi"] else "both (B first)"
    if a_pos:
        return "A"
    if b_pos:
        return "B"
    return "neither"


def _largest_gap_hours(rows: list[dict], era_start: float, now: float) -> float:
    """Largest zero-open stretch (hours) inside [era_start, now]."""
    opens = sorted(float(r.get("opened_ts") or 0.0) for r in rows
                   if float(r.get("opened_ts") or 0.0) >= era_start)
    points = [era_start] + opens + [now]
    return max((b - a) for a, b in zip(points, points[1:])) / 3600.0


# --------------------------------------------------------------------------- #
# Cost-slice decomposition (s-log7q, I4) — read-only, verdict-memo only
# --------------------------------------------------------------------------- #

_SLICE_MIN_N = 10          # thinner than this a slice is noise, not evidence


def _row_costs(r: dict, cm, gas: float, fee_bps: float) -> tuple[float, float]:
    """(realized cost, at-price cost) for one settled row, derived ON THE FLY.

    NOT the row-stamped ``cost_usd`` / ``ideal_cost_usd``. Those stamps only
    exist on rows opened after P1-7 shipped, so earlier rows carry ``0`` and
    read as cost-free — a large minority of the ledger (re-derive the share
    rather than trusting a number written here). Summing the stamps
    therefore UNDERSTATES drag, which is why ``rebaseline.book_stats`` derives
    every row the same way regardless of era, and why this module disagreed
    with it (verifier r8/r9, s-r7m3qk): the verdict memo's headline ``@net``
    and the cost-slice table printed six lines below it were on different cost
    bases, and ``section7_reading`` renders the headline into a zero-crossing
    recommendation ("go-live floor stays shut" vs "recalibration is on the
    table"). Same split as rebaseline: realized pays gas+fees, at-price also
    pays the full category spread.
    """
    spent = float(r.get("spent") or 0.0)
    cost = gas + spent * fee_bps / 10000.0
    return cost, cost + spent * cm.cost_of(str(r.get("category") or "other"))


def _cost_env():
    """(cost model, gas, fee bps) from config — one place, so every surface in
    this module charges identically."""
    from src.config import CONFIG
    return (CostModel.from_env(), float(CONFIG.copy_paper_gas_usd),
            float(CONFIG.copy_paper_trade_fee_bps))


def _row_modeled_cost(r: dict) -> float:
    """At-price modeled cost for one row (gas + fees + category spread)."""
    cm, gas, fee = _cost_env()
    return _row_costs(r, cm, gas, fee)[1]


def cost_slices(rows: list[dict]) -> dict:
    """Where does the net cost drag live? Group closed rows by category and by
    copy-size bucket, and for each slice compute the same trio the headline
    carries: gross realized ROI, @price ROI, and @net (after the per-row modeled
    gas + fee + category spread, DERIVED per row (never the stamps)). The verdict memo needs this because a
    book-level @net of −3% can hide slices that clear costs and slices that
    never can — the post-verdict gate wiring (deferred to the new era) reads
    exactly this table.
    """
    closed = [r for r in rows if r.get("closed")
              and float(r.get("spent") or 0.0) > 0]

    def _agg(group_rows: list[dict]) -> dict:
        spent = sum(float(r.get("spent") or 0.0) for r in group_rows)
        pnl = sum(float(r.get("pnl") or 0.0) for r in group_rows)
        ideal = sum(float(r.get("ideal_pnl") or 0.0) for r in group_rows)
        _cm, _gas, _fee = _cost_env()
        icost = sum(_row_costs(r, _cm, _gas, _fee)[1] for r in group_rows)
        return {"n": len(group_rows), "spent": round(spent, 2),
                "roi": round(pnl / spent, 4) if spent else 0.0,
                "ideal_roi": round(ideal / spent, 4) if spent else 0.0,
                "ideal_roi_net": round((ideal - icost) / spent, 4) if spent else 0.0}

    def _bucket(spent: float) -> str:
        # "under $25", never "<$25": these labels are rendered into a
        # parse_mode=HTML Telegram message, where a bare "<" starts a tag.
        # Telegram rejects the whole message with 400 "can't parse entities:
        # Unsupported start tag" — verified against the live API 2026-08-02 —
        # and <pre> does not protect it. Since COPY_PAPER_MAX_USD=50 every copy
        # lands in one of these buckets, so the 08-22 §7 verdict memo would
        # have been undeliverable, leaving verdict_sent unset and re-firing
        # daily forever. send_message now also falls back to plain text, but
        # not emitting the character is the first line of defence.
        for hi in (10.0, 25.0, 50.0):
            if spent < hi:
                return f"under ${hi:.0f}"
        return "$50+"

    by_cat: dict[str, list[dict]] = {}
    by_size: dict[str, list[dict]] = {}
    for r in closed:
        by_cat.setdefault(str(r.get("category") or "?"), []).append(r)
        by_size.setdefault(_bucket(float(r.get("spent") or 0.0)), []).append(r)
    return {
        "by_category": {k: _agg(v) for k, v in by_cat.items()
                        if len(v) >= _SLICE_MIN_N},
        "by_size": {k: _agg(v) for k, v in by_size.items()
                    if len(v) >= _SLICE_MIN_N},
    }


def verdict_readiness(cmp: dict, *, verdict_days: float, now: float) -> dict:
    """Will the §7 verdict be COMPUTABLE when it comes due?

    §7 needs ≥FALSIFY_MIN_WALLETS wallets at n≥FALSIFY_MIN_N settled in the
    clean era. Nothing watched whether that floor would actually be met, so the
    failure mode was to arrive on the due date, find the statistic
    undefined, and silently have no verdict — a pre-registered falsification
    experiment that quietly never resolves is strictly worse than either
    outcome. This runs daily so the shortfall is visible with time to act.
    """
    era = cmp.get("era_start")
    days_left = None
    if era:
        days_left = max(0.0, verdict_days - (now - era) / 86400.0)
    per = cmp.get("persistence") or {}
    # BOTH books, not just B. section7_reading renders a status per book, so a
    # witness that watched only B would report "readable" while A still prints
    # "inconclusive (Nw < 15w)" on the day — overstating exactly the thing this
    # exists to stop anyone from discovering late.
    counts = {k: ((per.get(k) or {}).get("era") or {}).get("n") or 0
              for k in ("a", "b")}
    return {"wallets": counts["b"], "counts": counts,
            "bar": FALSIFY_MIN_WALLETS, "min_n": FALSIFY_MIN_N,
            "days_left": (round(days_left, 1) if days_left is not None else None),
            "readable": counts["b"] >= FALSIFY_MIN_WALLETS,
            "readable_both": all(v >= FALSIFY_MIN_WALLETS
                                 for v in counts.values())}


def fmt_readiness(r: dict) -> str:
    """One line for the daily snapshot. Leads with the shortfall when short."""
    d = ("" if r["days_left"] is None
         else f" · {r['days_left']:.0f}d to verdict")
    c = r.get("counts") or {}
    books = f"A {c.get('a', 0)}w / B {c.get('b', 0)}w"
    if r["readable_both"]:
        return f"§7 readable: {books} at n≥{r['min_n']} (bar {r['bar']}w){d}"
    if r["readable"]:
        # B carries the verdict's recommendation, so this is not fatal — but A
        # will print inconclusive, and that should not be a surprise on the day.
        return (f"§7 readable on B ({books} at n≥{r['min_n']}, bar "
                f"{r['bar']}w) — A will read inconclusive{d}")
    return (f"⚠ §7 NOT YET READABLE: {books} at n≥{r['min_n']}, "
            f"need {r['bar']}w{d} — verdict would be undefined")


def section7_reading(cmp: dict) -> dict:
    """The mechanical ROADMAP §7 reading, posted with the verdict memo: is the
    copy-thesis falsified, and what does that imply. NO auto-execution — the
    memo recommends, the owner acts.

    Kill bar (§7): clean-era split-half wallet corr ≤ 0 across
    ≥FALSIFY_MIN_WALLETS wallets (each n≥FALSIFY_MIN_N) → falsified. Below the
    wallet bar the era is simply too young to kill anything.
    """
    out = {}
    for key in ("a", "b"):
        era = cmp["persistence"][key].get("era")
        if not era or era.get("n") is None:
            out[key] = {"status": "no-era-data", "corr": None, "wallets": 0}
            continue
        corr = era.get("realized")
        wallets = era.get("n") or 0
        if corr is None:
            status = "inconclusive (corr undefined)"
        elif wallets < FALSIFY_MIN_WALLETS:
            status = f"inconclusive ({wallets}w < {FALSIFY_MIN_WALLETS}w bar)"
        elif corr <= 0:
            status = "FALSIFIED"
        else:
            status = "alive"
        out[key] = {"status": status, "corr": corr, "wallets": wallets}
    b = cmp["b"]
    rec_bits = []
    if out["b"]["status"] == "FALSIFIED":
        rec_bits.append("B fails the §7 bar → retire the copy-thesis books")
    if b.get("ideal_roi_net") is not None and b.get("n_settled"):
        if b["ideal_roi_net"] <= 0:
            rec_bits.append(
                f"B @net {b['ideal_roi_net'] * 100:+.1f}% does not clear modeled "
                "costs → hold PREVIEW; go-live floor stays shut")
        else:
            rec_bits.append(
                f"B @net {b['ideal_roi_net'] * 100:+.1f}% clears modeled costs "
                "→ go-live floor recalibration is on the table")
    if not rec_bits:
        rec_bits.append("evidence incomplete — extend the era before deciding")
    out["recommendation"] = "; ".join(rec_bits)
    return out


def _fmt_reading(reading: dict) -> list:
    lines = ["§7 READING (mechanical, no auto-execution):"]
    for key, label in (("a", "A"), ("b", "B")):
        r = reading[key]
        corr = f"{r['corr']:+.2f}" if r.get("corr") is not None else "n/a"
        lines.append(f"  {label}: {r['status']} (corr {corr}, {r['wallets']}w)")
    lines.append(f"  → {reading['recommendation']}")
    return lines


def fmt_slices(title: str, slices: dict) -> list:
    lines = []
    if not slices:
        return lines
    lines.append(title)
    for name, s in sorted(slices.items(),
                          key=lambda kv: kv[1]["ideal_roi_net"]):
        lines.append(
            f"  {name:<14} n={s['n']:>3}  realized {s['roi'] * 100:+.1f}%  "
            f"@price {s['ideal_roi'] * 100:+.1f}%  "
            f"@net {s['ideal_roi_net'] * 100:+.1f}%")
    return lines


def compare(
    a_ledger_path: str,
    b_ledger_path: str,
    *,
    b_slippage_bps: int = 0,
    now: Optional[float] = None,
    era_floor: Optional[float] = None,
) -> dict:
    """Compute the full A-vs-B comparison. Era = [B's first open, now].

    ``era_floor`` (the clean-era marker written when the P0-1 fill fix
    deployed) restricts BOTH books to rows opened after it: the race that
    produced the 2026-07-18 verdict ran on the artifact fill model, so the
    restarted race only scores post-fix fills. A's all-time record remains as
    a witness, never the comparator."""
    now = now if now is not None else time.time()
    a_rows_all = _load_rows(a_ledger_path)
    b_rows_all = _load_rows(b_ledger_path)
    b_rows = ([r for r in b_rows_all
               if float(r.get("opened_ts") or 0.0) >= era_floor]
              if era_floor else b_rows_all)

    era_start = min((float(r.get("opened_ts") or 0.0) for r in b_rows),
                    default=None)
    a_rows_era = ([r for r in a_rows_all
                   if float(r.get("opened_ts") or 0.0) >= era_start]
                  if era_start else [])

    a_stats = _book_stats(a_rows_era)
    b_stats = _book_stats(b_rows)
    a_all = _book_stats(a_rows_all)

    # per-wallet routing table (era-windowed on both sides)
    a_pw = _per_wallet(a_rows_era)
    b_pw = _per_wallet(b_rows)
    routing = []
    for w in sorted(set(a_pw) | set(b_pw),
                    key=lambda x: -(a_pw.get(x, {}).get("pnl", 0.0)
                                    + b_pw.get(x, {}).get("pnl", 0.0))):
        routing.append({
            "wallet": w, "a": a_pw.get(w), "b": b_pw.get(w),
            "route": _route_verdict(a_pw.get(w), b_pw.get(w)),
        })

    # verdict validity — a starved book voids the confident winner
    validity = {"valid": True, "reasons": [], "extend_days": 0.0}
    if era_start is None:
        validity = {"valid": False,
                    "reasons": ["strategy B has opened nothing yet — no era"],
                    "extend_days": 0.0}
    else:
        for name, rows in (("A", a_rows_era), ("B", b_rows)):
            gap_h = _largest_gap_hours(rows, era_start, now)
            if gap_h >= STALL_VOID_HOURS:
                validity["valid"] = False
                validity["reasons"].append(
                    f"book {name} had a {gap_h:.0f}h zero-open window")
                validity["extend_days"] = max(validity["extend_days"],
                                              round(gap_h / 24.0, 1))

    # promotions / governance per scope
    def _gov(scope: str) -> dict:
        return {
            "promoted": sorted(promotion_state.promoted_set(scope)),
            "offers": {w: r.get("status") for w, r in
                       promotion_state.offers_map(scope).items()},
            "blacklisted": sorted(promotion_state.active_blacklist(now, scope=scope)),
        }

    # day-by-day cumulative realized PnL per book (era only)
    def _daily(rows: list[dict]) -> list[tuple[str, float]]:
        per: dict[str, float] = {}
        for r in rows:
            if not r.get("closed"):
                continue
            day = _dt.datetime.fromtimestamp(
                float(r.get("closed_ts") or 0.0), _dt.timezone.utc).strftime("%m-%d")
            per[day] = per.get(day, 0.0) + float(r.get("pnl") or 0.0)
        out, cum = [], 0.0
        for day in sorted(per):
            cum += per[day]
            out.append((day, round(cum, 2)))
        return out

    # Split-half wallet persistence (P0-4) — realized and at-their-price,
    # all-time and clean-era. THE number that says whether wallet-copying
    # works at all; rendered next to the ROADMAP §7 falsification bar.
    def _persist_corr(rows: list[dict]) -> dict:
        positions = [SimpleNamespace(**r) for r in rows if r.get("closed")]
        realized, n = split_half_corr(positions, pnl_attr="pnl")
        ideal, _ = split_half_corr(positions, pnl_attr="ideal_pnl")
        return {"realized": realized, "ideal": ideal, "n": n}

    a_rows_floor = ([r for r in a_rows_all
                     if float(r.get("opened_ts") or 0.0) >= era_floor]
                    if era_floor else None)
    persistence = {
        "a": {"all": _persist_corr(a_rows_all),
              "era": (_persist_corr(a_rows_floor)
                      if a_rows_floor is not None else None)},
        "b": {"all": _persist_corr(b_rows_all),
              "era": (_persist_corr(b_rows) if era_floor else None)},
    }

    result = {
        "now": now,
        "era_start": era_start,
        "era_floor": era_floor,
        "era_days": round((now - era_start) / 86400.0, 2) if era_start else 0.0,
        "b_slippage_bps": b_slippage_bps,
        "a": a_stats, "b": b_stats, "a_all_time": a_all,
        "routing": routing,
        "validity": validity,
        "persistence": persistence,
        "gov_a": _gov(""), "gov_b": _gov("b"),
        "daily_a": _daily(a_rows_era), "daily_b": _daily(b_rows),
        "b_cost_slices": cost_slices(b_rows),
    }
    # The §7 reading reads the finished comparison (persistence + B stats).
    result["section7"] = section7_reading(result)
    return result


def _fmt_book(name: str, s: dict) -> str:
    line = (f"{name}: {s['n_settled']} settled, ${s['pnl']:+.0f} "
            f"({s['roi'] * 100:+.1f}%/copy · @price {s['ideal_roi'] * 100:+.1f}%")
    # P1-7 net twin: @price after modeled gas + fees + the category spread —
    # the race read in real-money terms. Shown whenever the book has settled
    # rows; cost is derived per row, so it no longer depends on stamps.
    if s.get("n_settled"):
        line += f" · @net {s['ideal_roi_net'] * 100:+.1f}%"
    line += (f", win {s['win_rate'] * 100:.0f}%, ${s['spent']:.0f} cycled) · "
             f"{s['n_open']} open (${s['open_usd']:.0f})")
    if s.get("fill_suspect"):
        line += " ⚠SUSPECT-fills"
    return line


def _persistence_lines(cmp: dict) -> list:
    """The split-half persistence line(s) for the snapshot/verdict: all-time
    corr beside the clean-era read, with the ROADMAP §7 bar spelled out — so
    the race is watched against the number that can falsify it."""
    pa, pb = cmp["persistence"]["a"], cmp["persistence"]["b"]
    if not (pa["all"]["n"] or pb["all"]["n"] or cmp.get("era_floor")):
        return []

    def _seg(p: dict) -> str:
        r = p["all"]["realized"]
        s = f"{r:+.2f}" if r is not None else "n/a"
        s += f" ({p['all']['n']}w)"
        if p.get("era") is not None:
            e = p["era"]["realized"]
            s += (f" · post-fix {e:+.2f}" if e is not None else " · post-fix n/a")
            s += f" ({p['era']['n']}w)"
        return s

    return [
        f"📈 persistence (split-half, n≥{FALSIFY_MIN_N}): A {_seg(pa)} | B {_seg(pb)}",
        f"   kill bar: clean-era corr ≤ 0 across ≥{FALSIFY_MIN_WALLETS}w "
        f"(n≥{FALSIFY_MIN_N}) → falsified (ROADMAP §7)",
    ]


def format_snapshot(cmp: dict) -> str:
    """Compact daily Telegram snapshot (plain text, HTML-safe)."""
    v = cmp["validity"]
    lines = [
        f"🏁 A-vs-B race — day {cmp['era_days']:.1f}"
        + ("" if v["valid"] else " ⚠ WINDOW COMPROMISED"),
        _fmt_book("A (lagged)", cmp["a"]),
        _fmt_book(f"B (instant, +{cmp['b_slippage_bps']}bps)", cmp["b"]),
    ]
    # Fill-health witness on A's live-book fills (P0-1's acceptance, standing):
    # the first place a fill-model regression shows up, within 24h.
    da = cmp["a"]["drag"]
    if da["n"]:
        warn = " ⚠" if cmp["a"]["fill_suspect"] else ""
        lines.append(
            f"fills A{warn}: avg drag {da['avg_drag_bps']:+.0f}bps · "
            f"min {da['min_drag_bps']:+d} · {da['pct_better'] * 100:.0f}% better · "
            f"deep-gift {da['n_deep_gift']}/{da['n']}")
    lines.extend(_persistence_lines(cmp))
    routed = [r for r in cmp["routing"] if r["route"] in ("A", "B")]
    if routed:
        lines.append("routes: " + ", ".join(
            f"{r['wallet'][:8]}→{r['route']}" for r in routed[:6]))
    for reason in v["reasons"]:
        lines.append(f"⚠ {reason}")
    gov_bits = []
    if cmp["gov_a"]["promoted"]:
        gov_bits.append(f"A promoted {len(cmp['gov_a']['promoted'])}")
    if cmp["gov_b"]["promoted"]:
        gov_bits.append(f"B promoted {len(cmp['gov_b']['promoted'])}")
    if gov_bits:
        lines.append(" · ".join(gov_bits))
    return "\n".join(lines)


def format_verdict(cmp: dict) -> str:
    """The day-7 verdict memo (plain text; also the CLI report body)."""
    v = cmp["validity"]
    out = []
    out.append("=" * 64)
    out.append(f"A-vs-B RACE VERDICT — {cmp['era_days']:.1f} days "
               f"(era start {_dt.datetime.fromtimestamp(cmp['era_start'], _dt.timezone.utc):%Y-%m-%d %H:%M} UTC)"
               if cmp["era_start"] else "A-vs-B RACE — no era yet (B never opened)")
    out.append("=" * 64)
    if not v["valid"]:
        out.append("⚠ VERDICT INVALID — " + "; ".join(v["reasons"]))
        if v["extend_days"]:
            out.append(f"⚠ extend the window by ~{v['extend_days']:.1f} day(s) "
                       f"before calling a winner")
    # A verdict can never again be won on fills: if realized and at-their-price
    # diverge, the fill model is gifting and the race says so up front.
    for name, key in (("A", "a"), ("B", "b")):
        if cmp[key].get("fill_suspect"):
            out.append(
                f"⚠ FILL-SUSPECT {name}: realized {cmp[key]['roi'] * 100:+.1f}% vs "
                f"@price {cmp[key]['ideal_roi'] * 100:+.1f}% — the gap is the fill "
                f"model, not the wallets; do not call a winner on realized $")
    out.append("")
    out.append("HEADLINE (realized $ decides; @price is the artifact-proof cross-check:")
    out.append("  " + _fmt_book("A in-era ", cmp["a"]))
    out.append("  " + _fmt_book(f"B (+{cmp['b_slippage_bps']}bps)", cmp["b"]))
    out.append("  " + _fmt_book("A all-time", cmp["a_all_time"]) + "  [witness]")
    out.append("")
    out.extend(_persistence_lines(cmp))
    if cmp.get("section7"):
        out.append("")
        out.extend(_fmt_reading(cmp["section7"]))
    slices = cmp.get("b_cost_slices") or {}
    slice_lines = (fmt_slices("B cost slices — by category (net drag first):",
                               slices.get("by_category") or {})
                   + fmt_slices("B cost slices — by copy size:",
                                 slices.get("by_size") or {}))
    if slice_lines:
        out.append("")
        out.append("WHERE THE COST DRAG LIVES (read-only; post-era gate wiring "
                   "reads this table):")
        out.extend(slice_lines)
    out.append("")
    out.append(f"ROUTING TABLE (per-wallet, n>={MIN_WALLET_N} to route):")
    for r in cmp["routing"]:
        a, b = r["a"], r["b"]
        fa = (f"A {a['n']:>3} ${a['pnl']:+7.0f} {a['roi'] * 100:+6.1f}%"
              if a else "A   —              ")
        fb = (f"B {b['n']:>3} ${b['pnl']:+7.0f} {b['roi'] * 100:+6.1f}%"
              if b else "B   —              ")
        out.append(f"  {r['wallet'][:10]}… {fa} | {fb}  → {r['route']}")
    if not cmp["routing"]:
        out.append("  (no settled copies in either book yet)")
    out.append("")
    for name, key in (("A", "gov_a"), ("B", "gov_b")):
        g = cmp[key]
        out.append(f"GOVERNANCE {name}: promoted={len(g['promoted'])} "
                   f"offers={len(g['offers'])} blacklisted={len(g['blacklisted'])}"
                   + (f" ({', '.join(w[:8] for w in g['promoted'])})"
                      if g["promoted"] else ""))
    out.append("")
    for name, key in (("A", "daily_a"), ("B", "daily_b")):
        daily = cmp[key]
        if daily:
            out.append(f"CUM $ {name}: " + " ".join(f"{d}:{v:+.0f}" for d, v in daily))
    return "\n".join(out)
