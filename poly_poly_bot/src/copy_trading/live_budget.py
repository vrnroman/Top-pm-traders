"""The bankroll governor: every live cap as a fraction of the money that is there.

Why this exists (2026-09-05). The live tier's caps were sized for a bankroll
that does not exist: $25 a copy, $200 open, $500 a day, on a proxy wallet that
holds nothing and an owner budget of about $310. Every one of those caps was
larger than the whole bankroll, so none of them capped anything. Worse, the
tier only copied a target's trade when it was $10,000 or more, while the
evidence that earned the go-live (paper book B) copies every buy from $300.
The live wire and the evidence described two different strategies.

So one number, ``LIVE_BUDGET_USD``, now derives every live cap, and the copy
trigger is the evidence base's own (``COPY_PAPER_MIN_USD``), so the strategy
that trades is the strategy that was measured.

Three rules, in the order they matter:

* **Closed when absent.** No ``LIVE_BUDGET_USD`` means no live sizing: every
  live copy is refused with a reason that names the knob. A preview session
  keeps rehearsing on the tier's own caps, because a preview with nothing to
  size rehearses nothing.
* **Never more than what is there.** Live, the effective budget is
  min(stated, on-chain USDC). The stated number is the owner's intent; the
  chain is the fact. If the balance cannot be read, the stated number is used
  and the panel says so.
* **Fractions, not dollars.** Per copy 2.5%, per market 5% (two copies a side,
  the executor's existing rule), per day 30%, open at once 80%. The shape is
  the owner's rubric (bounded downside per shot, many small shots behind one
  cap); the exact fractions are a stated default, each an env knob, not a
  measured optimum.

A per-copy size under the exchange minimum is NOT rounded up: a $150 bankroll
would then put 3.3% on every ticket instead of 2.5%, quietly. It is left under
the floor, every copy is refused, and the panel says what budget would open it.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, replace
from typing import Optional

from src.config import CONFIG
from src.copy_trading import live_mode
from src.logger import logger


def _frac(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        v = float(raw) if raw else default
    except ValueError:
        v = default
    return v if 0 < v <= 1 else default


PER_COPY_FRAC = _frac("LIVE_BUDGET_PER_COPY_FRAC", 0.025)
DAILY_FRAC = _frac("LIVE_BUDGET_DAILY_FRAC", 0.30)
EXPOSURE_FRAC = _frac("LIVE_BUDGET_EXPOSURE_FRAC", 0.80)
# The floor: a drawdown from the STATED budget, not a trailing high-water
# mark. On a $310 book with $8 tickets a trailing mark fires on variance and
# the owner would be re-arming weekly; a fixed floor fires on a strategy
# signal (roughly eight to twelve net losing tickets). 30% is a stated
# default, not a measured optimum.
DRAWDOWN_FRAC = _frac("LIVE_BUDGET_DRAWDOWN_FRAC", 0.30)

# The chain is read by the guard thread (every pass, ~300s) and by the
# Telegram thread on demand; the executor's asyncio loop only ever reads the
# cache, because a blocking web3 call there stalls detection, verification
# and order placement together. A value older than the TTL counts as
# unreadable, which falls back to the stated budget and says so.
_BALANCE_TTL_S = 900.0
_balance_cache: Optional[tuple[float, Optional[float]]] = None
# The guard loop's last KNOWN cost of still-live positions (resolved ones
# excluded), so a copy that just opened does not shrink the next one. Same
# TTL as the balance; stale means "unknown", which sizes on cash alone.
_open_cost_cache: Optional[tuple[float, float]] = None


@dataclass(frozen=True)
class Caps:
    stated_usd: float
    balance_usd: Optional[float]   # cash on chain, when it was asked
    balance_read: bool             # a live read was attempted and succeeded
    open_cost_usd: float           # live positions at cost + resolved at payout, 0 if unknown
    effective_usd: float
    per_copy_usd: float
    per_market_usd: float
    daily_usd: float
    exposure_usd: float
    min_trader_bet_usd: float
    live: bool

    @property
    def tradeable(self) -> bool:
        """Is one copy at this budget even placeable at the order minimum?"""
        return self.per_copy_usd >= float(CONFIG.min_order_size_usd)

    @property
    def opening_budget_usd(self) -> float:
        """The smallest budget at which a copy clears the order minimum."""
        return round(float(CONFIG.min_order_size_usd) / PER_COPY_FRAC, 2)


def stated_budget() -> Optional[float]:
    """LIVE_BUDGET_USD as a positive number, or None when unset."""
    try:
        v = float(getattr(CONFIG, "live_budget_usd", 0.0) or 0.0)
    except (TypeError, ValueError):
        v = 0.0
    return v if v > 0 else None


def is_open() -> bool:
    return stated_budget() is not None


def refresh_balance(now: Optional[float] = None) -> Optional[float]:
    """Read the proxy wallet's USDC from the chain and cache it. BLOCKING:
    call from a thread that may wait (the guard loop, the Telegram thread),
    never from the executor's asyncio loop."""
    global _balance_cache
    now = time.time() if now is None else now
    try:
        from src.copy_trading.get_balance import get_usdc_balance
        b = float(get_usdc_balance())
        val: Optional[float] = b if b >= 0 else None
    except Exception as exc:
        logger.warn(f"[budget] balance read failed: {exc}")
        val = None
    _balance_cache = (now, val)
    return val


