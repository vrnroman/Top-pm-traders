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
import math
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

# How stale a t0 quote may be before it stops describing "the price we could
# have had when we were told". The worker quotes on dequeue, so this is queue
# delay, not market delay — it is the measuring instrument's own lag, and a
# row carrying more of it than this is excluded from the penalty statistics
# rather than quietly averaged in.
MAX_QUOTE_LAG_S = 30.0

# Process start. Trades whose own timestamp predates it happened while we were
# not listening, so the "latency" on them is really the age of the detector's
# look-back window (COPY_PAPER_MAX_AGE_S, an hour) flushing on the first sweep
# after a restart. They are recorded, flagged, and kept out of the latency
# statistics: on a repo that redeploys on every push, letting them in means the
# headline number mostly measures the last deploy.
PROCESS_START_TS = time.time()

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
        "fetched_at": snap.fetched_at,
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
        # When the book read fails, market_price hands back an EXPIRED cached
        # snapshot rather than nothing. Carried through so a second sample can
        # tell a genuine re-read from a replay of the first one.
        "fetched_at": snap.get("fetched_at"),
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
                if since_ts is not None:
                    try:
                        seen_at = float(d.get("detected_at") or 0)
                    except (TypeError, ValueError):
                        # A malformed stamp must skip its row, never take down
                        # the whole read (and with it the /speed command).
                        continue
                    if seen_at < since_ts:
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
    return _build_row(trade, t0, t1, quoted_at=time.time() - SECOND_SAMPLE_DELAY_S)


def _build_row(trade: dict, t0: dict, t1: Optional[dict], quoted_at: float) -> dict:
    """Assemble and persist one shadow-quote row from its two samples."""
    token_id = trade.get("token_id") or ""
    their_price = float(trade.get("their_price") or 0)
    their_ts = float(trade.get("their_ts") or 0)
    detected_at = float(trade.get("detected_at") or 0)
    notify_latency_s = (
        (detected_at - their_ts) if (their_ts > 0 and detected_at > 0) else None
    )
    # How long the trade sat in our own queue before we priced it. This is the
    # instrument's error bar, not the market's: a row with a large one says
    # what the book looked like minutes after the moment being described.
    quote_lag_s = (quoted_at - detected_at) if detected_at > 0 else None
    # Did this trade happen while we were actually listening?
    boot_flush = bool(their_ts and their_ts < PROCESS_START_TS)

    row = {
        "copy_id": trade.get("copy_id", ""),
        "target": trade.get("target", ""),
        "token_id": token_id,
        "category": trade.get("category", ""),
        # WHICH detector saw it. Set-Z wallets are polled per-wallet and are
        # detected in ~1s; everything else comes off the global feed, which is
        # structurally ~5 minutes stale. Pooling the two into one latency or
        # entry-penalty figure produces a number that describes neither, and
        # a row written without this label can never be split afterwards.
        "source": trade.get("source") or "feed",
        "title": (trade.get("title") or "")[:120],
        "their_price": their_price,
        "their_usd": float(trade.get("their_usd") or 0),
        "their_ts": their_ts,
        "detected_at": detected_at,
        "notify_latency_s": notify_latency_s,
        "quoted_at": quoted_at,
        # Validity flags — read by summarize(), never silently dropped.
        "quote_lag_s": quote_lag_s,
        "boot_flush": boot_flush,
        # t0 — the penalty at the moment we could first have acted
        "our_price": t0["our_price"],
        "penalty_bps": t0["penalty_bps"],
        "best_bid": t0["best_bid"],
        "best_ask": t0["best_ask"],
        "spread_bps": t0["spread_bps"],
        # t1 — the same measurement SECOND_SAMPLE_DELAY_S later. The delta is
        # the decay term: how much the entry degrades per extra second of lag.
        # When the book underlying t0 was actually read. Two rows sharing a
        # (token_id, book_ts) came from ONE read served twice out of
        # market_price's 5s cache, so they are one observation, not two.
        "book_ts": t0.get("fetched_at"),
        "delay_s": SECOND_SAMPLE_DELAY_S,
        "our_price_t1": (t1 or {}).get("our_price"),
        "penalty_bps_t1": (t1 or {}).get("penalty_bps"),
        "spread_bps_t1": (t1 or {}).get("spread_bps"),
        # True when the second look was served from the same (expired) cached
        # snapshot as the first, i.e. a replay, not a re-read. Such a row would
        # report exactly zero drift and quietly argue that speed does not
        # matter.
        "t1_stale": bool(
            t1 is not None and t0.get("fetched_at") is not None
            and t1.get("fetched_at") == t0.get("fetched_at")),
    }
    record(row)
    return row


