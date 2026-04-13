"""Tiered insider strategy configuration (1a/1b/1c)."""

from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import Literal, Optional

from src.config_validators import parse_addresses, validate_address

StrategyTier = Literal["1a", "1b", "1c", "legacy"]


def _opt(name: str, fallback: str) -> str:
    return os.environ.get(name, "").strip() or fallback

def _opt_float(name: str, fallback: float) -> float:
    v = os.environ.get(name, "").strip()
    return float(v) if v else fallback

def _opt_int(name: str, fallback: int) -> int:
    v = os.environ.get(name, "").strip()
    return int(v) if v else fallback

def _opt_bool(name: str, fallback: bool) -> bool:
    v = os.environ.get(name, "").strip()
    if not v:
        return fallback
    return v.lower() == "true"

def _load_wallets(env_key: str) -> list[str]:
    raw = _opt(env_key, "")
    if not raw:
        return []
    addrs = parse_addresses(raw)
    for a in addrs:
        validate_address(a, env_key)
    return addrs


@dataclass
class TierConfig:
    tier: StrategyTier
    enabled: bool = False
    wallets: list[str] = field(default_factory=list)
    copy_percentage: float = 10.0
    max_bet: float = 50.0
    min_bet: float = 5.0
    max_total_exposure: float = 500.0
    max_price: float = 0.85
    min_price: float = 0.0
    min_trader_bet: float = 0.0
    hold_to_settlement: bool = True
    alert_only: bool = False


_DEFAULT_GEO_TAGS = [
    # "politics" is deliberately excluded: Gamma attaches it to pop-culture
    # markets ("Russia-Ukraine Ceasefire before GTA VI?") and it dominates the
    # result set. The narrower tags below are true geopolitical/conflict tags.
    "geopolitics",
    "world",
    "elections",
    "ukraine",
    "israel",
    "middle-east",
    "iran",
    "russia",
    "china",
    "nato",
]


@dataclass
class Strategy1cConfig(TierConfig):
    auto_follow: bool = False
    new_account_age_days: float = 30.0
    min_first_bet: float = 5000.0
    dormant_days: float = 60.0
    max_lifetime_trades_for_new: int = 5
    geo_tags: list[str] = field(default_factory=lambda: list(_DEFAULT_GEO_TAGS))
    market_scan_interval_s: float = 3600.0
    activity_poll_interval_s: float = 60.0
    min_cluster_volume_usd: float = 25000.0
    min_cluster_wallet_size_usd: float = 2000.0
    # Same-funder cluster: require N wallets that share a non-CEX USDC funder.
    min_funder_cluster_wallets: int = 2  # 2 others + current trade = 3 total
    # Only fetch funder for wallets with ≤ this many Polymarket fills. Above
    # that, the funder signal is too dilute and the Etherscan call isn't worth it.
    funder_max_polymarket_trades: int = 20
    # Late-bet pattern: fires on large bets placed within `close_proximity_hours`
    # of the market's resolution time. `late_edge_threshold` (in price units,
    # 0-1 scale) upgrades the alert message when the bet price diverges from
    # local VWAP / Gamma mid by at least that much — the strongest non-wallet
    # insider signal: "big bet close to close, at a price the market disagrees with".
    close_proximity_hours: float = 24.0
    min_late_bet_usd: float = 10000.0
    late_edge_threshold: float = 0.05
    # Thin-market dominance: flag bets that consume a large fraction of the
    # Gamma-reported book depth or weekly volume on a *genuinely thin* geo
    # market. A market is "thin" only if weekly volume ≤ max_weekly_volume_for_thin_usd;
    # above that, book-depth ratios are meaningless because the book replenishes.
    min_thin_market_bet_usd: float = 5000.0
    thin_market_dominance_ratio: float = 0.40  # bet ≥ 40% of resting liquidity
    thin_market_weekly_ratio: float = 0.60     # bet ≥ 60% of weekly volume
    max_weekly_volume_for_thin_usd: float = 50000.0


def _load_tier_1a() -> TierConfig:
    wallets = _load_wallets("STRATEGY_1A_WALLETS")
    return TierConfig(
        tier="1a",
        enabled=_opt_bool("STRATEGY_1A_ENABLED", len(wallets) > 0),
        wallets=wallets,
        copy_percentage=_opt_float("STRATEGY_1A_COPY_PERCENTAGE", 10),
        max_bet=_opt_float("STRATEGY_1A_MAX_BET", 50),
        min_bet=_opt_float("STRATEGY_1A_MIN_BET", 5),
        max_total_exposure=_opt_float("STRATEGY_1A_MAX_TOTAL_EXPOSURE", 500),
        max_price=_opt_float("STRATEGY_1A_MAX_PRICE", 0.85),
        min_price=_opt_float("STRATEGY_1A_MIN_PRICE", 0),
        min_trader_bet=_opt_float("STRATEGY_1A_MIN_TRADER_BET", 0),
        hold_to_settlement=_opt_bool("STRATEGY_1A_HOLD_TO_SETTLEMENT", True),
        alert_only=False,
    )


def _load_tier_1b() -> TierConfig:
    wallets = _load_wallets("STRATEGY_1B_WALLETS")
    return TierConfig(
        tier="1b",
        enabled=_opt_bool("STRATEGY_1B_ENABLED", len(wallets) > 0),
        wallets=wallets,
        copy_percentage=_opt_float("STRATEGY_1B_COPY_PERCENTAGE", 5),
        max_bet=_opt_float("STRATEGY_1B_MAX_BET", 25),
        min_bet=_opt_float("STRATEGY_1B_MIN_BET", 5),
        max_total_exposure=_opt_float("STRATEGY_1B_MAX_TOTAL_EXPOSURE", 200),
        max_price=_opt_float("STRATEGY_1B_MAX_PRICE", 0.90),
        min_price=_opt_float("STRATEGY_1B_MIN_PRICE", 0.10),
        min_trader_bet=_opt_float("STRATEGY_1B_MIN_TRADER_BET", 10000),
        hold_to_settlement=_opt_bool("STRATEGY_1B_HOLD_TO_SETTLEMENT", False),
        alert_only=False,
    )


