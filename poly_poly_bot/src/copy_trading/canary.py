"""The canary: one minimum-size real order through the whole live path.

Owner-promoted to build-now, twice. Everything measured so far prices against
the live book but never touches the CLOB, so it cannot see the slippage from
our own order hitting the book, signing latency, or whether the matching
engine fills where the pricing function predicted. The canary closes that gap
with real money, bounded to one ticket at the exchange minimum.

Shape. ``stage()`` arms a one-shot. The next set-Z BUY that passes every
normal rail (Z membership, tier, bankroll governor, drift, spread) is placed
at the market's minimum size instead of the governor's per-copy size; the
runtime arm is pulled the moment the order is posted, so a second order cannot
follow; the fill report posts when the verifier sees the fill. Unfired after
24 hours it expires and says so once.

It requires every interlock key. Nothing here can place an order on its own:
it only changes the size of the one the live path would have placed anyway,
and stops the rest. Staging happens by default on the first ``/live CONFIRM``
that has never seen a canary fire, so the first live session is bounded to
one ticket unless the owner cancels that explicitly.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

from src.config import CONFIG
from src.copy_trading import live_budget, live_mode
from src.logger import logger

STATE_FILE = "canary.json"
TTL_S = 24 * 3600.0


def _path() -> str:
    return os.path.join(CONFIG.data_dir, STATE_FILE)


def read() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(d: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(_path()), exist_ok=True)
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, _path())
        return True
    except OSError as exc:
        logger.error(f"[canary] state write failed: {exc}")
        return False


def _num(x) -> float:
    try:
        return float(x or 0.0)
    except (TypeError, ValueError):
        return 0.0


def has_fired() -> bool:
    return bool(read().get("fired"))


def is_staged(now: Optional[float] = None) -> bool:
    """Staged, unfired, and inside its 24 hours."""
    d = read()
    if not d.get("staged") or d.get("fired"):
        return False
    now = time.time() if now is None else now
    return now < _num(d.get("expires_ts"))


def blocking_reasons() -> list[str]:
    """Why the canary cannot be staged right now, in plain words."""
    out = list(live_mode.blocking_reasons())
    if not live_budget.is_open():
        out.append("LIVE_BUDGET_USD not set (bankroll governor closed)")
    else:
        c = live_budget.caps(live=True)
        if c is not None and not c.tradeable:
            out.append(f"per copy ${c.per_copy_usd:.2f} is under the order minimum")
    return out


def stage(by: str = "telegram", now: Optional[float] = None,
          *, force_restage: bool = False) -> tuple[bool, str]:
    """Arm the one-shot. Refuses unless every interlock key is turned."""
    now = time.time() if now is None else now
    reasons = blocking_reasons()
    if reasons:
        return (False, "; ".join(reasons))
    d = read()
    if d.get("fired") and not force_restage:
        return (False, f"already fired at {_iso(d['fired'].get('placed_ts'))}; "
                       f"/canary RESET clears it so another can be staged")
    rec = {"staged": True, "staged_ts": now, "expires_ts": now + TTL_S,
           "by": by, "fired": None, "fill": None, "expired": False}
    if not _write(rec):
        return (False, "could not persist the canary record")
    logger.warn(f"[canary] STAGED by {by}: the next set-Z copy that passes the "
                f"rails goes out at minimum size, then the arm comes off")
    return (True, "staged")


def cancel(by: str = "telegram") -> bool:
    d = read()
    if not d.get("staged"):
        return False
    d["staged"] = False
    d["cancelled_by"] = by
    _write(d)
    logger.warn(f"[canary] cancelled by {by}: live copies size normally")
    return True


def reset(by: str = "telegram") -> None:
    """Clear a fired record so a new canary may be staged."""
    _write({"staged": False, "fired": None, "fill": None, "reset_by": by,
            "reset_ts": time.time()})


def expire_if_due(send=None, now: Optional[float] = None) -> bool:
    """Expire a stale unfired canary and say so ONCE. Returns True when it did."""
    d = read()
    if not d.get("staged") or d.get("fired"):
        return False
    now = time.time() if now is None else now
    if now < _num(d.get("expires_ts")):
        return False
    d["staged"] = False
    d["expired"] = True
    d["expired_ts"] = now
    _write(d)
    msg = ("🐤 Canary expired unfired after 24h: no set-Z copy passed the rails "
           "while it was staged. /canary CONFIRM stages it again.")
    logger.warn(f"[canary] {msg}")
    if send is not None:
        try:
            send(msg)
        except Exception as exc:
            logger.warn(f"[canary] expiry message failed: {exc}")
    return True


def size_for(clob_client, token_id: str, order_price: float) -> float:
    """The smallest order this market accepts, in USD, never under the bot's
    own order minimum. Falls back to the bot minimum when the book cannot be
    read, so the canary cannot silently grow past a ticket."""
    floor = float(CONFIG.min_order_size_usd)
    try:
        book = clob_client.get_order_book(token_id)
        min_shares = _num(getattr(book, "min_order_size", None))
        if min_shares > 0 and order_price > 0:
            return round(max(floor, min_shares * float(order_price)), 2)
    except Exception as exc:
        logger.warn(f"[canary] min order size unreadable, using the bot minimum: {exc}")
    return round(floor, 2)


def record_fired(*, order_id: str, market: str, token_id: str,
                 their_price: float, quoted_ask: Optional[float],
                 order_price: float, copy_size: float,
                 notify_latency_s: Optional[float], now: Optional[float] = None) -> dict:
    """The one order went out. Staged flips off here, whatever the fill does."""
    now = time.time() if now is None else now
    d = read()
    d["staged"] = False
    d["fired"] = {
        "order_id": order_id, "market": market, "token_id": token_id,
        "their_price": their_price, "quoted_ask": quoted_ask,
        "order_price": order_price, "copy_size": copy_size,
        "notify_latency_s": notify_latency_s, "placed_ts": now,
    }
    d["fill"] = None
    _write(d)
    logger.warn(f"[canary] FIRED: {order_id[:12]} ${copy_size:.2f} on "
                f"'{market[:40]}' at {order_price:.4f} (theirs {their_price:.4f}); "
                f"the arm comes off now")
    return d["fired"]


def record_fill(order_id: str, fill, now: Optional[float] = None) -> Optional[str]:
    """The verifier saw the order's fate. Returns the report text ONCE."""
    d = read()
    fired = d.get("fired") or {}
    if not fired or fired.get("order_id") != order_id or d.get("fill"):
        return None
    status = str(getattr(fill, "status", "") or "")
    if not status or status == "UNKNOWN":
        return None
    now = time.time() if now is None else now
    d["fill"] = {
        "status": status,
        "fill_price": _num(getattr(fill, "fill_price", 0.0)),
        "filled_shares": _num(getattr(fill, "filled_shares", 0.0)),
        "filled_usd": _num(getattr(fill, "filled_usd", 0.0)),
        "ts": now,
    }
    _write(d)
    return report_text(d)


