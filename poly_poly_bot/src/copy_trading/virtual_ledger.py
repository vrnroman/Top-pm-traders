"""The counterfactual book: what the paper copies would have paid for real.

Both paper books price entries with a *model*. Book A walks a simulated asks
book; book B stamps a flat +100bps. The shadow-quote observer records what the
live order book was actually showing at the moment each trade was detected,
priced with the live executor's own function — so for any copy that has both
a shadow quote and a settled paper row, we can re-run the settlement at the
price a real order would have taken.

That is the whole of this module, and its limits are deliberate:

* **Only copies the book ADMITTED and SETTLED.** The observer also captures
  the trades both books refuse, on purpose — but those have no outcome, and
  modelling one would invent a number. A third P&L figure that mixes measured
  and imagined rows is exactly the class of error this repo has spent its last
  several commits deleting.
* **The same cost basis the books use.** Only `cost_usd` (gas + fee) is
  charged. The spread is already inside the shadow price — we lifted the real
  ask — and `PaperPosition` is explicit that fill mechanics are never
  re-charged.
* **Counterfactual, and labelled as such, forever.** These figures never merge
  into a book's numbers, never enter the A-vs-B race, and never appear in the
  2026-08-22 verdict memo. They answer one question — "what would real
  execution have done to this book" — and nothing else.
"""

from __future__ import annotations

from typing import Optional

from src.copy_trading import shadow_quote
from src.copy_trading.copy_paper import PaperCopyLedger, is_dust_fill


def _num(x) -> float:
    try:
        return float(x or 0.0)
    except (TypeError, ValueError):
        return 0.0


# Below this many real-quote-matched copies, a dollar or ROI figure derived
# from them is labelled thin wherever it is rendered. ONE number, because two
# surfaces calling the same 11 rows "solid" and "not a number to lean on" is
# the disagreement this project has paid for before.
THIN_MATCHED_N = 15


def quote_map(quote_rows: Optional[list[dict]] = None) -> dict:
    """copy_id -> the real price a copy order would have taken.

    Same validity filter the headline figures use. A quote taken long after
    detection, or one from the backlog flushed after a restart, prices a book
    that has had minutes to move; re-settling at those entries would report
    drift as execution cost. Where both detectors quoted the same copy, the
    per-wallet prober's quote wins: it is the detection speed a set-Z copier
    actually gets.
    """
    rows = quote_rows if quote_rows is not None else shadow_quote.load_rows()
    quotes: dict = {}
    fast: set = set()
    for r in rows:
        cid = r.get("copy_id")
        price = r.get("our_price")
        if not (cid and price and shadow_quote.usable_quote(r)):
            continue
        is_fast = (r.get("source") or "feed") == shadow_quote.FAST_SOURCE
        if cid in fast and not is_fast:
            continue
        quotes[cid] = float(price)
        if is_fast:
            fast.add(cid)
    return quotes


def settled_rows(positions, min_opened_ts: Optional[float] = None) -> list:
    """The rows a counterfactual may re-settle: closed, non-dust, funded."""
    return [p for p in positions
            if getattr(p, "closed", False)
            and not is_dust_fill(p)
            and _num(getattr(p, "spent", 0)) > 0
            and (min_opened_ts is None
                 or _num(getattr(p, "opened_ts", 0)) >= min_opened_ts)]


def real_pnl_for(p, our_price: float, spent: Optional[float] = None) -> float:
    """One position re-settled at the real entry price.

    ``spent`` overrides the paper stake (the rehearsal ledger sizes at the
    owner's caps); the share count follows from the real price, and an early
    exit keeps the book's exit price because only the entry is re-priced.
    """
    s = _num(p.spent) if spent is None else float(spent)
    shares_real = s / our_price if our_price > 0 else 0.0
    if getattr(p, "exited_early", False):
        book_shares = _num(p.shares)
        proceeds = _num(p.pnl) + _num(p.spent)
        exit_price = (proceeds / book_shares) if book_shares > 0 else 0.0
        return shares_real * exit_price - s
    payout = shares_real if getattr(p, "won", False) else 0.0
    return payout - s


def replay_positions(positions, quotes: dict,
                     min_opened_ts: Optional[float] = None) -> dict:
    """The counterfactual over an in-memory set of positions (a book, or one
    wallet's slice of it). Same output shape as ``replay``."""
    settled = settled_rows(positions, min_opened_ts)
    matched, unmatched = [], 0
    for p in settled:
        if getattr(p, "copy_id", None) in quotes:
            matched.append(p)
        else:
            unmatched += 1

    spent = sum(_num(p.spent) for p in matched)
    paper_pnl = sum(_num(p.pnl) for p in matched)
    ideal_pnl = sum(_num(getattr(p, "ideal_pnl", 0.0)) for p in matched)
    paper_cost = sum(_num(getattr(p, "cost_usd", 0.0)) for p in matched)
    real_pnl = sum(real_pnl_for(p, quotes[p.copy_id]) for p in matched)

    def _roi(v):
        return (v / spent) if spent else None

    return {
        "n_settled": len(settled),
        "n_matched": len(matched),
        "n_unmatched": unmatched,
        "coverage": (len(matched) / len(settled)) if settled else None,
        "spent": round(spent, 2),
        "paper_pnl": round(paper_pnl, 2),
        "paper_roi": _roi(paper_pnl),
        "ideal_pnl": round(ideal_pnl, 2),
        "ideal_roi": _roi(ideal_pnl),
        "real_pnl": round(real_pnl, 2),
        "real_roi": _roi(real_pnl),
        "real_pnl_net": round(real_pnl - paper_cost, 2),
        "real_roi_net": _roi(real_pnl - paper_cost),
        "cost_usd": round(paper_cost, 2),
        # The whole point: what execution costs this book, in ROI points.
        "execution_drag_roi": (
            _roi(real_pnl) - _roi(paper_pnl)
            if spent and paper_pnl is not None else None),
        "counterfactual": True,
    }


def replay(ledger_path: str, quote_rows: Optional[list[dict]] = None,
           min_opened_ts: Optional[float] = None) -> dict:
    """Re-settle a paper book at the observed real quote prices.

    Returns the paper figures and the counterfactual side by side, plus the
    join accounting: how many settled rows had a quote and how many did not,
    because a counterfactual computed on an unstated fraction of the book is
    not a comparison.
    """
    quotes = quote_map(quote_rows)
    positions = list(PaperCopyLedger(ledger_path).positions.values())
    return replay_positions(positions, quotes, min_opened_ts)
