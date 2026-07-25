"""Forward paper-copy harness for Strategy 1b (execution-drag measurement).

The backtest (`backtest/trader_scoring_backtest.py`) shows that selecting copy
targets by realized closed-position ROI has strong out-of-sample edge — but it
assumes you fill at the target's price. In reality you see their trade *after*
it prints, the price has already moved, and you pay the spread to chase it.

This harness measures how much of that edge survives realistic execution. It
watches a watchlist of target wallets; when a target opens a BUY, it simulates a
copy entry against the *current* live order book (not their price), tracks the
paper position to resolution, and records both the realized PnL and the
execution drag (our entry price − their price). It places no real orders.

The core (fill simulation, PnL, dedup, ledger) is pure and unit tested; live
data access is injected so tests run without the network. A wallet graduates to
real capital only once its *copied* PnL is positive in this ledger.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional


# --------------------------------------------------------------------------- #
# Fill simulation (pure)
# --------------------------------------------------------------------------- #

# Floor on a credible fill price, as a fraction of the target's price. A
# same-side ask far below what the target just paid is stale/erroneous book
# data (a real CLOB ask under the market is arbitraged instantly), so we don't
# fill there. Shared by the live fill simulator and the dust-position guard.
#
# 2026-07-25 (ROADMAP P0-1): raised 0.5 -> 0.97. At 0.5 the simulator swept
# asks down to HALF the target's price: 39% of settled A-copies filled >2%
# better than the target, and that +$535 gift was the book's entire +$537
# profit — the whole "edge" was a fill-model artifact. A credible same-side
# ask cannot sit 3%+ under what the target just paid (our detection lag is
# minutes, not hours); anything cheaper is stale book data we now refuse.
MIN_FILL_FRAC = 0.97

# Threshold for the historical dust-fill quarantine, as a fraction of the
# target's price. Deliberately DECOUPLED from MIN_FILL_FRAC: the simulator
# floor moved (0.5 -> 0.97, P0-1) but the quarantine must keep its original
# semantics — it exists to hide only the pre-fix ABSURD fills (a stale ~0.001
# ask swept into tens of thousands of shares), not to reclassify the legacy
# fills in [0.5x, 0.97x) whose honest economics the at-their-price re-baseline
# (P0-2) needs visible in the settled sample. is_dust_fill() pins this at 0.5
# no matter where the simulator floor sits.
DUST_FILL_FRAC = 0.5


@dataclass
class FillSim:
    avg_price: float      # our realised average entry price (0 if unfilled)
    spent: float          # USDC deployed
    shares: float         # shares acquired
    drag_bps: int         # (avg_price - their_price) in bps of their_price


def simulate_copy_fill(
    their_price: float,
    asks: list[tuple[float, float]],
    copy_usd: float,
    *,
    max_slippage_bps: int = 200,
    min_fill_frac: float = MIN_FILL_FRAC,
) -> FillSim:
    """Simulate copying a BUY by walking the live asks book.

    We deploy up to ``copy_usd``, taking ask levels in ascending price until the
    budget is filled or the price exceeds ``their_price * (1 + max_slippage)``
    (we don't chase beyond that). Captures the realistic adverse-selection cost
    of acting after the target.

    Levels priced below ``their_price * min_fill_frac`` are skipped as stale or
    erroneous book data: a credible same-side ask can't sit far under the price
    the target just paid (a real CLOB ask below the market would be arbitraged
    instantly). Without this floor a single dust ask (e.g. 0.001 under a 0.62
    market) gets swept, inflating the share count and producing a nonsensical
    favourable "drag" of tens of thousands of dollars.
    """
    if their_price <= 0 or copy_usd <= 0 or not asks:
        return FillSim(0.0, 0.0, 0.0, 0)
    max_price = their_price * (1 + max_slippage_bps / 10000.0)
    min_price = their_price * min_fill_frac
    spent = shares = 0.0
    for price, size in sorted(asks):
        if price < min_price:
            continue  # non-credible deep-discount level — skip, don't sweep it
        if price > max_price or price >= 1.0 or size <= 0:
            break
        take_usd = min(copy_usd - spent, price * size)
        if take_usd <= 1e-9:
            break
        spent += take_usd
        shares += take_usd / price
        if spent >= copy_usd - 1e-9:
            break
    if shares <= 0:
        return FillSim(0.0, 0.0, 0.0, 0)
    avg = spent / shares
    drag = int(round((avg - their_price) / their_price * 10000)) if their_price else 0
    return FillSim(avg_price=avg, spent=spent, shares=shares, drag_bps=drag)


# --------------------------------------------------------------------------- #
# Paper position + ledger
# --------------------------------------------------------------------------- #

@dataclass
class PaperPosition:
    copy_id: str            # dedup key: {their_tx}-{token}
    target: str             # copied wallet
    condition_id: str
    token_id: str
    outcome_index: int
    category: str
    their_price: float
    entry_price: float      # our realised avg
    shares: float
    spent: float
    drag_bps: int
    opened_ts: float
    # human-readable context for notifications (optional; default-safe so old
    # ledger lines that predate these keys still load):
    title: str = ""         # market question, e.g. "Will BTC hit $100k in 2025?"
    slug: str = ""          # PM event slug -> polymarket.com/event/<slug>
    # Grouping key for the per-(wallet, event) cap: the feed's eventSlug ONLY —
    # never the market-slug fallback that `slug` may hold (market slugs are
    # unique per market, so they'd give correlated same-event props
    # falsely-distinct keys). Empty = ungroupable. Default-safe for old rows.
    event_key: str = ""
    # Discovery strategy theories that flagged the target wallet (e.g. ("1b","1f")),
    # stamped at open so per-strategy P&L attribution is stable even as the
    # watchlist re-flags the wallet later. Default-safe: old ledger lines load as ().
    flagged_by: tuple = ()
    # Which track booked this position — "1" (near-term copy) or "4" (long-horizon
    # book). Routed live by the bet's own horizon, so a single wallet's short bets
    # land here as "1" and its far-future bets as "4". Default-safe: old rows -> "1".
    strategy: str = "1"
    horizon_days: float = 0.0   # bet horizon at open (market endDate − entry), days
    # Mark-to-market (Strategy-4 long-horizon positions sit open for months, so we
    # mark them to a live mid instead of showing a blank until resolution). Unused
    # by the near-term book, which holds briefly and reports only realized PnL.
    mark_price: float = 0.0
    marked_ts: float = 0.0
    unrealized_pnl: float = 0.0
    # filled on resolution OR on following the target's exit:
    closed: bool = False
    won: Optional[bool] = None
    pnl: float = 0.0
    ideal_pnl: float = 0.0  # PnL had we filled at their_price (drag-free)
    closed_ts: float = 0.0
    exited_early: bool = False  # closed by mirroring the target's SELL, not resolution
    # Opened only thanks to the starved-wallet cap relief — a REAL-money book at
    # the normal category cap would have skipped this fill. Stamped so promotion
    # review can audit how much of a wallet's paper evidence came in over the
    # real cap. Default-safe: old ledger rows load as False.
    over_real_cap: bool = False

    def realize(self, won: bool, now: Optional[float] = None) -> None:
        payout = self.shares if won else 0.0
        self.won = won
        self.pnl = payout - self.spent
        ideal_cost = self.shares * self.their_price
        self.ideal_pnl = payout - ideal_cost
        self.closed = True
        self.closed_ts = now if now is not None else time.time()

    def realize_exit(self, exit_price: float, now: Optional[float] = None) -> None:
        """Close by mirroring the target's early SELL, at our achievable exit price.

        Traders don't always hold to resolution — when the target sells, we sell
        too, booking PnL at the price we could actually get rather than waiting
        for (and gambling on) settlement.
        """
        proceeds = self.shares * exit_price
        self.pnl = proceeds - self.spent
        self.ideal_pnl = proceeds - self.shares * self.their_price
        self.won = self.pnl > 0
        self.closed = True
        self.exited_early = True
        self.closed_ts = now if now is not None else time.time()

    def mark(self, mid: float, now: Optional[float] = None) -> None:
        """Mark an open position to a current mid price (Strategy-4 book).

        A long-horizon position can sit open for months; marking it to the live
        mid gives ``/pnl`` a running unrealized P&L instead of a blank until the
        market settles. No-op on a closed position or a non-positive mid (an
        empty/stale book), so a transient missing quote never corrupts the mark.
        """
        if self.closed or mid <= 0:
            return
        self.mark_price = mid
        self.unrealized_pnl = self.shares * mid - self.spent
        self.marked_ts = now if now is not None else time.time()


class PaperCopyLedger:
    """Append-only JSON ledger of paper-copy positions (open + closed)."""

    def __init__(self, path: str):
        self.path = path
        self.positions: dict[str, PaperPosition] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        # Tolerate unknown keys so a ledger written by a NEWER build (extra
        # fields) still loads after a rollback — a schema addition must never
        # brick the harness on the way back down.
        fields = {f.name for f in dataclasses.fields(PaperPosition)}
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                self.positions[d["copy_id"]] = PaperPosition(
                    **{k: v for k, v in d.items() if k in fields})

    def _persist(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            for p in self.positions.values():
                d = asdict(p)
                # Write over_real_cap only when set: rows stay byte-identical to
                # the legacy schema unless cap relief actually fired, so a
                # rollback to a strict-loader build doesn't brick on the ledger.
                if not d.get("over_real_cap"):
                    d.pop("over_real_cap", None)
                f.write(json.dumps(d) + "\n")
        os.replace(tmp, self.path)

    def has(self, copy_id: str) -> bool:
        return copy_id in self.positions

    def add(self, pos: PaperPosition) -> None:
        self.positions[pos.copy_id] = pos
        self._persist()

    def open_positions(self) -> list[PaperPosition]:
        return [p for p in self.positions.values() if not p.closed]

    def closed_positions(self) -> list[PaperPosition]:
        return [p for p in self.positions.values() if p.closed]

    def save(self) -> None:
        self._persist()


# --------------------------------------------------------------------------- #
# Engine (I/O injected)
# --------------------------------------------------------------------------- #

# detector() -> list of new target BUY trades, each a dict with keys:
#   copy_id, target, condition_id, token_id, outcome_index, category,
#   their_price, their_usd
DetectFn = Callable[[], list[dict]]
# book_fetcher(token_id) -> list[(price, size)] asks
BookFn = Callable[[str], list[tuple[float, float]]]
# resolver(condition_id) -> winning outcome_index, or None if unresolved
ResolveFn = Callable[[str], Optional[int]]
# mark_fetcher(token_id) -> current mid price, or None if no live quote
MarkFn = Callable[[str], Optional[float]]


@dataclass
class CycleSummary:
    detected: int = 0
    opened: int = 0
    skipped_unfilled: int = 0
    # guardrail skips (why a detected BUY was NOT copied):
    skipped_fill_gate: int = 0          # our fill landed too far from their price (either side)
    skipped_not_first_entry: int = 0    # averaging-down / re-entry into a copied market
    skipped_slate_cap: int = 0          # per-(wallet|category)-day concentration cap hit
    skipped_event_cap: int = 0          # per-(wallet,event) concurrent-exposure cap hit
    skipped_already_copied: int = 0     # copy_id already in the ledger (feed re-emit)
    opened_unkeyed_event: int = 0       # opened WITHOUT an event key while the event
                                        # cap is on (feed row had no eventSlug — the
                                        # cap could not group it; visible, not silent)
    skipped_category_gate: int = 0      # market category not in this wallet's approved
                                        # "winning markets" set (copy-and-hold edge gate)
    skipped_horizon: int = 0            # bet's resolution date is on the wrong side of the
                                        # horizon cut for this book (S1 skips longs, S4 skips shorts)
    resolved: int = 0
    marked: int = 0  # open positions marked-to-market this cycle (Strategy-4 book)
    exited: int = 0  # closed by following the target's SELL
    # the positions that resolved *this* cycle, so callers can name them in a
    # notification instead of only reporting cumulative ledger aggregates.
    resolved_positions: list["PaperPosition"] = field(default_factory=list)
    # detection-funnel rejects for this cycle (filled by the runner from the
    # detector's own stats, when the detector exposes them): reason -> count of
    # watched-wallet feed rows dropped BEFORE the engine ever saw them. This is
    # the starvation autopsy — "wallets trade but nothing opens" is invisible
    # without it (2026-07 A-book stall RCA).
    detector_rejects: dict = field(default_factory=dict)
    # every slate-cap bind this cycle as (target, category, "wallet-day" |
    # "category-day") — the aggregate skipped_slate_cap says HOW OFTEN the cap
    # bound, this says WHO it bound, so a book whose thesis is take-everything
    # (strategy B) can autopsy whether its caps are re-creating the censoring
    # it exists to remove.
    slate_cap_binds: list = field(default_factory=list)


class CopyPaperEngine:
    def __init__(
        self,
        ledger: PaperCopyLedger,
        detector: DetectFn,
        book_fetcher: BookFn,
        resolver: ResolveFn,
        *,
        copy_pct: float = 1.0,
        max_copy_usd: float = 50.0,
        max_slippage_bps: int = 200,
        exit_detector: Optional[DetectFn] = None,
        bid_fetcher: Optional[BookFn] = None,
        # --- entry guardrails (all default-OFF so the bare engine is unchanged;
        # the live runner switches them on from config) ---
        # fill-gate: skip a copy whose achievable fill lands more than this many
        # bps from the target's price ON EITHER SIDE (None = off). Two-sided
        # since 2026-07-25 (ROADMAP P0-1): the old one-sided gate rejected fills
        # 1.5% WORSE than the target but kept fills 50% BETTER — a filter that
        # rejects bad luck and keeps good luck manufactures alpha by
        # construction (it was Book A's entire profit). A far-better fill is the
        # same class of evidence corruption as a far-worse one: the book moved
        # since the target traded, so the copy no longer measures the target's
        # edge at the target's price.
        fill_gate_bps: Optional[int] = None,
        # only copy a wallet's FIRST entry into a (market, outcome); skip its
        # averaging-down / re-entry buys (the harness copies the opening trade).
        first_entry_only: bool = False,
        # slate circuit-breaker: cap copies opened per UTC day per wallet and per
        # category, so one correlated same-day slate can't dominate (None = off).
        max_copies_per_wallet_day: Optional[int] = None,
        max_copies_per_category_day: Optional[int] = None,
        # per-(wallet, event) concurrent-exposure cap: at most this many OPEN
        # copies of one wallet inside one underlying event (same match/fixture,
        # keyed by the trade's event slug). The day-caps above don't see this —
        # 3 props on ONE game pass a 3/wallet-day cap yet win or lose together
        # (0xa6fa lost $345 exactly this way, 2026-07 race RCA). A position that
        # resolves frees its slot; an empty slug is uncapped (can't group).
        # None = off, engine unchanged.
        max_copies_per_wallet_event: Optional[int] = None,
        # --- borrowed-clock fill (strategy B) ---
        # When set, fill every admitted copy AT THE TARGET'S OWN PRICE plus this
        # many bps, without walking the live book: the instant-copy paper book.
        # Detection may still lag minutes (data-api indexer), but the recorded
        # economics are those of a copier that acted within seconds — which is
        # exactly the evidence the promotion gate needs for that strategy, and
        # the same fill regime the A-vs-B counterfactual estimate used. None
        # (default) keeps the live-book walk, so the bare engine is unchanged.
        fill_at_their_price_bps: Optional[int] = None,
        # --- bet-horizon routing (Strategy 1 vs 4) ---
        # Only act on a detected BUY whose horizon (market endDate − now, in days,
        # carried on the trade as ``horizon_days``) is in this book's band. The
        # near-term book sets ``max_horizon_days`` so it skips far-future bets; the
        # long-horizon book sets ``min_horizon_days`` so it takes only those. Both
        # None (default) = horizon-blind, so the bare engine is unchanged.
        min_horizon_days: Optional[float] = None,
        max_horizon_days: Optional[float] = None,
        # mark open positions to a live mid each cycle (the long-horizon book; the
        # near-term book holds briefly and leaves this off).
        mark_fetcher: Optional[MarkFn] = None,
        # stamped on every position this engine opens, for per-strategy P&L.
        strategy: str = "1",
        # --- winning-markets-only gate (item A) ---
        # lowercased wallet -> set of approved market categories. When provided,
        # a detected BUY is copied only if its category is in that wallet's set
        # (its copy-and-hold edge cleared real-money cost there). A wallet ABSENT
        # from the map is unrestricted (no category data yet -> don't block); an
        # empty set means "copy none". None (default) = gate off, engine unchanged.
        allowed_categories: Optional[dict[str, set]] = None,
        # --- conviction sizing (item C) ---
        # wallet -> its own median copyable BUY size (USD). With this + a base
        # unit, each copy is sized to the target's conviction: their_usd relative
        # to their own median, winsorized to [conviction_min, conviction_max] of
        # the base unit, then capped at max_copy_usd. None (default) keeps the
        # legacy min(max_copy_usd, their_usd * copy_pct) sizing.
        wallet_median_usd: Optional[dict[str, float]] = None,
        conviction_base_usd: Optional[float] = None,
        conviction_min: float = 0.25,
        conviction_max: float = 2.0,
        # --- confidence-tiered stake (downward only; 2026-07 race RCA) ---
        # lowercased wallet -> stake multiplier in (0, 1]. A wallet the LLM gate
        # admitted on thin/low-confidence evidence starts at a fraction of the
        # normal size until its own settled record accrues; the multiplier NEVER
        # exceeds 1.0 (clamped), so this can only shrink exposure. None = off.
        stake_frac: Optional[dict[str, float]] = None,
        # --- evidence-throughput levers (starvation RCA 2026-07; both default-OFF
        # so the bare engine is unchanged) ---
        # starved-wallet slate priority: process this cycle's detected BUYs
        # coldest-wallet-first (fewest ledger copies), so wallets that still need
        # promotion evidence claim the shared daily caps before wallets that
        # already have plenty. Same caps, same total exposure — just routed.
        starved_priority: bool = False,
        # paper-only cap relief: a wallet with fewer than relief_evidence_n
        # copies (settled + in-flight) may exceed the per-CATEGORY daily cap up
        # to relief_max_per_category_day. The per-wallet cap still binds, and a
        # position admitted past the real cap is stamped over_real_cap=True so
        # promotion review can audit relief-fed evidence. Paper carries no
        # capital risk — the category cap models real-money correlation limits,
        # which shouldn't throttle evidence accrual. None = off.
        relief_evidence_n: Optional[int] = None,
        relief_max_per_category_day: Optional[int] = None,
    ):
        self.ledger = ledger
        self.detector = detector
        self.book_fetcher = book_fetcher
        self.resolver = resolver
        self.copy_pct = copy_pct
        self.max_copy_usd = max_copy_usd
        self.max_slippage_bps = max_slippage_bps
        self.min_horizon_days = min_horizon_days
        self.max_horizon_days = max_horizon_days
        self.mark_fetcher = mark_fetcher
        self.strategy = strategy
        # exit_detector() -> target SELLs: {target, token_id, their_price};
        # bid_fetcher(token_id) -> [(bid_price, size)] for our achievable exit.
        self.exit_detector = exit_detector
        self.bid_fetcher = bid_fetcher
        self.fill_gate_bps = fill_gate_bps
        self.first_entry_only = first_entry_only
        self.max_copies_per_wallet_day = max_copies_per_wallet_day
        self.max_copies_per_category_day = max_copies_per_category_day
        self.max_copies_per_wallet_event = max_copies_per_wallet_event
        self.fill_at_their_price_bps = fill_at_their_price_bps
        self.allowed_categories = (
            {k.lower(): set(v) for k, v in allowed_categories.items()}
            if allowed_categories is not None else None)
        self.wallet_median_usd = (
            {k.lower(): float(v) for k, v in wallet_median_usd.items()}
            if wallet_median_usd is not None else None)
        self.conviction_base_usd = conviction_base_usd
        self.conviction_min = conviction_min
        self.conviction_max = conviction_max
        self.stake_frac = (
            {k.lower(): min(1.0, float(v)) for k, v in stake_frac.items()
             if float(v) > 0}
            if stake_frac is not None else None)
        self.starved_priority = starved_priority
        self.relief_evidence_n = relief_evidence_n
        self.relief_max_per_category_day = relief_max_per_category_day

    def _copy_size(self, target: str, their_usd: float) -> float:
        """USD to deploy on a copy. Conviction-sized (target's bet vs its own
        median, winsorized) when configured, else the legacy proportional cap.
        A ``stake_frac`` entry for the wallet then scales the result DOWN (thin/
        low-confidence admits start small until their own record accrues)."""
        if self.wallet_median_usd is not None and self.conviction_base_usd:
            med = self.wallet_median_usd.get((target or "").lower(), 0.0)
            mult = 1.0
            if med > 0:
                mult = max(self.conviction_min,
                           min(self.conviction_max, their_usd / med))
            usd = min(self.max_copy_usd, self.conviction_base_usd * mult)
        else:
            usd = min(self.max_copy_usd, their_usd * self.copy_pct)
        if self.stake_frac is not None:
            usd *= self.stake_frac.get((target or "").lower(), 1.0)
        return usd

    def run_cycle(self, now: Optional[float] = None) -> CycleSummary:
        now = now if now is not None else time.time()
        s = CycleSummary()

        # Guardrail state, seeded from the existing ledger so caps/dedup persist
        # across cycles and restarts, then updated as we open within this cycle.
        day = int(now // 86400)
        entered_tokens = {(p.target, p.token_id) for p in self.ledger.positions.values()}
        wallet_day: dict[str, int] = {}
        cat_day: dict[str, int] = {}
        for p in self.ledger.positions.values():
            if int((p.opened_ts or 0) // 86400) == day:
                wallet_day[p.target] = wallet_day.get(p.target, 0) + 1
                cat_day[p.category] = cat_day.get(p.category, 0) + 1
        # per-(wallet, event) concurrent exposure, seeded from OPEN positions
        # only — a resolved copy frees its event slot (the cap limits correlated
        # simultaneous exposure, not lifetime participation in an event).
        # Rows opened before event_key existed seed with their slug — for those
        # rows slug was usually the true eventSlug, and a stale market-slug
        # value simply never collides (no cap, same as before the field).
        event_open: dict[tuple[str, str], int] = {}
        if self.max_copies_per_wallet_event is not None:
            for p in self.ledger.open_positions():
                key = getattr(p, "event_key", "") or p.slug
                if key:
                    k = ((p.target or "").lower(), key)
                    event_open[k] = event_open.get(k, 0) + 1

        # per-target ledger copy counts (settled + in-flight), for the two
        # evidence-throughput levers below. In-flight copies count as evidence:
        # they WILL settle, so a cold wallet with 12 opens isn't "starved".
        evidence_n: dict[str, int] = {}
        if self.starved_priority or self.relief_evidence_n is not None:
            for p in self.ledger.positions.values():
                evidence_n[p.target] = evidence_n.get(p.target, 0) + 1

        trades = list(self.detector())
        if self.starved_priority:
            # coldest wallet first — stable sort keeps each wallet's own trades
            # in feed (chronological) order, so only cross-wallet priority moves.
            trades.sort(key=lambda tr: evidence_n.get(tr.get("target"), 0))

        for tr in trades:
            s.detected += 1
            cid = tr["copy_id"]
            if self.ledger.has(cid):
                s.skipped_already_copied += 1
                continue
            # bet-horizon routing: each book takes only bets resolving on its side
            # of the cut. An undated bet (horizon None) is treated as near-term —
            # the near-term book still copies it, the long-horizon book skips it,
            # so a missing endDate never opens a months-long paper position.
            horizon = tr.get("horizon_days")
            if self.min_horizon_days is not None and (
                horizon is None or horizon < self.min_horizon_days):
                s.skipped_horizon += 1
                continue
            if (self.max_horizon_days is not None and horizon is not None
                    and horizon >= self.max_horizon_days):
                s.skipped_horizon += 1
                continue
            target = tr["target"]
            token = tr["token_id"]
            category = tr.get("category", "other")
            # winning-markets-only gate: copy a wallet's BUY only in the market
            # categories where its copy-and-hold edge cleared real-money cost.
            # Absent wallet -> unrestricted (no category data); present -> enforce.
            if self.allowed_categories is not None:
                approved = self.allowed_categories.get((target or "").lower())
                if approved is not None and category not in approved:
                    s.skipped_category_gate += 1
                    continue
            # first-entry-only: skip averaging-down / re-entry into a market we
            # already copied from this target (we copy the opening trade only).
            if self.first_entry_only and (target, token) in entered_tokens:
                s.skipped_not_first_entry += 1
                continue
            # slate circuit-breaker: cap correlated same-day copies per wallet
            # and per category before we even price the book.
            if (self.max_copies_per_wallet_day is not None
                    and wallet_day.get(target, 0) >= self.max_copies_per_wallet_day):
                s.skipped_slate_cap += 1
                s.slate_cap_binds.append((target, category, "wallet-day"))
                continue
            over_real_cap = False
            if (self.max_copies_per_category_day is not None
                    and cat_day.get(category, 0) >= self.max_copies_per_category_day):
                # starved-wallet relief: a wallet still under the evidence floor
                # may exceed the (real-money-shaped) category cap, up to the
                # relief ceiling. The fill is stamped over_real_cap so promotion
                # review can see which evidence a real book wouldn't have.
                starved = (self.relief_evidence_n is not None
                           and evidence_n.get(target, 0) < self.relief_evidence_n)
                relief_ok = (self.relief_max_per_category_day is not None
                             and cat_day.get(category, 0) < self.relief_max_per_category_day)
                if not (starved and relief_ok):
                    s.skipped_slate_cap += 1
                    s.slate_cap_binds.append((target, category, "category-day"))
                    continue
                over_real_cap = True
            # per-(wallet, event) cap: don't stack correlated props on one
            # match — they settle together, so N positions carry ~1 position's
            # worth of independent information at N positions' worth of risk.
            # Keyed on event_key (eventSlug only). A trade with no event key
            # can't be grouped — it opens uncapped but is COUNTED, so a feed
            # that stops sending eventSlug shows up in the cycle log instead of
            # silently disabling the guard.
            event_key = tr.get("event_key") or ""
            if self.max_copies_per_wallet_event is not None and event_key:
                if (event_open.get(((target or "").lower(), event_key), 0)
                        >= self.max_copies_per_wallet_event):
                    s.skipped_event_cap += 1
                    s.slate_cap_binds.append((target, category, "wallet-event"))
                    continue
            copy_usd = self._copy_size(target, tr.get("their_usd", 0) or 0.0)
            if self.fill_at_their_price_bps is not None:
                # borrowed-clock fill: at their price + fixed drag, no book walk.
                # Detectors already band price to [0.05, 0.95]; the <=0 guard is
                # belt-and-braces so a malformed feed row can't zero-divide.
                if tr["their_price"] <= 0:
                    s.skipped_unfilled += 1
                    continue
                price = min(
                    tr["their_price"] * (1 + self.fill_at_their_price_bps / 10000.0),
                    0.999)
                fill = FillSim(avg_price=price, spent=copy_usd,
                               shares=copy_usd / price,
                               drag_bps=self.fill_at_their_price_bps)
            else:
                fill = simulate_copy_fill(
                    tr["their_price"], self.book_fetcher(tr["token_id"]),
                    copy_usd, max_slippage_bps=self.max_slippage_bps,
                )
            if fill.shares <= 0:
                s.skipped_unfilled += 1
                continue
            # fill-gate: don't copy a moved book — skip when our achievable fill
            # is more than fill_gate_bps from the target's price IN EITHER
            # DIRECTION (two-sided, P0-1: keeping only favourable surprises is
            # how the old ledger manufactured its edge).
            if (self.fill_gate_bps is not None
                    and abs(fill.drag_bps) > self.fill_gate_bps):
                s.skipped_fill_gate += 1
                continue
            self.ledger.add(PaperPosition(
                copy_id=cid, target=target, condition_id=tr["condition_id"],
                token_id=token, outcome_index=int(tr["outcome_index"]),
                category=category, their_price=tr["their_price"],
                title=tr.get("title", ""), slug=tr.get("slug", ""),
                event_key=event_key,
                flagged_by=tuple(tr.get("flagged_by", ())),
                strategy=self.strategy,
                horizon_days=float(horizon) if horizon is not None else 0.0,
                entry_price=fill.avg_price, shares=fill.shares, spent=fill.spent,
                drag_bps=fill.drag_bps, opened_ts=now,
                over_real_cap=over_real_cap,
            ))
            s.opened += 1
            entered_tokens.add((target, token))
            wallet_day[target] = wallet_day.get(target, 0) + 1
            cat_day[category] = cat_day.get(category, 0) + 1
            evidence_n[target] = evidence_n.get(target, 0) + 1
            if event_key:
                ek = ((target or "").lower(), event_key)
                event_open[ek] = event_open.get(ek, 0) + 1
            elif self.max_copies_per_wallet_event is not None:
                s.opened_unkeyed_event += 1

        # exit-following: if the target sold something we hold, sell too (at our
        # achievable bid), before falling through to the resolution path.
        if self.exit_detector is not None:
            held = {(p.target, p.token_id): p for p in self.ledger.open_positions()}
            for ex in self.exit_detector():
                pos = held.get((ex.get("target"), ex.get("token_id")))
                if pos is None:
                    continue
                exit_price = ex.get("their_price")
                if self.bid_fetcher is not None:
                    book = self.bid_fetcher(pos.token_id)
                    if book:
                        exit_price = book[0][0]  # best bid
                if exit_price is None:
                    continue
                pos.realize_exit(float(exit_price), now=now)
                s.exited += 1

        for pos in self.ledger.open_positions():
            winner = self.resolver(pos.condition_id)
            if winner is None:
                continue
            pos.realize(won=(winner == pos.outcome_index), now=now)
            s.resolved += 1
            s.resolved_positions.append(pos)

        # mark-to-market: refresh unrealized PnL on whatever's still open (the
        # long-horizon book — months between open and resolution). Runs after the
        # resolution pass so a position that just settled isn't also re-marked.
        if self.mark_fetcher is not None:
            for pos in self.ledger.open_positions():
                mid = self.mark_fetcher(pos.token_id)
                if mid is not None and mid > 0:
                    pos.mark(float(mid), now=now)
                    s.marked += 1

        if s.resolved or s.exited or s.marked:
            self.ledger.save()
        return s


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def is_dust_fill(p: PaperPosition, min_fill_frac: float = DUST_FILL_FRAC) -> bool:
    """True if a position's recorded entry is an implausible deep-discount fill.

    These can only exist in ledgers written before ``simulate_copy_fill`` grew
    its price floor — a $50 budget swept a stale ~0.001 ask into ~50k shares,
    which is what made the cumulative drag read ``-$30000``. Excluding them keeps
    the cumulative stats and notifications honest without rewriting history.

    The default threshold is ``DUST_FILL_FRAC`` (0.5), NOT ``MIN_FILL_FRAC``:
    the simulator floor and the historical quarantine are separate concerns
    (see the constants above). Pinning this at 0.5 keeps legacy fills in
    [0.5x, 0.97x) of the target's price INSIDE the reported sample, where the
    at-their-price re-baseline can show their honest economics.
    """
    return (
        p.their_price > 0
        and 0 < p.entry_price < p.their_price * min_fill_frac
    )


def report(ledger: PaperCopyLedger) -> dict:
    open_all = ledger.open_positions()
    closed_all = ledger.closed_positions()
    open_pos = [p for p in open_all if not is_dust_fill(p)]
    closed = [p for p in closed_all if not is_dust_fill(p)]
    quarantined = (len(open_all) - len(open_pos)) + (len(closed_all) - len(closed))
    spent = sum(p.spent for p in closed)
    pnl = sum(p.pnl for p in closed)
    ideal = sum(p.ideal_pnl for p in closed)
    wins = sum(1 for p in closed if p.won)
    drags = [p.drag_bps for p in closed]
    return {
        "open": len(open_pos),
        "closed": len(closed),
        # positions excluded as pre-fix dust fills (0 once the ledger is clean):
        "quarantined": quarantined,
        "capital_deployed": round(spent, 2),
        "realized_pnl": round(pnl, 2),
        "realized_roi": round(pnl / spent, 4) if spent else 0.0,
        "ideal_pnl_no_drag": round(ideal, 2),
        "execution_drag_cost": round(ideal - pnl, 2),
        "avg_drag_bps": round(sum(drags) / len(drags), 1) if drags else 0.0,
        "hit_rate": round(wins / len(closed), 4) if closed else 0.0,
    }


# --------------------------------------------------------------------------- #
# Fill-health witness (P0-1's acceptance, made standing)
# --------------------------------------------------------------------------- #

# In the clean era the two-sided gate (|drag| <= fill_gate_bps, 150) and the
# 0.97 floor make a fill below -300bps IMPOSSIBLE by construction — seeing one
# means the fill path bypassed the engine or regressed. Matches the ROADMAP
# P0-1 acceptance ("no new ledger row has drag_bps < -300").
DEEP_GIFT_BPS = -300


def drag_stats(drags: list) -> dict:
    """Reduce a list of per-fill drag_bps to the fill-quality witness numbers:
    how far our entries landed from the target's price on average, the worst
    single case, how often we filled BETTER than the target (drag < 0 — small
    values are normal book drift, systematically negative averages are not),
    and how many fills breached the impossible-by-construction deep-gift floor.
    """
    n = len(drags)
    if not n:
        return {"n": 0, "avg_drag_bps": 0.0, "min_drag_bps": 0,
                "pct_better": 0.0, "n_deep_gift": 0}
    return {
        "n": n,
        "avg_drag_bps": round(sum(drags) / n, 1),
        "min_drag_bps": min(drags),
        "pct_better": round(sum(1 for d in drags if d < 0) / n, 4),
        "n_deep_gift": sum(1 for d in drags if d < DEEP_GIFT_BPS),
    }


def fill_health(positions, min_opened_ts: Optional[float] = None) -> dict:
    """The fill-quality witness over settled, non-dust positions, optionally
    floored to the clean era (``min_opened_ts``). P0-1's 48h acceptance —
    "avg drag >= 0, no row < -300bps" — stays a one-line read forever."""
    drags = [int(p.drag_bps) for p in positions
             if p.closed and not is_dust_fill(p)
             and (min_opened_ts is None or (getattr(p, "opened_ts", 0) or 0)
             >= min_opened_ts)]
    return drag_stats(drags)


def fill_health_suspect(h: dict, min_n: int = 5) -> bool:
    """True when the fill model looks like it is gifting price again: any
    deep gift (impossible by construction post-P0-1 — flags at any n), or a
    negative AVERAGE drag on a non-trivial sample (fills landing systematically
    better than the target's own price, the 2026-07 artifact signature)."""
    if h["n_deep_gift"] > 0:
        return True
    return h["n"] >= min_n and h["avg_drag_bps"] < 0


# --------------------------------------------------------------------------- #
# Telegram formatting (presentation; HTML parse mode)
# --------------------------------------------------------------------------- #

def _esc(s: str) -> str:
    """Minimal HTML escape for Telegram (parse_mode=HTML)."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _short_wallet(w: str) -> str:
    w = w or ""
    return f"{w[:6]}…{w[-4:]}" if len(w) > 12 else (w or "—")


def _signed_usd(x: float) -> str:
    """'+$69' / '-$50' — sign before the dollar sign so it reads as money."""
    return f"{'+' if x >= 0 else '-'}${abs(x):,.0f}"


def format_resolution_telegram(resolved: list[PaperPosition], rep: dict,
                               resolver=None) -> str:
    """Build the human-readable Telegram message for resolved paper copies.

    One block per market that just settled — what it was (the question), which
    OUTCOME we'd bought and whether it won or lost, the cost→payout economics, the
    execution drag we ate, and a link — followed by a single labelled line of
    cumulative-ledger context. ``resolver`` (an ``OutcomeNameResolver``, optional)
    names the bought outcome so each block says exactly which side settled; when
    omitted (e.g. in tests) the outcome name is left off rather than fetched.
    """
    # Skip pre-fix dust fills: a stale open position can still resolve after the
    # fix deploys, and its garbage entry price would render a nonsensical block.
    shown = [p for p in resolved if not is_dust_fill(p)]
    stale = len(resolved) - len(shown)
    n = len(shown)
    plural = "s" if n != 1 else ""
    lines = [f"📋 <b>Paper-copy</b> — {n} market{plural} resolved"]

    for p in shown:
        won = p.won
        verdict = "✅ <b>WON</b>" if won else "❌ <b>LOST</b>"
        title = _esc(p.title) or f"({p.category} market)"
        payout = p.spent + p.pnl  # = shares if won else 0
        roi = (p.pnl / p.spent * 100.0) if p.spent else 0.0
        # name the outcome we'd bought so it's clear WHICH side settled
        outcome_txt = ""
        if resolver is not None:
            outcome_txt = f" <b>“{_esc(resolver.label(p.condition_id, p.outcome_index))}”</b>"
        lines.append("")  # blank line separates blocks
        lines.append(f'{verdict}{outcome_txt} · "{title}"')
        lines.append(
            f"copied <code>{_short_wallet(p.target)}</code> · "
            f"${p.spent:,.0f} → ${payout:,.0f} ({_signed_usd(p.pnl)}, {roi:+.0f}%) · "
            f"entry {p.entry_price:.3f} vs their {p.their_price:.3f} ({p.drag_bps:+d}bps drag)"
        )
        if p.slug:
            lines.append(f"🔗 https://polymarket.com/event/{p.slug}")

    if stale:
        lines.append("")
        lines.append(
            f"⚠️ {stale} stale dust-fill position{'s' if stale != 1 else ''} "
            "excluded (pre-fix data)"
        )

    lines.append("")
    lines.append(
        "📊 <b>Ledger:</b> "
        f"{rep['closed']} closed · realized {_signed_usd(rep['realized_pnl'])} "
        f"(ROI {rep['realized_roi'] * 100:+.0f}%) · hit {rep['hit_rate'] * 100:.0f}% · "
        f"avg drag {rep['avg_drag_bps']:+.0f}bps · {rep['open']} open"
    )
    return "\n".join(lines)
