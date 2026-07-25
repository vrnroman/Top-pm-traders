"""Unified per-strategy + per-wallet P&L aggregation across both copy systems.

Two independent copy systems accrue P&L:

  * **System A — tiered live executor.** Realized rows in ``realized-pnl.jsonl``
    (each now stamped with ``tier`` 1a/1b/1c and the followed ``trader_address``)
    plus open inventory positions marked to market (``OpenPositionPnl``).

  * **System B — discovery → paper-copy harness.** ``PaperPosition`` rows in
    ``copy_paper_ledger.jsonl``: every position carries its ``target`` wallet and
    the discovery strategy theories (1a..1j) that ``flagged_by`` it. Closed
    positions are realized (resolution or exit-following); open ones are marked
    to market when a live mid is available (else they show as open count/capital
    only).

This module is **pure** — rows/positions/price-derived inputs are injected, so it
unit-tests without network or disk. It produces, per strategy and overall, the
realized/unrealized/net P&L and a per-wallet breakdown ranked for the
promote-to-real-money / drop decision.

Attribution & double-counting rules (see ``build_unified``):
  * One ``WalletPnl`` per (system, wallet). A System-B wallet flagged by several
    theories is listed under *each* theory's ``StrategyPnl`` but counted **once**
    in the grand total.
  * ROI denominator is *measured* capital: System A counts realized cost + priced
    open cost; System B counts closed (realized) ``spent`` plus the cost of open
    positions that carry a live mark — unmarked opens show as open count/capital
    only and don't drag ROI.
  * Rows/positions with no attribution land in ``untagged-A`` / ``untagged-B`` so
    the grand total always reconciles.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional

from src.copy_trading.copy_paper import PaperPosition, is_dust_fill
from src.copy_trading.pnl import OpenPositionPnl

_UNKNOWN_WALLET = "(unknown)"
_LEGACY_WALLET = "(legacy)"
UNTAGGED_A = "untagged-A"
UNTAGGED_B = "untagged-B"
# System-A rows with NO attribution at all (no tier, no followed wallet) AND
# written before the cutoff below are pre-schema debris — the 2026-07-11
# preview-realization sweep that booked 61 dead Apr/May-era positions (-$575)
# in one burst once the closed=true Gamma fix un-starved the resolver. They are
# real history (never deleted) but not a result of any current strategy, so
# they get their own track and /pnl can show "current strategies" and "legacy
# backlog" as separate numbers instead of one misleading net.
LEGACY_A = "legacy-A"
# The cutoff is TIME-bounded on purpose: preview_resolver/auto_redeemer can
# still write rows whose trader_address falls back to "" today, and an
# attribution-only rule would misfile those LIVE losses as dead backlog
# (2026-07-16 review catch). Anything unattributed after this date is
# reported as current (untagged-A), where it is visible and investigable.
LEGACY_CUTOFF_ISO = "2026-07-12"
# The long-horizon paper book is one track (not split by discovery theory). Like
# the near-term copier, its open positions are marked to market when priced.
STRATEGY4_LABEL = "S4"
# The borrowed-clock (instant-copy) paper book — strategy B of the 2026-07
# A-vs-B race. One track label (like S4), not per-theory: the race compares
# BOOKS, and its per-wallet split lives in scripts/strategy_compare.py.
PAPER_B_LABEL = "B-instant"

# Resolved-sample maturity bands — "is there enough *settled* paper data to
# judge this wallet/theory yet?" A freshly-enabled theory (1a/1e/1j) looks like
# noise until enough of its copies resolve, so we annotate by settled count.
MATURITY_THIN = 5      # below this: too few resolved bets to read the ROI as signal
MATURITY_READY = 15    # at/above this: enough settled data to consider promoting

# Realized-vs-at-their-price divergence tripwire (2026-07-25, instrumentation-
# for-trust): a paper track whose realized ROI sits more than this far from its
# at-their-price ROI is earning its result from the FILL MODEL, not from the
# wallets — the exact 2026-07 artifact (realized +8.55% vs at-price -0.11%, an
# 866bps gap, would have flagged SUSPECT months before it was found by hand).
# Display-only: never gates anything, but it is rendered and logged loudly.
DIVERGENCE_SUSPECT_BPS = 200
DIVERGENCE_MIN_CLOSED = 10   # below this settled count the gap is small-sample noise


def wilson_lower_bound(wins: int, n: int, z: float = 1.96) -> Optional[float]:
    """Lower bound of the Wilson score interval for a hit-rate over ``n``
    resolved bets (None when n==0). Honest about small samples: 3/3 wins gives
    ~0.44, not 1.0 — so a tiny lucky streak doesn't read as a proven edge. The
    band tightens toward the point estimate as ``n`` grows."""
    if n <= 0:
        return None
    phat = wins / n
    denom = 1.0 + z * z / n
    centre = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def maturity_tag(n_closed: int) -> str:
    """A glyph for how much *settled* data backs a wallet/theory's numbers."""
    if n_closed < MATURITY_THIN:
        return "\U0001f9ca"   # 🧊 thin — too few resolved bets to trust
    if n_closed < MATURITY_READY:
        return "\U0001f331"   # 🌱 building
    return "✅"           # ✅ enough settled data


