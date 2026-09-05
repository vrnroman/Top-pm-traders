"""Global per-UTC-day spend guardrail.

Single source of truth for "how much USD has the bot bet today". Used by
Strategy 1 (copy trading, both legacy and tiered) so a hard cap holds across
all copy tiers running side by side.

State persists to data/daily-spend.json with atomic writes and resets on
UTC date rollover.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass

from src.config import CONFIG
from src.logger import logger
from src.utils import round_cents, today_utc


_STATE_FILE = os.path.join(CONFIG.data_dir, "daily-spend.json")
_lock = threading.Lock()


@dataclass
class _State:
    date: str = ""
    spent_usd: float = 0.0


_state = _State()


def _atomic_write(path: str, data: dict) -> None:
    dir_path = os.path.dirname(path)
    os.makedirs(dir_path, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_locked() -> None:
    """Load state from disk and roll over if the UTC date changed.

    Caller must hold ``_lock``.
    """
    today = today_utc()
    try:
        with open(_STATE_FILE, "r") as f:
            raw = json.load(f)
        _state.date = raw.get("date", "")
        _state.spent_usd = float(raw.get("spent_usd", 0.0))
    except (FileNotFoundError, json.JSONDecodeError):
        _state.date = ""
        _state.spent_usd = 0.0

    if _state.date != today:
        _state.date = today
        _state.spent_usd = 0.0
        _save_locked()


def _save_locked() -> None:
    _atomic_write(_STATE_FILE, {"date": _state.date, "spent_usd": _state.spent_usd})


with _lock:
    _load_locked()


def can_spend(amount_usd: float) -> tuple[bool, str]:
    """Return ``(ok, reason)``. ``ok=False`` means the placement must be skipped.

    Checked against ``CONFIG.max_daily_volume_usd``.
    """
    if amount_usd <= 0:
        return True, ""
    # The env cap, lowered by the bankroll governor when a budget is stated.
    from src.copy_trading import live_budget
    cap = live_budget.daily_cap()
    with _lock:
        _load_locked()
        spent = _state.spent_usd
    if spent >= cap:
        return False, f"Daily spend cap reached: ${spent:.2f} >= ${cap:.2f}"
    if spent + amount_usd > cap:
        return False, (
            f"Daily spend cap would be exceeded: ${spent:.2f} + ${amount_usd:.2f} > ${cap:.2f}"
        )
    return True, ""


def record_spend(amount_usd: float, source: str) -> None:
    """Record a successful placement against the daily cap.

    ``source`` is a free-form tag (e.g. ``"copy:1a"``, ``"copy:1b"``) used in
    the log line so the audit trail is self-describing.
    """
    if amount_usd <= 0:
        return
    with _lock:
        _load_locked()
        _state.spent_usd = round_cents(_state.spent_usd + amount_usd)
        _save_locked()
        spent = _state.spent_usd
    logger.info(
        f"[daily-cap] +${amount_usd:.2f} ({source}) | total today "
        f"${spent:.2f} / ${CONFIG.max_daily_volume_usd:.2f}"
    )


def reset_state() -> None:
    """Reset daily-spend tracking to zero (paired with a P&L reset). Does not
    write disk — the reset routine clears daily-spend.json separately."""
    global _state
    with _lock:
        _state = _State()


def status() -> dict:
    """Snapshot for /status-style commands."""
    with _lock:
        _load_locked()
        return {
            "date": _state.date,
            "spent_usd": round_cents(_state.spent_usd),
            "cap_usd": CONFIG.max_daily_volume_usd,
            "remaining_usd": round_cents(
                max(0.0, CONFIG.max_daily_volume_usd - _state.spent_usd)
            ),
        }
