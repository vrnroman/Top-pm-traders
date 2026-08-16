"""Shadow quotes — what a real copy order WOULD have paid, measured live.

The pre-flip question this answers, in the owner's words: *how fast can I be
notified, and how much worse will my entry price be than the wallet I copied?*

Neither number existed before this module:

* **Latency** was unmeasurable. `source_detected_at` holds the *target's own*
  trade timestamp, not the moment we saw it (see `QueuedTrade`), so the gap
  that a real copier races was recorded nowhere. The detector now stamps
  `their_ts` and `detected_at`; the difference is the notify latency.
* **Entry penalty** was only ever *modeled*. Book A walks a simulated asks
  book and Book B stamps a flat +100bps by construction, and — decisively —
  book A's fill gate **censors** the copies where the book ran away, which are
  exactly the expensive ones. So the recorded drag is a survivor's average.
  This module quotes EVERY detected trade, including the ones both books
  refuse, using `order_executor.quote_copy_order` — the same function the live
  executor prices with.

Nothing here places an order, signs anything, or spends. It reads public book
prices and appends a JSONL row. It is deliberately additive: it never feeds
back into either paper book, so it cannot change what the pre-registered
2026-08-22 verdict measures.

Two samples per trade, `t0` and `t0 + SECOND_SAMPLE_DELAY_S`:
the first says what the penalty is, the second says whether *being faster
would buy anything* — if the book has already moved by the time we look
again, latency is the binding constraint; if it has not, it is not, and
chasing speed would be wasted effort. That decay term is the actual flip
decision, which is why one sample was not enough.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

from src.config import CONFIG
from src.copy_trading.order_executor import entry_penalty_bps, quote_copy_order
from src.logger import logger

# The second look must clear market_price's 5s snapshot cache, or it would be
# handed back the identical prices and the decay term would read as a
# guaranteed zero — a measurement that can only produce one answer.
SECOND_SAMPLE_DELAY_S = 12.0

# Bound the work a sweep can create: each sampled trade costs two book reads,
# and the detector can emit a burst when a slate settles.
MAX_SAMPLES_PER_SWEEP = 40

# Keep the log bounded on a small VM disk (a known past failure: the disk hit
# 84% and the box was a day from full). Trimmed to the newest rows on rollover.
MAX_ROWS = 20000
_TRIM_TO = 15000


def _path() -> str:
    return os.path.join(CONFIG.data_dir, "shadow-quotes.jsonl")


def _snapshot_dict(clob_client, token_id: str) -> Optional[dict]:
    """Live book for a token as the plain dict the pricing function takes."""
    from src.copy_trading.market_price import fetch_market_snapshot
    snap = fetch_market_snapshot(clob_client, token_id)
    if snap is None:
        return None
    return {
        "best_bid": snap.best_bid,
        "best_ask": snap.best_ask,
        "midpoint": snap.midpoint,
        "spread_bps": snap.spread_bps,
    }


def quote_once(clob_client, token_id: str, their_price: float,
               side: str = "BUY") -> Optional[dict]:
    """One shadow quote: the book now, our price, and the penalty in bps."""
    snap = _snapshot_dict(clob_client, token_id)
    if snap is None:
        return None
    our_price = quote_copy_order(side, their_price, snap)
    if our_price is None:
        return None
    return {
        "our_price": our_price,
        "best_bid": snap["best_bid"],
        "best_ask": snap["best_ask"],
        "spread_bps": snap["spread_bps"],
        "penalty_bps": entry_penalty_bps(our_price, their_price),
    }


def record(row: dict) -> None:
    """Append one shadow-quote row. Never raises into the caller."""
    try:
        path = _path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception as exc:
        logger.warn(f"[shadow] record failed: {exc}")


def _maybe_trim() -> None:
    """Cap the JSONL so it cannot grow without bound on the VM disk."""
    try:
        path = _path()
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= MAX_ROWS:
            return
        keep = lines[-_TRIM_TO:]
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep)
        os.replace(tmp, path)
        logger.info(f"[shadow] trimmed quote log {len(lines)} -> {len(keep)} rows")
    except Exception as exc:
        logger.warn(f"[shadow] trim failed: {exc}")


def load_rows(path: Optional[str] = None, since_ts: Optional[float] = None) -> list[dict]:
    """Read back the shadow-quote rows (newest last), optionally time-floored."""
    p = path or _path()
    out: list[dict] = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                if since_ts is not None and float(d.get("detected_at") or 0) < since_ts:
                    continue
                out.append(d)
    except OSError:
        return []
    return out


def sample_trade(clob_client, trade: dict) -> Optional[dict]:
    """Two-sample shadow quote for one detected trade dict from the detector.

    ``trade`` is a row emitted by ``copy_paper_live.make_detector`` — it
    carries ``their_price``, ``their_ts`` and ``detected_at``.

    Blocking by design: it sleeps between the two samples, so it runs on the
    shadow worker thread, never on the paper engine's cycle.
    """
    token_id = trade.get("token_id") or ""
    their_price = float(trade.get("their_price") or 0)
    if not token_id or their_price <= 0:
        return None

    their_ts = float(trade.get("their_ts") or 0)
    detected_at = float(trade.get("detected_at") or 0)
    # Latency the copier is actually racing: their trade -> our detection.
    # None (not 0) when the source gave us no usable timestamp, so a missing
    # input is never scored as instantaneous.
    notify_latency_s = (
        (detected_at - their_ts) if (their_ts > 0 and detected_at > 0) else None
    )

    t0 = quote_once(clob_client, token_id, their_price)
    if t0 is None:
        return None

    time.sleep(SECOND_SAMPLE_DELAY_S)
    t1 = quote_once(clob_client, token_id, their_price)

    row = {
        "copy_id": trade.get("copy_id", ""),
        "target": trade.get("target", ""),
        "token_id": token_id,
        "category": trade.get("category", ""),
        "title": (trade.get("title") or "")[:120],
        "their_price": their_price,
        "their_usd": float(trade.get("their_usd") or 0),
        "their_ts": their_ts,
        "detected_at": detected_at,
        "notify_latency_s": notify_latency_s,
        "quoted_at": time.time(),
        # t0 — the penalty at the moment we could first have acted
        "our_price": t0["our_price"],
        "penalty_bps": t0["penalty_bps"],
        "best_bid": t0["best_bid"],
        "best_ask": t0["best_ask"],
        "spread_bps": t0["spread_bps"],
        # t1 — the same measurement SECOND_SAMPLE_DELAY_S later. The delta is
        # the decay term: how much the entry degrades per extra second of lag.
        "delay_s": SECOND_SAMPLE_DELAY_S,
        "our_price_t1": (t1 or {}).get("our_price"),
        "penalty_bps_t1": (t1 or {}).get("penalty_bps"),
        "spread_bps_t1": (t1 or {}).get("spread_bps"),
    }
    record(row)
    return row


def observe(detected: list[dict], clob_client) -> int:
    """Shadow-quote a batch of detected trades. Returns rows written.

    Fail-soft by construction: a broken book read costs one row, never the
    caller. Bounded by MAX_SAMPLES_PER_SWEEP.
    """
    if not detected or clob_client is None:
        return 0
    batch = detected[:MAX_SAMPLES_PER_SWEEP]
    dropped = len(detected) - len(batch)
    if dropped > 0:
        # Never let a cap truncate silently — a partial sample that reads as
        # complete is how a measurement lies.
        logger.info(f"[shadow] sampling {len(batch)} of {len(detected)} detected "
                    f"({dropped} over the per-sweep cap)")
    written = 0
    for t in batch:
        try:
            if sample_trade(clob_client, t):
                written += 1
        except Exception as exc:
            logger.warn(f"[shadow] sample failed: {exc}")
    if written:
        logger.info(f"[shadow] recorded {written} shadow quote(s)")
        _maybe_trim()
    return written


# --------------------------------------------------------------------------- #
# The worker: an observer callback that never blocks the paper cycle
# --------------------------------------------------------------------------- #

def make_observer(clob_client_factory, queue_max: int = 500):
    """Return (observer_callback, stop_fn) backed by a daemon worker thread.

    The engine calls the callback on its own cycle thread, so the callback
    only enqueues and returns immediately; the worker does the book reads and
    the 12s inter-sample sleep. A full queue DROPS and says so — the paper
    book's cadence is never held up by a measurement, and a silent drop would
    make the sample look complete when it is not.
    """
    import queue as _queue
    import threading

    q: "_queue.Queue[dict]" = _queue.Queue(maxsize=queue_max)
    stop = threading.Event()
    seen: set = set()

    def _worker() -> None:
        client = None
        while not stop.is_set():
            try:
                trade = q.get(timeout=1.0)
            except _queue.Empty:
                continue
            try:
                if client is None:
                    client = clob_client_factory()
                if client is None:
                    continue
                sample_trade(client, trade)
            except Exception as exc:
                logger.warn(f"[shadow] worker sample failed: {exc}")

    threading.Thread(target=_worker, name="shadow-quote", daemon=True).start()

    def observer(detected: list) -> None:
        dropped = 0
        for t in detected:
            cid = t.get("copy_id") or ""
            # One quote per detected trade ever: the detector re-emits the same
            # trade every sweep until it ages out of the window, and re-quoting
            # it would weight slow-moving markets by how long they linger.
            if cid and cid in seen:
                continue
            if cid:
                seen.add(cid)
            try:
                q.put_nowait(dict(t))
            except Exception:
                dropped += 1
        if dropped:
            logger.warn(f"[shadow] queue full — dropped {dropped} sample(s)")
        if len(seen) > 50000:
            seen.clear()

    return observer, stop.set


# --------------------------------------------------------------------------- #
# Reporting — the numbers the owner actually asked for
# --------------------------------------------------------------------------- #

def _pct(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    idx = q * (len(s) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def by_wallet(rows: list[dict], min_n: int = 3) -> list[dict]:
    """Per-wallet latency and entry-penalty, worst penalty first.

    The averaged figure answers "how bad is it"; this answers the question
    that actually decides the flip — *which* wallets are copyable at a price
    worth paying, and which bleed their edge into the spread before we can
    reach them. Wallets under ``min_n`` samples are returned too, flagged
    thin: hiding them would make a sparse sample look like a complete one.
    """
    groups: dict = {}
    for r in rows:
        w = (r.get("target") or "").lower()
        if not w:
            continue
        groups.setdefault(w, []).append(r)
    out = []
    for w, rs in groups.items():
        s = summarize(rs)
        out.append({
            "wallet": w,
            "n": s["n"],
            "thin": s["n"] < min_n,
            "latency_p50_s": s["latency_p50_s"],
            "penalty_p50_bps": s["penalty_p50_bps"],
            "penalty_p90_bps": s["penalty_p90_bps"],
            "decay_mean_bps": s["decay_mean_bps"],
            "top_category": max(
                {r.get("category") or "other" for r in rs},
                key=lambda c: sum(1 for r in rs if (r.get("category") or "other") == c),
            ),
        })
    # Worst entry penalty first — the wallets whose edge is hardest to reach.
    out.sort(key=lambda d: (d["penalty_p50_bps"] is None, -(d["penalty_p50_bps"] or 0)))
    return out


def collecting_since(rows: list[dict]) -> Optional[float]:
    """Oldest sample's detection time, for an honest empty/partial state."""
    stamps = [float(r["detected_at"]) for r in rows if r.get("detected_at")]
    return min(stamps) if stamps else None


def summarize(rows: list[dict]) -> dict:
    """Latency and entry-penalty distributions over shadow-quote rows."""
    lat = [float(r["notify_latency_s"]) for r in rows
           if r.get("notify_latency_s") is not None]
    pen = [float(r["penalty_bps"]) for r in rows
           if r.get("penalty_bps") is not None]
    decay = [float(r["penalty_bps_t1"]) - float(r["penalty_bps"])
             for r in rows
             if r.get("penalty_bps_t1") is not None
             and r.get("penalty_bps") is not None]
    return {
        "n": len(rows),
        "n_latency": len(lat),
        "latency_p50_s": _pct(lat, 0.50),
        "latency_p90_s": _pct(lat, 0.90),
        "latency_max_s": max(lat) if lat else None,
        "n_penalty": len(pen),
        "penalty_p50_bps": _pct(pen, 0.50),
        "penalty_p90_bps": _pct(pen, 0.90),
        "penalty_mean_bps": (sum(pen) / len(pen)) if pen else None,
        "penalty_worse_frac": (sum(1 for p in pen if p > 0) / len(pen)) if pen else None,
        "n_decay": len(decay),
        "decay_mean_bps": (sum(decay) / len(decay)) if decay else None,
    }
