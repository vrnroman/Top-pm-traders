"""Set-Z candidates: the gate proposes, the owner admits.

The ruling (run-review s-p7w2ln): the gate may offer as many wallets as pass,
but final approval of which wallets go live is his alone, never gate-only. So
this module does two things and refuses a third:

* It renders every wallet that passes the go-live gate plus set Z's rails as
  ONE card carrying what has already been measured for it: the clean-era
  record, the real-quote replay with its own sample size, the entry penalty,
  the concentration rail with its reason spelled out, the share of profit
  that came from mirrored exits, the slice split, and how recently it traded.
  Nothing is ranked by return; cards are ordered by real-quote sample size and
  the header says so.
* Each card carries one button whose label says what the tap does. The tap
  re-runs the gate and the rails at that moment and only then calls
  ``zset.admit``, which has no force path.
* It never admits on its own. ``seed_zset.py --apply`` still exists for a
  scripted seed, and it calls the same evaluation, so there is one gate.
"""

from __future__ import annotations

import os
import statistics
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

from src.config import CONFIG
from src.copy_trading import era_state, promotion_gate, shadow_quote, virtual_ledger, zset
from src.copy_trading.copy_paper import PaperCopyLedger, is_dust_fill
from src.logger import logger

# A wallet needs at least this many clean-era rows in the OTHER book before
# its disagreement counts as evidence rather than noise.
CONTRADICTION_MIN_N = 10

# Below this many real-quote matches the replay figure is shown as thin and
# never as a headline: 9 matched rows reading +123% is the exact shape that
# fools a reader. One definition, shared with the rehearsal ledger.
REAL_QUOTE_THIN_N = virtual_ledger.THIN_MATCHED_N

# What one admitted wallet costs the per-wallet prober, for the header.
PROBER_POLLS_PER_MIN_PER_WALLET = 20


def _num(x) -> float:
    try:
        return float(x or 0.0)
    except (TypeError, ValueError):
        return 0.0


def wallet_rows(positions, wallet: str):
    """A wallet's settled, non-dust rows and the time it last traded."""
    key = (wallet or "").lower()
    settled, last_ts = [], 0.0
    for p in positions:
        if (getattr(p, "target", "") or "").lower() != key:
            continue
        last_ts = max(last_ts, _num(getattr(p, "opened_ts", 0.0)),
                      _num(getattr(p, "closed_ts", 0.0)))
        if getattr(p, "closed", False) and not is_dust_fill(p):
            settled.append(p)
    return settled, (last_ts or None)


def clean_roi(settled, era: Optional[float]):
    """At-their-price ROI over the clean era, and the row count behind it."""
    rows = [p for p in settled
            if _num(getattr(p, "opened_ts", 0.0)) >= (era or 0)
            and _num(getattr(p, "spent", 0.0)) > 0]
    if not rows:
        return (None, 0)
    spent = sum(_num(p.spent) for p in rows)
    ideal = sum(_num(getattr(p, "ideal_pnl", 0.0)) for p in rows)
    return ((ideal / spent) if spent else None, len(rows))


@dataclass
class Candidate:
    wallet: str
    ok: bool                       # gate AND every rail
    gate_ready: bool               # golive_check alone
    checks: list = field(default_factory=list)       # gate + rails, rendered
    gate_checks: list = field(default_factory=list)  # what zset.admit consumes
    ideal_roi: Optional[float] = None
    n_ideal: int = 0
    paper_roi: Optional[float] = None
    trimmed_roi: Optional[float] = None
    n_trimmed_kept: int = 0
    n_trimmed_dropped: int = 0
    settled: list = field(default_factory=list)
    a_roi: Optional[float] = None
    a_n: int = 0
    last_ts: Optional[float] = None
    n_open: int = 0

    @property
    def n_fail(self) -> int:
        return sum(1 for c in self.checks if not c[1])