def promotion_verdict(net_pnl: float, n_closed: int) -> tuple[str, str]:
    """(verdict, reason) for the promote-paper-wallet-to-real-money decision.

    Gates on *settled sample size* + *positive measured PnL* — deliberately NOT
    on hit-rate, because a +EV longshot theory (e.g. 1e) wins well under 50% of
    the time by design and a hit-rate gate would wrongly hold it. Advisory only;
    promotion stays a manual ``.env`` edit the owner makes."""
    if n_closed < MATURITY_READY:
        return ("HOLD", f"only {n_closed} resolved (need ≥{MATURITY_READY})")
    if net_pnl <= 0:
        return ("HOLD", "paper PnL not positive")
    return ("PROMOTE-READY", f"{n_closed} resolved, ${net_pnl:+.0f} paper")


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #

@dataclass
class WalletPnl:
    wallet: str
    system: str                       # "A" or "B"
    strategies: tuple = ()            # labels this wallet contributes under, e.g. ("B:1b","B:1f")
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0       # System A opens + marked System-B/S4 opens
    cost_basis: float = 0.0           # ROI denominator — measured capital (see module docstring)
    open_cost: float = 0.0            # capital in still-open positions (display only)
    n_open: int = 0
    n_open_marked: int = 0            # opens that carry a live mark (rest are unpriced)
    n_closed: int = 0
    wins: int = 0
    losses: int = 0
    # Honest-economics pair (paper books only, settled rows only): the PnL the
    # same positions would have booked filled at the TARGET's price, and the
    # capital those settled rows deployed. The at-their-price ROI derived from
    # them cannot be inflated by the fill model (2026-07 artifact; P0-2).
    ideal_pnl: float = 0.0
    closed_cost: float = 0.0

    @property
    def net_pnl(self) -> float:
        return round(self.realized_pnl + self.unrealized_pnl, 2)

    @property
    def roi(self) -> Optional[float]:
        return (self.net_pnl / self.cost_basis) if self.cost_basis > 0 else None

    @property
    def realized_roi_closed(self) -> Optional[float]:
        """Realized ROI over SETTLED capital only — the like-for-like comparator
        of ``at_price_roi`` (the headline ``roi`` includes marked opens)."""
        return (self.realized_pnl / self.closed_cost) if self.closed_cost > 0 else None

    @property
    def at_price_roi(self) -> Optional[float]:
        """ROI had every settled fill been at the target's own price."""
        return (self.ideal_pnl / self.closed_cost) if self.closed_cost > 0 else None

    @property
    def hit_rate(self) -> Optional[float]:
        n = self.wins + self.losses
        return (self.wins / n) if n else None


