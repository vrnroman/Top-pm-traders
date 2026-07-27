"""Trader scoring for copy-trading selection (Strategy 1b).

Empirically validated approach for picking *which* wallets to copy.

Background (see research/COPY_TRADING_FINDINGS.md):
    Ranking Polymarket wallets by naive all-time PnL/ROI has ~zero predictive
    power for future returns — UNLESS realized PnL is measured only over
    *provably-closed* positions. The naive method counts still-open slow
    positions at a mark-to-market guess, which injects enough noise to destroy
    the signal (out-of-sample Spearman ~0.0). Measured correctly — realized ROI
    on positions that have either been redeemed or fully exited — past ROI
    predicts future ROI (Spearman ~0.29-0.36 across multiple time splits) and a
    top-quartile selection earns strongly positive returns out-of-sample.

This module contains the *pure* scoring logic (no network), so it can be unit
tested against fixture activity and reused by both the historical backtest
(`backtest/trader_scoring_backtest.py`) and the forward copy-paper harness.

A position is counted as realized only when it is "provably closed":
    - the wallet has a REDEEM event for that market (winnings claimed), OR
    - net shares across all outcomes have returned to ~0 (fully sold out).
Markets that are neither redeemed nor fully exited are treated as still-open
and excluded from the realized-ROI score (their mark-to-market value is noise).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional

# Activity event types that are income unrelated to directional skill
# (liquidity-provision rewards). Excluded from PnL so we don't mistake a market
# maker for a skilled bettor.
REWARD_TYPES = frozenset({"REWARD", "MAKER_REBATE", "YIELD"})

# Below this absolute net-share residual a position is considered fully exited.
SHARE_EPSILON = 1.0


def tstat(values) -> float:
    """t-stat of the mean of ``values`` (mean / standard error) — edge per unit
    of noise. 0 for fewer than two points or zero spread. The single definition
    of t-stat used across the bot (wallet skill scoring + copy-replay)."""
    import statistics
    vals = list(values)
    n = len(vals)
    if n < 2:
        return 0.0
    sd = statistics.pstdev(vals)
    return statistics.mean(vals) / (sd / (n ** 0.5)) if sd > 0 else 0.0


# ---------------------------------------------------------------------------
# Market categorisation (drives per-segment scoring; sports is the strongest
# segment empirically, research the weakest/noisiest).
# ---------------------------------------------------------------------------

_CRYPTO_KW = (
    "bitcoin", "ethereum", " btc", " eth", "up or down", "price of",
    "solana", "dogecoin", "all-time high", "hit $", "reach $",
)
_SPORTS_KW = (
    " vs ", "vs.", "nba", "nfl", "mlb", "nhl", "ufc", "premier league",
    "champions league", " cup", "o/u", "set ", "f1 ", "grand prix",
    "masters", "wimbledon", "open?", "playoff", "super bowl", "la liga",
)
_RESEARCH_KW = (
    "election", "president", "nominee", "senate", "governor", "poll", "war",
    "invasion", "ceasefire", "nato", "russia", "ukraine", "china", "iran",
    "israel", "gaza", "nuclear", "sanctions", "regime", "coup", "fed ",
    "rate cut", "cpi", "gdp", "recession", "shutdown", "supreme court",
    "resign", "impeach", "treaty", "deal by", "tariff", "opec",
)


def classify_market(title: str) -> str:
    """Bucket a market into sports/crypto/research/other by its title.

    Order matters: crypto and research keywords are checked before sports so a
    market like "Bitcoin vs ..." is not misfiled as sports.
    """
    t = (title or "").lower()
    if any(k in t for k in _CRYPTO_KW):
        return "crypto"
    if any(k in t for k in _RESEARCH_KW):
        return "research"
    if any(k in t for k in _SPORTS_KW):
        return "sports"
    return "other"


# ---------------------------------------------------------------------------
# Per-market realized result
# ---------------------------------------------------------------------------

@dataclass
class MarketResult:
    """Realized result for one (wallet, market) pair over its full history."""

    condition_id: str
    category: str
    capital: float        # total USDC spent buying (cost basis)
    pnl: float            # realized: (sells + redeems) - buys
    closed: bool          # provably closed (redeemed or fully exited)
    last_ts: float        # epoch seconds of the last activity in this market

    @property
    def roi(self) -> float:
        return self.pnl / self.capital if self.capital > 0 else 0.0


def realized_market_results(activity: Iterable[dict]) -> list[MarketResult]:
    """Reduce a wallet's activity feed into per-market realized results.

    `activity` is the list returned by the Polymarket data-api `/activity`
    endpoint (each item: type, conditionId, outcomeIndex, side, size, usdcSize,
    timestamp, title). REDEEM events count as cash inflow (winnings); reward
    types are ignored.
    """
    buy: dict[str, float] = defaultdict(float)
    inflow: dict[str, float] = defaultdict(float)
    last_ts: dict[str, float] = defaultdict(float)
    net_shares: dict[tuple[str, object], float] = defaultdict(float)
    redeemed: set[str] = set()
    category: dict[str, str] = {}

    for ev in activity:
        etype = ev.get("type")
        cid = ev.get("conditionId")
        if not cid or etype in REWARD_TYPES:
            continue
        ts = float(ev.get("timestamp") or 0)
        usd = float(ev.get("usdcSize") or 0)
        size = float(ev.get("size") or 0)
        oi = ev.get("outcomeIndex")

        if cid not in category and ev.get("title"):
            category[cid] = classify_market(ev.get("title"))
        if ts > last_ts[cid]:
            last_ts[cid] = ts

        if etype == "TRADE":
            side = ev.get("side")
            if side == "BUY":
                buy[cid] += usd
                net_shares[(cid, oi)] += size
            elif side == "SELL":
                inflow[cid] += usd
                net_shares[(cid, oi)] -= size
        elif etype == "REDEEM":
            inflow[cid] += usd
            redeemed.add(cid)

    results: list[MarketResult] = []
    for cid in set(buy) | set(inflow):
        capital = buy.get(cid, 0.0)
        if capital <= 0:
            continue
        residual = sum(v for (c, _o), v in net_shares.items() if c == cid)
        closed = (cid in redeemed) or (abs(residual) < SHARE_EPSILON)
        results.append(
            MarketResult(
                condition_id=cid,
                category=category.get(cid, "other"),
                capital=capital,
                pnl=inflow.get(cid, 0.0) - capital,
                closed=closed,
                last_ts=last_ts.get(cid, 0.0),
            )
        )
    return results


# ---------------------------------------------------------------------------
# Wallet score
# ---------------------------------------------------------------------------

@dataclass
class WalletScore:
    """Realized-performance score for a wallet over a time window.

    Only provably-closed markets whose last activity falls in [start, end) are
    counted. ``category`` "ALL" aggregates every segment.
    """

    capital: float = 0.0
    pnl: float = 0.0
    n_closed: int = 0
    wins: int = 0

    @property
    def roi(self) -> float:
        return self.pnl / self.capital if self.capital > 0 else 0.0

    @property
    def hit_rate(self) -> float:
        return self.wins / self.n_closed if self.n_closed > 0 else 0.0


def score_wallet(
    activity: Iterable[dict],
    *,
    start_ts: float = 0.0,
    end_ts: float = float("inf"),
    category: str = "ALL",
) -> WalletScore:
    """Score a wallet over closed markets resolved within [start_ts, end_ts).

    ``category`` of "ALL" includes every segment; otherwise restrict to one of
    sports/crypto/research/other.
    """
    score = WalletScore()
    for r in realized_market_results(activity):
        if not r.closed:
            continue
        if not (start_ts <= r.last_ts < end_ts):
            continue
        if category != "ALL" and r.category != category:
            continue
        score.capital += r.capital
        score.pnl += r.pnl
        score.n_closed += 1
        if r.pnl > 0:
            score.wins += 1
    return score


@dataclass
class RankedWallet:
    address: str
    score: WalletScore


def select_copy_targets(
    scored: dict[str, WalletScore],
    *,
    min_capital: float = 5000.0,
    min_closed: int = 10,
    top_k: int = 20,
) -> list[RankedWallet]:
    """Pick copy targets: reliability-filter, then rank by realized ROI.

    Defaults match the validated backtest configuration (>= $5k deployed and
    >= 10 closed markets in the lookback window to avoid small-sample luck).
    """
    eligible = [
        RankedWallet(addr, s)
        for addr, s in scored.items()
        if s.capital >= min_capital and s.n_closed >= min_closed
    ]
    eligible.sort(key=lambda rw: rw.score.roi, reverse=True)
    return eligible[:top_k]


# ---------------------------------------------------------------------------
# Robust scoring — validated to ~triple forward ROI vs raw-ROI ranking.
# Raw ROI rewards one lucky bet; the t-statistic of per-market PnL rewards
# repeatable edge, a concentration cap drops single-bet wallets, and a recency
# check keeps only wallets still winning lately. See COPY_TRADING_FINDINGS.md:
#   raw ROI +9.4% | t-stat +26.2% | recency+conc+t-stat +34.7% (TOP-20 forward).
# ---------------------------------------------------------------------------

@dataclass
class WalletMetrics:
    """Per-wallet stats over a window's provably-closed markets."""

    capital: float = 0.0
    pnl: float = 0.0
    n_closed: int = 0
    wins: int = 0
    pnls: list[float] = field(default_factory=list)
    early_pnl: float = 0.0
    late_pnl: float = 0.0
    early_n: int = 0
    late_n: int = 0

    @property
    def roi(self) -> float:
        return self.pnl / self.capital if self.capital > 0 else 0.0

    @property
    def hit_rate(self) -> float:
        return self.wins / self.n_closed if self.n_closed > 0 else 0.0

    @property
    def tstat(self) -> float:
        """t-stat of mean per-market PnL — edge per unit of noise."""
        return tstat(self.pnls)

    @property
    def concentration(self) -> float:
        """Share of gross profit from the single best market (1.0 = all of it)."""
        gross = sum(p for p in self.pnls if p > 0)
        return max(self.pnls) / gross if gross > 0 else 1.0

    @property
    def recency_ok(self) -> bool:
        """Edge present in the recent half (and overall), not just historically."""
        if self.early_n and self.late_n:
            return self.early_pnl > 0 and self.late_pnl > 0
        return self.pnl > 0

    def rank_score(self, method: str) -> float:
        if method == "tstat":
            return self.tstat
        if method == "median_roi":
            import statistics
            return (statistics.median(p / self.capital for p in self.pnls)
                    if self.pnls and self.capital > 0 else 0.0)
        return self.roi