def evaluate(wallet: str, b_positions, a_positions, *, era: Optional[float],
             now: float, book_corr) -> Optional[Candidate]:
    """One wallet through the go-live gate plus set Z's rails.

    The single evaluation both the seeding script and the Telegram cards run,
    so there is one gate. Returns None when the wallet has no settled rows.
    """
    settled, last_ts = wallet_rows(b_positions, wallet)
    if not settled:
        return None
    honest = promotion_gate.honest_kwargs_from(CONFIG)
    floor_kwargs = promotion_gate.floor_kwargs_from(CONFIG)
    stats = promotion_gate.compute_stats(wallet, settled)
    ideal_roi, n_ideal = promotion_gate.ideal_roi_for(settled, min_opened_ts=era)
    ready, checks = promotion_gate.golive_check(
        stats, last_trade_ts=last_ts, now=now,
        min_settled=CONFIG.copy_golive_min_settled,
        max_idle_days=CONFIG.copy_golive_max_idle_days,
        min_roi=CONFIG.copy_golive_min_roi, floor_kwargs=floor_kwargs,
        ideal_roi=ideal_roi, n_ideal_settled=n_ideal,
        book_corr=book_corr, **honest)

    a_settled, _ = wallet_rows(a_positions, wallet)
    a_roi, a_n = clean_roi(a_settled, era)
    conc_ok, conc_detail = zset.concentration_check(settled, min_opened_ts=era)
    contra_ok, contra_detail = zset.contradiction_check(a_roi, a_n, CONTRADICTION_MIN_N)
    bl = zset._blacklist_block(wallet)
    all_checks = list(checks) + [
        ("still positive with its best 3 copies deleted", conc_ok, conc_detail),
        ("the other book does not contradict it", contra_ok, contra_detail),
        ("not under the bot's own auto-demote", bl is None, bl or "no active demotion"),
    ]
    trimmed, kept, dropped = zset.trimmed_roi(settled, min_opened_ts=era)
    key = wallet.lower()
    n_open = sum(1 for p in b_positions
                 if (getattr(p, "target", "") or "").lower() == key
                 and not getattr(p, "closed", False))
    return Candidate(
        wallet=wallet, ok=bool(ready and conc_ok and contra_ok and bl is None),
        gate_ready=bool(ready), checks=all_checks, gate_checks=list(checks),
        ideal_roi=ideal_roi, n_ideal=n_ideal, paper_roi=stats.roi,
        trimmed_roi=trimmed, n_trimmed_kept=kept, n_trimmed_dropped=dropped,
        settled=settled, a_roi=a_roi, a_n=a_n, last_ts=last_ts, n_open=n_open)


def load_books():
    """Both paper books and the clean-era floor, from the data dir."""
    era = era_state.era_floor_ts(os.path.join(CONFIG.data_dir, "ab_race_state.json"))
    b_positions = list(PaperCopyLedger(CONFIG.copy_paper_b_ledger).positions.values())
    a_positions = list(PaperCopyLedger(CONFIG.copy_paper_ledger).positions.values())
    return era, b_positions, a_positions


def candidates(b_positions, a_positions, *, era: Optional[float], now: float,
               wallets: Optional[Iterable[str]] = None):
    """Every wallet with enough clean rows, evaluated. Returns
    ``(passers, near_misses, book_corr)``; passers in no particular order."""
    book_corr = promotion_gate.split_half_corr(b_positions, min_opened_ts=era)
    if wallets is None:
        counts: dict = {}
        for p in b_positions:
            w = (getattr(p, "target", "") or "").lower()
            if w and getattr(p, "closed", False):
                counts[w] = counts.get(w, 0) + 1
        # The first gate check needs COPY_GOLIVE_MIN_SETTLED rows, so a wallet
        # with fewer cannot pass; skipping it here is the same answer, faster.
        wallets = sorted(w for w, n in counts.items()
                         if n >= CONFIG.copy_golive_min_settled)
    passers, near = [], []
    for w in wallets:
        c = evaluate(w, b_positions, a_positions, era=era, now=now, book_corr=book_corr)
        if c is None:
            continue
        if c.ok:
            passers.append(c)
        elif c.n_fail <= 2:
            near.append(c)
    return passers, near, book_corr


