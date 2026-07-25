"""The clean-era marker — one tiny JSON file that records when the fill model
was fixed (ROADMAP P0-1/P0-3, 2026-07-25), so every trust surface scopes
"clean post-fix data" the SAME way:

  * the A-vs-B race reporter (``main._ab_race_reporter_loop``) floors the race
    era at it, so the next verdict cannot include the artifact-era fills that
    produced the voided 2026-07-18 verdict;
  * ``/pnl`` and the daily snapshot scope their fill-health and wallet-
    persistence witnesses to it (the falsification bar in ROADMAP §7 is defined
    on post-fix data);
  * ``scripts/rebaseline_ledger.py --since`` reads the same number.

The file is ``ab_race_state.json`` — it already existed for verdict-once
semantics (``verdict_sent``/``verdict_ts``); ``era_floor_ts`` rides along
rather than growing a second state file. The reporter seeds it at first fire
if absent (deploy-time floor ≈ first-fire time), and the 2026-07-25 deploy
writes it explicitly so the floor matches the actual fix deploy.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional


def load(path: str) -> dict:
    """Best-effort read of the state file; {} on missing/corrupt (never raises —
    a trust surface that crashes on a missing marker would hide the numbers it
    exists to show)."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(path: str, state: dict) -> None:
    """Atomic tmp+replace write (the ledger convention: a crash mid-write must
    never leave a truncated state file). Raises OSError on failure — callers
    that fire-and-forget (the reporter) catch and log."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def era_floor_ts(path: str) -> Optional[float]:
    """The clean-era start (epoch seconds), or None if not recorded yet."""
    v = load(path).get("era_floor_ts")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def seed_era_floor(path: str, now: Optional[float] = None) -> float:
    """Record the era floor if absent and return it. Idempotent: an existing
    floor is NEVER moved (the clean era's start is a fact, not a setting)."""
    st = load(path)
    existing = st.get("era_floor_ts")
    if existing is not None:
        return float(existing)
    ts = now if now is not None else time.time()
    st["era_floor_ts"] = ts
    save(path, st)
    return ts
