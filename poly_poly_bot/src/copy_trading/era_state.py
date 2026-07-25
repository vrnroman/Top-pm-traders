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
    VALID floor is NEVER moved (the clean era's start is a fact, not a setting).

    When a new floor IS written (fresh file, wiped backup, or an unparsable
    hand-edit), any prior ``verdict_sent``/``verdict_ts`` is cleared: a new era
    is a new verdict window. Without this, the fallback path this function
    exists for (a restored pre-P0-3 backup carrying ``verdict_sent: true``)
    would scope the race to clean fills while the verdict memo stayed silenced
    forever, with nothing logged (2026-07-25 code-review catch). An unparsable
    floor is treated as absent — the daily reporter must not go down on a bad
    hand-edit of a file whose reader side tolerates the same input.
    """
    st = load(path)
    existing = st.get("era_floor_ts")
    if existing is not None:
        try:
            return float(existing)
        except (TypeError, ValueError):
            pass  # unparsable floor: fall through and re-seed
    ts = now if now is not None else time.time()
    st["era_floor_ts"] = ts
    st.pop("verdict_sent", None)
    st.pop("verdict_ts", None)
    save(path, st)
    return ts
