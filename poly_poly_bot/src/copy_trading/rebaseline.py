"""Re-baseline the paper ledgers on at-their-price economics (ROADMAP P0-2).

The 2026-07-25 analysis showed Book A's entire realized profit was gifted by
the fill simulator (39% of fills swept asks far under the target's price; that
+$535 was the book's +$537). Every settled row already carries ``ideal_pnl`` —
the PnL had it filled at the TARGET's own price — so history can be re-scored
honestly instead of discarded. This module is the pure computation behind
``scripts/rebaseline_ledger.py``: same numbers, so ``/pnl`` and the script
always reconcile (the roadmap's acceptance).

Two ROI numbers per book/wallet, side by side, forever:
  * realized       — what the ledger booked (includes fill-model effects);
  * at-their-price — the drag-free comparator the fill model cannot inflate.
The difference ("gifted") is exactly what the fills added or subtracted.
"""

from __future__ import annotations

from typing import Optional

from src.copy_trading.copy_paper import drag_stats, is_dust_fill


def _closed_clean(positions, min_opened_ts: Optional[float]):
    """Settled, non-dust rows, optionally floored to the clean era — the same
    sample rule every trust surface uses (dust rows are quarantined absurd
    fills; pre-floor rows stay visible, scoped by the caller)."""
    return [p for p in positions
            if p.closed and not is_dust_fill(p)
            and (min_opened_ts is None or (getattr(p, "opened_ts", 0) or 0)
                 >= min_opened_ts)]


def book_stats(positions, min_opened_ts: Optional[float] = None) -> dict:
    """Realized vs at-their-price for one book (a ledger's worth of positions)."""
    rows = _closed_clean(positions, min_opened_ts)
    spent = sum(p.spent for p in rows)
    pnl = sum(p.pnl for p in rows)
    ideal = sum(p.ideal_pnl for p in rows)
    wins = sum(1 for p in rows if p.won)
    return {
        "n": len(rows),
        "spent": round(spent, 2),
        "pnl": round(pnl, 2),
        "roi": (pnl / spent) if spent else None,
        "ideal_pnl": round(ideal, 2),
        "ideal_roi": (ideal / spent) if spent else None,
        # what the fill model added (positive = fills made it look BETTER):
        "gifted": round(pnl - ideal, 2),
        "hit_rate": (wins / len(rows)) if rows else None,
        "drag": drag_stats([int(p.drag_bps) for p in rows]),
    }


def per_wallet(positions, min_opened_ts: Optional[float] = None) -> list:
    """The same re-baseline per target wallet, biggest fill-gift first — the
    wallets whose "edge" was mostly the simulator sort to the top."""
    rows = _closed_clean(positions, min_opened_ts)
    by_wallet: dict = {}
    for p in rows:
        by_wallet.setdefault((p.target or "").lower(), []).append(p)
    out = []
    for w, ws in by_wallet.items():
        s = book_stats(ws)
        s["wallet"] = w
        out.append(s)
    out.sort(key=lambda s: -s["gifted"])
    return out
