"""The thing that watches real money while nobody is looking.

Everything the bot does with real capital has a failure mode that has never
run: the auto-redeemer has never fired, `recover_pending_orders` has never
reconciled a real order, the proxy wallet has never been drained mid-session,
and the VM has previously gone network-dead for hours (with no positions open,
that cost nothing; with positions open it costs whatever the market does).

Two halves, deliberately separated:

* **Detectors** always run, in preview and live alike, and only ever log or
  alert. They are how the failure modes get exercised before real money exists.
* **Actions** (retry, cancel, self-disarm) are gated on `live_mode.is_armed()`,
  so today they are inert, and they become live the moment the owner arms
  without a second deploy or a second code path to trust.

Alerting reuses `disk_watch`'s edge-triggered shape: one message when a
condition becomes true, silence while it stays true, one when it clears. A
watcher that repeats itself every cycle is one the owner learns to ignore,
which is the same as not having it.

**Self-disarm is the one action that is always allowed**, even unarmed, because
it can only ever move toward preview. Anything that reduces live exposure
should be reachable from inside the loop; anything that increases it needs the
owner's key.
"""

from __future__ import annotations

import json
import os
import time
from typing import Callable, Optional

from src.config import CONFIG
from src.copy_trading import live_mode
from src.logger import logger

STATE_FILE = "live_guard.json"

# A resting order older than this is not "working", it is stuck.
STUCK_ORDER_S = 15 * 60.0

# A resolved position still unredeemed this long after resolution means the
# redeemer is not doing its job, and unredeemed positions are dead capital.
UNREDEEMED_S = 6 * 3600.0

# Balance drift beyond this fraction between what we think we hold and what the
# chain says means our accounting is wrong. Trading on wrong accounting is how
# a small bug becomes a large one.
BALANCE_DRIFT_FRAC = 0.10

# Consecutive cycle failures before the session stops trusting itself.
CRASH_LOOP_N = 5


def _state_path() -> str:
    return os.path.join(CONFIG.data_dir, STATE_FILE)


def _read_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_state_path()), exist_ok=True)
        tmp = _state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, _state_path())
    except OSError as exc:
        logger.warn(f"[guard] state write failed: {exc}")


def edge(key: str, active: bool, message: str, send: Optional[Callable] = None,
         state: Optional[dict] = None) -> bool:
    """Alert only on a change of state. Returns True when it alerted.

    Same contract as disk_watch: becoming true speaks once, staying true is
    silent, clearing speaks once.
    """
    st = _read_state() if state is None else state
    was = bool(st.get(key))
    if was == bool(active):
        return False
    st[key] = bool(active)
    if state is None:
        _write_state(st)
    if send is not None:
        try:
            send(message if active else f"✅ resolved: {message}")
        except Exception as exc:
            logger.warn(f"[guard] alert send failed: {exc}")
    logger.warn(f"[guard] {key}: {'ENTERED' if active else 'CLEARED'}: {message}")
    return True


# --------------------------------------------------------------------------- #
# Detectors. Pure: they take a state snapshot and return findings.
# --------------------------------------------------------------------------- #

def find_stuck_orders(pending: list, now: Optional[float] = None,
                      max_age_s: float = STUCK_ORDER_S) -> list:
    """Orders that have been resting far too long to still be working."""
    now = time.time() if now is None else now
    out = []
    for o in pending or []:
        placed = getattr(o, "placed_at", None)
        if placed is None and isinstance(o, dict):
            placed = o.get("placed_at")
        try:
            placed = float(placed or 0)
        except (TypeError, ValueError):
            continue
        if placed > 0 and (now - placed) > max_age_s:
            out.append(o)
    return out


def find_unredeemed(positions: list, now: Optional[float] = None,
                    max_age_s: float = UNREDEEMED_S) -> list:
    """Resolved positions that should have been redeemed and were not."""
    now = time.time() if now is None else now
    out = []
    for p in positions or []:
        if not getattr(p, "closed", False):
            continue
        if getattr(p, "redeemed", False):
            continue
        closed_ts = 0.0
        try:
            closed_ts = float(getattr(p, "closed_ts", 0) or 0)
        except (TypeError, ValueError):
            pass
        if closed_ts > 0 and (now - closed_ts) > max_age_s:
            out.append(p)
    return out


