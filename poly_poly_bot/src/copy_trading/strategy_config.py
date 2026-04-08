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


@dataclass
class Strategy1cConfig(TierConfig):
    auto_follow: bool = False
    new_account_age_days: float = 30.0
    min_first_bet: float = 5000.0
    dormant_days: float = 60.0


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
