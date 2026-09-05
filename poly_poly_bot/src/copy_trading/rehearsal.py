"""The rehearsal ledger: what the owner's actual bankroll would have made.

Every figure so far is a percentage on paper books of $30k to $111k. He is
about to risk about $310. So this re-runs the counterfactual for the set-Z
wallets at exactly his caps: each copy sized at the governor's per-copy cap,
the daily and open-exposure caps binding as they would live, every entry
settled at the real quoted book. The answer is in dollars, per wallet and in
total, with the window's effective start stated (real quotes only exist from
the day the prober went live).

Labelled counterfactual, forever. It never merges into /pnl or the verdict
memo, and it carries no verdict text of its own: a plain record of what the
same month would have done to his money, next to what the paper book said.
"""

from __future__ import annotations

import heapq
import time
from datetime import datetime, timezone
from typing import Iterable, Optional

from src.config import CONFIG
from src.copy_trading import live_budget, virtual_ledger
from src.logger import logger

# Below this many taken copies a wallet's dollar figure is shown as thin.
THIN_N = 5


def _num(x) -> float:
    try:
        return float(x or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def rehearse(*, budget_usd: float, positions, quotes: dict,
             wallets: Iterable[str], since_ts: float,
             era: Optional[float] = None) -> dict:
    """Replay the wallets' copies at the owner's caps, at real quotes.

    Copies without a usable real quote are counted, never simulated. Caps
    bind in time order: a copy that would push open exposure past the cap,
    or the UTC day's spend past its cap, is held and counted as held.
    """
    per_copy = round(budget_usd * live_budget.PER_COPY_FRAC, 2)
    daily_cap = round(budget_usd * live_budget.DAILY_FRAC, 2)
    exposure_cap = round(budget_usd * live_budget.EXPOSURE_FRAC, 2)
    keys = {w.lower() for w in wallets}
    start = max(float(era or 0.0), float(since_ts))

    rows = [p for p in virtual_ledger.settled_rows(positions, start)
            if (getattr(p, "target", "") or "").lower() in keys]
    rows.sort(key=lambda p: _num(getattr(p, "opened_ts", 0.0)))

    per: dict = {}
    for w in keys:
        per[w] = {"n_settled": 0, "n_matched": 0, "n_taken": 0, "n_held": 0,
                  "spent": 0.0, "real_pnl": 0.0, "ideal_pnl": 0.0, "paper_roi_num": 0.0,
                  "paper_spent": 0.0}
    open_heap: list = []          # (closed_ts, spent)
    open_total = 0.0
    day_spent: dict = {}
    first_ts: Optional[float] = None

    for p in rows:
        w = (getattr(p, "target", "") or "").lower()
        d = per[w]
        d["n_settled"] += 1
        d["paper_roi_num"] += _num(getattr(p, "ideal_pnl", 0.0))
        d["paper_spent"] += _num(getattr(p, "spent", 0.0))
        cid = getattr(p, "copy_id", None)
        if cid not in quotes:
            continue
        d["n_matched"] += 1
        opened = _num(getattr(p, "opened_ts", 0.0))
        closed = _num(getattr(p, "closed_ts", 0.0)) or opened
        first_ts = opened if first_ts is None else min(first_ts, opened)
        while open_heap and open_heap[0][0] <= opened:
            _, s = heapq.heappop(open_heap)
            open_total -= s
        day = _day(opened)
        if open_total + per_copy > exposure_cap + 1e-9 or \
                day_spent.get(day, 0.0) + per_copy > daily_cap + 1e-9:
            d["n_held"] += 1
            continue
        d["n_taken"] += 1
        d["spent"] += per_copy
        day_spent[day] = day_spent.get(day, 0.0) + per_copy
        heapq.heappush(open_heap, (max(closed, opened), per_copy))
        open_total += per_copy
        d["real_pnl"] += virtual_ledger.real_pnl_for(p, quotes[cid], spent=per_copy)
        paper_spent = _num(getattr(p, "spent", 0.0))
        ideal_roi = (_num(getattr(p, "ideal_pnl", 0.0)) / paper_spent) if paper_spent > 0 else 0.0
        d["ideal_pnl"] += per_copy * ideal_roi

    total = {"n_taken": 0, "n_held": 0, "spent": 0.0, "real_pnl": 0.0, "ideal_pnl": 0.0}
    for d in per.values():
        for k in total:
            total[k] += d[k]
        d["thin"] = d["n_taken"] < THIN_N
        for k in ("spent", "real_pnl", "ideal_pnl"):
            d[k] = round(d[k], 2)
    for k in ("spent", "real_pnl", "ideal_pnl"):
        total[k] = round(total[k], 2)
    return {"budget_usd": budget_usd, "per_copy_usd": per_copy,
            "daily_cap_usd": daily_cap, "exposure_cap_usd": exposure_cap,
            "since_ts": start, "first_matched_ts": first_ts,
            "wallets": per, "total": total, "counterfactual": True}


def render(res: dict) -> str:
    """Plain lines, no verdict text."""
    b = res["budget_usd"]
    head = (f"🎯 <b>Rehearsal at your caps</b> (counterfactual, not a book): "
            f"${b:,.0f} budget, ${res['per_copy_usd']:.2f} a copy, "
            f"${res['daily_cap_usd']:.0f} a day, ${res['exposure_cap_usd']:.0f} open")
    lines = [head]
    if res["first_matched_ts"]:
        lines.append(f"window from {_day(res['first_matched_ts'])} (first real quote), "
                     f"asked from {_day(res['since_ts'])}")
    else:
        lines.append(f"window asked from {_day(res['since_ts'])}: no copy with a real quote yet")
    for w, d in sorted(res["wallets"].items(), key=lambda kv: -kv[1]["n_taken"]):
        tag = f"<code>{w[:10]}…</code>"
        if d["n_taken"] == 0:
            lines.append(f"{tag}: {d['n_settled']} settled, {d['n_matched']} with a real "
                         f"quote, none taken")
            continue
        thin = " · thin, under 5 taken" if d["thin"] else ""
        lines.append(f"{tag}: {d['n_taken']} taken, {d['n_held']} held by caps, "
                     f"{d['n_settled'] - d['n_matched']} unquoted → "
                     f"${d['real_pnl']:+,.2f} at real quotes, ${d['ideal_pnl']:+,.2f} at "
                     f"their price, on ${d['spent']:,.0f} cycled{thin}")
    t = res["total"]
    lines.append(f"total: {t['n_taken']} copies, ${t['spent']:,.0f} cycled → "
                 f"<b>${t['real_pnl']:+,.2f}</b> at real quotes, ${t['ideal_pnl']:+,.2f} "
                 f"at their price")
    return "\n".join(lines)


def daily_message(now: Optional[float] = None) -> str:
    """The two month-one lines for the 08:00 block: the rehearsal for set Z
    and the real-money line. Fails soft: a line that cannot be computed says
    so instead of rendering a zero."""
    now = time.time() if now is None else now
    parts: list[str] = []
    try:
        from src.copy_trading import era_state, shadow_quote, zset
        from src.copy_trading.copy_paper import PaperCopyLedger
        import os as _os
        budget = live_budget.stated_budget()
        wallets = zset.wallets()
        if budget is None:
            parts.append("🎯 rehearsal: LIVE_BUDGET_USD not set, nothing to size")
        elif not wallets:
            parts.append("🎯 rehearsal: set Z is empty, nothing to rehearse")
        else:
            era = era_state.era_floor_ts(_os.path.join(CONFIG.data_dir, "ab_race_state.json"))
            positions = list(PaperCopyLedger(CONFIG.copy_paper_b_ledger).positions.values())
            quotes = virtual_ledger.quote_map(shadow_quote.load_rows(since_ts=now - 31 * 86400))
            res = rehearse(budget_usd=budget, positions=positions, quotes=quotes,
                           wallets=wallets, since_ts=now - 30 * 86400, era=era)
            parts.append(render(res))
    except Exception as exc:
        logger.warning(f"[rehearsal] failed: {exc}")
        parts.append(f"🎯 rehearsal: could not compute ({exc})")
    parts.append(real_money_line(now=now))
    return "\n\n".join(parts)


def real_money_line(now: Optional[float] = None) -> str:
    """Bankroll now, distance to the floor, realized today. Real rows only.

    Renders figures only after the first arm; before that it says so, never
    a $0.00 that reads like a measurement.
    """
    now = time.time() if now is None else now
    from src.copy_trading import live_mode
    arm = live_mode.read_arm()
    if not arm.get("first_armed_ts"):
        return "💵 real money: not armed yet, no real-money figures"
    try:
        from src.copy_trading import inventory, pnl
        bal = live_budget._read_balance(now)
        open_cost = float(inventory.get_inventory_summary().get("total_cost_basis_usd", 0.0) or 0.0)
        floor = live_budget.floor_usd()
        today = _day(now)
        rows = [r for r in pnl.load_realized()
                if r.get("source") == "redeemer"
                and str(r.get("timestamp", ""))[:10] == today]
        realized = round(sum(_num(r.get("pnl")) for r in rows), 2)
        if bal is None:
            return (f"💵 real money: balance unreadable · open at cost ${open_cost:,.2f} · "
                    f"realized today ${realized:+,.2f} ({len(rows)} redeem(s))")
        equity = live_budget.equity_usd(bal, open_cost)
        floor_txt = (f" · floor ${floor:,.0f} · distance ${equity - floor:+,.2f}"
                     if floor is not None else " · no floor (LIVE_BUDGET_USD unset)")
        return (f"💵 real money: bankroll ${equity:,.2f} (USDC ${bal:,.2f} + open at cost "
                f"${open_cost:,.2f}){floor_txt} · realized today ${realized:+,.2f} "
                f"({len(rows)} redeem(s))")
    except Exception as exc:
        return f"💵 real money: could not compute ({exc})"