@dataclass
class StrategyPnl:
    label: str                        # "A:1a".."A:1c", "B:1a".."B:1j", or an untagged bucket
    system: str
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    cost_basis: float = 0.0
    open_cost: float = 0.0
    n_open: int = 0
    n_closed: int = 0
    wins: int = 0
    losses: int = 0
    ideal_pnl: float = 0.0            # settled rows' drag-free PnL (paper books; see WalletPnl)
    closed_cost: float = 0.0          # settled rows' deployed capital
    wallets: list = field(default_factory=list)   # list[WalletPnl]

    @property
    def net_pnl(self) -> float:
        return round(self.realized_pnl + self.unrealized_pnl, 2)

    @property
    def roi(self) -> Optional[float]:
        return (self.net_pnl / self.cost_basis) if self.cost_basis > 0 else None

    @property
    def realized_roi_closed(self) -> Optional[float]:
        return (self.realized_pnl / self.closed_cost) if self.closed_cost > 0 else None

    @property
    def at_price_roi(self) -> Optional[float]:
        return (self.ideal_pnl / self.closed_cost) if self.closed_cost > 0 else None

    @property
    def n_wallets(self) -> int:
        return len(self.wallets)

    def _add(self, w: WalletPnl) -> None:
        self.realized_pnl += w.realized_pnl
        self.unrealized_pnl += w.unrealized_pnl
        self.cost_basis += w.cost_basis
        self.open_cost += w.open_cost
        self.n_open += w.n_open
        self.n_closed += w.n_closed
        self.wins += w.wins
        self.losses += w.losses
        self.ideal_pnl += w.ideal_pnl
        self.closed_cost += w.closed_cost
        self.wallets.append(w)


def divergence_suspect(sp: StrategyPnl) -> bool:
    """True when a paper track's realized ROI and at-their-price ROI disagree
    by more than DIVERGENCE_SUSPECT_BPS — i.e. the track's result is coming
    from the fill model, not the wallets. Compared over SETTLED capital only
    (like-for-like), and only once the sample clears DIVERGENCE_MIN_CLOSED so
    a couple of lucky fills can't trip it."""
    if sp.n_closed < DIVERGENCE_MIN_CLOSED:
        return False
    r, i = sp.realized_roi_closed, sp.at_price_roi
    if r is None or i is None:
        return False
    return abs(r - i) * 10000 > DIVERGENCE_SUSPECT_BPS


@dataclass
class BestWorst:
    by_pnl_best: list = field(default_factory=list)
    by_pnl_worst: list = field(default_factory=list)
    by_roi_best: list = field(default_factory=list)
    by_roi_worst: list = field(default_factory=list)


@dataclass
class WalletHighlight:
    """One wallet that is notable within a strategy, tagged with *why* it placed
    (top/bottom by PnL and/or ROI). The deduped per-strategy view lists each
    wallet once carrying all of its tags, instead of the old four-list layout
    where a wallet that led both PnL and ROI — the common case in small
    strategies — was printed up to four times."""
    wallet: object                    # WalletPnl
    pnl_best: bool = False
    pnl_worst: bool = False
    roi_best: bool = False
    roi_worst: bool = False

    @property
    def tags(self) -> list:
        # A wallet that is *both* top and bottom on a metric is the whole (tiny)
        # population — the ranking says nothing, so drop the contradictory pair.
        out = []
        if self.pnl_best and not self.pnl_worst:
            out.append("▲PnL")
        elif self.pnl_worst and not self.pnl_best:
            out.append("▼PnL")
        if self.roi_best and not self.roi_worst:
            out.append("▲ROI")
        elif self.roi_worst and not self.roi_best:
            out.append("▼ROI")
        return out


@dataclass
class UnifiedPnl:
    strategies: list = field(default_factory=list)   # list[StrategyPnl], ordered
    total_realized: float = 0.0
    total_unrealized: float = 0.0

    @property
    def total_net(self) -> float:
        return round(self.total_realized + self.total_unrealized, 2)


# --------------------------------------------------------------------------- #
# System A aggregation
# --------------------------------------------------------------------------- #