def _load_geo_tags() -> list[str]:
    raw = _opt("STRATEGY_1C_GEO_TAGS", "")
    if not raw:
        return list(_DEFAULT_GEO_TAGS)
    return [t.strip() for t in raw.split(",") if t.strip()]


def _load_tier_1c() -> Strategy1cConfig:
    return Strategy1cConfig(
        tier="1c",
        enabled=_opt_bool("STRATEGY_1C_ENABLED", False),
        wallets=[],
        copy_percentage=_opt_float("STRATEGY_1C_COPY_PERCENTAGE", 5),
        max_bet=_opt_float("STRATEGY_1C_MAX_BET", 10),
        min_bet=_opt_float("STRATEGY_1C_MIN_BET", 5),
        max_total_exposure=_opt_float("STRATEGY_1C_MAX_TOTAL_EXPOSURE", 100),
        max_price=_opt_float("STRATEGY_1C_MAX_PRICE", 0.90),
        min_price=_opt_float("STRATEGY_1C_MIN_PRICE", 0.10),
        min_trader_bet=_opt_float("STRATEGY_1C_MIN_TRADER_BET", 0),
        hold_to_settlement=False,
        alert_only=_opt_bool("STRATEGY_1C_ALERT_ONLY", True),
        auto_follow=_opt_bool("STRATEGY_1C_AUTO_FOLLOW", False),
        new_account_age_days=_opt_float("STRATEGY_1C_NEW_ACCOUNT_AGE_DAYS", 30),
        min_first_bet=_opt_float("STRATEGY_1C_MIN_FIRST_BET", 5000),
        dormant_days=_opt_float("STRATEGY_1C_DORMANT_DAYS", 60),
        max_lifetime_trades_for_new=_opt_int("STRATEGY_1C_MAX_LIFETIME_TRADES_FOR_NEW", 5),
        geo_tags=_load_geo_tags(),
        market_scan_interval_s=_opt_float("STRATEGY_1C_MARKET_SCAN_INTERVAL_S", 3600),
        activity_poll_interval_s=_opt_float("STRATEGY_1C_ACTIVITY_POLL_INTERVAL_S", 60),
        min_cluster_volume_usd=_opt_float("STRATEGY_1C_MIN_CLUSTER_VOLUME_USD", 25000),
        min_cluster_wallet_size_usd=_opt_float("STRATEGY_1C_MIN_CLUSTER_WALLET_SIZE_USD", 2000),
        min_funder_cluster_wallets=_opt_int("STRATEGY_1C_MIN_FUNDER_CLUSTER_WALLETS", 2),
        funder_max_polymarket_trades=_opt_int("STRATEGY_1C_FUNDER_MAX_PM_TRADES", 20),
        close_proximity_hours=_opt_float("STRATEGY_1C_CLOSE_PROXIMITY_HOURS", 24),
        min_late_bet_usd=_opt_float("STRATEGY_1C_MIN_LATE_BET_USD", 10000),
        late_edge_threshold=_opt_float("STRATEGY_1C_LATE_EDGE_THRESHOLD", 0.05),
        min_thin_market_bet_usd=_opt_float("STRATEGY_1C_MIN_THIN_MARKET_BET_USD", 5000),
        thin_market_dominance_ratio=_opt_float("STRATEGY_1C_THIN_MARKET_DOMINANCE_RATIO", 0.40),
        thin_market_weekly_ratio=_opt_float("STRATEGY_1C_THIN_MARKET_WEEKLY_RATIO", 0.60),
        max_weekly_volume_for_thin_usd=_opt_float("STRATEGY_1C_MAX_WEEKLY_VOLUME_FOR_THIN_USD", 50000),
    )


TIER_1A = _load_tier_1a()
TIER_1B = _load_tier_1b()
TIER_1C = _load_tier_1c()
TIERED_MODE = len(TIER_1A.wallets) > 0 or len(TIER_1B.wallets) > 0 or TIER_1C.enabled


def get_all_tiered_wallets() -> list[str]:
    """All tracked wallets across tiers (for detection). Does NOT include 1c (dynamic)."""
    seen: set[str] = set()
    if TIER_1A.enabled:
        for w in TIER_1A.wallets:
            seen.add(w.lower())
    if TIER_1B.enabled:
        for w in TIER_1B.wallets:
            seen.add(w.lower())
    return list(seen)


# Wallet -> tier mapping
_wallet_tier_map: dict[str, StrategyTier] = {}

def _build_wallet_tier_map() -> None:
    if TIER_1A.enabled:
        for w in TIER_1A.wallets:
            _wallet_tier_map[w.lower()] = "1a"
    if TIER_1B.enabled:
        for w in TIER_1B.wallets:
            _wallet_tier_map[w.lower()] = "1b"

_build_wallet_tier_map()


def get_wallet_tier(address: str) -> Optional[StrategyTier]:
    """Look up which tier a wallet belongs to (case-insensitive)."""
    return _wallet_tier_map.get(address.lower())


def get_tier_config(tier: StrategyTier) -> TierConfig:
    """Get the config object for a given tier."""
    if tier == "1a":
        return TIER_1A
    elif tier == "1b":
        return TIER_1B
    elif tier == "1c":
        return TIER_1C
    raise ValueError(f"Unknown tier: {tier}")
