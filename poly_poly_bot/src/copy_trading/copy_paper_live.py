"""Live Polymarket I/O for the forward paper-copy harness (Strategy 1b).

Factored out of the CLI so the offline script and the in-bot runner share one
tested code path. These are the three dependencies `CopyPaperEngine` needs:
detection (data-api), books (CLOB), resolution (gamma). They place no orders.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

from src.copy_trading.trader_scoring import classify_market

DATA = os.environ.get("DATA_API_URL", "https://data-api.polymarket.com")
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

_S = requests.Session()


def _get(base: str, path: str, **params):
    for _ in range(3):
        try:
            r = _S.get(base + path, params=params, timeout=25)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(0.3)
    return None


# Detection-funnel reject counters (the starvation autopsy). One canonical key
# set, shared by both detector flavors and by main.py's cycle log line.
REJECT_STAT_KEYS = ("rows", "not_buy", "stale", "price_band",
                    "below_min_usd", "missing_ids", "emitted")


def _blank_reject_stats() -> dict:
    return {k: 0 for k in REJECT_STAT_KEYS}


def make_detector(
    wallets: list[str],
    max_age_s: float,
    min_usd: float,
    flagged_by_map: Optional[dict] = None,
    horizon_resolver: Optional[Callable[[str], Optional[float]]] = None,
):
    """Return a detector() yielding fresh, large target BUY trades to copy.

    ``flagged_by_map`` maps a lowercased wallet -> list of discovery strategy
    theories that flagged it; each emitted trade carries that list so the paper
    position can be attributed to a strategy at open time.

    ``horizon_resolver`` (condition_id -> days-until-resolution) stamps each trade
    with ``horizon_days`` so the engine can route it by its own resolution date:
    the near-term book skips far-future bets, the long-horizon book takes only
    them. Omitted (None) -> ``horizon_days`` is None and routing is horizon-blind
    (every detected BUY is eligible), preserving the original behaviour.
    """
    fb = {k.lower(): v for k, v in (flagged_by_map or {}).items()}

    def detect() -> list[dict]:
        out = []
        stats = detect.stats = _blank_reject_stats()
        cutoff = time.time() - max_age_s
        for w in wallets:
            acts = _get(DATA, "/activity", user=w, limit=30) or []
            # Stamped per wallet, right after ITS fetch returns: a sweep over
            # ~400 wallets takes minutes, so a single stamp taken at the top
            # of detect() would credit the last wallet with the first one's
            # freshness and understate real latency by most of the sweep.
            now_wall = time.time()
            for a in acts:
                stats["rows"] += 1
                if a.get("type") != "TRADE" or a.get("side") != "BUY":
                    stats["not_buy"] += 1
                    continue
                if float(a.get("timestamp") or 0) < cutoff:
                    stats["stale"] += 1
                    continue
                price = float(a.get("price") or 0)
                if not (0.05 <= price <= 0.95):
                    stats["price_band"] += 1
                    continue
                usd = float(a.get("usdcSize") or 0)
                if usd <= 0:
                    usd = float(a.get("size") or 0) * price
                if usd < min_usd:
                    stats["below_min_usd"] += 1
                    continue
                tx = a.get("transactionHash") or ""
                token = a.get("asset") or ""
                if not tx or not token:
                    stats["missing_ids"] += 1
                    continue
                title = a.get("title", "") or ""
                condition_id = a.get("conditionId", "")
                horizon_days = (
                    horizon_resolver(condition_id) if horizon_resolver else None
                )
                out.append({
                    "copy_id": f"{tx}-{token}",
                    "target": w,
                    "condition_id": condition_id,
                    "token_id": token,
                    "outcome_index": int(a.get("outcomeIndex") or 0),
                    "category": classify_market(title),
                    "title": title,
                    # event slug drives the polymarket.com/event/<slug> link;
                    # data-api uses eventSlug, falling back to the market slug.
                    "slug": a.get("eventSlug") or a.get("slug") or "",
                    # event_key groups same-event markets for the per-(wallet,
                    # event) cap — eventSlug ONLY, no market-slug fallback: a
                    # market slug is unique per market, so falling back would
                    # give correlated props falsely-distinct keys and the cap
                    # would "pass" without grouping anything (2026-07-16 review
                    # catch). Empty = ungroupable, engine counts it visibly.
                    "event_key": a.get("eventSlug") or "",
                    "flagged_by": tuple(fb.get(w.lower(), ())),
                    # days until the bet's market resolves (None if unknown) —
                    # routes the bet between the near-term and long-horizon books.
                    "horizon_days": horizon_days,
                    "their_price": price,
                    "their_usd": usd,
                    # --- latency witnesses (additive; no filter reads these) ---
                    # their_ts: when the TARGET traded, per the data-api row.
                    # detected_at: when THIS poll actually saw it. The gap is
                    # the notify latency a real copier would be racing, and
                    # it is measured here rather than modeled because every
                    # other surface in this repo only had the target's own
                    # timestamp to work with.
                    "their_ts": float(a.get("timestamp") or 0),
                    "detected_at": now_wall,
                })
                stats["emitted"] += 1
        return out

    detect.stats = {}
    return detect


def fetch_asks(token_id: str) -> list[tuple[float, float]]:
    b = _get(CLOB, "/book", token_id=token_id)
    if not b:
        return []
    return [(float(a["price"]), float(a["size"])) for a in (b.get("asks") or [])]


def fetch_bids(token_id: str) -> list[tuple[float, float]]:
    """Best-bid-first list — the price we could SELL into when mirroring an exit."""
    b = _get(CLOB, "/book", token_id=token_id)
    if not b:
        return []
    bids = [(float(x["price"]), float(x["size"])) for x in (b.get("bids") or [])]
    bids.sort(reverse=True)  # highest (best) bid first
    return bids


def make_exit_detector(wallets: list[str], max_age_s: float, max_pages: int = 6):
    """Return a detector() yielding recent target SELLs (to mirror as exits).

    Pages through each wallet's activity (newest-first) until it reaches events
    older than ``max_age_s``, so a SELL isn't missed just because an active
    wallet printed dozens of other events after it. Without paging (the old
    limit=30 single page) a busy scalper's exit scrolled off the first page
    before the next poll, which is why the live book only ever mirrored ~7% of
    exits — the one positive-edge leg. ``max_pages`` bounds the cost per wallet.
    """

    def detect() -> list[dict]:
        out = []
        cutoff = time.time() - max_age_s
        for w in wallets:
            offset = 0
            for _ in range(max_pages):
                acts = _get(DATA, "/activity", user=w, limit=100, offset=offset) or []
                if not acts:
                    break
                stop = False
                for a in acts:
                    if float(a.get("timestamp") or 0) < cutoff:
                        stop = True
                        continue
                    if a.get("type") != "TRADE" or a.get("side") != "SELL":
                        continue
                    token = a.get("asset") or ""
                    if not token:
                        continue
                    out.append({
                        "target": w,
                        "token_id": token,
                        "their_price": float(a.get("price") or 0),
                    })
                if stop or len(acts) < 100:
                    break
                offset += 100
        return out

    return detect


# --------------------------------------------------------------------------- #
# Shared global /trades feed — near-real-time detection at fixed cost
# --------------------------------------------------------------------------- #
# Polls the global recent-trades feed ONCE per cycle and filters it to the
# watched wallets, instead of hitting each wallet's /activity (N calls/cycle).
# Detection cost is then independent of how many wallets are on the watchlist, so
# the shortlist scales to hundreds while detection stays near-real-time.

_TRADES_MAX_OFFSET = 3000  # data-api hard-caps /trades pagination here (>3000 -> HTTP 400)


def _fetch_trades_page(min_usd: float, offset: int) -> list[dict]:
    """One newest-first page of the global trade feed for trades >= ``min_usd``.

    ``filterType=CASH&filterAmount`` bounds the feed to material trades so one poll
    covers the recent window without paging the whole exchange; ``takerOnly=true``
    returns each fill once (the taker leg), matching the discovery universe build.
    """
    return _get(
        DATA, "/trades", limit=500, offset=offset,
        filterType="CASH", filterAmount=int(max(min_usd, 1)), takerOnly="true",
    ) or []


class TradeFeed:
    """One shared poll of the global /trades feed per cycle, cached briefly.

    The BUY detector and the SELL (exit) detector both read ``recent()`` within a
    cycle, so a short TTL collapses them onto a single fetch. Newest-first paging
    stops as soon as it crosses the age cutoff (so a quiet window costs one page)
    or hits the data-api's offset cap.
    """

    def __init__(self, fetch=_fetch_trades_page, now=time.time, ttl_s: float = 10.0):
        self._fetch = fetch
        self._now = now
        self._ttl_s = ttl_s
        self._cache: list[dict] = []
        self._cache_ts: float = 0.0

    def recent(self, min_usd: float, max_age_s: float) -> list[dict]:
        if self._cache and (self._now() - self._cache_ts) < self._ttl_s:
            return self._cache
        cutoff = self._now() - max_age_s
        out: list[dict] = []
        offset = 0
        while offset <= _TRADES_MAX_OFFSET:
            page = self._fetch(min_usd, offset) or []
            if not page:
                break
            crossed = False
            for t in page:
                if float(t.get("timestamp") or 0) < cutoff:
                    crossed = True   # newest-first: this and the rest are too old
                    break
                out.append(t)
            if crossed or len(page) < 500:
                break
            offset += 500
        self._cache = out
        self._cache_ts = self._now()
        return out


def make_feed_detector(
    wallets: list[str],
    max_age_s: float,
    min_usd: float,
    flagged_by_map: Optional[dict] = None,
    horizon_resolver: Optional[Callable[[str], Optional[float]]] = None,
    feed: Optional["TradeFeed"] = None,
    feed_min_usd: Optional[float] = None,
):
    """Feed-based drop-in for ``make_detector`` — same emitted trade shape.

    Reads the shared global feed (down to ``feed_min_usd``, a low floor so exits
    are also visible) and keeps only watched-wallet copyable BUYs >= ``min_usd``.
    """
    fb = {k.lower(): v for k, v in (flagged_by_map or {}).items()}
    watched = {w.lower() for w in wallets}
    feed = feed if feed is not None else TradeFeed()
    floor = feed_min_usd if feed_min_usd is not None else min_usd

    def detect() -> list[dict]:
        out = []
        # watched-wallet detection-funnel stats — the starvation autopsy. Only
        # rows that already passed the watched filter are counted, so a busy
        # global feed can't drown the signal ("our wallets trade but nothing
        # opens" must be attributable to a reason, 2026-07 A-book stall RCA).
        stats = detect.stats = _blank_reject_stats()
        now_wall = time.time()
        cutoff = now_wall - max_age_s
        for t in feed.recent(floor, max_age_s):
            w = t.get("proxyWallet") or t.get("user") or ""
            if w.lower() not in watched:
                continue
            stats["rows"] += 1
            if str(t.get("side") or "").upper() != "BUY":
                stats["not_buy"] += 1
                continue
            if float(t.get("timestamp") or 0) < cutoff:
                stats["stale"] += 1
                continue
            price = float(t.get("price") or 0)
            if not (0.05 <= price <= 0.95):
                stats["price_band"] += 1
                continue
            usd = float(t.get("usdcSize") or 0) or float(t.get("size") or 0) * price
            if usd < min_usd:
                stats["below_min_usd"] += 1
                continue
            tx = t.get("transactionHash") or ""
            token = t.get("asset") or ""
            if not tx or not token:
                stats["missing_ids"] += 1
                continue
            title = t.get("title", "") or ""
            condition_id = t.get("conditionId", "") or ""
            horizon_days = (
                horizon_resolver(condition_id) if horizon_resolver else None
            )
            out.append({
                "copy_id": f"{tx}-{token}",
                "target": w,
                "condition_id": condition_id,
                "token_id": token,
                "outcome_index": int(t.get("outcomeIndex") or 0),
                "category": classify_market(title),
                "title": title,
                "slug": t.get("eventSlug") or t.get("slug") or "",
                # eventSlug ONLY (no market-slug fallback) — see make_detector.
                "event_key": t.get("eventSlug") or "",
                "flagged_by": tuple(fb.get(w.lower(), ())),
                "horizon_days": horizon_days,
                "their_price": price,
                "their_usd": usd,
                # latency witnesses — see make_detector. The feed carries the
                # target's own trade timestamp; now_wall is when this sweep
                # read it out of the feed.
                "their_ts": float(t.get("timestamp") or 0),
                "detected_at": now_wall,
            })
            stats["emitted"] += 1
        return out

    detect.stats = {}
    return detect


def make_feed_exit_detector(
    wallets: list[str],
    max_age_s: float,
    feed: Optional["TradeFeed"] = None,
    feed_min_usd: float = 100.0,
):
    """Feed-based drop-in for ``make_exit_detector`` — watched-wallet SELLs.

    Shares the same feed poll as the BUY detector (so no extra request), reading
    down to ``feed_min_usd`` so a modest exit isn't missed by the feed's floor.
    """
    watched = {w.lower() for w in wallets}
    feed = feed if feed is not None else TradeFeed()

    def detect() -> list[dict]:
        out = []
        cutoff = time.time() - max_age_s
        for t in feed.recent(feed_min_usd, max_age_s):
            w = t.get("proxyWallet") or t.get("user") or ""
            if w.lower() not in watched:
                continue
            if str(t.get("side") or "").upper() != "SELL":
                continue
            if float(t.get("timestamp") or 0) < cutoff:
                continue
            token = t.get("asset") or ""
            if not token:
                continue
            out.append({
                "target": w,
                "token_id": token,
                "their_price": float(t.get("price") or 0),
            })
        return out

    return detect


def resolve(condition_id: str) -> Optional[int]:
    """Winning outcome index for a resolved market, else None (still open)."""
    if not condition_id:
        return None
    j = _get(GAMMA, "/markets", condition_ids=condition_id, closed="true")
    if not j:
        return None
    op = j[0].get("outcomePrices")
    if isinstance(op, str):
        try:
            op = json.loads(op)
        except json.JSONDecodeError:
            return None
    if not op:
        return None
    for i, p in enumerate(op):
        try:
            if float(p) >= 0.99:
                return i
        except (ValueError, TypeError):
            continue
    return None


def _parse_end_ts(end_date: str) -> Optional[float]:
    """Parse a Gamma ``endDate`` ISO string to a unix timestamp, or None."""
    if not end_date:
        return None
    try:
        dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def fetch_end_ts(condition_id: str) -> Optional[float]:
    """Resolution end timestamp (unix secs) for a market via Gamma, or None.

    No ``closed`` filter, so it sees *open* markets — exactly the far-future ones
    a long-horizon bet lives in (Gamma's /markets returns only open markets unless
    ``closed=true``). A market that's already settled returns nothing here, which
    reads as "no horizon" → the bet is treated near-term, which is correct."""
    if not condition_id:
        return None
    j = _get(GAMMA, "/markets", condition_ids=condition_id)
    if not j:
        return None
    return _parse_end_ts(j[0].get("endDate") or j[0].get("endDateIso") or "")


def make_horizon_resolver(
    now: Callable[[], float] = time.time,
) -> Callable[[str], Optional[float]]:
    """Return ``horizon(condition_id) -> days until resolution`` (endDate − now).

    The detector calls this on every fresh BUY so each bet can be routed by its
    own resolution date — short bets to the near-term copier, far-future bets to
    the long-horizon book. End dates are cached per condition (they're effectively
    fixed), so the live loop costs one Gamma call per *new* market, not per cycle.
    A failed lookup isn't cached, so a transient miss retries next time; it just
    returns None meanwhile (treated as near-term)."""
    cache: dict[str, float] = {}

    def horizon(condition_id: str) -> Optional[float]:
        if not condition_id:
            return None
        ts = cache.get(condition_id)
        if ts is None:
            ts = fetch_end_ts(condition_id)
            if ts is None:
                return None
            cache[condition_id] = ts
        return (ts - now()) / 86400.0

    return horizon


def fetch_mid(token_id: str) -> Optional[float]:
    """Current mid price (mean of best ask and best bid) for marking a position
    to market. None when either side of the book is empty (no usable quote)."""
    b = _get(CLOB, "/book", token_id=token_id)
    if not b:
        return None
    asks = [float(a["price"]) for a in (b.get("asks") or [])]
    bids = [float(x["price"]) for x in (b.get("bids") or [])]
    if not asks or not bids:
        return None
    return (min(asks) + max(bids)) / 2.0


def _load_targets(path: str) -> list[dict]:
    """The ``targets`` list from a watchlist JSON, or [] on missing/corrupt file.

    One place to harden watchlist parsing for every per-field loader below, so a
    schema or error-handling change can't be applied to one loader and missed by
    the others (which would degrade the book non-uniformly)."""
    if not path or not os.path.exists(path):
        return []
    try:
        data = json.load(open(path))
    except (json.JSONDecodeError, OSError):
        return []
    return data.get("targets", []) or []


def load_watchlist_wallets(path: str) -> list[str]:
    """Read wallet addresses from a trader_scoring_backtest watchlist JSON."""
    return [t["wallet"] for t in _load_targets(path) if t.get("wallet")]


def load_watchlist_flagged_by(path: str) -> dict:
    """Map lowercased wallet -> list of discovery theories (``flagged_by``).

    Lets the paper harness stamp each opened position with the strategy theories
    that qualified the target, for per-strategy P&L attribution. Missing file or
    missing field -> empty map / empty list."""
    out: dict = {}
    for t in _load_targets(path):
        w = t.get("wallet")
        if w:
            out[w.lower()] = list(t.get("flagged_by", []))
    return out


def load_watchlist_categories(path: str) -> dict:
    """Map lowercased wallet -> set of approved "winning market" categories.

    The discovery sweep stamps each target with ``approved_categories`` — the
    market types where its copy-and-hold edge cleared real-money cost. The engine
    restricts a wallet's copies to those categories.

    Only wallets with a NON-EMPTY approved set are returned. An empty set means
    the wallet has no PROVEN winning market yet (it's still accruing resolved
    copies — discovery already drops wallets that have enough evidence AND no
    winning market). Treating that as "block everything" would deadlock such a
    wallet and could silently empty the book; instead we omit it, so the engine
    leaves it unrestricted ("absent -> don't block") until a winning category is
    proven, at which point it gets restricted to it."""
    out: dict = {}
    for t in _load_targets(path):
        w = t.get("wallet")
        cats = set(t.get("approved_categories") or [])
        if w and cats:
            out[w.lower()] = cats
    return out


def load_watchlist_median_usd(path: str) -> dict:
    """Map lowercased wallet -> its own median copyable BUY size (USD).

    Used for conviction sizing: a copy is scaled to the target's bet relative to
    this median. Missing field -> wallet omitted (engine falls back to legacy
    proportional sizing for it)."""
    out: dict = {}
    for t in _load_targets(path):
        w = t.get("wallet")
        v = t.get("median_usd")
        if w and v:
            try:
                out[w.lower()] = float(v)
            except (ValueError, TypeError):
                continue
    return out