def aggregate_system_a(
    realized_rows: list[dict],
    open_positions: list[OpenPositionPnl],
    tier_of: Optional[Callable[[str], Optional[str]]] = None,
) -> list[WalletPnl]:
    """Per-wallet P&L for the tiered executor.

    ``tier_of`` (e.g. ``strategy_config.get_wallet_tier``) is a fallback for rows
    written before tier-stamping landed; ``None`` skips the fallback.
    """
    acc: dict[str, WalletPnl] = {}

    def _wp(wallet: str, label: str) -> WalletPnl:
        # Legacy rows get their OWN accumulator key: a WalletPnl is added
        # whole to every label it carries (StrategyPnl._add), so letting the
        # empty-wallet key accrue BOTH legacy and tier-stamped rows would make
        # the legacy track over-claim tier rows (and the /pnl "current
        # strategies" line over-subtract). Keeping the keys disjoint makes the
        # legacy split exact by construction.
        key = _LEGACY_WALLET if label == LEGACY_A else (wallet or _UNKNOWN_WALLET)
        wp = acc.get(key)
        if wp is None:
            wp = WalletPnl(wallet=key, system="A")
            acc[key] = wp
        return wp

    def _label(tier: str, wallet: str, ts_iso: str = "") -> str:
        if tier:
            return f"A:{tier}"
        # Legacy = no tier AND no followed wallet AND written before the
        # cutoff (the pre-attribution-schema backlog). An unattributed row
        # from AFTER the cutoff is a live result that lost its attribution —
        # it must stay visible under "current" (untagged), never be filed as
        # dead backlog.
        if not wallet and ts_iso and ts_iso[:10] < LEGACY_CUTOFF_ISO:
            return LEGACY_A
        return UNTAGGED_A

    def _resolve_tier(stamped: str, wallet: str) -> str:
        if stamped:
            return stamped
        if wallet and tier_of is not None:
            return tier_of(wallet) or ""
        return ""

    for r in realized_rows:
        wallet = (r.get("trader_address") or "").lower()
        tier = _resolve_tier(r.get("tier") or "", wallet)
        label = _label(tier, wallet, str(r.get("timestamp") or ""))
        wp = _wp(wallet, label)
        pnl = float(r.get("pnl", 0.0) or 0.0)
        cost = float(r.get("cost_basis", 0.0) or 0.0)
        wp.realized_pnl += pnl
        wp.cost_basis += cost
        wp.n_closed += 1
        won = r.get("won")
        if won is True or (won is None and pnl > 0):
            wp.wins += 1
        elif won is False or (won is None and pnl < 0):
            wp.losses += 1
        _add_label(wp, label)

    for p in open_positions:
        wallet = (p.trader_address or "").lower()
        tier = _resolve_tier(p.tier or "", wallet)
        label = _label(tier, wallet)
        wp = _wp(wallet, label)
        wp.open_cost += p.cost
        wp.n_open += 1
        if p.unrealized_pnl is not None:
            wp.unrealized_pnl += p.unrealized_pnl
            wp.cost_basis += p.cost        # only priced (measured) open cost feeds ROI
        _add_label(wp, label)

    _round(acc.values())
    return list(acc.values())


# --------------------------------------------------------------------------- #
# System B aggregation
# --------------------------------------------------------------------------- #

def aggregate_system_b(
    paper_positions: list[PaperPosition],
    flagged_by_now: Optional[dict] = None,
) -> list[WalletPnl]:
    """Per-wallet P&L for the paper-copy harness.

    ``flagged_by_now`` (lowercased wallet -> theory list, e.g. from the current
    watchlist) is a fallback for positions opened before ``flagged_by`` was
    stamped. Dust fills are excluded to match ``copy_paper.report``.
    """
    flagged_by_now = {k.lower(): v for k, v in (flagged_by_now or {}).items()}
    acc: dict[str, WalletPnl] = {}

    for p in paper_positions:
        if is_dust_fill(p):
            continue
        wallet = (p.target or "").lower() or _UNKNOWN_WALLET
        wp = acc.get(wallet)
        if wp is None:
            wp = WalletPnl(wallet=wallet, system="B")
            acc[wallet] = wp

        theories = list(p.flagged_by) or flagged_by_now.get(wallet, [])
        labels = [f"B:{t}" for t in theories] if theories else [UNTAGGED_B]

        if p.closed:
            wp.realized_pnl += p.pnl
            wp.cost_basis += p.spent       # realized capital feeds ROI
            wp.ideal_pnl += p.ideal_pnl    # drag-free comparator (P0-2)
            wp.closed_cost += p.spent
            wp.n_closed += 1
            if p.won:
                wp.wins += 1
            else:
                wp.losses += 1
        else:
            wp.open_cost += p.spent
            wp.n_open += 1
            if p.mark_price > 0:           # a marked open contributes to unrealized + ROI
                wp.unrealized_pnl += p.unrealized_pnl
                wp.cost_basis += p.spent
                wp.n_open_marked += 1

        for lbl in labels:
            _add_label(wp, lbl)

    _round(acc.values())
    return list(acc.values())


