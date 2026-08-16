"""The live/preview interlock — the one door to real money.

Before this module, flipping to real trading meant editing `PREVIEW_MODE` in
the VM's `.env` and restarting the container: `CONFIG.preview_mode` is read
once at import and frozen. That is slow at the exact moment speed matters,
and it is also unaudited — nothing records who flipped it, when, or why.

So the flip becomes a runtime decision with a **two-key interlock**, and both
keys are needed to place a real order:

  1. ``LIVE_ARM_ENABLED=true`` in the environment — the owner's key. It is
     false by default, it can only be set on the VM, and no chat message,
     script, or agent can set it.
  2. ``/live CONFIRM`` in Telegram — the moment's key. It writes the arm
     record below.

Either key alone leaves the bot in preview. This is deliberate: key 1 cannot
be turned by anything running inside the process, and key 2 cannot be turned
by anything that is not the owner's own Telegram chat. The arm is also
**disarmed by default on every restart** unless it was explicitly persisted,
so a crash-loop can never wake up trading.

`is_preview()` is the single question the trading paths ask. It fails CLOSED:
any error reading the arm state means preview.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

from src.config import CONFIG
from src.logger import logger

_ARM_FILE = "live_arm.json"


def _path() -> str:
    return os.path.join(CONFIG.data_dir, _ARM_FILE)


def read_arm() -> dict:
    """The persisted arm record, or an empty (disarmed) one."""
    try:
        with open(_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def is_armed() -> bool:
    """True only when BOTH interlock keys are turned.

    ``armed`` must be a real boolean True. A hand-edited
    ``{"armed": "false"}`` is a truthy *string* and would otherwise arm the
    bot — the exact inversion you least want on the money path.
    """
    if not CONFIG.live_arm_enabled:
        return False
    return read_arm().get("armed") is True


def is_preview() -> bool:
    """The one question the trading paths ask: are we still on paper?

    Preview unless the process was started with ``PREVIEW_MODE=false`` AND the
    runtime arm is set. Fails closed — an unreadable arm file means preview.
    """
    try:
        if CONFIG.preview_mode:
            return True
        return not is_armed()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warn(f"[live] arm check failed, staying in PREVIEW: {exc}")
        return True


def arm(reason: str = "", by: str = "telegram") -> tuple[bool, str]:
    """Turn the moment's key. Returns (ok, human-readable detail).

    Refuses when the owner's env key is not set — which is the state this
    ships in, so the command is fully wired and fully inert until the owner
    decides otherwise.
    """
    if not CONFIG.live_arm_enabled:
        return (False, "LIVE_ARM_ENABLED is not set on the VM — the owner's "
                       "key is required before this command can do anything.")
    if CONFIG.preview_mode:
        return (False, "PREVIEW_MODE=true at the process level; arming alone "
                       "will not place orders. Both keys are required.")
    rec = {"armed": True, "ts": time.time(), "by": by, "reason": reason}
    try:
        os.makedirs(os.path.dirname(_path()), exist_ok=True)
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        os.replace(tmp, _path())
    except OSError as exc:
        return (False, f"could not persist the arm record: {exc}")
    logger.warn(f"[live] ARMED for real orders by {by}: {reason or 'no reason given'}")
    return (True, "armed")


def disarm(by: str = "telegram") -> bool:
    """Turn the moment's key back off. Always safe, always allowed."""
    rec = {"armed": False, "ts": time.time(), "by": by}
    try:
        os.makedirs(os.path.dirname(_path()), exist_ok=True)
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        os.replace(tmp, _path())
    except OSError as exc:
        logger.warn(f"[live] disarm write failed: {exc}")
        return False
    logger.warn(f"[live] DISARMED by {by} — back to paper")
    return True


def status() -> dict:
    """Everything the pre-flight panel needs to render the interlock."""
    rec = read_arm()
    return {
        "env_key": bool(CONFIG.live_arm_enabled),      # LIVE_ARM_ENABLED
        "process_live": not bool(CONFIG.preview_mode),  # PREVIEW_MODE=false
        # Strict, matching is_armed(). A truthy-but-not-True value (a
        # hand-edited "false" string) must not make the panel report ARMED
        # while the gate correctly refuses — failing safe on money but lying
        # to the reader is still lying to the reader.
        "runtime_armed": rec.get("armed") is True,
        "armed_ts": rec.get("ts"),
        "armed_by": rec.get("by"),
        "reason": rec.get("reason"),
        "preview": is_preview(),
    }


def blocking_reasons() -> list[str]:
    """Why a real order cannot be placed right now, in plain words."""
    out: list[str] = []
    if CONFIG.preview_mode:
        out.append("PREVIEW_MODE=true (process-level; needs a redeploy to change)")
    if not CONFIG.live_arm_enabled:
        out.append("LIVE_ARM_ENABLED not set on the VM (owner's key)")
    if read_arm().get("armed") is not True:
        out.append("runtime not armed (/live CONFIRM)")
    return out


def approvals_warning() -> Optional[str]:
    """The on-chain side-effect that fires on the FIRST live boot.

    Worth surfacing at the flip because it is not a read: `runner.py` calls
    `check_and_set_approvals()` when preview is off at startup, which sends
    ERC-20/1155 approval transactions from the wallet. It costs gas and is the
    genuinely irreversible step, and it happens before any order is placed.
    """
    return ("PREVIEW_MODE=false starts real on-chain activity at BOOT, before "
            "any order and regardless of the runtime arm: token approvals "
            "(check_and_set_approvals), the auto-redeemer, and inventory "
            "reconcile against the real proxy wallet. Those are real "
            "transactions and they cost gas.")