def note_open_cost(open_cost_usd: float, now: Optional[float] = None) -> None:
    """The guard loop's live open cost, when it could read the resolved set."""
    global _open_cost_cache
    now = time.time() if now is None else now
    try:
        v = float(open_cost_usd)
    except (TypeError, ValueError):
        return
    _open_cost_cache = (now, max(0.0, v))


def _read_open_cost(now: Optional[float] = None) -> Optional[float]:
    now = time.time() if now is None else now
    if _open_cost_cache is not None and now - _open_cost_cache[0] < _BALANCE_TTL_S:
        return _open_cost_cache[1]
    return None


def _read_balance(now: Optional[float] = None) -> Optional[float]:
    """The cached USDC balance, or None when nothing fresh is cached. Never
    touches the network."""
    now = time.time() if now is None else now
    if _balance_cache is not None and now - _balance_cache[0] < _BALANCE_TTL_S:
        return _balance_cache[1]
    return None


def caps(*, live: Optional[bool] = None, balance: Optional[float] = None,
         now: Optional[float] = None) -> Optional[Caps]:
    """The caps a copy is sized under right now, or None when closed.

    ``live`` defaults to the interlock's answer. ``balance`` lets a caller (or
    a drill) supply the chain's number instead of reading it.
    """
    stated = stated_budget()
    if stated is None:
        return None
    if live is None:
        live = not live_mode.is_preview()
    bal: Optional[float] = None
    read = False
    open_cost = 0.0
    if live:
        bal = balance if balance is not None else _read_balance(now)
        read = bal is not None
        oc = _read_open_cost(now)
        open_cost = float(oc) if oc is not None else 0.0
    # Equity, not cash: cash falls by the size of every position the bot
    # opens, so sizing on cash alone shrank the per-copy size under the $5
    # minimum after three copies and the trader went silent by design
    # (2026-09-06). Live positions count at cost, resolved ones at their
    # payout (the guard hands over the sum). The stated budget still caps
    # everything, and the sink checks cash before a post.
    effective = min(stated, bal + open_cost) if (live and read) else stated
    per_copy = round(effective * PER_COPY_FRAC, 2)
    per_market = round(per_copy * max(1, int(CONFIG.max_copies_per_market_side)), 2)
    return Caps(
        stated_usd=stated, balance_usd=bal, balance_read=read,
        open_cost_usd=round(open_cost, 2),
        effective_usd=effective, per_copy_usd=per_copy,
        per_market_usd=per_market,
        daily_usd=round(effective * DAILY_FRAC, 2),
        exposure_usd=round(effective * EXPOSURE_FRAC, 2),
        min_trader_bet_usd=float(CONFIG.copy_paper_min_usd),
        live=live,
    )


def govern_tier(cfg, *, live: Optional[bool] = None,
                balance: Optional[float] = None):
    """The tier config a copy is sized under, and why it is closed if it is.

    Returns ``(config, reason)``. ``reason`` is None when sizing may proceed.
    The governed config never raises a cap above the tier's own; it only
    lowers them, and it replaces the trigger threshold with the evidence
    base's so live copies the trades the paper book measured.
    """
    if live is None:
        live = not live_mode.is_preview()
    c = caps(live=live, balance=balance)
    if c is None:
        if live:
            return (cfg, "bankroll governor closed: LIVE_BUDGET_USD is not set, "
                         "so no live copy is sized")
        return (cfg, None)  # preview rehearses on the tier's own caps
    if not c.tradeable:
        return (cfg, f"bankroll governor: per copy ${c.per_copy_usd:.2f} "
                     f"({PER_COPY_FRAC * 100:g}% of ${c.effective_usd:,.0f}) is "
                     f"under the ${float(CONFIG.min_order_size_usd):.0f} order "
                     f"minimum; nothing trades until the budget is at least "
                     f"${c.opening_budget_usd:,.0f}")
    governed = replace(
        cfg,
        max_bet=min(float(cfg.max_bet), c.per_copy_usd),
        max_total_exposure=min(float(cfg.max_total_exposure), c.exposure_usd),
        min_trader_bet=c.min_trader_bet_usd,
    )
    return (governed, None)