def aggregate_strategy4(s4_positions: list[PaperPosition]) -> list[WalletPnl]:
    """Per-wallet P&L for the Strategy-4 long-horizon paper book.

    Distinct from ``aggregate_system_b`` (the near-term copier, whose open
    positions carry no mark) in two ways: open long-horizon positions are *marked
    to market* — a position carrying a ``mark_price`` contributes its unrealized
    P&L and its capital to ROI, exactly like a priced System-A open — and every
    wallet is grouped under the single ``S4`` track rather than per discovery
    theory. Closed positions are realized at resolution. Dust fills excluded.

    Returns its own ``WalletPnl`` list (system ``"B"`` so it slots into the unified
    builder). A wallet that is *also* on the near-term book gets a separate
    ``WalletPnl`` there, so its two tracks show side by side — exactly the
    dual-membership view: short bets under the copier, long bets under S4.
    """
    acc: dict[str, WalletPnl] = {}
    for p in s4_positions:
        if is_dust_fill(p):
            continue
        wallet = (p.target or "").lower() or _UNKNOWN_WALLET
        wp = acc.get(wallet)
        if wp is None:
            wp = WalletPnl(wallet=wallet, system="B")
            acc[wallet] = wp
        if p.closed:
            wp.realized_pnl += p.pnl
            wp.cost_basis += p.spent       # realized capital feeds ROI
            wp.ideal_pnl += p.ideal_pnl    # drag-free comparator (P0-2)
            wp.closed_cost += p.spent
            wp.n_closed += 1
            if p.won:
                wp.wins += 1
            else:
                wp.losses += 1
        else:
            wp.open_cost += p.spent
            wp.n_open += 1
            if p.mark_price > 0:           # only a marked open contributes to ROI
                wp.unrealized_pnl += p.unrealized_pnl
                wp.cost_basis += p.spent
                wp.n_open_marked += 1
        _add_label(wp, STRATEGY4_LABEL)
    _round(acc.values())
    return list(acc.values())


def aggregate_paper_b(b_positions: list[PaperPosition]) -> list[WalletPnl]:
    """Per-wallet P&L for the strategy-B (borrowed-clock instant-copy) book.

    Same shape as ``aggregate_strategy4``: every wallet groups under the single
    ``B-instant`` track so /pnl shows the race's B book as one line-item family
    next to the near-term copier's per-theory tracks. Near-term-book opens carry
    no mark (marked on-read by /pnl), so unmarked opens count position/cost only.
    """
    acc: dict[str, WalletPnl] = {}
    for p in b_positions:
        if is_dust_fill(p):
            continue
        wallet = (p.target or "").lower() or _UNKNOWN_WALLET
        wp = acc.get(wallet)
        if wp is None:
            wp = WalletPnl(wallet=wallet, system="B")
            acc[wallet] = wp
        if p.closed:
            wp.realized_pnl += p.pnl
            wp.cost_basis += p.spent       # realized capital feeds ROI
            wp.ideal_pnl += p.ideal_pnl    # drag-free comparator (P0-2)
            wp.closed_cost += p.spent
            wp.n_closed += 1
            if p.won:
                wp.wins += 1
            else:
                wp.losses += 1
        else:
            wp.open_cost += p.spent
            wp.n_open += 1
            if p.mark_price > 0:           # a marked open contributes to ROI
                wp.unrealized_pnl += p.unrealized_pnl
                wp.cost_basis += p.spent
                wp.n_open_marked += 1
        _add_label(wp, PAPER_B_LABEL)
    _round(acc.values())
    return list(acc.values())


# --------------------------------------------------------------------------- #
# Unification + ranking
# --------------------------------------------------------------------------- #

def build_unified(a_wallets: list[WalletPnl], b_wallets: list[WalletPnl]) -> UnifiedPnl:
    """Combine per-wallet lists into ordered per-strategy blocks + grand totals.

    Grand totals are summed over the **unique** wallet list (never over strategy
    blocks), so a multi-theory wallet isn't counted twice.
    """
    all_wallets = list(a_wallets) + list(b_wallets)

    strat: dict[str, StrategyPnl] = {}
    for wp in all_wallets:
        for lbl in wp.strategies:
            sp = strat.get(lbl)
            if sp is None:
                sp = StrategyPnl(label=lbl, system=(
                    "A" if lbl.startswith("A:") or lbl in (UNTAGGED_A, LEGACY_A)
                    else "B"))
                strat[lbl] = sp
            sp._add(wp)

    ordered = sorted(strat.values(), key=lambda s: _label_sort_key(s.label))
    for sp in ordered:
        sp.realized_pnl = round(sp.realized_pnl, 2)
        sp.unrealized_pnl = round(sp.unrealized_pnl, 2)
        sp.cost_basis = round(sp.cost_basis, 2)
        sp.open_cost = round(sp.open_cost, 2)
        sp.ideal_pnl = round(sp.ideal_pnl, 2)
        sp.closed_cost = round(sp.closed_cost, 2)

    total_realized = round(sum(w.realized_pnl for w in all_wallets), 2)
    total_unrealized = round(sum(w.unrealized_pnl for w in all_wallets), 2)
    return UnifiedPnl(
        strategies=ordered,
        total_realized=total_realized,
        total_unrealized=total_unrealized,
    )


