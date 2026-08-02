"""Strategy 1 (copy trading) P&L aggregation.

Copy-trading P&L has two independent pieces:

  * **Realized** — locked in when a resolved market is redeemed. The redeemer
    (``auto_redeemer``) is the only place a copy position is closed, so it
    appends one row per redemption to ``data/realized-pnl.jsonl`` and we sum
    those rows here. A ledger (rather than re-deriving from the live positions
    API) is deliberate: it survives the position dropping out of the API once
    redeemed, and it survives bot restarts.

  * **Unrealized** — open positions marked to market. Inventory tracks shares
    and weighted-average cost; we value each at the current price. Redeemed
    positions are removed from inventory by the redeemer, so realized and
    unrealized never double-count the same position.

The aggregation is pure (prices and ledger rows are injected), so it unit-tests
without network or disk. The thin file-I/O wrappers compute their path from
``CONFIG.data_dir`` at call time so tests can point them at a tmp dir.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Callable, Optional

from src.config import CONFIG
from src.logger import logger


# --------------------------------------------------------------------------- #
# Realized-P&L ledger (one row per redeemed position)
# --------------------------------------------------------------------------- #

def realized_pnl_path() -> str:
    return os.path.join(CONFIG.data_dir, "realized-pnl.jsonl")


def _settled_resolution_keys(rows: list[dict]) -> set[tuple[str, str]]:
    """(condition_id, token_id) of every resolution exit already in the ledger."""
    return {(str(r.get("condition_id") or ""), str(r.get("token_id") or ""))
            for r in rows if r.get("exit") == "resolution"}


def append_realized(entry: dict) -> None:
    """Append a realized-P&L row. Best-effort: never raises into the caller.

    A resolution exit is a one-per-position fact: a resolved market can never
    be re-bought, so (condition_id, token_id) + ``exit=resolution`` is a unique
    settled identity. The 2026-07-30 incident: inventory was re-populated with
    61 already-realized legacy positions and the preview resolver re-booked the
    whole batch — every all-time sum over the file double-counted −$575 until
    scrubbed. Guard here (the single funnel every writer goes through) rather
    than in each caller. Non-resolution exits (live sells) may legitimately
    repeat a token, so they are never deduped.
    """
    if entry.get("exit") == "resolution":
        key = (str(entry.get("condition_id") or ""), str(entry.get("token_id") or ""))
        if key in _settled_resolution_keys(load_realized()):
            logger.warning(
                f"[pnl] dedup: resolution exit already booked for "
                f"{key[0][:12]}…/{key[1][:12]}… — skipping duplicate append")
            return
    path = realized_pnl_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:  # noqa: BLE001
        logger.error(f"[pnl] Failed to append realized P&L: {e}")


def load_realized() -> list[dict]:
    """Load all realized-P&L rows. Missing file -> empty list."""
    path = realized_pnl_path()
    if not os.path.exists(path):
        return []
    rows: list[dict] = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as e:  # noqa: BLE001
        logger.warn(f"[pnl] Failed to read realized P&L ledger: {e}")
    return rows


# --------------------------------------------------------------------------- #
# Aggregation (pure)
# --------------------------------------------------------------------------- #

@dataclass
class OpenPositionPnl:
    token_id: str
    market: str
    shares: float
    avg_price: float
    cur_price: Optional[float]
    cost: float                       # shares * avg_price
    value: float                      # mark-to-market value (0 if unpriced)
    unrealized_pnl: Optional[float]   # None when no live price available
    unrealized_pct: Optional[float]
    tier: str = ""                    # strategy tier (1a/1b/1c) stamped at buy time
    trader_address: str = ""          # followed wallet whose trade we copied


@dataclass
class Strategy1Pnl:
    realized_pnl: float = 0.0
    realized_wins: int = 0
    realized_losses: int = 0
    unrealized_pnl: float = 0.0
    open_positions: int = 0
    cost_basis: float = 0.0           # cost of open positions
    market_value: float = 0.0         # current value of priced open positions
    priced: int = 0                   # open positions we could value
    unpriced: int = 0                 # open positions with no live price
    positions: list[OpenPositionPnl] = field(default_factory=list)

    @property
    def net_pnl(self) -> float:
        return round(self.realized_pnl + self.unrealized_pnl, 2)

    @property
    def unrealized_roi(self) -> Optional[float]:
        return (self.unrealized_pnl / self.cost_basis) if self.cost_basis else None

    @property
    def realized_trades(self) -> int:
        return self.realized_wins + self.realized_losses

    @property
    def hit_rate(self) -> Optional[float]:
        n = self.realized_trades
        return (self.realized_wins / n) if n else None


def summarize_realized(rows: list[dict]) -> tuple[float, int, int]:
    """Sum realized rows -> (total_pnl, wins, losses).

    ``won`` is taken from the row when present; otherwise inferred from the
    sign of ``pnl`` (a break-even row counts as neither win nor loss).
    """
    total = 0.0
    wins = losses = 0
    for r in rows:
        pnl = float(r.get("pnl", 0.0) or 0.0)
        total += pnl
        won = r.get("won")
        if won is True or (won is None and pnl > 0):
            wins += 1
        elif won is False or (won is None and pnl < 0):
            losses += 1
    return round(total, 2), wins, losses


def value_open_positions(
    positions: dict[str, dict],
    price_fn: Callable[[str], Optional[float]],
    *,
    fee: float = 0.0,
) -> list[OpenPositionPnl]:
    """Mark each open inventory position to market.

    ``positions`` is the inventory shape: ``{token_id: {shares, avg_price,
    market, ...}}``. ``price_fn`` returns the current price for a token (or
    ``None`` if unavailable). ``fee`` is an optional exit fee applied to the
    sell value; copy positions are held to fee-free on-chain redemption, so it
    defaults to 0 (mark-to-midpoint).
    """
    out: list[OpenPositionPnl] = []
    for token_id, pos in positions.items():
        shares = float(pos.get("shares", 0) or 0)
        avg = float(pos.get("avg_price", 0) or 0)
        if shares <= 0:
            continue
        cost = shares * avg
        cur = price_fn(token_id)
        if cur is not None:
            value = shares * cur * (1 - fee)
            upnl = value - cost
            upct = (upnl / cost) if cost > 0 else 0.0
        else:
            value = 0.0
            upnl = None
            upct = None
        out.append(OpenPositionPnl(
            token_id=token_id,
            market=pos.get("market", "") or "",
            shares=shares,
            avg_price=avg,
            cur_price=cur,
            cost=cost,
            value=value,
            unrealized_pnl=upnl,
            unrealized_pct=upct,
            tier=pos.get("tier", "") or "",
            trader_address=pos.get("trader_address", "") or "",
        ))
    return out


def summarize(
    realized_rows: list[dict],
    open_positions: list[OpenPositionPnl],
) -> Strategy1Pnl:
    """Combine the realized ledger and marked-to-market open positions."""
    realized, wins, losses = summarize_realized(realized_rows)
    s = Strategy1Pnl(
        realized_pnl=realized,
        realized_wins=wins,
        realized_losses=losses,
        open_positions=len(open_positions),
        positions=open_positions,
    )
    for p in open_positions:
        s.cost_basis += p.cost
        if p.unrealized_pnl is not None:
            s.unrealized_pnl += p.unrealized_pnl
            s.market_value += p.value
            s.priced += 1
        else:
            s.unpriced += 1
    s.unrealized_pnl = round(s.unrealized_pnl, 2)
    s.cost_basis = round(s.cost_basis, 2)
    s.market_value = round(s.market_value, 2)
    return s