def admit(wallet: str, *, era: Optional[float], b_positions, a_positions,
          now: Optional[float] = None) -> tuple[bool, list, Optional[Candidate]]:
    """The owner's tap: re-run the gate NOW, then let ``zset.admit`` decide.

    Returns ``(admitted, checks, candidate)``. The gate result is recomputed
    at the moment of the tap so a card rendered an hour ago cannot admit a
    wallet that has since decayed.
    """
    now = time.time() if now is None else now
    book_corr = promotion_gate.split_half_corr(b_positions, min_opened_ts=era)
    c = evaluate(wallet, b_positions, a_positions, era=era, now=now, book_corr=book_corr)
    if c is None:
        return (False, [("has settled copies in book B", False, "none")], None)
    ok, checks = zset.admit(
        wallet, ready=c.gate_ready, checks=c.gate_checks, settled=c.settled,
        era_floor=era, other_book_roi=c.a_roi, other_book_n=c.a_n,
        rails_supplied=True, source="telegram-gate")
    return (ok, checks, c)


# --------------------------------------------------------------------------- #
# The measured columns
# --------------------------------------------------------------------------- #

def real_quote_slice(wallet: str, b_positions, quotes: dict, era: Optional[float]) -> dict:
    """The wallet's own counterfactual at real quotes, with its sample size."""
    key = (wallet or "").lower()
    mine = [p for p in b_positions if (getattr(p, "target", "") or "").lower() == key]
    out = virtual_ledger.replay_positions(mine, quotes, min_opened_ts=era)
    out["thin"] = out["n_matched"] < REAL_QUOTE_THIN_N
    return out


def penalty_slice(wallet: str, quote_rows: list[dict]) -> Optional[dict]:
    """Median entry penalty for this wallet, prober rows preferred."""
    key = (wallet or "").lower()
    mine = [r for r in quote_rows
            if (r.get("target") or "").lower() == key and shadow_quote.valid_for_penalty(r)]
    if not mine:
        return None
    fast = [r for r in mine if (r.get("source") or "feed") == shadow_quote.FAST_SOURCE]
    rows, source = (fast, "prober") if len(fast) >= 10 else (mine, "slow feed")
    vals = [float(r["penalty_bps"]) for r in rows if r.get("penalty_bps") is not None]
    if not vals:
        return None
    return {"n": len(vals), "p50_bps": statistics.median(vals),
            "mean_bps": sum(vals) / len(vals), "source": source}


def exits_share(settled, era: Optional[float]) -> dict:
    """How much of the clean-era profit came from mirroring the target's SELL."""
    rows = [p for p in settled if _num(getattr(p, "opened_ts", 0.0)) >= (era or 0)]
    exits = [p for p in rows if getattr(p, "exited_early", False)]
    return {"n_exits": len(exits), "n_settled": len(rows),
            "exit_pnl": round(sum(_num(getattr(p, "pnl", 0.0)) for p in exits), 2),
            "total_pnl": round(sum(_num(getattr(p, "pnl", 0.0)) for p in rows), 2)}


def slice_lines(settled, era: Optional[float]) -> tuple[str, str]:
    """Category and price-band splits at their price, as two short lines."""
    rows = [p for p in settled if _num(getattr(p, "opened_ts", 0.0)) >= (era or 0)]
    cats: dict = {}
    bands: dict = {}
    for p in rows:
        c = getattr(p, "category", "") or "other"
        cats.setdefault(c, [0.0, 0.0, 0])
        cats[c][0] += _num(getattr(p, "ideal_pnl", 0.0)); cats[c][1] += _num(getattr(p, "spent", 0.0)); cats[c][2] += 1
        pr = _num(getattr(p, "their_price", 0.0))
        b = ("≤0.25" if pr <= 0.25 else "0.25-0.5" if pr <= 0.5 else
             "0.5-0.75" if pr <= 0.75 else ">0.75")
        bands.setdefault(b, [0.0, 0.0, 0])
        bands[b][0] += _num(getattr(p, "ideal_pnl", 0.0)); bands[b][1] += _num(getattr(p, "spent", 0.0)); bands[b][2] += 1

    def fmt(d: dict) -> str:
        parts = []
        for k, (pnl, spent, n) in sorted(d.items(), key=lambda kv: -kv[1][1]):
            parts.append(f"{k} {pnl:+,.0f} on {spent:,.0f} ({n})")
        return " · ".join(parts) if parts else "no rows"
    return fmt(cats), fmt(bands)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v * 100:+.1f}%"


