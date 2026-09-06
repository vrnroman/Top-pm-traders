"""The canary: one minimum-size real order through the whole live path.

Owner-promoted to build-now, twice. Everything measured so far prices against
the live book but never touches the CLOB, so it cannot see the slippage from
our own order hitting the book, signing latency, or whether the matching
engine fills where the pricing function predicted. The canary closes that gap
with real money, bounded to one ticket at the exchange minimum.

Shape. ``stage()`` arms a one-shot. The next set-Z BUY that passes every
normal rail (Z membership, tier, bankroll governor, drift, spread) is placed
at the $5 order minimum (or the market's own minimum if that is higher)
instead of the governor's per-copy size; the runtime arm is pulled the moment
the order is posted, so a second order cannot follow; the fill report posts
when the verifier sees the fill, as one row next to what the models said that
copy would cost. Unfired after 24 hours it expires and says so once.

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


def stage(by: str = "telegram", now: Optional[float] = None) -> tuple[bool, str]:
    """Arm the one-shot. Refuses unless every interlock key is turned, and
    refuses while a fired record stands (RESET clears it)."""
    now = time.time() if now is None else now
    reasons = blocking_reasons()
    if reasons:
        return (False, "; ".join(reasons))
    d = read()
    if d.get("fired"):
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
    # The expiry does NOT touch the arm (owner ruling 2026-09-06): an arm
    # pulled for a reason unrelated to money is the "stupid reason" deals
    # stopped for a day. The staged test copy simply lapses; trading, if
    # armed, continues at full size.
    msg = ("🐤 The staged test copy lapsed after 24h: no followed-wallet buy "
           "passed the rails in that time. Trading continues at normal size if "
           "armed. /canary CONFIRM stages another test copy.")
    logger.warn(f"[canary] {msg}")
    if send is not None:
        try:
            send(msg)
        except Exception as exc:
            logger.warn(f"[canary] expiry message failed: {exc}")
    return True


# The CLOB's usual minimum when the book does not say: five shares.
DEFAULT_MIN_SHARES = 5.0


def size_for(clob_client, token_id: str, order_price: float) -> float:
    """The smallest order this market accepts, in USD, never under the bot's
    own order minimum. The book is read as a dict OR an object (the v2 client
    has returned both shapes); when it cannot be read, five shares is assumed
    rather than one dollar, so the canary is never sized under the exchange
    minimum and rejected as if no copy had passed."""
    floor = float(CONFIG.min_order_size_usd)
    min_shares = 0.0
    try:
        book = clob_client.get_order_book(token_id)
        raw = (book.get("min_order_size") if isinstance(book, dict)
               else getattr(book, "min_order_size", None))
        min_shares = _num(raw)
    except Exception as exc:
        logger.warn(f"[canary] min order size unreadable: {exc}")
    if min_shares <= 0:
        min_shares = DEFAULT_MIN_SHARES
    price = float(order_price) if order_price and float(order_price) > 0 else 1.0
    return round(max(floor, min_shares * price), 2)


def model_price(their_price: float) -> float:
    """What paper book B would have booked this entry at: their price plus
    the flat slippage model the book uses. Stored at fire time so the report
    never recomputes it from a book that has moved."""
    raw = getattr(CONFIG, "copy_paper_b_slippage_bps", 100)
    bps = 100.0 if raw is None else float(raw)
    return round(min(0.999, float(their_price) * (1.0 + bps / 10000.0)), 4)


def consume(*, market: str, token_id: str, their_price: float,
            quoted_ask: Optional[float], copy_size: float,
            notify_latency_s: Optional[float], now: Optional[float] = None) -> bool:
    """Spend the one shot BEFORE the order is posted.

    Staged flips off and a fired record is written with no order id yet. An
    ambiguous post (the CLOB accepted it, the reply was lost) or a failed
    post then costs the owner a RESET and a re-arm, never a second ticket.
    Returns False when the record could not be persisted; the caller must
    then refuse to post.
    """
    now = time.time() if now is None else now
    d = read()
    d["staged"] = False
    d["fired"] = {
        "order_id": None, "posted": False, "market": market, "token_id": token_id,
        "their_price": their_price, "quoted_ask": quoted_ask,
        "model_price": model_price(their_price),
        "order_price": None, "copy_size": copy_size,
        "notify_latency_s": notify_latency_s, "placed_ts": now,
    }
    d["fill"] = None
    ok = _write(d)
    if ok:
        logger.warn(f"[canary] CONSUMED: one ${copy_size:.2f} order on '{market[:40]}' "
                    f"is about to post; the arm comes off now")
    return ok


def record_fired(*, order_id: str, order_price: float, now: Optional[float] = None) -> dict:
    """The one order posted. Completes the record ``consume`` opened."""
    now = time.time() if now is None else now
    d = read()
    f = d.get("fired") or {}
    f.update({"order_id": order_id, "posted": True, "order_price": order_price,
              "placed_ts": now})
    d["fired"] = f
    d["staged"] = False
    _write(d)
    logger.warn(f"[canary] FIRED: {order_id[:12]} ${_num(f.get('copy_size')):.2f} on "
                f"'{str(f.get('market') or '')[:40]}' at {order_price:.4f} "
                f"(theirs {_num(f.get('their_price')):.4f})")
    return f


def record_post_failed(reason: str) -> None:
    """The one order did not post. The shot stays spent; RESET re-opens it."""
    d = read()
    f = d.get("fired") or {}
    f.update({"posted": False, "post_error": str(reason)[:200]})
    d["fired"] = f
    d["staged"] = False
    _write(d)
    logger.error(f"[canary] the one order did not post: {reason}")


def record_fill(order_id: str, fill, now: Optional[float] = None) -> Optional[str]:
    """The verifier saw the order's fate. Returns the report text ONCE."""
    d = read()
    fired = d.get("fired") or {}
    if not fired or not fired.get("order_id") or fired.get("order_id") != order_id \
            or d.get("fill"):
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
    from src.copy_trading.telegram_notifier import _escape_html
    if not f.get("posted"):
        return (f"🐤 <b>Test copy failed</b>: the order did not post "
                f"({_escape_html(str(f.get('post_error') or 'no order id returned'))}). "
                f"Trading is paused. <code>/canary RESET</code> then "
                f"<code>/live CONFIRM</code> to try again.")
    lines = [f"🐤 <b>Test copy report</b>, order {str(f.get('order_id') or '')[:12]}",
             f"market: {_escape_html(str(f.get('market') or '')[:60])}",
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
        lines.extend(_one_row(f, fp))
    else:
        lines.append("fill: waiting for the verifier")
    lines.append("Trading paused when this order was posted. /live CONFIRM resumes it.")
    return "\n".join(lines)


def _one_row(f: dict, fill_price: float) -> list[str]:
    """Two ledgers, one row: what the models said this copy would cost, next
    to what it cost. A plain diff, labelled thin at n=1, no stamp."""
    if fill_price <= 0:
        return ["models vs fill: n/a (no fill price)"]
    size = _num(f.get("copy_size"))
    model = _num(f.get("model_price")) or model_price(_num(f.get("their_price")))
    quoted = _num(f.get("quoted_ask"))

    def delta(ref: float) -> str:
        if ref <= 0:
            return "n/a"
        bps = (fill_price - ref) / ref * 10000.0
        cents = size * (fill_price - ref) / ref
        return f"{bps:+.0f}bps ({cents:+.2f} on ${size:.2f})"
    return [
        f"models vs fill, n=1, thin: paper model {model:.4f} · quoted ask at detection "
        f"{quoted:.4f} · fill {fill_price:.4f}",
        f"  fill vs paper model {delta(model)} · fill vs quoted ask {delta(quoted)}",
    ]


# --------------------------------------------------------------------------- #
# The test order: one real order on a market the owner (or the run) chose,
# through the exact live order path, to prove the pipeline places and fills.
# It does NOT pull the arm: it is proof, not a bounded first session, and the
# owner asked for it precisely so that trading can proceed with confidence.
# --------------------------------------------------------------------------- #

TEST_FILE = "testorder.json"
# A market whose favoured outcome trades under this is not a "penny test".
TEST_MIN_PRICE = 0.90


def _test_path() -> str:
    return os.path.join(CONFIG.data_dir, TEST_FILE)


def read_test() -> dict:
    try:
        with open(_test_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_test(d: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(_test_path()), exist_ok=True)
        tmp = _test_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, _test_path())
        return True
    except OSError as exc:
        logger.error(f"[testorder] state write failed: {exc}")
        return False


def test_blocking_reasons() -> list[str]:
    out = list(live_mode.blocking_reasons())
    if not live_budget.is_open():
        out.append("LIVE_BUDGET_USD not set (bankroll governor closed)")
    return out


async def fire_test_order(clob_client, token_id: str, *, title: str = "",
                          now: Optional[float] = None) -> tuple[bool, str]:
    """Place ONE order at the exchange minimum on ``token_id``, BUY side, at
    the live best ask, through ``trade_executor._execute_copy_order`` (the
    same function every real copy uses), and hand it to the verifier.

    Returns ``(ok, plain message for the owner)``. Refuses when the interlock
    is not fully open, when the book cannot be read, or when the favoured
    price is under TEST_MIN_PRICE (then it is a bet, not a test).
    """
    from datetime import datetime, timezone

    from src.copy_trading import trade_executor
    from src.copy_trading.daily_spend_guard import can_spend, record_spend
    from src.copy_trading.order_executor import quote_copy_order
    from src.copy_trading.trade_queue import enqueue_pending_order
    from src.models import DetectedTrade, PendingOrder

    now = time.time() if now is None else now
    reasons = test_blocking_reasons()
    if reasons:
        return (False, "cannot place a test order: " + "; ".join(reasons))
    snapshot = await trade_executor._get_market_snapshot(clob_client, token_id)
    if not snapshot or not snapshot.get("best_ask"):
        return (False, "cannot read the order book for that market")
    ask = float(snapshot["best_ask"])
    if ask < TEST_MIN_PRICE:
        return (False, f"the favoured outcome trades at {ask:.3f}, under "
                       f"{TEST_MIN_PRICE:.2f}: that is a bet, not a test")
    order_price = quote_copy_order("BUY", ask, snapshot) or ask
    size_usd = size_for(clob_client, token_id, order_price)
    ok_spend, why = can_spend(size_usd)
    if not ok_spend:
        return (False, f"the daily cap refuses it: {why}")

    trade = DetectedTrade(
        id=f"test-{token_id[:16]}-{int(now)}", trader_address="",
        timestamp=datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        market=title or f"token {token_id[:16]}", token_id=token_id,
        condition_id="", side="BUY", size=0.0, price=ask)
    rec = {"placed_ts": now, "token_id": token_id, "market": trade.market,
           "ask_at_send": ask, "order_price": order_price, "copy_size": size_usd,
           "order_id": None, "posted": False, "fill": None}
    if not _write_test(rec):
        return (False, "could not persist the test record; nothing was sent")

    result = await trade_executor._execute_copy_order(clob_client, trade, size_usd, snapshot)
    if result is None:
        rec["post_error"] = "the exchange returned no order id"
        _write_test(rec)
        return (False, f"test order on '{trade.market[:50]}' did NOT post: "
                       f"the exchange returned no order id. See the log for the "
                       f"exact error.")
    rec.update({"order_id": result.order_id, "posted": True,
                "order_price": result.order_price, "shares": result.shares})
    _write_test(rec)
    record_spend(size_usd, source="testorder")
    enqueue_pending_order(PendingOrder(
        trade=trade, order_id=result.order_id, order_price=result.order_price,
        copy_size=size_usd, placed_at=now * 1000, market_key=trade.market,
        side="BUY", source_detected_at=now * 1000, enqueued_at=now * 1000,
        order_submitted_at=now * 1000, source="testorder", tier=None))
    logger.warn(f"[testorder] POSTED {result.order_id[:12]} ${size_usd:.2f} "
                f"({result.shares:.2f} sh) on '{trade.market[:40]}' at "
                f"{result.order_price:.4f}")
    return (True, f"Test order placed: {result.shares:.2f} shares of "
                  f"'{trade.market[:50]}' at {result.order_price:.3f} "
                  f"(${size_usd:.2f}), order {result.order_id[:12]}. The fill "
                  f"report follows when the exchange confirms it; the win or "
                  f"loss shows in Polymarket when the match resolves.")


def record_test_fill(order_id: str, fill, now: Optional[float] = None) -> Optional[str]:
    """The verifier saw the test order's fate. Returns the report ONCE."""
    d = read_test()
    if not d or d.get("order_id") != order_id or d.get("fill"):
        return None
    status = str(getattr(fill, "status", "") or "")
    if not status or status == "UNKNOWN":
        return None
    now = time.time() if now is None else now
    d["fill"] = {"status": status,
                 "fill_price": _num(getattr(fill, "fill_price", 0.0)),
                 "filled_shares": _num(getattr(fill, "filled_shares", 0.0)),
                 "filled_usd": _num(getattr(fill, "filled_usd", 0.0)), "ts": now}
    _write_test(d)
    f = d["fill"]
    if status == "FILLED" or (status == "PARTIAL" and f["filled_shares"] > 0):
        return (f"Test order filled: {f['filled_shares']:.2f} shares of "
                f"'{str(d.get('market'))[:50]}' at {f['fill_price']:.3f} "
                f"(${f['filled_usd']:.2f}), order {str(order_id)[:12]}. The live "
                f"order path works end to end. Result shows in Polymarket when "
                f"the match resolves.")
    return (f"Test order {status.lower()}: order {str(order_id)[:12]} on "
            f"'{str(d.get('market'))[:50]}' did not fill. "
            f"{'It was cancelled by the verifier.' if status == 'UNFILLED' else ''}")


def status_lines() -> list[str]:
    """Plain lines for /canary. The caller escapes them."""
    d = read()
    now = time.time()
    out: list[str] = []
    if is_staged(now):
        left = (_num(d.get("expires_ts")) - now) / 3600
        out.append(f"staged by {d.get('by', '?')} at {_iso(d.get('staged_ts'))}, "
                   f"expires in {left:.1f}h")
    elif d.get("fired") and not d["fired"].get("posted"):
        out.append(f"spent at {_iso(d['fired'].get('placed_ts'))} but the order did not "
                   f"post; /canary RESET to stage again")
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
