"""Go-live readiness watch — pings the owner the moment a PROMOTED wallet
crosses the full ``/golive`` bar, instead of the owner polling ``/golive`` by
hand (owner ask, 2026-07-17: 0x4861 sat 4 settles short of READY and nothing
would have said so when it got there).

Edge-triggered with persisted state (``data/golive_watch.json``):

  * not-ready -> READY   → one 🟢 alert naming the wallet + the /golive command
  * READY -> not-ready   → one ⏸ alert (so real money is never flipped on a
    readiness that has already decayed)
  * no repeats while the state holds; a wallet that drops back and re-crosses
    alerts again (re-armed). First sight of a not-ready wallet is silent.

A transition is recorded ONLY after its alert actually sent (``send`` returned
truthy), so a Telegram hiccup retries next cycle instead of losing the signal.
Pure logic with injectable IO — the live wrapper in ``main.py`` supplies the
ledger, the promoted set, thresholds from CONFIG, and the Telegram sender.
"""

from __future__ import annotations

import html
import json
import os
import time
from typing import Callable, Iterable, Optional

from src.copy_trading import promotion_gate
from src.copy_trading.copy_paper import is_dust_fill
from src.logger import logger


def _read_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        # drop non-dict VALUES too — a hand-edited/partially-written entry
        # would otherwise AttributeError every cycle and (since state is only
        # rewritten on change) never self-heal (2026-07-17 review catch).
        return {k: v for k, v in data.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        return {}


def _write_state(path: str, state: dict) -> None:
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except OSError as e:  # the watch must never break the copy cycle
        logger.warning(f"[GOLIVE-WATCH] state save failed: {e}")


def evaluate_promoted(
    positions: Iterable,
    promoted: Iterable[str],
    *,
    now: float,
    min_settled: int,
    max_idle_days: float,
    min_roi: float,
    floor_kwargs: dict,
    era_floor: Optional[float] = None,
    min_ideal_roi: Optional[float] = None,
    min_split_half_corr: Optional[float] = None,
) -> dict:
    """wallet(lower) -> (ready, stats, checks) for every promoted wallet,
    computed exactly like the manual ``/golive`` command (same gate, same
    dust-fill exclusion).

    When the honest-metrics floors are on (``min_ideal_roi`` /
    ``min_split_half_corr``, owner ruling 2026-07-25), each wallet's at-their-
    price ROI and the book's split-half persistence are evaluated on clean-era
    rows (opened >= ``era_floor``); with no floor recorded there IS no clean
    era, so both checks get None and fail closed."""
    keys = {(w or "").lower() for w in promoted if w}
    settled: dict[str, list] = {k: [] for k in keys}
    last_ts: dict[str, float] = {k: 0.0 for k in keys}
    positions = list(positions)
    for p in positions:
        k = (getattr(p, "target", "") or "").lower()
        if k not in keys:
            continue
        last_ts[k] = max(last_ts[k],
                         float(getattr(p, "opened_ts", 0.0) or 0.0),
                         float(getattr(p, "closed_ts", 0.0) or 0.0))
        if getattr(p, "closed", False) and not is_dust_fill(p):
            settled[k].append(p)
    # Book-level persistence over the clean era: one computation, shared by
    # every promoted wallet's check (this book is what a go-live would trade).
    book_corr = None
    if min_split_half_corr is not None and era_floor is not None:
        book_corr = promotion_gate.split_half_corr(
            positions, min_opened_ts=era_floor)
    out = {}
    for k in keys:
        stats = promotion_gate.compute_stats(k, settled[k])
        ideal_roi, n_ideal = None, 0
        if min_ideal_roi is not None and era_floor is not None:
            ideal_roi, n_ideal = promotion_gate.ideal_roi_for(
                settled[k], min_opened_ts=era_floor)
        ready, checks = promotion_gate.golive_check(
            stats, last_trade_ts=last_ts[k] or None, now=now,
            min_settled=min_settled, max_idle_days=max_idle_days,
            min_roi=min_roi, floor_kwargs=floor_kwargs,
            ideal_roi=ideal_roi, n_ideal_settled=n_ideal,
            min_ideal_roi=min_ideal_roi,
            book_corr=book_corr, min_split_half_corr=min_split_half_corr)
        out[k] = (ready, stats, checks)
    return out


def _ready_message(wallet: str, stats, min_settled: int) -> str:
    w = html.escape(wallet)
    return (
        f"🟢 <b>GO-LIVE READY</b> — <code>{w}</code>\n"
        f"{stats.n_closed} settled (≥{min_settled}) · "
        f"ROI {(stats.roi or 0) * 100:+.0f}% · ${stats.net_pnl:+.0f} paper · "
        f"floor holds · recently active\n"
        f"Confirm with <code>/golive {w}</code>. Real money still needs "
        f"the manual <code>PREVIEW_MODE</code> flip — nothing was changed."
    )


def _unready_message(wallet: str, checks) -> str:
    # The gate's reason strings carry literal '<' ("copy ROI +3% < floor
    # +5%") — unescaped they 400 the parse_mode=HTML send, and the retry
    # loop would fail identically forever, losing exactly the decay alert
    # this exists for (2026-07-17 review catch).
    fails = html.escape("; ".join(
        f"{label} ({detail})" for label, ok, detail in checks if not ok)
        or "unknown")
    return (
        f"⏸ <b>No longer go-live ready</b> — <code>{html.escape(wallet)}</code>\n"
        f"Slipped on: {fails}. Alert re-arms if it crosses the bar again."
    )


def run_golive_watch(
    positions: Iterable,
    *,
    promoted: Iterable[str],
    state_path: str,
    send: Callable[[str], object],
    now: Optional[float] = None,
    min_settled: int,
    max_idle_days: float,
    min_roi: float,
    floor_kwargs: dict,
    era_floor: Optional[float] = None,
    min_ideal_roi: Optional[float] = None,
    min_split_half_corr: Optional[float] = None,
) -> list[tuple[str, bool]]:
    """One watch pass. Returns the [(wallet, ready)] transitions it alerted."""
    now = time.time() if now is None else now
    results = evaluate_promoted(
        positions, promoted, now=now, min_settled=min_settled,
        max_idle_days=max_idle_days, min_roi=min_roi,
        floor_kwargs=floor_kwargs, era_floor=era_floor,
        min_ideal_roi=min_ideal_roi,
        min_split_half_corr=min_split_half_corr)
    state = _read_state(state_path)
    # prune wallets no longer promoted so the state file can't grow stale keys
    # — but ONLY when the promoted store actually returned wallets: its reader
    # degrades to {} on a missing/corrupt file, and pruning on that transient
    # would wipe the edge state and re-fire duplicate READY alerts once the
    # store reads fine again (2026-07-17 review catch).
    changed = False
    if results:
        pruned = {k: v for k, v in state.items() if k in results}
        changed = pruned != state
        state = pruned
    transitions: list[tuple[str, bool]] = []
    for w, (ready, stats, checks) in sorted(results.items()):
        prev = (state.get(w) or {}).get("ready")
        if prev == ready:
            continue
        if prev is None and not ready:
            # first sight, not ready: remember silently — the alert is for the
            # CROSSING, not for the standing state.
            state[w] = {"ready": False, "ts": now}
            changed = True
            continue
        msg = (_ready_message(w, stats, min_settled) if ready
               else _unready_message(w, checks))
        if not send(msg):
            # Telegram failed — leave state untouched so next cycle retries.
            logger.warning(f"[GOLIVE-WATCH] alert send failed for {w} — will retry")
            continue
        state[w] = {"ready": ready, "ts": now}
        changed = True
        transitions.append((w, ready))
        logger.info(f"[GOLIVE-WATCH] {w} -> {'READY' if ready else 'not ready'} (alerted)")
    if changed:
        _write_state(state_path, state)
    return transitions