def render_card(c: Candidate, *, rq: dict, pen: Optional[dict], ex: dict,
                slices: tuple[str, str], now: float, in_z: bool, esc) -> str:
    """One wallet, everything measured for it, no verdict text."""
    w = c.wallet
    head = ("🅩 <b>in set Z</b>" if in_z else "🅩 <b>candidate</b>") + f" <code>{esc(w)}</code>"
    lines = [head]
    lines.append(f"clean era: {_pct(c.ideal_roi)} at their price over {c.n_ideal} settled"
                 f" · paper {_pct(c.paper_roi)}")
    if rq["n_matched"] == 0:
        lines.append("real quotes: none matched yet")
    elif rq["thin"]:
        lines.append(f"real quotes: thin, {rq['n_matched']} of {rq['n_settled']} matched; "
                     f"not a number to lean on")
    else:
        cov = (rq["coverage"] or 0) * 100
        lines.append(f"real quotes: {_pct(rq['real_roi'])} over {rq['n_matched']} matched "
                     f"({cov:.0f}% coverage), vs {_pct(rq['ideal_roi'])} at their price "
                     f"on the same rows")
    if pen:
        lines.append(f"entry penalty: median {pen['p50_bps']:.0f}bps, mean "
                     f"{pen['mean_bps']:.0f}bps (n={pen['n']}, {pen['source']})")
    else:
        lines.append("entry penalty: no usable quotes")
    lines.append(f"concentration rail: {_pct(c.trimmed_roi)} with its best "
                 f"{c.n_trimmed_dropped} copies deleted ({c.n_trimmed_kept} kept); "
                 f"shown because totals hide jackpots, not used to order")
    lines.append(f"mirrored exits: {ex['n_exits']} of {ex['n_settled']} settled, "
                 f"${ex['exit_pnl']:+,.0f} of ${ex['total_pnl']:+,.0f} profit")
    lines.append(f"slices at their price: {esc(slices[0])}")
    lines.append(f"price bands: {esc(slices[1])}")
    idle = (now - c.last_ts) / 86400 if c.last_ts else None
    lines.append(f"active: last copy {idle:.1f}d ago · {c.n_open} open" if idle is not None
                 else f"active: no timestamp · {c.n_open} open")
    n_ok = sum(1 for ch in c.checks if ch[1])
    lines.append(f"gate: {n_ok}/{len(c.checks)} checks pass")
    if in_z:
        lines.append("<i>already in set Z; /zset drop to remove</i>")
    return "\n".join(lines)


def admit_keyboard(wallet: str) -> dict:
    """The one button, labelled with what the tap does."""
    return {"inline_keyboard": [[
        {"text": f"Admit {wallet[:10]}… to set Z", "callback_data": f"zadm:{wallet}"},
    ]]}


def header(n_pass: int, n_z: int, n_near: int) -> str:
    return ("🅩 <b>Set-Z candidates</b>\n"
            f"{n_pass} pass the gate and the rails today; {n_z} already in Z; "
            f"{n_near} near misses (1-2 failing checks, not shown).\n"
            "Admission is yours: each button admits one wallet after re-running "
            "the gate at that moment. Cards are ordered by real-quote sample size, "
            "not by return.\n"
            f"Prober load: about {PROBER_POLLS_PER_MIN_PER_WALLET} polls a minute "
            "per admitted wallet.")