def best_worst(wallets: list, k: int = 3) -> BestWorst:
    """Top-k / bottom-k wallets by net P&L and by ROI.

    ROI rankings include only wallets with a defined ROI (non-zero measured
    capital), so a wallet with nothing realized yet isn't ranked on ROI.
    """
    by_net = sorted(wallets, key=lambda w: w.net_pnl)
    with_roi = [w for w in wallets if w.roi is not None]
    by_roi = sorted(with_roi, key=lambda w: w.roi)
    return BestWorst(
        by_pnl_best=list(reversed(by_net[-k:])),
        by_pnl_worst=by_net[:k],
        by_roi_best=list(reversed(by_roi[-k:])),
        by_roi_worst=by_roi[:k],
    )


def _wallet_key(w) -> tuple:
    return (w.system, w.wallet)


def strategy_highlights(wallets: list, k: int = 3) -> list:
    """Deduped notable wallets for one strategy: the union of the top/bottom-``k``
    by net PnL and by ROI, each wallet listed **once** and tagged (via
    ``WalletHighlight``) with every ranking it placed in, ordered by net PnL
    descending (best first). ROI itself is shown per-line by the caller, so a
    single tagged list replaces the four overlapping best/worst lists that made
    ``/wallets`` repeat the same wallet several times."""
    bw = best_worst(wallets, k=k)
    order: list = []
    seen: dict = {}

    def _mark(group, attr):
        for w in group:
            key = _wallet_key(w)
            h = seen.get(key)
            if h is None:
                h = WalletHighlight(wallet=w)
                seen[key] = h
                order.append(h)
            setattr(h, attr, True)

    _mark(bw.by_pnl_best, "pnl_best")
    _mark(bw.by_pnl_worst, "pnl_worst")
    _mark(bw.by_roi_best, "roi_best")
    _mark(bw.by_roi_worst, "roi_worst")
    order.sort(key=lambda h: h.wallet.net_pnl, reverse=True)
    return order


def top_wallets(
    a_wallets: list, b_wallets: list, k: int = 3, positive_only: bool = True
) -> list:
    """Top-``k`` unique wallets by net PnL across *all* strategies — the part-1
    "best overall" list. Each ``WalletPnl`` is already one-per-(system, wallet),
    so a wallet flagged by several theories appears once here even though it
    spans several per-strategy blocks. ``positive_only`` keeps it to the
    actually-good wallets (the promotion candidates)."""
    allw = list(a_wallets) + list(b_wallets)
    if positive_only:
        allw = [w for w in allw if w.net_pnl > 0]
    allw.sort(key=lambda w: w.net_pnl, reverse=True)
    return allw[:k]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _add_label(wp: WalletPnl, label: str) -> None:
    if label not in wp.strategies:
        wp.strategies = wp.strategies + (label,)


def _round(wps) -> None:
    for wp in wps:
        wp.realized_pnl = round(wp.realized_pnl, 2)
        wp.unrealized_pnl = round(wp.unrealized_pnl, 2)
        wp.cost_basis = round(wp.cost_basis, 2)
        wp.open_cost = round(wp.open_cost, 2)
        wp.ideal_pnl = round(wp.ideal_pnl, 2)
        wp.closed_cost = round(wp.closed_cost, 2)


def _label_sort_key(label: str):
    """Order: System A strategies, then System B, then untagged buckets, with
    the legacy backlog dead last (it is history, not a live strategy).

    Within a system, natural theory order (1a, 1b, ... 1j)."""
    if label == UNTAGGED_A:
        return (2, "")
    if label == UNTAGGED_B:
        return (3, "")
    if label == LEGACY_A:
        return (4, "")
    system, _, name = label.partition(":")
    return (0 if system == "A" else 1, name)
