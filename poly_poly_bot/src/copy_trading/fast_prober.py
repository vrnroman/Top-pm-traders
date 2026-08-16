"""Per-wallet fast detection for set Z.

Measured 2026-08-16 on the live box, polling every 3 seconds for 5 minutes:

    global /trades feed   n~800   youngest trade ever seen   264.5s
    per-wallet /activity  n=9     youngest trade ever seen     0.9s

Two endpoints, no overlap in their ranges. The global feed the paper harness
reads is structurally about five minutes behind; the per-wallet endpoint is
effectively real time. The harness is on the slow one for a good reason at its
own scale (one request covers ~500 wallets instead of 500 requests per cycle),
but that trade is wrong for a set of two.

So this poller exists for set Z only, and it deliberately does NOT feed either
paper book:

* Wiring it into the paper feed would change which trades enter the sample and
  void the pre-registered 2026-08-22 verdict, six days before it lands.
* Its output goes to the shadow-quote log (measurement) and, once armed, to
  the live executor's queue. Both are outside the frozen sample.

It is also the thing that makes the go-live economics honest: every entry-price
number measured through the slow feed carries ~5 minutes of book drift that a
real Z copier would never eat.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Iterable, Optional

from src.config import CONFIG
from src.logger import logger

# Per-wallet polling is cheap at Z's size. At 2 wallets and a 3s tick that is
# 40 requests/minute, well inside what the data API tolerates, and it is the
# difference between seconds and minutes of detection lag.
DEFAULT_INTERVAL_S = 3.0

# Never act on a trade this old: on a restart the endpoint hands back the
# wallet's recent history, and copying an hour-old entry at today's book is
# how a copier buys the move it already missed.
MAX_ACT_AGE_S = 120.0


def _num(x) -> float:
    try:
        return float(x or 0.0)
    except (TypeError, ValueError):
        return 0.0


def poll_once(wallets: Iterable[str], seen: set,
              *, fetch: Optional[Callable] = None,
              now: Optional[float] = None) -> list[dict]:
    """One pass over the Z wallets. Returns newly detected BUY trades.

    Emits the same dict shape the paper detectors emit, so everything
    downstream (shadow quotes, sizing, the executor) treats it identically.
    """
    from src.copy_trading.copy_paper_live import DATA, _get
    from src.copy_trading.trader_scoring import classify_market

    fetch = fetch or (lambda w: _get(DATA, "/activity", user=w, limit=30) or [])
    out: list[dict] = []
    for w in wallets:
        try:
            acts = fetch(w)
        except Exception as exc:
            logger.warn(f"[fast] {w[:10]} poll failed: {exc}")
            continue
        seen_at = time.time() if now is None else now
        for a in acts or []:
            if a.get("type") != "TRADE" or str(a.get("side") or "").upper() != "BUY":
                continue
            tx, token = a.get("transactionHash") or "", a.get("asset") or ""
            if not tx or not token:
                continue
            copy_id = f"{tx}-{token}"
            if copy_id in seen:
                continue
            seen.add(copy_id)
            their_ts = _num(a.get("timestamp"))
            age = seen_at - their_ts if their_ts > 0 else None
            price = _num(a.get("price"))
            usd = _num(a.get("usdcSize")) or _num(a.get("size")) * price
            title = a.get("title", "") or ""
            row = {
                "copy_id": copy_id,
                "target": w.lower(),
                "condition_id": a.get("conditionId", "") or "",
                "token_id": token,
                "outcome_index": int(a.get("outcomeIndex") or 0),
                "category": classify_market(title),
                "title": title,
                "slug": a.get("eventSlug") or a.get("slug") or "",
                "event_key": a.get("eventSlug") or "",
                "their_price": price,
                "their_usd": usd,
                "their_ts": their_ts,
                "detected_at": seen_at,
                "age_s": age,
                # Stale rows are recorded for measurement but must never be
                # acted on; the caller reads this rather than re-deriving it.
                "actionable": bool(age is not None and age <= MAX_ACT_AGE_S),
                "source": "fast-prober",
            }
            out.append(row)
    return out


def run_forever(shutdown: threading.Event,
                *, interval_s: float = DEFAULT_INTERVAL_S,
                wallets_fn: Optional[Callable[[], list]] = None,
                on_trades: Optional[Callable[[list], None]] = None) -> None:
    """Poll set Z's wallets until shutdown. Never raises into the caller.

    ``wallets_fn`` is read every tick, so admitting or evicting a wallet takes
    effect on the next pass without a restart. An empty set Z means this loop
    costs one comparison per tick, which is the correct behaviour when nothing
    has earned real money yet.
    """
    from src.copy_trading import zset

    wallets_fn = wallets_fn or zset.wallets
    seen: set = set()
    warm = True
    while not shutdown.is_set():
        try:
            wallets = list(wallets_fn() or [])
            if wallets:
                trades = poll_once(wallets, seen)
                if warm:
                    # The first pass returns each wallet's recent history. It
                    # is real data for the seen-set, but its ages are history,
                    # not detection latency, so it is never handed on.
                    warm = False
                    if trades:
                        logger.info(f"[fast] primed on {len(trades)} historical "
                                    f"trade(s) from {len(wallets)} Z wallet(s)")
                elif trades:
                    fresh = [t for t in trades if t["actionable"]]
                    stale = len(trades) - len(fresh)
                    ages = [t["age_s"] for t in trades if t["age_s"] is not None]
                    logger.info(
                        f"[fast] {len(trades)} new Z trade(s), {len(fresh)} "
                        f"actionable" + (f", {stale} too old to act on" if stale else "")
                        + (f", youngest {min(ages):.1f}s" if ages else ""))
                    if on_trades is not None:
                        on_trades(trades)
            if len(seen) > 20000:
                seen.clear()
                warm = True
        except Exception as exc:
            logger.error(f"[fast] prober cycle failed: {exc}")
        shutdown.wait(interval_s)


def make_shadow_sink(observer) -> Callable[[list], None]:
    """Route detected Z trades into the shadow-quote measurement.

    Measurement only. This is what gives set Z an entry-price number taken at
    ITS detection speed rather than the paper harness's, which is the whole
    reason the two numbers were not comparable before.
    """
    def sink(trades: list) -> None:
        try:
            observer(trades)
        except Exception as exc:
            logger.warn(f"[fast] shadow sink failed: {exc}")
    return sink
