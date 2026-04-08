"""Strategy 1c pattern detection for insider/manipulation signals.

Detects three patterns:
  1. New account + large geopolitical bet (account age < 30d, bet > $5K, geo market)
  2. Cluster detection (3+ wallets, same direction, same market, within 1h window)
  3. Dormant account reactivation (inactive > 60d, large geo bet)

Thread-safe with module-level state. Call _reset_pattern_detector() in tests.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from src.config import CONFIG
from src.copy_trading.strategy_config import TIER_1C
from src.logger import logger
from src.models import DetectedTrade

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


def is_geopolitical_market(title: str) -> bool:
    """Check if a market title relates to geopolitical events."""
    lower = title.lower()
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

MAX_RECENT_BETS = 500
CLUSTER_WINDOW_S = 3600  # 1 hour

@dataclass
class RecentBet:
    """A recent bet for cluster detection."""
    wallet: str
    market: str
    side: str
    size: float
    timestamp: float  # epoch seconds


_recent_bets: list[RecentBet] = []


def _add_recent_bet(wallet: str, market: str, side: str, size: float) -> None:
    """Add a bet to the recent bets list, pruning if over limit."""
    now = time.time()
    _recent_bets.append(RecentBet(
        wallet=wallet.lower(),
        market=market,
        side=side,
        size=size,
        timestamp=now,
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
    cutoff = now - CLUSTER_WINDOW_S
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

def _check_new_account_geo(
    trade: DetectedTrade,
    wa: WalletActivity,
) -> Optional[PatternAlert]:
    """Pattern 1: New account placing a large geopolitical bet.

    Triggers when:
      - Account age < 30 days (configurable via TIER_1C.new_account_age_days)
      - Trade size > $5K (configurable via TIER_1C.min_first_bet)
      - Market is geopolitical
    """
    now = time.time()
    age_days = (now - wa.first_seen) / 86400

    if age_days >= TIER_1C.new_account_age_days:
        return None

    if trade.size < TIER_1C.min_first_bet:
        return None

    if not is_geopolitical_market(trade.market):
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
            f"New account ({age_days:.0f}d old) placed ${trade.size:,.0f} "
            f"{trade.side} on geo market"
        ),
        severity="high",
    )


def _check_cluster(trade: DetectedTrade) -> Optional[PatternAlert]:
    """Pattern 2: Coordinated cluster — 3+ wallets, same direction, same market, within 1h.

    Triggers when 3 or more distinct wallets (including current) bet the same
    direction on the same market within the cluster window.
    """
    cluster = _find_cluster(trade.market, trade.side, trade.trader_address)
    # cluster excludes the current wallet; we need 2+ others = 3+ total
    if len(cluster) < 2:
        return None

    wallets_involved = [b.wallet for b in cluster] + [trade.trader_address.lower()]
    total_volume = sum(b.size for b in cluster) + trade.size

    alert_key = f"cluster:{trade.market}:{trade.side}:{len(wallets_involved)}"
    if _is_duplicate_alert(alert_key):
        return None

    return PatternAlert(
        pattern="cluster",
        market=trade.market,
        side=trade.side,
        size=total_volume,
        wallet=trade.trader_address,
        details=(
            f"{len(wallets_involved)} wallets betting {trade.side} on same market "
            f"within 1h (total ${total_volume:,.0f})"
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
    """Pattern 3 with explicit previous last_seen timestamp."""
    now = time.time()
    inactive_days = (now - previous_last_seen) / 86400

    if inactive_days < TIER_1C.dormant_days:
        return None

    if trade.size < TIER_1C.min_first_bet:
        return None

    if not is_geopolitical_market(trade.market):
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
            f"Dormant account ({inactive_days:.0f}d inactive) placed "
            f"${trade.size:,.0f} {trade.side} on geo market"
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
            "cluster": "Coordinated Cluster",
            "dormant_reactivation": "Dormant Reactivation",
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

async def analyze_trade_for_patterns(trade: DetectedTrade) -> list[PatternAlert]:
    """Analyze a trade for Strategy 1c patterns.

    Updates wallet activity, adds to recent bets, runs all three pattern
    checks, sends Telegram alerts for any matches.

    Args:
        trade: The detected trade to analyze.

    Returns:
        List of PatternAlert objects (may be empty).
    """
    alerts: list[PatternAlert] = []

    # Capture previous last_seen before updating
    wa = _get_or_create_wallet(trade.trader_address)
    previous_last_seen = wa.last_seen

    # Update wallet activity
    wa = _update_wallet_activity(trade.trader_address, trade.size)

    # Add to recent bets for cluster detection
    _add_recent_bet(trade.trader_address, trade.market, trade.side, trade.size)

    # Pattern 1: New account + large geo bet
    alert = _check_new_account_geo(trade, wa)
    if alert is not None:
        alerts.append(alert)

    # Pattern 2: Cluster detection
    alert = _check_cluster(trade)
    if alert is not None:
        alerts.append(alert)

    # Pattern 3: Dormant reactivation
    alert = _check_dormant_reactivation_with_prev(trade, wa, previous_last_seen)
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