def compute_wallet_metrics(
    activity: Iterable[dict],
    *,
    start_ts: float = 0.0,
    end_ts: float = float("inf"),
    category: str = "ALL",
    recency_split_ts: float | None = None,
) -> WalletMetrics:
    """WalletMetrics over closed markets resolved within [start_ts, end_ts)."""
    if recency_split_ts is None and start_ts > 0 and end_ts < float("inf"):
        recency_split_ts = (start_ts + end_ts) / 2.0
    m = WalletMetrics()
    for r in realized_market_results(activity):
        if not r.closed or not (start_ts <= r.last_ts < end_ts):
            continue
        if category != "ALL" and r.category != category:
            continue
        m.capital += r.capital
        m.pnl += r.pnl
        m.n_closed += 1
        m.pnls.append(r.pnl)
        if r.pnl > 0:
            m.wins += 1
        if recency_split_ts is not None:
            if r.last_ts < recency_split_ts:
                m.early_pnl += r.pnl
                m.early_n += 1
            else:
                m.late_pnl += r.pnl
                m.late_n += 1
    return m


@dataclass
class RankedMetrics:
    address: str
    metrics: WalletMetrics


def select_targets(
    scored: dict[str, WalletMetrics],
    *,
    method: str = "robust",
    min_capital: float = 5000.0,
    min_closed: int = 10,
    top_k: int = 20,
    max_concentration: float = 0.6,
    require_recency: bool = True,
    demote_hit_rate: float | None = None,
    demote_min_closed: int = 0,
) -> list[RankedMetrics]:
    """Validated copy-target selection.

    method="robust" (default): filter concentration <= max_concentration and
    (if require_recency) recent-half profitability, then rank by t-stat.
    method="tstat"/"roi"/"median_roi": that metric with only the reliability
    filters (capital, closed count).

    ``demote_hit_rate`` (opt-in; P1-2): wallets with >= demote_min_closed closed
    positions AND a hit rate at/above this bar rank BELOW everyone else, keeping
    their relative order. The bar signature is the settlement-lag scooper — a
    near-perfect hit rate earned buying near-$1 outcomes, which the t-stat rank
    otherwise selects FOR (§1.6: the screen manufactured scoopers, the LLM gate
    paid $0.09 each to kill them). Demote, not exclude: the downstream curve /
    copy-replay gates make the real call with richer data. None = legacy order.
    """
    rank_by = "tstat" if method == "robust" else method
    out = []
    for addr, m in scored.items():
        if m.capital < min_capital or m.n_closed < min_closed:
            continue
        if method == "robust":
            if m.concentration > max_concentration:
                continue
            if require_recency and not m.recency_ok:
                continue
        out.append(RankedMetrics(addr, m))

    def _key(rm: RankedMetrics):
        m = rm.metrics
        demoted = (demote_hit_rate is not None
                   and m.n_closed >= demote_min_closed
                   and m.hit_rate >= demote_hit_rate)
        return (0 if demoted else 1, m.rank_score(rank_by))

    out.sort(key=_key, reverse=True)
    return out[:top_k]
