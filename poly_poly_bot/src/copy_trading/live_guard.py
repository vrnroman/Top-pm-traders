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

# Consecutive cycle failures before the session stops trusting itself.
CRASH_LOOP_N = 5

# A resolved position worth less than this is not "stuck capital". The funder
# on this box carries dozens of resolved losers worth exactly zero; counting
# them as failed redemptions would disarm every live session forever over
# tickets there is nothing to collect on, and a gate that fires on a false
# positive is worse than the bug it guards.
STUCK_VALUE_USD = 1.0


def _field(o, name, default=None):
    """Read a field from an attribute object OR a dict.

    `inventory.Position` is a plain `dict` alias, so reading production state
    with `getattr(p, "closed", False)` returned False for every real position,
    forever, while the drills stayed green because they hand in attribute
    objects production never produces. Anything reading real state must accept
    both shapes.
    """
    if isinstance(o, dict):
        return o.get(name, default)
    return getattr(o, name, default)


def _num_field(o, name):
    """A numeric field from a dict or attribute object, or None if absent."""
    v = _field(o, name, None)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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
        placed = _field(o, "placed_at")
        try:
            placed = float(placed or 0)
        except (TypeError, ValueError):
            continue
        if placed <= 0:
            continue
        # `trade_executor` stamps `placed_at = time.time() * 1000`, i.e. epoch
        # MILLISECONDS, while this compared it against epoch seconds. So a
        # real resting order read as ~57 years in the future and the detector
        # (and the armed cancel) were dead on production state, green only
        # because the drill built its own seconds-shaped object. Normalise
        # rather than assume either unit.
        if placed > 1e11:
            placed /= 1000.0
        if (now - placed) > max_age_s:
            out.append(o)
    return out


def find_unredeemed(positions: list, now: Optional[float] = None,
                    max_age_s: float = UNREDEEMED_S) -> list:
    """Resolved positions that should have been redeemed and were not."""
    now = time.time() if now is None else now
    out = []
    for p in positions or []:
        if not _field(p, "closed", False):
            continue
        if _field(p, "redeemed", False):
            continue
        closed_ts = 0.0
        try:
            closed_ts = float(_field(p, "closed_ts", 0) or 0)
        except (TypeError, ValueError):
            pass
        if closed_ts > 0 and (now - closed_ts) > max_age_s:
            out.append(p)
    return out


def _is_neg_risk(p) -> bool:
    return bool(_field(p, "negRisk", False))


def redeemable_positions(proxy_wallet: str) -> list:
    """Positions the chain says are redeemable RIGHT NOW.

    The inventory store holds OPEN positions and knows nothing about
    resolution, so asking it about unredeemed capital was asking the wrong
    source. This asks the same source the redeemer itself uses. Read-only;
    returns [] rather than raising, since a guard that dies on a bad read
    is worse than one that reports nothing.
    """
    if not proxy_wallet:
        return []
    try:
        import asyncio

        from src.copy_trading.auto_redeemer import _fetch_redeemable_positions
        rows = list(asyncio.run(_fetch_redeemable_positions(proxy_wallet)) or [])
        # The redeemer itself SKIPS neg-risk positions (they use a different
        # redemption mechanism), so they sit in this list forever. Counting
        # them as "failed to redeem" meant three of them would self-disarm a
        # live session permanently, for something the redeemer was never going
        # to do. Excluded here, mirroring the redeemer's own rule.
        kept = [p for p in rows if not _is_neg_risk(p)]
        skipped = len(rows) - len(kept)
        if skipped:
            logger.info(f"[guard] {skipped} neg-risk position(s) excluded: the "
                        f"redeemer skips them by design")
        return kept
    except Exception as exc:
        # None, not []. An empty list means "nothing is stuck"; a failed read
        # means we do not know, and reporting the second as the first sends a
        # "resolved" message for a condition nobody confirmed cleared.
        logger.warn(f"[guard] redeemable lookup failed: {exc}")
        return None


