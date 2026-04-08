"""Legacy risk manager for copy-trading strategy.

Evaluates whether a detected trade should be copied based on daily volume
limits, per-market caps, price bounds, balance checks, and sizing rules.
Persists state to data/risk-state.json with atomic writes.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.config import CONFIG
from src.logger import logger
from src.models import CopyDecision, DetectedTrade
from src.utils import round_cents, today_utc


# ---------------------------------------------------------------------------
# Risk state
# ---------------------------------------------------------------------------

@dataclass
class RiskState:
    """Tracks daily volume, per-market spend, and last reset date."""

    daily_volume_usd: float = 0.0
    daily_volume_date: str = ""
    daily_spend_by_market: dict[str, float] = field(default_factory=dict)


_state = RiskState()
_STATE_FILE = os.path.join(CONFIG.data_dir, "risk-state.json")


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
    """Load risk state from disk, resetting if date has changed."""
    global _state
    try:
        with open(_STATE_FILE, "r") as f:
            raw = json.load(f)
        _state.daily_volume_usd = float(raw.get("daily_volume_usd", 0))
        _state.daily_volume_date = raw.get("daily_volume_date", "")
        _state.daily_spend_by_market = raw.get("daily_spend_by_market", {})
    except (FileNotFoundError, json.JSONDecodeError):
        _state = RiskState()

    # Reset if new day
    today = today_utc()
    if _state.daily_volume_date != today:
        _state.daily_volume_usd = 0.0
        _state.daily_volume_date = today
        _state.daily_spend_by_market = {}
        _save_state()


def _save_state() -> None:
    """Persist risk state to disk."""
    _atomic_write_json(_STATE_FILE, {
        "daily_volume_usd": _state.daily_volume_usd,
        "daily_volume_date": _state.daily_volume_date,
        "daily_spend_by_market": _state.daily_spend_by_market,
    })


# Load on import
_load_state()


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------

def _evaluate_trade_with_state(
    trade: DetectedTrade,
    state: RiskState,
    balance: Optional[float] = None,
) -> CopyDecision:
    """Evaluate a trade against risk rules. Returns a CopyDecision.

    Checks (in order):
    1. NaN guard on price/size
    2. Daily volume limit
    3. Trade age (MAX_TRADE_AGE_HOURS)
    4. Copy size calculation (PERCENTAGE or FIXED)
    5. Min/max order size bounds
    6. Daily volume headroom with 2% tolerance
    7. Price validation (0.10 - 0.95)
    8. Per-market cap (BUY only)
    9. Balance check (BUY only)
    10. Final min size check
    """

    # 1. NaN guard
    if math.isnan(trade.price) or math.isnan(trade.size):
        return CopyDecision(should_copy=False, copy_size=0, reason="NaN price or size")

    # 2. Daily volume limit
    if state.daily_volume_usd >= CONFIG.max_daily_volume_usd:
        return CopyDecision(
            should_copy=False,
            copy_size=0,
            reason=f"Daily volume limit reached: ${state.daily_volume_usd:.2f} >= ${CONFIG.max_daily_volume_usd:.2f}",
        )

    # 3. Trade age
    try:
        from datetime import datetime, timezone

        trade_ts = datetime.fromisoformat(trade.timestamp.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - trade_ts).total_seconds() / 3600
        if age_hours > CONFIG.max_trade_age_hours:
            return CopyDecision(
                should_copy=False,
                copy_size=0,
                reason=f"Trade too old: {age_hours:.1f}h > {CONFIG.max_trade_age_hours}h",
            )
    except (ValueError, TypeError):
        pass  # If we can't parse the timestamp, skip this check

    # 4. Copy size calculation
    if CONFIG.copy_strategy == "PERCENTAGE":
        raw_size = trade.size * CONFIG.copy_size / 100.0
    else:
        # FIXED mode
        raw_size = CONFIG.copy_size

    # 5. Min/max bounds
    copy_size = round_cents(raw_size)
    if copy_size < CONFIG.min_order_size_usd:
        copy_size = CONFIG.min_order_size_usd
    if copy_size > CONFIG.max_order_size_usd:
        copy_size = CONFIG.max_order_size_usd

    # 6. Daily volume headroom with 2% tolerance
    remaining_daily = CONFIG.max_daily_volume_usd - state.daily_volume_usd
    tolerance = CONFIG.max_daily_volume_usd * 0.02
    if copy_size > remaining_daily + tolerance:
        # Clip to remaining headroom
        copy_size = round_cents(remaining_daily)
        if copy_size < CONFIG.min_order_size_usd:
            return CopyDecision(
                should_copy=False,
                copy_size=0,
                reason=f"Remaining daily volume ${remaining_daily:.2f} < min order ${CONFIG.min_order_size_usd:.2f}",
            )

    # 7. Price validation (0.10 - 0.95)
    if trade.price < 0.10:
        return CopyDecision(
            should_copy=False,
            copy_size=0,
            reason=f"Price too low: {trade.price:.4f} < 0.10",
        )
    if trade.price > 0.95:
        return CopyDecision(
            should_copy=False,
            copy_size=0,
            reason=f"Price too high: {trade.price:.4f} > 0.95",
        )

    # 8. Per-market cap (BUY only)
    if trade.side == "BUY":
        market_key = trade.market or trade.condition_id
        current_market_spend = state.daily_spend_by_market.get(market_key, 0.0)
        if current_market_spend + copy_size > CONFIG.max_position_per_market_usd:
            remaining_market = CONFIG.max_position_per_market_usd - current_market_spend
            if remaining_market < CONFIG.min_order_size_usd:
                return CopyDecision(
                    should_copy=False,
                    copy_size=0,
                    reason=f"Per-market cap reached for {market_key}: ${current_market_spend:.2f}",
                )
            copy_size = round_cents(remaining_market)

    # 9. Balance check (BUY only)
    if trade.side == "BUY" and balance is not None:
        if copy_size > balance:
            copy_size = round_cents(balance)
            if copy_size < CONFIG.min_order_size_usd:
                return CopyDecision(
                    should_copy=False,
                    copy_size=0,
                    reason=f"Insufficient balance: ${balance:.2f}",
                )

    # 10. Final min check
    if copy_size < CONFIG.min_order_size_usd:
        return CopyDecision(
            should_copy=False,
            copy_size=0,
            reason=f"Final copy size ${copy_size:.2f} < min ${CONFIG.min_order_size_usd:.2f}",
        )

    return CopyDecision(should_copy=True, copy_size=round_cents(copy_size))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def evaluate_trade(
    trade: DetectedTrade,
    balance: Optional[float] = None,
) -> CopyDecision:
    """Evaluate a trade using global risk state."""
    _load_state()  # Ensure date reset
    return _evaluate_trade_with_state(trade, _state, balance)


def record_placement(trade: DetectedTrade, copy_size: float) -> None:
    """Record a placed trade against daily volume and per-market spend."""
    _state.daily_volume_usd += copy_size
    market_key = trade.market or trade.condition_id
    if trade.side == "BUY":
        prev = _state.daily_spend_by_market.get(market_key, 0.0)
        _state.daily_spend_by_market[market_key] = prev + copy_size
    _save_state()
    logger.info(
        f"[risk] Recorded placement: ${copy_size:.2f} | "
        f"daily total: ${_state.daily_volume_usd:.2f} / ${CONFIG.max_daily_volume_usd:.2f}"
    )


def adjust_placement(trade: DetectedTrade, delta_usd: float) -> None:
    """Adjust recorded placement (e.g. partial fill correction).

    Positive delta = increase recorded volume.
    Negative delta = decrease (refund).
    """
    _state.daily_volume_usd = max(0, _state.daily_volume_usd + delta_usd)
    if trade.side == "BUY":
        market_key = trade.market or trade.condition_id
        prev = _state.daily_spend_by_market.get(market_key, 0.0)
        _state.daily_spend_by_market[market_key] = max(0, prev + delta_usd)
    _save_state()
    logger.info(f"[risk] Adjusted placement by ${delta_usd:+.2f} | daily total: ${_state.daily_volume_usd:.2f}")


def get_risk_status() -> dict:
    """Return current risk state summary for Telegram status commands."""
    _load_state()
    return {
        "daily_volume_usd": round_cents(_state.daily_volume_usd),
        "max_daily_volume_usd": CONFIG.max_daily_volume_usd,
        "daily_volume_date": _state.daily_volume_date,
        "markets_tracked": len(_state.daily_spend_by_market),
        "daily_spend_by_market": {
            k: round_cents(v) for k, v in _state.daily_spend_by_market.items()
        },
    }