def record_unquotable(trade: dict, reason: str = "no usable book") -> dict:
    """Log a detected trade we could NOT price, so the drop is never silent.

    One-sided and empty books are exactly the expensive-to-copy cases, so
    dropping them without a trace would reproduce book A's survivor bias one
    layer down: the sample would quietly become "trades that were easy to
    price". The row carries no prices, so every statistic skips it by
    construction, and `summarize` counts it as `n_unquotable`.
    """
    row = {
        "copy_id": trade.get("copy_id", ""),
        "target": trade.get("target", ""),
        "token_id": trade.get("token_id", ""),
        "category": trade.get("category", ""),
        "their_price": float(trade.get("their_price") or 0),
        "their_ts": float(trade.get("their_ts") or 0),
        "detected_at": float(trade.get("detected_at") or 0),
        "unquotable": True,
        "reason": reason,
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
    # Seeded from the log on start, so a restart does not re-quote the whole
    # look-back window and count it as fresh samples. Memory-only dedup made
    # every redeploy inflate the sample with duplicates of the same trade.
    seen: set = set()
    try:
        for r in load_rows():
            cid = r.get("copy_id")
            if cid:
                seen.add(cid)
        if seen:
            logger.info(f"[shadow] resumed with {len(seen)} already-quoted trade(s)")
    except Exception as exc:
        logger.warn(f"[shadow] could not seed dedup set: {exc}")

    def _worker() -> None:
        """Two-stage, non-blocking.

        Stage 1 quotes t0 the moment a trade is dequeued; stage 2 re-quotes it
        once its delay has elapsed. The old shape slept 12s inside the sample,
        which serialised the whole queue: with ~40 trades a sweep the median
        trade was priced ~5 minutes after it was detected, so the "entry price
        the moment we were told" carried minutes of our own queue delay. Now
        the sleep overlaps instead of stacking.
        """
        client = None
        pending: list = []          # (due_ts, trade, t0)
        while not stop.is_set():
            try:
                if client is None:
                    client = clob_client_factory()
            except Exception as exc:
                logger.warn(f"[shadow] client unavailable: {exc}")
            # Stage 1 — drain everything waiting, pricing each immediately.
            drained = 0
            while client is not None and drained < MAX_SAMPLES_PER_SWEEP:
                try:
                    trade = q.get_nowait()
                except _queue.Empty:
                    break
                drained += 1
                try:
                    t0 = quote_once(client, trade.get("token_id") or "",
                                    float(trade.get("their_price") or 0))
                    if t0 is not None:
                        pending.append(
                            (time.time() + SECOND_SAMPLE_DELAY_S, trade, t0,
                             time.time()))
                    else:
                        # Unpriceable book. Recorded, not dropped: see
                        # record_unquotable.
                        record_unquotable(trade)
                except Exception as exc:
                    logger.warn(f"[shadow] t0 quote failed: {exc}")
                    record_unquotable(trade, reason=str(exc)[:120])
            # Stage 2 — anything whose delay has elapsed gets its second look.
            now = time.time()
            ready = [p for p in pending if p[0] <= now]
            pending = [p for p in pending if p[0] > now]
            for _due, trade, t0, quoted_at in ready:
                try:
                    t1 = quote_once(client, trade.get("token_id") or "",
                                    float(trade.get("their_price") or 0))
                    _build_row(trade, t0, t1, quoted_at=quoted_at)
                except Exception as exc:
                    logger.warn(f"[shadow] t1 quote failed: {exc}")
            stop.wait(0.5)

    threading.Thread(target=_worker, name="shadow-quote", daemon=True).start()

    def observer(detected: list) -> None:
        dropped = 0
        queued = 0
        for t in detected:
            cid = t.get("copy_id") or ""
            # One quote per detected trade ever: the detector re-emits the same
            # trade every sweep until it ages out of the window, and re-quoting
            # it would weight slow-moving markets by how long they linger.
            if cid and cid in seen:
                continue
            # The worker sleeps 12s between a trade's two samples, so it drains
            # ~5/min while a sweep can emit 30-40. Cap per sweep so the queue
            # holds recent trades instead of a growing backlog of stale ones —
            # and mark the skipped ones seen, so what is measured stays a
            # clean per-sweep head rather than an ever-lagging tail.
            if queued >= MAX_SAMPLES_PER_SWEEP:
                if cid:
                    seen.add(cid)
                dropped += 1
                continue
            if cid:
                seen.add(cid)
            try:
                q.put_nowait(dict(t))
                queued += 1
            except Exception:
                dropped += 1
        if dropped:
            # Never silent: a truncated sample that renders as a complete one
            # is how a measurement lies.
            logger.info(f"[shadow] sampled {queued} of {queued + dropped} new "
                        f"detected trade(s) this sweep")
        if len(seen) > 50000:
            seen.clear()

    return observer, stop.set


# --------------------------------------------------------------------------- #
# Reporting — the numbers the owner actually asked for
# --------------------------------------------------------------------------- #

def _num(v) -> Optional[float]:
    """float(v) or None. Never raises: a corrupt field must skip, not crash."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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


def by_wallet(rows: list[dict], min_n: int = 3,
              known: Optional[bool] = None) -> list[dict]:
    """Per-wallet latency and entry-penalty, worst penalty first.

    The averaged figure answers "how bad is it"; this answers the question
    that actually decides the flip — *which* wallets are copyable at a price
    worth paying, and which bleed their edge into the spread before we can
    reach them. Wallets under ``min_n`` samples are returned too, flagged
    thin: hiding them would make a sparse sample look like a complete one.
    """
    # The coverage decision is the caller's, taken once on the full sample and
    # passed down. NOT applied as a row filter here: a row can be both
    # latency-valid and penalty-eligible, so stripping it for lacking a book
    # stamp silently shrank the table's LATENCY column to a third of the
    # headline's. summarize() applies coverage to the penalty path only.
    if known is None:
        known = coverage_known(rows)
    groups: dict = {}
    for r in rows:
        w = (r.get("target") or "").lower()
        if not w:
            continue
        groups.setdefault(w, []).append(r)
    out = []
    for w, rs in groups.items():
        s = summarize(rs, known=known)
        # Report the count the penalty figure is actually computed from, not
        # the raw row count: showing n=8 next to a median derived from 7 is
        # the small kind of lie that makes a reader trust the big numbers.
        n_used = s["n_penalty"]
        out.append({
            "wallet": w,
            "n": n_used,
            "thin": n_used < min_n,
            # Its own count: the latency median comes from a different valid
            # subset than the penalty median, so one shared n would misstate
            # whichever of the two it is not describing.
            "n_latency": s["n_latency"],
            "latency_p50_s": s["latency_p50_s"],
            "penalty_p50_bps": s["penalty_p50_bps"],
            "penalty_p90_bps": s["penalty_p90_bps"],
            "decay_mean_bps": s["decay_mean_bps"],
            "top_category": max(
                {r.get("category") or "other" for r in rs},
                key=lambda c: sum(1 for r in rs if (r.get("category") or "other") == c),
            ),
        })
    # A wallet whose every sample was excluded has nothing to say; listing it
    # with an "n/a" would pad the table and imply coverage that is not there.
    out = [d for d in out if d["n"] > 0]
    # Worst entry penalty first: the wallets whose edge is hardest to reach.
    out.sort(key=lambda d: (d["penalty_p50_bps"] is None, -(d["penalty_p50_bps"] or 0)))
    return out


def collecting_since(rows: list[dict]) -> Optional[float]:
    """Oldest sample's detection time, for an honest empty/partial state."""
    stamps = [float(r["detected_at"]) for r in rows if r.get("detected_at")]
    return min(stamps) if stamps else None


def valid_for_latency(r: dict) -> bool:
    """A row whose latency describes us, not a restart.

    Excludes the boot flush: on the first sweep after a restart the detector
    re-emits its whole look-back window (an hour), so those trades' "latency"
    is the age of the window, not how fast we were told. Older rows written
    before this flag existed carry no `their_ts`-vs-start information, so they
    are judged by the same rule applied to the field they do have.
    """
    if r.get("notify_latency_s") is None:
        return False
    return not r.get("boot_flush", False)


def usable_quote(r: dict) -> bool:
    """Is this row's PRICE trustworthy, regardless of what is derived from it?

    Split out from `valid_for_penalty` so the counterfactual book can reuse
    the bias rules without also requiring a computed penalty field. Same two
    exclusions: the restart backlog, and quotes taken long after detection.
    """
    if r.get("boot_flush", False):
        return False
    lag = r.get("quote_lag_s")
    try:
        return lag is None or float(lag) <= MAX_QUOTE_LAG_S
    except (TypeError, ValueError):
        return False


def valid_for_penalty(r: dict) -> bool:
    """A row whose quote still describes the moment we were told.

    Excludes BOTH biases, not just one. The restart backlog is disqualifying
    here for the same reason it is for latency, and more obviously: a trade
    detected 26 minutes late is priced against a book that has had 26 minutes
    to move, so the "penalty" is drift, not what we would have paid. Declining
    to give a latency number off those rows and then reporting a bold penalty
    median off the very same rows is the contradiction this guards.

    Also excludes rows priced long after detection, which carry our own queue
    delay rather than the market's move. Rows written before these fields
    existed are admitted; the fields are absent, not large.
    """
    if r.get("penalty_bps") is None:
        return False
    return usable_quote(r)


FAST_SOURCE = "fast-prober"

# Rows written before the label existed cannot be attributed, so the fast
# slice is dated from the deploy that introduced it rather than pretending
# the whole history is separable.
FAST_LABEL_SINCE_ISO = "2026-08-16"


def by_source(rows: list[dict], source: str) -> list[dict]:
    """Rows from one detector. Unlabelled rows are the global feed."""
    if source == "feed":
        return [r for r in rows if (r.get("source") or "feed") == "feed"]
    return [r for r in rows if r.get("source") == source]


def coverage_known(rows: list[dict]) -> bool:
    """Does this sample, as a whole, carry independence stamps?

    The coverage rule is a WHOLE-SAMPLE decision, so it must be computed once
    on the full set and then handed down to anything that splits it. Deciding
    it inside each slice is what let the per-wallet table disagree with the
    headline twice: once because an unstamped wallet saw "nothing has it", and
    once because pre-filtering the rows stripped latency the table still
    needed.
    """
    return any(r.get("book_ts") is not None
               for r in rows if valid_for_penalty(r))


def apply_coverage_filter(rows: list[dict],
                          known: Optional[bool] = None) -> list[dict]:
    """Drop rows whose independence is unknowable, but only once it is knowable.

    `book_ts` is what makes a row's independence measurable, and rows written
    before it existed lack it. Once ANY row has it, the ones without it are
    dropped, the same rule this module applies to the restart backlog.

    It is deliberately a WHOLE-SAMPLE decision, not a per-row predicate, which
    makes it the one filter here that can answer differently for different
    slices of the same data. So every surface must apply it to the SAME set
    before splitting: `/speed` once filtered the headline and then let
    `by_wallet` re-partition the raw rows, and a wallet whose rows all
    predated the stamp saw "no row has it" and kept them all. The table then
    led with a +3548bps wallet drawn entirely from rows the headline had
    excluded four lines above, 15x outside its own p90. Idempotent, so
    applying it again downstream is safe.
    """
    if known is None:
        known = coverage_known(rows)
    if not known:
        return list(rows)
    keep = []
    for r in rows:
        # Only penalty-eligible rows are subject to it: a latency-only row has
        # no price to be clustered with, so dropping it would shrink the
        # latency sample for no reason.
        if valid_for_penalty(r) and r.get("book_ts") is None:
            continue
        keep.append(r)
    return keep


def summarize(rows: list[dict], known: Optional[bool] = None) -> dict:
    """Latency and entry-penalty distributions over shadow-quote rows.

    Biased rows are EXCLUDED and counted, never averaged in: the two figures
    here are meant to decide whether real money gets deployed, and a number
    that quietly folds in a restart artifact or the sampler's own queue delay
    is worse than no number.
    """
    lat_rows = [r for r in rows if valid_for_latency(r)]
    pen_rows = [r for r in rows if valid_for_penalty(r)]
    # MIXED COVERAGE. `book_ts` is what makes a row's independence knowable.
    # Once ANY row carries it, rows without it are excluded and counted, the
    # same rule this module already applies to the restart backlog. Without
    # this the independence line describes a minority of the sample while the
    # headline is computed from all of it, so the warning switches itself off
    # as the field populates even though most rows remain unmeasured, which is
    # the disclosure expiring rather than becoming true.
    before = len(pen_rows)
    if known is None:
        known = coverage_known(rows)
    if known:
        pen_rows = [r for r in pen_rows if r.get("book_ts") is not None]
    n_unmeasured = before - len(pen_rows)
    def _finite(vals):
        # A NaN or Inf sorts unpredictably and poisons every percentile. Not
        # reachable from the live writer, but a hand-edited or truncated line
        # must not be able to render "+nanbps" as a measurement.
        return [v for v in vals if math.isfinite(v)]

    lat = _finite([float(r["notify_latency_s"]) for r in lat_rows])
    pen = _finite([float(r["penalty_bps"]) for r in pen_rows])
    # The decay term drives one specific conclusion ("does being faster buy
    # anything"), so it takes the same filtered rows AND drops any second
    # sample that was a cache replay rather than a fresh read — that would
    # report a guaranteed zero drift and argue against speed for free.
    # Averaged PER BOOK MOVE, not per row: the label says "N distinct book
    # moves", so the estimator has to agree with it. Row-weighting let one
    # read with 30 clone copies outvote four quiet reads (+1324bps rendered
    # where the move-weighted answer was +300). The gate moved to move-keys
    # two rounds ago and the estimator did not follow.
    _by_move: dict = {}
    for r in pen_rows:
        if (r.get("penalty_bps_t1") is None or r.get("penalty_bps") is None
                or r.get("t1_stale", False)):
            continue
        d = float(r["penalty_bps_t1"]) - float(r["penalty_bps"])
        if not math.isfinite(d):
            continue
        _by_move.setdefault((r.get("token_id"), r.get("book_ts")), []).append(d)
    decay = [sum(v) / len(v) for v in _by_move.values()]
    # How much of the sample is INDEPENDENT. A burst of copies on one market
    # priced from one cached book read is one observation wearing many row
    # numbers: in production 23 of 27 rows were a single in-play match and 14
    # shared one identical ask. Row count alone would present that as a
    # settled distribution, which is the confident-and-wrong shape.
    n_markets = len({r.get("token_id") for r in pen_rows if r.get("token_id")})
    read_keys = {(r.get("token_id"), r.get("book_ts")) for r in pen_rows
                 if r.get("token_id") and r.get("book_ts") is not None}
    # None, not 0, when no row carries the field: "0 book reads behind 38
    # samples" is not a low number, it is an unmeasured one, and rendering it
    # as zero produced a correct-looking warning for an untrue reason.
    n_reads = len(read_keys) if read_keys else None
    decay_rows = [r for r in pen_rows
                  if r.get("penalty_bps_t1") is not None
                  and r.get("penalty_bps") is not None
                  and not r.get("t1_stale", False)]
    # Key on the READ that produced the move, never on the penalty values.
    # penalty_bps is computed against each copy's OWN their_price, so 13
    # copies priced off one cached book read yield 13 different penalties and
    # would have counted as 13 independent moves. Production showed 15
    # distinct reads reported as 37 moves, which is impossible by
    # construction. Bounding moves by reads makes that unrepresentable.
    move_keys = {(r.get("token_id"), r.get("book_ts")) for r in decay_rows
                 if r.get("token_id") and r.get("book_ts") is not None}
    n_moves = len(move_keys) if move_keys else None
    return {
        "n": len(rows),
        "n_markets": n_markets,
        "n_book_reads": n_reads,
        "n_decay_moves": n_moves,
        # Rows dropped because their independence is unknowable while other
        # rows' is known. Named on the panel, never silently averaged in.
        "n_excluded_unmeasured": n_unmeasured,
        "n_excluded_boot": sum(1 for r in rows if r.get("boot_flush")),
        "n_excluded_lag": sum(
            1 for r in rows
            if r.get("penalty_bps") is not None
            and not r.get("boot_flush", False)
            and not valid_for_penalty(r)),
        # Detected trades we could not price at all (one-sided or empty books).
        # Named, because those are disproportionately the expensive-to-copy
        # ones: dropping them silently would move book A's survivor bias out
        # of the fill gate and into the book read.
        "n_unquotable": sum(1 for r in rows if r.get("unquotable")),
        # _num, not float(): usable_quote() already tolerates a corrupt
        # quote_lag_s, and this line raising on the same value would take the
        # whole /speed command down with it.
        "quote_lag_p50_s": _pct(
            _finite([v for v in (_num(r.get("quote_lag_s")) for r in rows)
                     if v is not None]), 0.50),
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