# NOTE: there was a fourth trigger here, balance drift. It is deleted rather
# than left in place. Nothing in this codebase produces an EXPECTED balance to
# compare the chain against, so `main` never supplied one and the trigger could
# not fire on any real pass, while the drill kept it green by handing in
# 1000.0/1000.0. A guard that advertises four triggers and has three is worse
# than one that advertises three: the extra name is a false green on the
# safety net itself. Reinstate it when there is a real expected-balance
# source, with a test that asserts the LOOP supplies it.


def should_self_disarm(*, crash_streak: int = 0,
                       unredeemed: int = 0,
                       feed_stale_s: Optional[float] = None,
                       equity_usd: Optional[float] = None,
                       floor_usd: Optional[float] = None) -> tuple[bool, str]:
    """Has the session lost enough trust in its own state to stop trading?

    Three triggers are statements about OUR reliability, not about the
    market. The fourth is different in kind: a losing session is not a trust
    failure, but a bankroll below the floor the owner set is a stop of a
    different kind, and it is his to override (a fresh /live CONFIRM after
    the trip counts as that override; see ``run_once``).
    """
    if crash_streak >= CRASH_LOOP_N:
        return (True, f"{crash_streak} consecutive cycle failures: the session "
                      f"cannot complete a pass, so it cannot know its own state")
    if unredeemed >= 3:
        return (True, f"{unredeemed} resolved positions failed to redeem: "
                      f"capital is stuck and the redeemer is not working")
    if feed_stale_s is not None and feed_stale_s > 900:
        return (True, f"no trade data for {feed_stale_s / 60:.0f} minutes: "
                      f"copying blind is worse than not copying")
    if (equity_usd is not None and floor_usd is not None
            and float(equity_usd) < float(floor_usd)):
        return (True, f"bankroll ${float(equity_usd):,.0f} is under the floor "
                      f"${float(floor_usd):,.0f} you set: a losing session is not "
                      f"a trust failure, but a bankroll below the floor is a stop "
                      f"of a different kind. /live CONFIRM overrides it")
    return (False, "")


FLOOR_REASON_PREFIX = "bankroll $"
FLOOR_DISARM_BY = "live-guard:floor"


def is_floor_reason(why: str) -> bool:
    return bool(why) and why.startswith(FLOOR_REASON_PREFIX)


def _floor_overridden() -> bool:
    """Did the owner re-arm right after a floor trip? Then the floor is his
    to ignore for THIS arm session only. The fact lives on the arm record
    (``live_mode.arm`` sets ``floor_override`` when the disarm it follows was
    the floor's), so any later disarm clears it and the floor is back."""
    try:
        arm = live_mode.read_arm()
        return arm.get("armed") is True and arm.get("floor_override") is True
    except Exception:
        return False


def active_block() -> Optional[str]:
    """A self-disarm condition the guard currently holds, other than the
    floor, in its own words. Arming under it would be pulled within one pass,
    silently, because the edge already fired. None when clear."""
    st = _read_state()
    if not st.get("self_disarm"):
        return None
    why = str(st.get("self_disarm_reason") or "")
    if is_floor_reason(why):
        return None  # the floor is the owner's to override by arming
    return why or "a self-disarm condition is active"


# --------------------------------------------------------------------------- #
# The pass
# --------------------------------------------------------------------------- #

