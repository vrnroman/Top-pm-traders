"""Disk-trajectory watch — edge-triggered WARNING before the disk fills.

The VM has one 20G disk shared by the data dir (caches, ledgers), the logs,
and the docker image. Both cache systems prune themselves (wcache since
2026-07-28, rescache since 2026-08-02), but the 2026-08-02 audit found the
disk at 84% with an unbounded cache growing ~100MB/day: nothing paged on the
*trajectory*, only the crash would have shown up. This samples free space once
per discovery sweep (6h), keeps a two-point slope in a tiny state file, and
trips when either the absolute floor or the projected days-to-full crosses a
bar — once per crossing (edge-triggered), so the watched channel isn't
trained to ignore a repeating warning.

Pure evaluation split from the side-effecting wrapper so it unit-tests
without disk or Telegram.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from typing import Optional

from src.logger import logger

FREE_GB_ENV = "DISK_ALERT_FREE_GB"
DAYS_ENV = "DISK_ALERT_DAYS"
_DEFAULT_FREE_GB = 2.5     # e2-small: image pulls need ~1.5G headroom at deploy
_DEFAULT_DAYS = 14.0       # alert two weeks before a full disk, not the day of


def evaluate(free_gb: float, prev: Optional[dict], *,
             floor_gb: float = _DEFAULT_FREE_GB,
             days_bar: float = _DEFAULT_DAYS,
             now: float) -> dict:
    """Decide whether the disk trajectory trips, from the current free space
    and the previous sample (``prev`` = state file contents or None).

    Returns ``tripped``, a human ``reason``, the shrink rate (GB/day, positive
    when the disk is filling), and projected days until ``floor_gb`` is hit
    (None when not shrinking or no slope yet). A single sample can't slope, so
    the first run trips only on the absolute floor.
    """
    shrink_gb_day = 0.0
    days_to_floor: Optional[float] = None
    if prev and prev.get("free_gb") is not None and prev.get("ts") is not None:
        dt_days = (now - float(prev["ts"])) / 86400.0
        # Below ~1h between samples a few-MB wobble extrapolates to a bogus
        # GB/day slope (a manual rerun right after a sweep, say) — too tight a
        # pair to say anything about trajectory (code-review L8).
        if dt_days >= 1.0 / 24.0:
            shrink_gb_day = (float(prev["free_gb"]) - free_gb) / dt_days
            if shrink_gb_day > 0 and free_gb > floor_gb:
                days_to_floor = (free_gb - floor_gb) / shrink_gb_day
    if free_gb <= floor_gb:
        return {"tripped": True,
                "reason": f"free {free_gb:.1f}G at/below floor {floor_gb:.1f}G",
                "shrink_gb_day": round(shrink_gb_day, 3),
                "days_to_floor": 0.0}
    if days_to_floor is not None and days_to_floor <= days_bar:
        return {"tripped": True,
                "reason": (f"free {free_gb:.1f}G shrinking "
                           f"{shrink_gb_day * 1000:.0f}MB/day → hits "
                           f"{floor_gb:.1f}G floor in ~{days_to_floor:.0f}d"),
                "shrink_gb_day": round(shrink_gb_day, 3),
                "days_to_floor": round(days_to_floor, 1)}
    return {"tripped": False, "reason": "",
            "shrink_gb_day": round(shrink_gb_day, 3),
            "days_to_floor": (round(days_to_floor, 1)
                              if days_to_floor is not None else None)}


def _load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def check(data_dir: str, *, now: Optional[float] = None,
          sender=None, state_name: str = "disk-watch.json") -> dict:
    """Sample free space, update the slope state, and on a NEW trip log a
    WARNING and Telegram the owner; on recovery log INFO. Returns the
    evaluation either way. Never raises — a watchdog must not break the sweep
    it rides. ``sender`` is the Telegram callable (injected for tests)."""
    now = now if now is not None else time.time()
    path = os.path.join(data_dir, state_name)
    try:
        free_gb = shutil.disk_usage(data_dir).free / (1024 ** 3)
        prev = _load_state(path)
        res = evaluate(
            free_gb, prev,
            floor_gb=float(os.environ.get(FREE_GB_ENV, _DEFAULT_FREE_GB)),
            days_bar=float(os.environ.get(DAYS_ENV, _DEFAULT_DAYS)),
            now=now)
        alerting = bool(prev.get("alerting"))
        if res["tripped"] and not alerting:
            logger.warning(f"[disk-watch] TRIPPED: {res['reason']}")
            if sender:
                sender(f"⚠️ <b>Disk trajectory</b> — {res['reason']} "
                       f"(caches pruned per sweep; investigate if this repeats)")
            res["alerted"] = True
        elif not res["tripped"] and alerting:
            logger.info(f"[disk-watch] recovered: free {free_gb:.1f}G")
            res["alerted"] = False
        else:
            res["alerted"] = False
            if res["tripped"]:
                logger.warning(f"[disk-watch] still tripped: {res['reason']}")
        _save_state(path, {"ts": now, "free_gb": round(free_gb, 3),
                           "alerting": res["tripped"]})
        return res
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[disk-watch] check failed: {e}")
        return {"tripped": False, "reason": "", "error": str(e)}
