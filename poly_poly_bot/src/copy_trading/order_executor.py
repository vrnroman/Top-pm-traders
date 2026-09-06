"""The single source of truth for what price a copy order would take.

There used to be two pricing paths in this repo and they disagreed. This
module held an "adaptive" one that capped the fill at ``trader_price * 1.02``
and posted via ``create_order``/``post_order``; the path that actually runs
(``trade_executor._execute_copy_order``) takes the raw ``best_ask`` with no cap
at all and posts via ``create_and_post_order``. Nothing imported this module
outside a smoke-test's import list, so the capped variant was dead code that
read like the live one — the worst kind, because the two answer "how much
worse is my entry price than the wallet I copied" in opposite directions
(cap = never chase, raw = always chase).

So this module is now the *pricing decision only*, extracted verbatim from the
live path, and both callers use it:

  * ``trade_executor._execute_copy_order`` — builds and posts the real order;
  * ``shadow_quote`` — measures what that order WOULD have paid, without
    placing it (PREVIEW measurement, ROADMAP §9.7 / the pre-flip question).

Keeping them on one function is the point: a shadow measurement that models
different pricing than the live executor is worse than no measurement, because
it reads as evidence. Change the pricing here and both move together.
"""

from __future__ import annotations

from typing import Optional

from src.models import DetectedTrade

# A CLOB limit price must sit strictly inside (0, 1). Markets tick in 0.1,
# 0.01, 0.001 or 0.0001; the tick is read from the book and the price is
# rounded TOWARD crossing on it.
PRICE_MIN = 0.01
PRICE_MAX = 0.99
PRICE_DP = 2
DEFAULT_TICK = 0.01


def _tick_of(snapshot: Optional[dict]) -> float:
    try:
        t = float((snapshot or {}).get("tick_size") or 0)
    except (TypeError, ValueError):
        t = 0.0
    return t if 0 < t < 1 else DEFAULT_TICK


def round_toward_crossing(price: float, side: str, tick: float) -> float:
    """Round a price onto the market's tick so a BUY is at or ABOVE the ask and
    a SELL at or BELOW the bid.

    Found by the first real order (2026-09-06): the executor rounded 0.984 to
    two decimals, posted a BUY at 0.980 against an ask of 0.984, and the order
    rested below the book until the verifier cancelled it two seconds later.
    Most football markets tick in thousandths, so every copy on them would have
    done the same: a deal refused for no reason anyone chose.
    """
    from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
    p = Decimal(str(price)); t = Decimal(str(tick))
    q = (p / t).quantize(Decimal("1"), rounding=ROUND_CEILING if side == "BUY" else ROUND_FLOOR)
    return float(q * t)


def quote_copy_order(
    side: str,
    trader_price: float,
    snapshot: Optional[dict],
) -> Optional[float]:
    """The limit price a copy order would be posted at, or None if unusable.

    Mirrors the live executor exactly: BUY lifts the current ``best_ask``,
    SELL hits the current ``best_bid``, and with no snapshot we fall back to
    the target's own price. Rounded onto the market's tick TOWARD crossing
    (see ``round_toward_crossing``), then validated: a price at or outside
    (0, 1) is not postable and returns None, the same branch the live path
    treats as "skip this trade".

    ``snapshot`` is the plain dict shape ``market_price.fetch_market_snapshot``
    returns (``best_bid`` / ``best_ask``, and ``tick_size`` when the book
    carried one), not the pydantic MarketSnapshot.
    """
    if side == "BUY":
        raw = snapshot.get("best_ask") if snapshot else None
    else:
        raw = snapshot.get("best_bid") if snapshot else None

    if raw is None or float(raw) <= 0:
        raw = trader_price

    try:
        order_price = round_toward_crossing(float(raw), side, _tick_of(snapshot))
    except (TypeError, ValueError):
        return None

    if order_price <= 0 or order_price >= 1:
        return None
    return order_price


def shares_for(copy_size: float, order_price: float) -> float:
    """Share count for a USD copy size at ``order_price`` (live path's rule)."""
    if order_price <= 0:
        return 0.0
    return copy_size / order_price


def entry_penalty_bps(our_price: float, their_price: float) -> Optional[int]:
    """How much worse our entry is than the copied wallet's, in bps.

    Positive = we paid MORE than they did (the normal, bad direction for a
    BUY); negative = we got in cheaper. Returns None when their price is
    unusable, so a missing input can never be silently scored as "no penalty".
    """
    if not their_price or their_price <= 0 or our_price <= 0:
        return None
    return int(round((our_price - their_price) / their_price * 10000))


def would_post(trade: DetectedTrade, snapshot: Optional[dict]) -> Optional[float]:
    """Convenience wrapper: the price this DetectedTrade would be posted at."""
    return quote_copy_order(trade.side, trade.price, snapshot)