def run_once(*, pending_orders: Optional[list] = None,
             positions: Optional[list] = None,
             redeemable: Optional[list] = None,
             crash_streak: int = 0,
             feed_stale_s: Optional[float] = None,
             equity_usd: Optional[float] = None,
             floor_usd: Optional[float] = None,
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
    # Two sources, deliberately. `positions` are ledger-shaped rows carrying
    # closed/redeemed/closed_ts; `redeemable` comes from the chain, where
    # being in the list AT ALL means the capital is sitting unredeemed, so
    # there is no age field to test and none is invented.
    # None means the redeemable read FAILED, which is not the same as zero.
    # Skip the edge entirely in that case rather than announcing a clearance.
    redeem_unknown = redeemable is None
    # Only positions actually worth collecting count as a failed redemption.
    # Rows with no value figure at all are counted, because unknown is not
    # zero.
    # NOT `stuck`: that name already holds the stuck ORDERS a few lines above,
    # and shadowing it silently zeroed the stuck-order detector.
    stuck_capital = [p for p in (redeemable or [])
                     if _num_field(p, "currentValue") is None
                     or _num_field(p, "currentValue") >= STUCK_VALUE_USD]
    dust = len(redeemable or []) - len(stuck_capital)
    if dust:
        logger.info(f"[guard] {dust} resolved position(s) worth under "
                    f"${STUCK_VALUE_USD:.0f} excluded from the stuck-redemption "
                    f"count: there is nothing to collect on them")
    unred = find_unredeemed(positions or [], now=now) + stuck_capital
    edge("stuck_orders", bool(stuck),
         f"⏳ {len(stuck)} order(s) resting over "
         f"{STUCK_ORDER_S / 60:.0f} minutes", send, st)
    if not redeem_unknown:
        edge("unredeemed", bool(unred),
             f"💤 {len(unred)} resolved position(s) not redeemed after "
             f"{UNREDEEMED_S / 3600:.0f}h", send, st)
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

    # The floor: silenced for THIS arm session once the owner has re-armed
    # right after a trip. The number still renders in the daily line; only
    # the automatic stop steps aside.
    overridden = _floor_overridden()
    eq_for_trigger = None if overridden else equity_usd
    disarm, why = should_self_disarm(
        crash_streak=crash_streak,
        unredeemed=0 if redeem_unknown else len(unred),
        feed_stale_s=feed_stale_s,
        equity_usd=eq_for_trigger, floor_usd=floor_usd)
    disarmed = False
    # The self-disarm edge is DETECTED and logged whether or not anything is
    # armed, which is how it gets exercised before there is money on it. The
    # owner is messaged when the guard ACTS (every time it pulls the arm, not
    # only on the edge: a condition that entered while unarmed still has to
    # announce the disarm it causes after he arms), and told once when a
    # condition he was told about clears. A "self-disarmed, back to paper"
    # for a session that was never off paper is noise: 255 went out in 20 days.
    if disarm:
        if armed:
            # Always allowed: this can only move toward preview. The floor
            # disarms under its own name so the next arm can carry the override.
            by = FLOOR_DISARM_BY if is_floor_reason(why) else "live-guard"
            disarmed = bool(live_mode.disarm(by=by))
            if not disarmed:
                # `disarm` already hard-disarms this process on a write
                # failure. Say it out loud too: the durable record still says
                # armed, so the NEXT process to boot would trade.
                live_mode.hard_disarm("the guard could not persist a disarm")
                logger.error("[guard] disarm requested but the arm record could "
                             "not be written; this process is hard-disarmed, but "
                             "the persisted arm still reads ARMED and a restart "
                             "would trade. Fix the disk, then /live DISARM.")
                if send is not None:
                    try:
                        send("🚨 <b>Could not persist a disarm.</b> This process "
                             "has stopped trading, but the saved arm still reads "
                             "ARMED, so a restart would trade. Check disk space "
                             "on the VM, then send <code>/live DISARM</code>.")
                    except Exception:
                        pass
            if send is not None:
                try:
                    send(f"🛑 <b>Self-disarmed</b>, back to paper.\n{why}")
                    st["self_disarm_messaged"] = True
                except Exception as exc:
                    logger.warn(f"[guard] alert send failed: {exc}")
        st["self_disarm_reason"] = why
        edge("self_disarm", True, f"🛑 Self-disarmed, back to paper. {why}", None, st)
    else:
        cleared = edge("self_disarm", False, "self-disarm condition cleared", None, st)
        if cleared:
            if st.get("self_disarm_messaged") and send is not None:
                try:
                    send("✅ resolved: self-disarm condition cleared")
                except Exception as exc:
                    logger.warn(f"[guard] alert send failed: {exc}")
            st["self_disarm_messaged"] = False
            st.pop("self_disarm_reason", None)

    _write_state(st)
    return {
        "armed": armed,
        "stuck_orders": len(stuck),
        "unredeemed": len(unred),
        "cancelled": acted,
        "self_disarmed": disarmed,
        "disarm_reason": why,
        "floor_overridden": overridden,
    }