def daily_cap(*, live: Optional[bool] = None) -> float:
    """The per-UTC-day spend cap: the env cap, lowered by the governor."""
    base = float(CONFIG.max_daily_volume_usd)
    c = caps(live=live)
    return min(base, c.daily_usd) if c is not None else base


def floor_usd() -> Optional[float]:
    """The bankroll below which the guard pulls the arm, or None when closed."""
    stated = stated_budget()
    if stated is None:
        return None
    return round(stated * (1.0 - DRAWDOWN_FRAC), 2)


def live_open_cost(summary: dict,
                   redeemable: Optional[list]) -> tuple[float, int, bool]:
    """Cost basis of positions that are still LIVE, and how many were dropped.

    A position the chain has already resolved is not worth its cost any more;
    its value is its payout, which shows up as USDC when it is redeemed. The
    funder wallet on this box carried 61 resolved losers at a cost basis
    several times the month's budget, so counting them made the bankroll read
    far above the floor and the floor could not fire with ZERO USDC on chain.

    Returns ``(live_cost, n_excluded, known)``. ``known`` is False when the
    resolved set could not be read, in which case the caller is told the
    number is the old, inflated one rather than being handed a guess.
    """
    positions = (summary or {}).get("positions") or {}
    total = float((summary or {}).get("total_cost_basis_usd") or 0.0)
    if redeemable is None:
        return (round(total, 2), 0, False)
    if total > 0 and not positions:
        # A cost basis with no per-position rows cannot be attributed, so
        # nothing can be excluded. Returning 0 here would UNDERSTATE the
        # bankroll and fire the floor on a false positive, and a gate that
        # fires on a false positive is worse than the bug it guards.
        return (round(total, 2), 0, False)
    done = set()
    for r in redeemable or []:
        for key in ("tokenId", "asset", "token_id"):
            v = (r or {}).get(key) if isinstance(r, dict) else None
            if v:
                done.add(str(v))
    live, excluded = 0.0, 0
    for tid, p in positions.items():
        if str(tid) in done:
            excluded += 1
            continue
        try:
            live += float((p or {}).get("cost_basis") or 0.0)
        except (TypeError, ValueError):
            continue
    return (round(live, 2), excluded, True)


def equity_usd(balance: Optional[float], open_cost_usd: float) -> Optional[float]:
    """Realized equity: USDC on chain plus STILL-LIVE positions at cost.

    At cost, never marked, so it cannot fire on a deployment or flap on
    quotes; only a settled loss or a redeem moves it. The caller supplies a
    cost basis that already excludes resolved positions (see live_open_cost);
    counting those was what made the floor inert on the real wallet.
    """
    if balance is None:
        return None
    return round(float(balance) + float(open_cost_usd or 0.0), 2)


def per_copy_cap(*, live: Optional[bool] = None) -> Optional[float]:
    """The per-copy ceiling, or None when the governor is closed."""
    c = caps(live=live)
    return c.per_copy_usd if c is not None else None


def status_lines(*, live: Optional[bool] = None) -> list[str]:
    """Plain-text lines for the Telegram panels. The caller escapes them."""
    if live is None:
        live = not live_mode.is_preview()
    c = caps(live=live)
    if c is None:
        return ["❌ bankroll governor: LIVE_BUDGET_USD is not set. Live copies "
                "are refused; preview runs on the tier's own caps."]
    if not c.live:
        basis = "preview, chain not read"
    elif c.balance_read:
        basis = (f"min of ${c.stated_usd:,.0f} stated and ${c.balance_usd:,.2f} cash "
                 f"+ ${c.open_cost_usd:,.2f} in open positions")
    else:
        basis = "chain balance unreadable, using the stated number"
    out = [f"✅ bankroll governor: ${c.effective_usd:,.2f} effective ({basis})",
           f"  per copy ${c.per_copy_usd:.2f} · per market ${c.per_market_usd:.2f} "
           f"· per day ${c.daily_usd:.2f} · open at once ${c.exposure_usd:.2f} "
           f"· copies target buys from ${c.min_trader_bet_usd:,.0f}"]
    if not c.tradeable:
        out.append(f"  ⚠️ per copy ${c.per_copy_usd:.2f} is under the "
                   f"${float(CONFIG.min_order_size_usd):.0f} order minimum: "
                   f"nothing trades until the budget is at least "
                   f"${c.opening_budget_usd:,.0f}")
    return out
