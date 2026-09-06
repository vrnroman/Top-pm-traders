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
import time
import tempfile
import threading
from dataclasses import dataclass, field

from src.config import CONFIG
from src.logger import logger
from src.utils import round_cents, today_utc


_STATE_FILE = os.path.join(CONFIG.data_dir, "daily-spend.json")
_lock = threading.Lock()


@dataclass
class _State:
    date: str = ""
    spent_usd: float = 0.0
    # Copies placed today per followed wallet (lowercased address -> count).
    # One wallet at 7.7 signals a day would otherwise take the whole daily
    # cap first-come, before the stronger, slower wallets fire at all.
    wallet_copies: dict = field(default_factory=dict)
    # The previous UTC day's map, kept at rollover so the 08:00 line can
    # report a COMPLETED window ("yesterday") rather than eight hours of today.
    wallet_copies_yesterday: dict = field(default_factory=dict)
    # Set when today's file could not be read: spending is refused until the
    # UTC rollover, the unreadable file is kept aside as evidence, and the
    # reason persists across restarts within the day.
    closed_reason: str = ""
    yesterday: str = ""


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
        wc = raw.get("wallet_copies") or {}
        _state.wallet_copies = {str(k).lower(): int(v) for k, v in wc.items()} if isinstance(wc, dict) else {}
        wy = raw.get("wallet_copies_yesterday") or {}
        _state.wallet_copies_yesterday = {str(k).lower(): int(v) for k, v in wy.items()} if isinstance(wy, dict) else {}
        _state.yesterday = str(raw.get("yesterday") or "")
        _state.closed_reason = str(raw.get("closed_reason") or "")
    except FileNotFoundError:
        _state.date = ""
        _state.spent_usd = 0.0
        _state.wallet_copies = {}
    except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as exc:
        # Unreadable is not "nothing spent". A bot that cannot say what it
        # spent today stops for today and says so; the file goes aside for
        # the RCA and a fresh one carries the reason until midnight UTC.
        aside = f"{_STATE_FILE}.corrupt-{int(time.time())}"
        try:
            os.replace(_STATE_FILE, aside)
        except OSError:
            aside = "(could not move it aside)"
        _state.date = today
        _state.spent_usd = 0.0
        _state.wallet_copies = {}
        _state.closed_reason = (f"today's spend file was unreadable ({exc.__class__.__name__}); "
                                f"kept aside as {os.path.basename(aside)}; no live copy "
                                f"until {today} rolls over (UTC)")
        _save_locked()
        logger.error(f"[daily-cap] {_state.closed_reason}")
        _announce_closed(_state.closed_reason)

    if _state.date != today:
        if _state.date:
            _state.yesterday = _state.date
            _state.wallet_copies_yesterday = dict(_state.wallet_copies)
        _state.date = today
        _state.spent_usd = 0.0
        _state.wallet_copies = {}
        _state.closed_reason = ""  # the rollover reopens the day
        _save_locked()


def _announce_closed(reason: str) -> None:
    """One BOT-class push naming the closed day. Never raises."""
    try:
        from src import telegram_bot as tb
        tb.send_message("⛔ <b>Spend guard closed for today.</b> " + tb._esc(reason)
                        if hasattr(tb, "_esc") else "⛔ Spend guard closed for today. " + reason,
                        kind=getattr(tb, "KIND_BOT", None))
    except Exception as exc:
        logger.warn(f"[daily-cap] could not announce the closed day: {exc}")


def _save_locked() -> None:
    _atomic_write(_STATE_FILE, {"date": _state.date, "spent_usd": _state.spent_usd,
                                "wallet_copies": dict(_state.wallet_copies),
                                "wallet_copies_yesterday": dict(_state.wallet_copies_yesterday),
                                "yesterday": _state.yesterday,
                                "closed_reason": _state.closed_reason})


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
        closed = _state.closed_reason
    if closed:
        return False, f"spend guard closed: {closed}"
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
    # Print the cap that can_spend() enforces (the governor's, when a
    # budget is stated), not the raw env number: the audit trail said
    # "$10 / $500" on a day the real ceiling was $32.
    from src.copy_trading import live_budget
    logger.info(
        f"[daily-cap] +${amount_usd:.2f} ({source}) | total today "
        f"${spent:.2f} / ${live_budget.daily_cap():.2f}"
    )


def reset_state() -> None:
    """Reset daily-spend tracking to zero (paired with a P&L reset). Does not
    write disk — the reset routine clears daily-spend.json separately."""
    global _state
    with _lock:
        _state = _State()


def closed_reason() -> str:
    """Why today is closed, or empty."""
    with _lock:
        _load_locked()
        return _state.closed_reason


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


# --------------------------------------------------------------------------- #
# Per-wallet daily copy cap
# --------------------------------------------------------------------------- #

def wallet_copies_today(wallet: str) -> int:
    with _lock:
        _load_locked()
        return int(_state.wallet_copies.get((wallet or "").lower(), 0))


def can_copy_wallet(wallet: str) -> tuple[bool, str]:
    """May one more BUY be copied from this wallet today?

    ``LIVE_MAX_PER_WALLET_DAY`` (0 = no cap). Bounds how much of the daily
    money one busy wallet can take, so the slower, stronger wallets still get
    their turn.
    """
    cap = int(getattr(CONFIG, "live_max_per_wallet_day", 0) or 0)
    if cap <= 0:
        return True, ""
    n = wallet_copies_today(wallet)
    if n >= cap:
        return False, (f"per-wallet daily cap: {n} of {cap} copies from "
                       f"{(wallet or '')[:10]} already today")
    return True, ""


def record_wallet_copy(wallet: str) -> None:
    with _lock:
        _load_locked()
        key = (wallet or "").lower()
        _state.wallet_copies[key] = int(_state.wallet_copies.get(key, 0)) + 1
        _save_locked()


def wallet_copies_window() -> tuple[str, dict]:
    """(UTC date, wallet -> copies) for the last COMPLETED day, or today's
    partial map labelled as such when no rollover has happened yet."""
    with _lock:
        _load_locked()
        if _state.yesterday:
            return (_state.yesterday, dict(_state.wallet_copies_yesterday))
        return (_state.date + " (partial)", dict(_state.wallet_copies))