def _iso(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(ts)))
    except (TypeError, ValueError):
        return "?"


def report_text(d: Optional[dict] = None) -> str:
    """What the one real order actually did, next to what was predicted."""
    d = read() if d is None else d
    f = d.get("fired") or {}
    fill = d.get("fill") or {}
    if not f:
        return "🐤 Canary: not fired yet."
    lines = [f"🐤 <b>Canary report</b>, order {f.get('order_id', '')[:12]}",
             f"market: {f.get('market', '')[:60]}",
             f"their price {_num(f.get('their_price')):.4f} · quoted ask at detection "
             f"{_num(f.get('quoted_ask')):.4f} · our order {_num(f.get('order_price')):.4f} "
             f"· ${_num(f.get('copy_size')):.2f}"]
    lat = f.get("notify_latency_s")
    if lat is not None:
        lines.append(f"told {_num(lat):.1f}s after their trade")
    if fill:
        fp = _num(fill.get("fill_price"))
        theirs = _num(f.get("their_price"))
        pen = ((fp - theirs) / theirs * 10000.0) if (theirs > 0 and fp > 0) else None
        lines.append(f"fill: {fill.get('status')} · {_num(fill.get('filled_shares')):.2f} "
                     f"shares at {fp:.4f} (${_num(fill.get('filled_usd')):.2f})")
        lines.append(f"entry penalty vs their price: "
                     f"{pen:+.0f}bps" if pen is not None else "entry penalty: n/a (no fill price)")
    else:
        lines.append("fill: waiting for the verifier")
    lines.append("The arm came off when this order was posted. /live CONFIRM arms again.")
    return "\n".join(lines)


def status_lines() -> list[str]:
    """Plain lines for /canary. The caller escapes them."""
    d = read()
    now = time.time()
    out: list[str] = []
    if is_staged(now):
        left = (_num(d.get("expires_ts")) - now) / 3600
        out.append(f"staged by {d.get('by', '?')} at {_iso(d.get('staged_ts'))}, "
                   f"expires in {left:.1f}h")
    elif d.get("fired"):
        out.append(f"fired at {_iso(d['fired'].get('placed_ts'))}; "
                   f"{'fill recorded' if d.get('fill') else 'fill pending'}")
    elif d.get("expired"):
        out.append(f"expired unfired at {_iso(d.get('expired_ts'))}")
    else:
        out.append("not staged")
    reasons = blocking_reasons()
    if reasons:
        out.append("cannot stage now: " + "; ".join(reasons))
    return out