def balance_drift(expected_usd: Optional[float],
                  actual_usd: Optional[float]) -> Optional[float]:
    """Fractional gap between our accounting and the chain, or None."""
    if expected_usd is None or actual_usd is None:
        return None
    base = max(abs(expected_usd), 1.0)
    return abs(expected_usd - actual_usd) / base


def should_self_disarm(*, crash_streak: int = 0,
                       drift: Optional[float] = None,
                       unredeemed: int = 0,
                       feed_stale_s: Optional[float] = None) -> tuple[bool, str]:
    """Has the session lost enough trust in its own state to stop trading?

    Every trigger is a statement about OUR reliability, not about the market.
    A losing session is not a reason to disarm; a session that cannot tell you
    what it holds is.
    """
    if crash_streak >= CRASH_LOOP_N:
        return (True, f"{crash_streak} consecutive cycle failures: the session "
                      f"cannot complete a pass, so it cannot know its own state")
    if drift is not None and drift > BALANCE_DRIFT_FRAC:
        return (True, f"balance accounting is off by {drift * 100:.0f}%: "
                      f"trading on wrong accounting compounds the error")
    if unredeemed >= 3:
        return (True, f"{unredeemed} resolved positions failed to redeem: "
                      f"capital is stuck and the redeemer is not working")
    if feed_stale_s is not None and feed_stale_s > 900:
        return (True, f"no trade data for {feed_stale_s / 60:.0f} minutes: "
                      f"copying blind is worse than not copying")
    return (False, "")


# --------------------------------------------------------------------------- #
# The pass
# --------------------------------------------------------------------------- #

def run_once(*, pending_orders: Optional[list] = None,
             positions: Optional[list] = None,
             expected_usd: Optional[float] = None,
             actual_usd: Optional[float] = None,
             crash_streak: int = 0,
             feed_stale_s: Optional[float] = None,
             send: Optional[Callable] = None,
             cancel_order: Optional[Callable] = None,
             now: Optional[float] = None) -> dict:
    """One guard pass. Detects always; acts only when armed.

    Returns a findings dict so the caller (and the tests) can assert on it
    rather than on log text.
    """
    now = time.time() if now is None else now
    armed = live_mode.is_armed()
    st = _read_state()

    stuck = find_stuck_orders(pending_orders or [], now=now)
    unred = find_unredeemed(positions or [], now=now)
    drift = balance_drift(expected_usd, actual_usd)

    edge("stuck_orders", bool(stuck),
         f"⏳ {len(stuck)} order(s) resting over "
         f"{STUCK_ORDER_S / 60:.0f} minutes", send, st)
    edge("unredeemed", bool(unred),
         f"💤 {len(unred)} resolved position(s) not redeemed after "
         f"{UNREDEEMED_S / 3600:.0f}h", send, st)
    edge("balance_drift", bool(drift is not None and drift > BALANCE_DRIFT_FRAC),
         f"⚠️ balance accounting off by "
         f"{(drift or 0) * 100:.0f}%", send, st)

    acted: list = []
    if armed and stuck and cancel_order is not None:
        for o in stuck:
            oid = getattr(o, "order_id", None) or (
                o.get("order_id") if isinstance(o, dict) else None)
            if not oid:
                continue
            try:
                cancel_order(oid)
                acted.append(oid)
                logger.warn(f"[guard] cancelled stuck order {oid}")
            except Exception as exc:
                logger.error(f"[guard] could not cancel {oid}: {exc}")

    disarm, why = should_self_disarm(
        crash_streak=crash_streak, drift=drift, unredeemed=len(unred),
        feed_stale_s=feed_stale_s)
    disarmed = False
    if disarm:
        # Always allowed, armed or not: this can only move toward preview.
        if armed:
            live_mode.disarm(by="live-guard")
            disarmed = True
        edge("self_disarm", True,
             f"🛑 <b>Self-disarmed</b>, back to paper.\n{why}", send, st)
    else:
        edge("self_disarm", False, "self-disarm condition cleared", send, st)

    _write_state(st)
    return {
        "armed": armed,
        "stuck_orders": len(stuck),
        "unredeemed": len(unred),
        "balance_drift": drift,
        "cancelled": acted,
        "self_disarmed": disarmed,
        "disarm_reason": why,
    }
