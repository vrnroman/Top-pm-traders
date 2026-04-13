"""Strategy 1c pattern detection for insider/manipulation signals.

Detects four patterns:
  1.  new_account_geo       — on-chain-young wallet, ≤ N lifetime fills, large geo bet
  1b. first_ever_bet_geo    — wallet has zero prior Polymarket trades, large geo bet
  2.  cluster               — 3+ sizeable wallets, same side, same market, within 1h
  3.  dormant_reactivation  — prior trade ≥ dormant_days ago, large geo bet

Patterns 1/1b/3 rely on live lookups:
  - on-chain age via `wallet_age.get_wallet_age_days`
  - Polymarket trade history via `wallet_history.get_prior_trade_ts`
Both calls are cheap after the first cache hit per wallet and fail closed
(unknown → no alert). Thread-safe with module-level state; tests call
`_reset_pattern_detector()` between cases.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from src.config import CONFIG
from src.copy_trading.strategy_config import TIER_1C
from src.logger import logger
from src.models import DetectedTrade


def _extract_tx_hash(trade: DetectedTrade) -> str:
    """Pull the canonical tx hash out of a DetectedTrade id.

    DetectedTrade.id has the form `{tx_hash}-{token_id}-{side}`. The tx hash
    is the portion before the first dash. Returns "" if the id is malformed.
    """
    tid = getattr(trade, "id", "") or ""
    idx = tid.find("-")
    return tid[:idx] if idx > 0 else ""

# ---------------------------------------------------------------------------
# Geopolitical keywords
# ---------------------------------------------------------------------------

GEO_KEYWORDS: list[str] = [
    "war", "invasion", "military", "troops", "attack", "bomb", "missile",
    "nuclear", "sanctions", "ceasefire", "nato", "un ", "united nations",
    "coup", "regime", "dictator", "assassination", "terrorism", "terrorist",
    "election", "referendum", "impeach", "resign", "president", "prime minister",
    "china", "russia", "ukraine", "taiwan", "iran", "israel", "palestine",
    "gaza", "north korea", "dprk", "syria", "yemen", "houthi",
    "oil price", "opec", "embargo", "tariff", "trade war",
    "cyber attack", "espionage", "intelligence",
    "border", "territory", "annex", "occupation",
    "refugee", "humanitarian", "crisis",
    "geopolitical", "geopolitics", "conflict", "escalat",
]


def is_geopolitical_market(title: str, condition_id: str = "") -> bool:
    """Check if a market is geopolitical.

    Preference order:
      1. Gamma tag match (via geo_market_scanner) — authoritative, tag-based.
      2. Title keyword match — fallback for trades arriving before the scanner
         has primed its cache, or for condition_ids we haven't discovered yet.
    """
    if condition_id:
        try:
            from src.copy_trading.geo_market_scanner import is_geo_market_cid
            if is_geo_market_cid(condition_id):
                return True
        except Exception:
            pass
    lower = (title or "").lower()
    return any(kw in lower for kw in GEO_KEYWORDS)


# ---------------------------------------------------------------------------
# Wallet activity tracking
# ---------------------------------------------------------------------------

@dataclass
class WalletActivity:
    """Tracks lifetime activity metrics for a wallet address."""
    first_seen: float = 0.0      # epoch seconds
    last_seen: float = 0.0       # epoch seconds
    trade_count: int = 0
    total_volume: float = 0.0    # USD


_wallet_activities: dict[str, WalletActivity] = {}


def _get_or_create_wallet(address: str) -> WalletActivity:
    """Get existing wallet activity or create a new one."""
    key = address.lower()
    if key not in _wallet_activities:
        now = time.time()
        _wallet_activities[key] = WalletActivity(first_seen=now, last_seen=now)
    return _wallet_activities[key]


def _update_wallet_activity(address: str, size: float) -> WalletActivity:
    """Update wallet activity with a new trade."""
    wa = _get_or_create_wallet(address)
    now = time.time()
    wa.last_seen = now
    wa.trade_count += 1
    wa.total_volume += size
    return wa


# ---------------------------------------------------------------------------
# Recent bet tracking for cluster detection
# ---------------------------------------------------------------------------

MAX_RECENT_BETS = 10_000
CLUSTER_WINDOW_S = 3600  # 1 hour — recent-bets retention (used by funder cluster + VWAP)
CLUSTER_DETECTION_WINDOW_S = 900  # 15 min — tight window for loose cluster pattern

@dataclass
class RecentBet:
    """A recent bet for cluster and edge detection.

    `funder`  — wallet's first non-CEX USDC sender on Polygon ("" if unknown).
    `price`   — fill price in USDC, used to compute local VWAP for edge checks
                when Gamma-side best-bid/ask data is stale.

    Populated at bet-creation time so pattern checks scan synchronously. An
    empty funder makes the bet invisible to `same_funder_cluster` but still
    counts toward the loose `cluster` pattern.
    """
    wallet: str
    market: str
    side: str
    size: float
    timestamp: float  # epoch seconds
    funder: str = ""
    price: float = 0.0


_recent_bets: list[RecentBet] = []


def _add_recent_bet(
    wallet: str,
    market: str,
    side: str,
    size: float,
    funder: str = "",
    price: float = 0.0,
    trade_ts: Optional[float] = None,
) -> None:
    """Add a bet to the recent bets list, pruning if over limit.

    `trade_ts` is the actual epoch seconds of the trade; callers should pass it
    so the cluster window's prune step drops truly stale entries even if a
    stale trade somehow reaches this path. Falls back to wall-clock `now` when
    callers can't supply it (legacy tests).
    """
    now = time.time()
    ts = float(trade_ts) if trade_ts is not None else now
    _recent_bets.append(RecentBet(
        wallet=wallet.lower(),
        market=market,
        side=side,
        size=size,
        timestamp=ts,
        funder=(funder or "").lower(),
        price=float(price or 0.0),
    ))
    # Prune old entries and enforce max size
    cutoff = now - CLUSTER_WINDOW_S
    while len(_recent_bets) > MAX_RECENT_BETS:
        _recent_bets.pop(0)
    # Also prune entries older than the window
    while _recent_bets and _recent_bets[0].timestamp < cutoff:
        _recent_bets.pop(0)


def _find_cluster(market: str, side: str, exclude_wallet: str) -> list[RecentBet]:
    """Find recent bets on the same market and side within the cluster window."""
    now = time.time()
    cutoff = now - CLUSTER_DETECTION_WINDOW_S
    matches: list[RecentBet] = []
    seen_wallets: set[str] = set()
    exclude_lower = exclude_wallet.lower()

    for bet in _recent_bets:
        if bet.timestamp < cutoff:
            continue
        if bet.market != market or bet.side != side:
            continue
        if bet.wallet == exclude_lower:
            continue
        if bet.wallet in seen_wallets:
            continue
        seen_wallets.add(bet.wallet)
        matches.append(bet)

    return matches


# ---------------------------------------------------------------------------
# Alert dedup
# ---------------------------------------------------------------------------

MAX_ALERT_DEDUP = 1000

# OrderedDict used as an LRU set: key -> True
_seen_alerts: OrderedDict[str, bool] = OrderedDict()


def _is_duplicate_alert(alert_key: str) -> bool:
    """Check if an alert has already been sent. Marks it as seen."""
    if alert_key in _seen_alerts:
        return True
    _seen_alerts[alert_key] = True
    # Evict oldest if over limit
    while len(_seen_alerts) > MAX_ALERT_DEDUP:
        _seen_alerts.popitem(last=False)
    return False


# ---------------------------------------------------------------------------
# Pattern alerts
# ---------------------------------------------------------------------------

@dataclass
class PatternAlert:
    """A detected pattern alert."""
    pattern: str          # "new_account_geo", "cluster", "dormant_reactivation"
    market: str
    side: str
    size: float
    wallet: str
    details: str
    severity: str = "medium"  # "low", "medium", "high"


# ---------------------------------------------------------------------------
# Pattern checks
# ---------------------------------------------------------------------------

_UNSET = object()


def _is_novel_wallet(
    age_days: Optional[float],
    prior_trade_ts: Optional[int],
    polymarket_count_truncated: bool,
) -> bool:
    """Wallet-quality gate used by the weak patterns (cluster, thin-market).

    A wallet is "novel" if it shows at least one of: first-ever Polymarket
    trade, dormant for ≥ dormant_days, or on-chain age < new_account_age_days.
    Fail-closed: if a lookup was truncated or didn't return data, the wallet
    is not considered novel and the weak pattern is suppressed. This is how
    we keep organic whale activity from firing cluster / thin-market alerts.
    """
    if polymarket_count_truncated:
        return False
    if prior_trade_ts is None:
        return True  # first-ever trade on Polymarket
    now = time.time()
    if (now - float(prior_trade_ts)) / 86400 >= TIER_1C.dormant_days:
        return True
    if age_days is not None and age_days < TIER_1C.new_account_age_days:
        return True
    return False


def _check_new_account_geo(
    trade: DetectedTrade,
    wa: WalletActivity,
    age_days=_UNSET,
    polymarket_trade_count: Optional[int] = None,
    polymarket_count_truncated: bool = False,
) -> Optional[PatternAlert]:
    """Pattern 1: New account placing a large geopolitical bet.

    Triggers when ALL of the following hold:
      - On-chain wallet age < new_account_age_days (None → fail closed)
      - Trade size ≥ min_first_bet
      - Market is geopolitical (Gamma tag or keyword fallback)
      - Wallet has ≤ max_lifetime_trades_for_new fills on Polymarket — this is
        the Polymarket-wide count from wallet_history, not the bot-observed one,
        so it survives bot restart and covers activity we never saw ourselves.

    `polymarket_trade_count` is the number of distinct fills currently cached
    for the wallet (from `wallet_history.get_prior_trade_ts`). If it is None,
    we fall back to the bot-observed `wa.trade_count` for tests / legacy paths.
    If `polymarket_count_truncated` is True the wallet has at least
    `wallet_history._LOOKUP_LIMIT` fills and therefore is definitively not new.
    """
    if age_days is _UNSET:
        age_days = (time.time() - wa.first_seen) / 86400
    if age_days is None or age_days >= TIER_1C.new_account_age_days:
        return None

    if trade.size < TIER_1C.min_first_bet:
        return None

    if not is_geopolitical_market(trade.market, getattr(trade, "condition_id", "")):
        return None

    if polymarket_count_truncated:
        return None

    effective_count = (
        polymarket_trade_count if polymarket_trade_count is not None else wa.trade_count
    )
    if effective_count > TIER_1C.max_lifetime_trades_for_new:
        return None

    alert_key = f"new_geo:{trade.trader_address}:{trade.market}:{trade.side}"
    if _is_duplicate_alert(alert_key):
        return None

    return PatternAlert(
        pattern="new_account_geo",
        market=trade.market,
        side=trade.side,
        size=trade.size,
        wallet=trade.trader_address,
        details=(
            f"New account ({age_days:.0f}d old, {effective_count} total fills) "
            f"placed ${trade.size:,.0f} {trade.side} on geo market"
        ),
        severity="high",
    )


def _check_first_ever_bet_geo(
    trade: DetectedTrade,
    prior_trade_ts: Optional[int],
    polymarket_trade_count: Optional[int],
) -> Optional[PatternAlert]:
    """Pattern 1b: This is the wallet's first-ever trade on Polymarket.

    `prior_trade_ts` comes from `wallet_history.get_prior_trade_ts(addr, tx)`:
    it is None when Polymarket has no record of any trade from this wallet
    other than the one we're analyzing right now.

    Strongest insider archetype: a wallet that has never traded Polymarket
    before suddenly shows up with a ≥ min_first_bet position on a geo market.
    """
    if prior_trade_ts is not None:
        return None
    # The history lookup may have failed (e.g. Data API 429). Only fire the
    # pattern when we have positive evidence of "no prior" — i.e. the count we
    # got back was an explicit 0 or 1 (1 = just the current trade).
    if polymarket_trade_count is None or polymarket_trade_count > 1:
        return None
    if trade.size < TIER_1C.min_first_bet:
        return None
    if not is_geopolitical_market(trade.market, getattr(trade, "condition_id", "")):
        return None

    alert_key = f"first_ever:{trade.trader_address}:{trade.market}:{trade.side}"
    if _is_duplicate_alert(alert_key):
        return None

    return PatternAlert(
        pattern="first_ever_bet_geo",
        market=trade.market,
        side=trade.side,
        size=trade.size,
        wallet=trade.trader_address,
        details=(
            f"First-ever Polymarket trade: ${trade.size:,.0f} {trade.side} on geo market"
        ),
        severity="high",
    )


def _check_cluster(
    trade: DetectedTrade,
    wallet_is_novel: bool = False,
) -> Optional[PatternAlert]:
    """Pattern 2: Coordinated cluster — 3+ wallets, same direction, same market, ≤15 min apart.

    Gated on `wallet_is_novel`: the current trade's wallet must have a novelty
    signal (first-ever, dormant, or on-chain-new). Without this gate, any hot
    geo market trivially produces 3 wallets betting the same side within the
    window, drowning the channel in organic-whale noise.
    """
    if not wallet_is_novel:
        return None
    if trade.size < TIER_1C.min_cluster_wallet_size_usd:
        return None

    cluster = _find_cluster(trade.market, trade.side, trade.trader_address)
    sizeable = [b for b in cluster if b.size >= TIER_1C.min_cluster_wallet_size_usd]
    if len(sizeable) < 2:
        return None

    wallets_involved = [b.wallet for b in sizeable] + [trade.trader_address.lower()]
    total_volume = sum(b.size for b in sizeable) + trade.size

    if total_volume < TIER_1C.min_cluster_volume_usd:
        return None

    # Dedup key intentionally does NOT include wallet count or volume —
    # otherwise every new wallet joining the same cluster bumps the key
    # and fires a fresh alert (producing the "12 / 13 / 14 / 15 wallets"
    # alert-spam chain we saw in prod).
    alert_key = f"cluster:{trade.market}:{trade.side}"
    if _is_duplicate_alert(alert_key):
        return None

    window_min = CLUSTER_DETECTION_WINDOW_S // 60
    return PatternAlert(
        pattern="cluster",
        market=trade.market,
        side=trade.side,
        size=trade.size,
        wallet=trade.trader_address,
        details=(
            f"{len(wallets_involved)} wallets betting {trade.side} on same market "
            f"within {window_min}m — current ${trade.size:,.0f}, cluster total ${total_volume:,.0f}"
        ),
        severity="high",
    )


def _check_same_funder_cluster(
    trade: DetectedTrade,
    current_funder: str,
) -> Optional[PatternAlert]:
    """Pattern 2b: Same-funder cluster — the strong version of cluster detection.

    Fires when N+ sizeable wallets in the recent-bets window share the *same*
    non-CEX USDC funder on Polygon. A shared funder is a much stronger insider
    signal than "three wallets happened to bet the same side" — it implies the
    wallets are operated by (or funded by) the same entity.

    Skipped entirely when the current trade has no known funder (empty string),
    because the base case is "wallets we explicitly looked up". The cache warms
    over time and coverage improves with the bot's uptime.
    """
    if not current_funder:
        return None
    if trade.size < TIER_1C.min_cluster_wallet_size_usd:
        return None

    now = time.time()
    cutoff = now - CLUSTER_WINDOW_S
    cf = current_funder.lower()
    current_wallet = trade.trader_address.lower()

    matches: list[RecentBet] = []
    seen_wallets: set[str] = {current_wallet}
    for bet in _recent_bets:
        if bet.timestamp < cutoff:
            continue
        if bet.funder != cf:
            continue
        if bet.size < TIER_1C.min_cluster_wallet_size_usd:
            continue
        if bet.wallet in seen_wallets:
            continue
        seen_wallets.add(bet.wallet)
        matches.append(bet)

    if len(matches) < TIER_1C.min_funder_cluster_wallets:
        return None

    total_volume = sum(b.size for b in matches) + trade.size
    wallets_involved = [b.wallet for b in matches] + [current_wallet]

    # Same dedup-key discipline as _check_cluster: market+funder only,
    # not wallet count, so joiners don't re-trigger the alert.
    alert_key = f"funder:{cf}:{trade.market}"
    if _is_duplicate_alert(alert_key):
        return None

    return PatternAlert(
        pattern="same_funder_cluster",
        market=trade.market,
        side=trade.side,
        size=trade.size,
        wallet=trade.trader_address,
        details=(
            f"{len(wallets_involved)} wallets funded by {cf[:10]}… "
            f"betting on geo markets within 1h — current ${trade.size:,.0f}, "
            f"cluster total ${total_volume:,.0f}"
        ),
        severity="high",
    )


def _reference_price_for_market(
    trade: DetectedTrade,
    gm: Optional["GeoMarket"],  # noqa: F821 — forward ref for clarity
    min_samples: int = 5,
) -> tuple[Optional[float], str]:
    """Return (reference_price, source) for this market.

    Preference order:
      1. Local VWAP of the last hour's trades on the same market (freshest).
         Only used when we have at least `min_samples` other bets to average.
      2. Gamma mid (best_bid + best_ask) / 2 when both sides are known.
      3. Gamma last_price.
      4. None → the caller should skip edge computation.
    """
    current_wallet = trade.trader_address.lower()
    vols: list[tuple[float, float]] = []  # (price, size)
    for bet in _recent_bets:
        if bet.market != trade.market:
            continue
        if bet.wallet == current_wallet:
            continue
        if bet.price <= 0 or bet.size <= 0:
            continue
        vols.append((bet.price, bet.size))
    if len(vols) >= min_samples:
        total_sz = sum(s for _, s in vols)
        if total_sz > 0:
            vwap = sum(p * s for p, s in vols) / total_sz
            return vwap, "local-vwap"

    if gm is not None:
        if gm.best_bid > 0 and gm.best_ask > 0:
            return (gm.best_bid + gm.best_ask) / 2.0, "gamma-mid"
        if gm.last_price > 0:
            return gm.last_price, "gamma-last"
    return None, "none"


def _check_late_geo_bet(trade: DetectedTrade) -> Optional[PatternAlert]:
    """Pattern 4: Large bet placed close to market resolution.

    Base trigger:
      - Market end_ts is in the future AND within close_proximity_hours
      - Trade size ≥ min_late_bet_usd
      - Market is geopolitical

    If a reference price can be computed and the current trade's price is
    far from it (|price - reference| ≥ late_edge_threshold), the alert is
    upgraded to flag the edge explicitly. Insiders who know the outcome
    tend to both (a) bet close to resolution and (b) pay a price different
    from the market consensus — catching both together is the strongest
    non-wallet-based signal we can compute.
    """
    if trade.size < TIER_1C.min_late_bet_usd:
        return None
    if not is_geopolitical_market(trade.market, getattr(trade, "condition_id", "")):
        return None

    try:
        from src.copy_trading.geo_market_scanner import get_geo_market
        gm = get_geo_market(getattr(trade, "condition_id", "") or "")
    except Exception:
        gm = None
    if gm is None or gm.end_ts <= 0:
        return None

    now = time.time()
    hours_to_close = (gm.end_ts - now) / 3600.0
    if hours_to_close <= 0 or hours_to_close > TIER_1C.close_proximity_hours:
        return None

    ref_price, ref_source = _reference_price_for_market(trade, gm)
    edge = None
    if ref_price is not None and trade.price > 0:
        edge = abs(trade.price - ref_price)

    alert_key = f"late:{trade.trader_address}:{trade.market}:{trade.side}"
    if _is_duplicate_alert(alert_key):
        return None

    base = f"${trade.size:,.0f} {trade.side} with {hours_to_close:.1f}h to resolution"
    if edge is not None and edge >= TIER_1C.late_edge_threshold:
        details = (
            f"Large late bet @ EDGE: {base} @ {trade.price:.3f} "
            f"(ref {ref_price:.3f} via {ref_source}, edge {edge:.3f})"
        )
    elif edge is not None:
        details = f"Large late bet: {base} @ {trade.price:.3f} (ref {ref_price:.3f})"
    else:
        details = f"Large late bet: {base}"

    return PatternAlert(
        pattern="late_geo_bet",
        market=trade.market,
        side=trade.side,
        size=trade.size,
        wallet=trade.trader_address,
        details=details,
        severity="high",
    )


def _check_thin_market_dominance(
    trade: DetectedTrade,
    wallet_is_novel: bool = False,
) -> Optional[PatternAlert]:
    """Pattern 5: Bet size dominates a *genuinely thin* market's liquidity.

    Gated on two things to avoid firing on every medium-sized whale trade:
      1. Wallet must be novel (first-ever / dormant / on-chain-new). A known
         whale moving $15k on a geo market is noise, not an insider signal.
      2. Market must actually be thin — weekly volume ≤ max_weekly_volume_for_thin_usd.
         Gamma's `liquidity` is rest-book depth and constantly replenishes;
         using it alone labels any liquid market "thin". The weekly-volume
         gate is the honest thinness check.

    Then we fire only the ratio that actually crossed a threshold (not both),
    so the alert message doesn't leak comforting numbers next to alarming ones.
    """
    if not wallet_is_novel:
        return None
    if trade.size < TIER_1C.min_thin_market_bet_usd:
        return None
    if not is_geopolitical_market(trade.market, getattr(trade, "condition_id", "")):
        return None

    try:
        from src.copy_trading.geo_market_scanner import get_geo_market
        gm = get_geo_market(getattr(trade, "condition_id", "") or "")
    except Exception:
        gm = None
    if gm is None:
        return None

    # Thinness gate: a market that traded > max_weekly_volume_for_thin_usd in
    # the last week is not thin, regardless of current book snapshot. Unknown
    # volume (0) is treated as "possibly thin" and allowed through.
    if gm.volume_1w_usd > TIER_1C.max_weekly_volume_for_thin_usd:
        return None

    liquidity_ratio = 0.0
    if gm.liquidity_usd > 0:
        liquidity_ratio = trade.size / gm.liquidity_usd
    volume_ratio = 0.0
    if gm.volume_1w_usd > 0:
        volume_ratio = trade.size / gm.volume_1w_usd

    if liquidity_ratio == 0.0 and volume_ratio == 0.0:
        return None

    liquidity_fires = liquidity_ratio >= TIER_1C.thin_market_dominance_ratio
    volume_fires = volume_ratio >= TIER_1C.thin_market_weekly_ratio
    if not (liquidity_fires or volume_fires):
        return None

    alert_key = f"thin:{trade.trader_address}:{trade.market}:{trade.side}"
    if _is_duplicate_alert(alert_key):
        return None

    dom_bits = []
    if liquidity_fires:
        dom_bits.append(f"{liquidity_ratio*100:.0f}% of ${gm.liquidity_usd:,.0f} liquidity")
    if volume_fires:
        dom_bits.append(f"{volume_ratio*100:.0f}% of ${gm.volume_1w_usd:,.0f} weekly volume")
    return PatternAlert(
        pattern="thin_market_dominance",
        market=trade.market,
        side=trade.side,
        size=trade.size,
        wallet=trade.trader_address,
        details=(
            f"${trade.size:,.0f} {trade.side} = " + " / ".join(dom_bits)
        ),
        severity="high",
    )


def _check_dormant_reactivation(
    trade: DetectedTrade,
    wa: WalletActivity,
) -> Optional[PatternAlert]:
    """Pattern 3: Dormant account reactivation with large geo bet.

    Triggers when:
      - Account was inactive for > 60 days (configurable via TIER_1C.dormant_days)
      - Trade is a large geopolitical bet
    """
    now = time.time()

    # Need at least one previous trade to determine dormancy
    if wa.trade_count <= 1:
        return None

    # Check time since the *previous* last_seen (before this trade updated it).
    # Since _update_wallet_activity already set last_seen = now, we approximate
    # by checking if the gap between first_seen and now is large relative to
    # trade_count (i.e., very low activity).
    # Better approach: we check the gap. Since last_seen was just updated,
    # we stored the *previous* last_seen nowhere. Instead, check: if the
    # wallet has very few trades over a long period, that's suspicious.
    # Actually, we need the previous last_seen. We'll compute it from the
    # fact that _update_wallet_activity is called before this check.
    # The caller should pass the previous last_seen. For simplicity,
    # we track it inline.
    #
    # NOTE: The caller (_analyze) records previous_last_seen before updating.
    return None  # Handled inline in analyze_trade_for_patterns


def _check_dormant_reactivation_with_prev(
    trade: DetectedTrade,
    wa: WalletActivity,
    previous_last_seen: float,
) -> Optional[PatternAlert]:
    """Legacy path: dormancy inferred from bot-observed last_seen.

    Kept for tests that pre-date the wallet_history lookup. Real production
    calls should use `_check_dormant_reactivation_from_history` below.
    """
    return _dormant_alert(trade, previous_last_seen, source="in-memory")


def _check_dormant_reactivation_from_history(
    trade: DetectedTrade,
    prior_trade_ts: Optional[int],
) -> Optional[PatternAlert]:
    """Pattern 3: wallet has a prior Polymarket trade, but it is ≥ dormant_days old.

    `prior_trade_ts` comes from `wallet_history.get_prior_trade_ts(addr, tx)`.
    None means no prior activity (→ handled by `first_ever_bet_geo` instead).
    """
    if prior_trade_ts is None:
        return None
    return _dormant_alert(trade, float(prior_trade_ts), source="polymarket")


def _dormant_alert(
    trade: DetectedTrade,
    previous_ts: float,
    source: str,
) -> Optional[PatternAlert]:
    now = time.time()
    inactive_days = (now - previous_ts) / 86400

    if inactive_days < TIER_1C.dormant_days:
        return None

    if trade.size < TIER_1C.min_first_bet:
        return None

    if not is_geopolitical_market(trade.market, getattr(trade, "condition_id", "")):
        return None

    alert_key = f"dormant:{trade.trader_address}:{trade.market}:{trade.side}"
    if _is_duplicate_alert(alert_key):
        return None

    return PatternAlert(
        pattern="dormant_reactivation",
        market=trade.market,
        side=trade.side,
        size=trade.size,
        wallet=trade.trader_address,
        details=(
            f"Dormant account ({inactive_days:.0f}d inactive, via {source}) "
            f"placed ${trade.size:,.0f} {trade.side} on geo market"
        ),
        severity="high",
    )


# ---------------------------------------------------------------------------
# Telegram alert sender
# ---------------------------------------------------------------------------

async def _send_pattern_alert(alert: PatternAlert) -> None:
    """Send a Telegram alert for a detected pattern."""
    try:
        from src.copy_trading.telegram_notifier import _send_message, _escape_html

        severity_icon = {"low": "🟡", "medium": "🟠", "high": "🔴"}.get(alert.severity, "⚪")
        pattern_label = {
            "new_account_geo": "New Account + Geo Bet",
            "first_ever_bet_geo": "First-Ever Polymarket Bet",
            "cluster": "Coordinated Cluster",
            "same_funder_cluster": "Same-Funder Cluster",
            "dormant_reactivation": "Dormant Reactivation",
            "late_geo_bet": "Late Geo Bet (Near Resolution)",
            "thin_market_dominance": "Thin Market Dominance",
        }.get(alert.pattern, alert.pattern)

        text = (
            f'{severity_icon} <b>Pattern: {_escape_html(pattern_label)}</b>\n'
            f'Market: "{_escape_html(alert.market)}"\n'
            f'Side: {alert.side} | Size: ${alert.size:,.0f}\n'
            f'Wallet: {alert.wallet[:10]}...\n'
            f'{_escape_html(alert.details)}'
        )
        await _send_message(text)
    except Exception as exc:
        logger.warn(f"[pattern] Failed to send alert: {exc}")


# ---------------------------------------------------------------------------
# Main analysis entry point
# ---------------------------------------------------------------------------

async def analyze_trade_for_patterns(
    trade: DetectedTrade,
    trade_ts: Optional[float] = None,
) -> list[PatternAlert]:
    """Analyze a trade for Strategy 1c patterns.

    Updates wallet activity, adds to recent bets, runs all three pattern
    checks, sends Telegram alerts for any matches.

    Args:
        trade: The detected trade to analyze.
        trade_ts: Actual epoch seconds of the trade, used so the cluster
            window prunes stale entries correctly. Defaults to wall-clock
            time when not supplied (legacy callers / tests).

    Returns:
        List of PatternAlert objects (may be empty).
    """
    alerts: list[PatternAlert] = []

    # Bot-observed wallet bookkeeping (still used for cluster state and as a
    # fallback for tests that don't wire the live lookups).
    wa = _get_or_create_wallet(trade.trader_address)
    wa = _update_wallet_activity(trade.trader_address, trade.size)

    # Live lookups — run them concurrently to hide their individual latencies.
    # All fail closed: unknown = no alert.
    age_days: Optional[float] = None
    prior_ts: Optional[int] = None
    pm_count: Optional[int] = None
    pm_truncated: bool = False
    try:
        from src.copy_trading.wallet_age import get_wallet_age_days
        from src.copy_trading.wallet_history import get_prior_trade_ts

        tx_hash = _extract_tx_hash(trade)
        age_task = asyncio.create_task(get_wallet_age_days(trade.trader_address))
        hist_task = asyncio.create_task(
            get_prior_trade_ts(trade.trader_address, exclude_tx=tx_hash)
        )
        results = await asyncio.gather(age_task, hist_task, return_exceptions=True)
        age_res, hist_res = results
        if not isinstance(age_res, BaseException):
            age_days = age_res
        if not isinstance(hist_res, BaseException):
            prior_ts, pm_count, pm_truncated = hist_res
    except Exception as exc:
        logger.debug(f"[pattern] live lookup failed: {exc}")

    # Funder lookup — gated to keep Etherscan traffic bounded. We only fetch
    # funder info for sizeable, not-obviously-active wallets where the signal
    # is worth the API call. The cache is permanent in SQLite so repeat
    # sightings cost zero.
    current_funder = ""
    worth_funder_lookup = (
        trade.size >= TIER_1C.min_cluster_wallet_size_usd
        and not pm_truncated
        and (pm_count is None or pm_count <= TIER_1C.funder_max_polymarket_trades)
    )
    if worth_funder_lookup:
        try:
            from src.copy_trading.wallet_funder import get_funder
            info = await get_funder(trade.trader_address)
            current_funder = info.funder
        except Exception as exc:
            logger.debug(f"[pattern] funder lookup failed: {exc}")

    # Now that we have the funder, add the recent-bet entry with it attached.
    # Same-funder cluster and edge-price checks read these fields synchronously.
    _add_recent_bet(
        trade.trader_address,
        trade.market,
        trade.side,
        trade.size,
        funder=current_funder,
        price=trade.price,
        trade_ts=trade_ts,
    )

    # Pattern 1: New account + large geo bet
    alert = _check_new_account_geo(
        trade, wa,
        age_days=age_days,
        polymarket_trade_count=pm_count,
        polymarket_count_truncated=pm_truncated,
    )
    if alert is not None:
        alerts.append(alert)

    # Pattern 1b: First-ever Polymarket bet on a geo market
    alert = _check_first_ever_bet_geo(trade, prior_trade_ts=prior_ts, polymarket_trade_count=pm_count)
    if alert is not None:
        alerts.append(alert)

    # Novelty gate used by the weak patterns (cluster, thin_market_dominance).
    # Computed once from the already-fetched wallet_history/wallet_age data.
    wallet_is_novel = _is_novel_wallet(
        age_days=age_days,
        prior_trade_ts=prior_ts,
        polymarket_count_truncated=pm_truncated,
    )

    # Pattern 2: Cluster detection (same side + same market + novelty gate)
    alert = _check_cluster(trade, wallet_is_novel=wallet_is_novel)
    if alert is not None:
        alerts.append(alert)

    # Pattern 2b: Same-funder cluster (stronger — wallets share a USDC funder)
    alert = _check_same_funder_cluster(trade, current_funder=current_funder)
    if alert is not None:
        alerts.append(alert)

    # Pattern 3: Dormant reactivation (live Polymarket history, not bot memory)
    alert = _check_dormant_reactivation_from_history(trade, prior_trade_ts=prior_ts)
    if alert is not None:
        alerts.append(alert)

    # Pattern 4: Large bet placed close to market resolution (with edge detection)
    alert = _check_late_geo_bet(trade)
    if alert is not None:
        alerts.append(alert)

    # Pattern 5: Bet dominates a thin market's liquidity / weekly volume
    alert = _check_thin_market_dominance(trade, wallet_is_novel=wallet_is_novel)
    if alert is not None:
        alerts.append(alert)

    # Send Telegram alerts
    for a in alerts:
        logger.info(f"[pattern] Detected: {a.pattern} — {a.details}")
        await _send_pattern_alert(a)

    return alerts


# ---------------------------------------------------------------------------
# Test helper
# ---------------------------------------------------------------------------

def _reset_pattern_detector() -> None:
    """Reset all module-level state. For testing only."""
    global _wallet_activities, _recent_bets, _seen_alerts
    _wallet_activities = {}
    _recent_bets = []
    _seen_alerts = OrderedDict()
