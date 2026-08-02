"""Verdict-decision overlay (s-log7q, phase-2 P1).

The 08-22 §7 memo posts a mechanical recommendation; the owner answers with a
one-word Telegram command (/verdict hold|retire|recalibrate + confirm). The
confirmed answer lands HERE — a small JSON overlay on the data volume that
readers prefer over env vars.

Why an overlay and not a .env edit: deploys re-materialize .env from the
ENV_FILE secret every push, so a decision written to .env silently resurrects
the old config on the next deploy (the exact rot class the 2026-08-02
verdict-clock fix existed to kill). The data volume survives deploys; the
overlay is the only write the bot makes that outlives them.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

OVERLAY_NAME = "verdict-overlay.json"
DRAFT_NAME = "verdict-draft.json"
DRAFT_TTL_S = 3600.0        # a previewed decision dies unconfirmed after 1h

# The actions /verdict accepts and the config patch each drafts. Values are
# strings to mirror env semantics; readers coerce.
ACTION_PATCHES = {
    "hold": {},                     # accept the recommendation, change nothing
    "retire": {"COPY_PAPER_ENABLED": "false",
               "WALLET_DISCOVERY_ENABLED": "false"},
    "recalibrate": {"AB_RACE_VERDICT_DAYS": "30"},
}

# Which knobs take effect where, rendered in the preview so nothing is
# surprising: reporter knobs ride the next daily fire, boot knobs the next
# container start.
EFFECT_TIMING = {
    "AB_RACE_VERDICT_DAYS": "next daily report fire",
    "COPY_PAPER_ENABLED": "next container restart",
    "WALLET_DISCOVERY_ENABLED": "next container restart",
}


def overlay_path(data_dir: str) -> str:
    return os.path.join(data_dir, OVERLAY_NAME)


def draft_path(data_dir: str) -> str:
    return os.path.join(data_dir, DRAFT_NAME)


def load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def effective(data_dir: str, key: str, default):
    """Overlay-preferred config read: the confirmed verdict decision wins over
    env whenever it names this knob."""
    return load(overlay_path(data_dir)).get(key, default)


def effective_bool(data_dir: str, key: str, default: bool) -> bool:
    v = str(effective(data_dir, key, str(default))).strip().lower()
    return v not in ("false", "0", "no", "off")


def new_draft(action: str, *, now: Optional[float] = None) -> dict:
    """The previewed decision: action, its patch, and an expiry."""
    now = now if now is not None else time.time()
    return {"action": action, "patch": dict(ACTION_PATCHES[action]),
            "ts": now, "expires": now + DRAFT_TTL_S}


def draft_valid(draft: dict, *, now: Optional[float] = None) -> bool:
    now = now if now is not None else time.time()
    return bool(draft.get("action")) and now <= float(draft.get("expires") or 0.0)


def confirm(data_dir: str, *, now: Optional[float] = None) -> Optional[dict]:
    """Apply a valid draft: merge its patch into the overlay, record the
    decision, and drop the draft. Returns the applied patch, or None when the
    draft is missing/expired."""
    now = now if now is not None else time.time()
    draft = load(draft_path(data_dir))
    if not draft_valid(draft, now=now):
        return None
    ov = load(overlay_path(data_dir))
    patch = dict(draft.get("patch") or {})
    ov.update(patch)
    ov["_decision"] = {"action": draft["action"], "ts": now}
    save(overlay_path(data_dir), ov)
    try:
        os.remove(draft_path(data_dir))
    except OSError:
        pass
    return patch
