"""Tiered risk manager for 1a/1b/1c strategy tiers.

Each tier has independent exposure tracking with its own limits.
Sizing algorithm:
  1. raw_size = trader_bet * COPY_PERCENTAGE / 100
  2. size = max(raw_size, MIN_BET)
  3. size = min(size, MAX_BET)
  4. remaining = MAX_TOTAL_EXPOSURE - current_open_total
  5. if size > remaining: size = remaining
  6. if size < MIN_BET: SKIP

State persisted to data/tiered-risk-state.json with atomic writes.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src.config import CONFIG
from src.copy_trading.strategy_config import (
    StrategyTier,
    TierConfig,
    get_tier_config,
)
from src.logger import logger
from src.models import DetectedTrade, TieredCopyDecision
from src.utils import round_cents, today_utc


# ---------------------------------------------------------------------------
# Tier exposure tracking
# ---------------------------------------------------------------------------

@dataclass
class TierExposure:
    """Per-tier open exposure and daily volume tracking."""

    open_total: float = 0.0
    daily_date: str = ""
    daily_volume: float = 0.0


_tier_exposures: dict[str, TierExposure] = {
    "1a": TierExposure(),
    "1b": TierExposure(),
    "1c": TierExposure(),
}

_STATE_FILE = os.path.join(CONFIG.data_dir, "tiered-risk-state.json")


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _atomic_write_json(path: str, data: dict) -> None:
    """Write JSON atomically: write to tmp file then rename."""
    dir_path = os.path.dirname(path)
    os.makedirs(dir_path, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _load_state() -> None:
    """Load tiered risk state from disk."""
    global _tier_exposures
    try:
        with open(_STATE_FILE, "r") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return

    today = today_utc()
    for tier_key in ("1a", "1b", "1c"):
        tier_data = raw.get(tier_key, {})
        exp = _tier_exposures[tier_key]
        exp.open_total = float(tier_data.get("open_total", 0))
        exp.daily_date = tier_data.get("daily_date", "")
        exp.daily_volume = float(tier_data.get("daily_volume", 0))
        # Reset daily volume on new day
        if exp.daily_date != today:
            exp.daily_volume = 0.0
            exp.daily_date = today


def _save_state() -> None:
    """Persist tiered risk state to disk."""
    data: dict = {}
    for tier_key in ("1a", "1b", "1c"):
        exp = _tier_exposures[tier_key]
        data[tier_key] = {
            "open_total": exp.open_total,
            "daily_date": exp.daily_date,
            "daily_volume": exp.daily_volume,
        }
    _atomic_write_json(_STATE_FILE, data)


# Load on import
_load_state()


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------

def _evaluate_tiered_trade_with_state(
    trade: DetectedTrade,
    tier: StrategyTier,
    exposure: TierExposure,
    cfg: TierConfig,
) -> TieredCopyDecision:
    """Evaluate a trade against tiered risk rules.

    Checks (in order):
    1. NaN guard
    2. Trade age
    3. Min trader bet
    4. Price bounds (min_price, max_price)
    5. Sizing algorithm
    6. Alert-only mode (1c)
    """

    def skip(reason: str) -> TieredCopyDecision:
        return TieredCopyDecision(
            should_copy=False,
            copy_size=0,
            tier=tier,
            alert_only=cfg.alert_only,
            reason=reason,
        )

    # 1. NaN guard
    if math.isnan(trade.price) or math.isnan(trade.size):
        return skip("NaN price or size")

    # 2. Trade age
    try:
        trade_ts = datetime.fromisoformat(trade.timestamp.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - trade_ts).total_seconds() / 3600
        if age_hours > CONFIG.max_trade_age_hours:
            return skip(f"Trade too old: {age_hours:.1f}h > {CONFIG.max_trade_age_hours}h")
    except (ValueError, TypeError):
        pass

    # 3. Min trader bet
    if cfg.min_trader_bet > 0 and trade.size < cfg.min_trader_bet:
        return skip(
            f"Trader bet ${trade.size:.2f} < min_trader_bet ${cfg.min_trader_bet:.2f} for tier {tier}"
        )

    # 4. Price bounds
    if trade.price < cfg.min_price:
        return skip(f"Price {trade.price:.4f} < tier {tier} min {cfg.min_price}")
    if trade.price > cfg.max_price:
        return skip(f"Price {trade.price:.4f} > tier {tier} max {cfg.max_price}")

    # 5. Sizing algorithm
    #    Step 1: raw_size = trader_bet * COPY_PERCENTAGE / 100
    raw_size = trade.size * cfg.copy_percentage / 100.0

    #    Step 2: size = max(raw_size, MIN_BET)
    size = max(raw_size, cfg.min_bet)

    #    Step 3: size = min(size, MAX_BET)
    size = min(size, cfg.max_bet)

    #    Step 4: remaining = MAX_TOTAL_EXPOSURE - current_open_total
    remaining = cfg.max_total_exposure - exposure.open_total

    #    Step 5: if size > remaining: size = remaining
    if size > remaining:
        size = remaining

    #    Step 6: if size < MIN_BET: SKIP
    if size < cfg.min_bet:
        return skip(
            f"Tier {tier} exposure full: remaining=${remaining:.2f} < min_bet=${cfg.min_bet:.2f}"
        )

    size = round_cents(size)

    # 6. Alert-only mode (1c tier)
    if cfg.alert_only:
        return TieredCopyDecision(
            should_copy=False,
            copy_size=size,
            tier=tier,
            alert_only=True,
            reason=f"Alert-only mode for tier {tier} (would copy ${size:.2f})",
        )

    return TieredCopyDecision(
        should_copy=True,
        copy_size=size,
        tier=tier,
        alert_only=False,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def evaluate_tiered_trade(
    trade: DetectedTrade,
    tier: StrategyTier,
) -> TieredCopyDecision:
    """Evaluate a trade for a specific tier using global state."""
    _load_state()
    cfg = get_tier_config(tier)
    if not cfg.enabled:
        return TieredCopyDecision(
            should_copy=False,
            copy_size=0,
            tier=tier,
            alert_only=False,
            reason=f"Tier {tier} is disabled",
        )
    exposure = _tier_exposures.get(tier, TierExposure())
    return _evaluate_tiered_trade_with_state(trade, tier, exposure, cfg)


def record_tiered_placement(tier: StrategyTier, copy_size: float) -> None:
    """Record a placed trade for a tier (increases open exposure and daily volume)."""
    exp = _tier_exposures.get(tier)
    if exp is None:
        logger.warn(f"[tiered-risk] Unknown tier: {tier}")
        return

    today = today_utc()
    if exp.daily_date != today:
        exp.daily_volume = 0.0
        exp.daily_date = today

    exp.open_total += copy_size
    exp.daily_volume += copy_size
    _save_state()
    logger.info(
        f"[tiered-risk] Recorded tier {tier} placement: ${copy_size:.2f} | "
        f"open: ${exp.open_total:.2f} / ${get_tier_config(tier).max_total_exposure:.2f}"
    )


def release_tiered_exposure(tier: StrategyTier, amount: float) -> None:
    """Release exposure when a position is closed or settled.

    Args:
        tier: Which tier to release from.
        amount: USD amount to release (positive).
    """
    exp = _tier_exposures.get(tier)
    if exp is None:
        logger.warn(f"[tiered-risk] Unknown tier for release: {tier}")
        return

    exp.open_total = max(0, exp.open_total - amount)
    _save_state()
    logger.info(
        f"[tiered-risk] Released tier {tier} exposure: ${amount:.2f} | "
        f"open now: ${exp.open_total:.2f}"
    )


def get_tiered_risk_status() -> dict:
    """Return current tiered risk state summary for Telegram status commands."""
    _load_state()
    result: dict = {}
    for tier_key in ("1a", "1b", "1c"):
        exp = _tier_exposures[tier_key]
        try:
            cfg = get_tier_config(tier_key)  # type: ignore[arg-type]
            max_exposure = cfg.max_total_exposure
            enabled = cfg.enabled
        except ValueError:
            max_exposure = 0
            enabled = False
        result[tier_key] = {
            "enabled": enabled,
            "open_total": round_cents(exp.open_total),
            "max_total_exposure": max_exposure,
            "daily_volume": round_cents(exp.daily_volume),
            "daily_date": exp.daily_date,
        }
    return result
