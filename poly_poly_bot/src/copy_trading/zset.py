"""Set Z — the only wallets real money is ever allowed to follow.

The problem this exists to kill, found 2026-08-16: the live watched-wallet list
was `CONFIG.user_addresses + promotion_state.promoted_wallets()`, and that
promoted store still held two wallets one-tapped in July whose whole clean-era
record is 2 and 7 settled copies. Flipping `PREVIEW_MODE=false` would have put
real capital behind them. A hand-editable list is not a safety mechanism.

So Z is a store with exactly one writer: `promotion_gate.golive_check`
returning ready, plus one extra rail below. Nothing admits a wallet to Z by
being typed in, by being interesting, or by being promoted somewhere else.
`admit()` refuses a wallet the gate has not passed, which means the failure
mode of a careless caller is a rejection, not a live position.

Z reuses `promotion_state`'s existing scope mechanism (`scope="z"` gives
`promoted_wallets_z.json`, `copy_blacklist_z.json`, ...), so it introduces no
new storage concept: scope "" is strategy A, "b" is strategy B, "z" is the
real-money set.

**The drop-top-3 rail.** `golive_check` is a threshold on totals, and totals
hide concentration. One candidate this run cleared every existing bar with
61% of its book-B profit and 115% of its book-A profit sitting in three
tickets: remove three copies and it is flat. That is a lottery ticket with a
p-value attached, not an edge, and no amount of sample size fixes it because
the sample size is exactly what makes it look fine. So Z additionally requires
that a wallet still clears its ROI floor after its three best copies are
deleted. Cheap, blunt, and it is the one test the concentrated candidate fails.
"""

from __future__ import annotations

from typing import Iterable, Optional

from src.copy_trading import promotion_state
from src.logger import logger

SCOPE = "z"

# Records already warned about, so the per-trade path does not repeat itself.
_warned_non_gate: set = set()

# How many of a wallet's best copies are deleted before re-checking its floor.
DROP_TOP_N = 3

# The floor the trimmed record must still clear. Zero, not the full admission
# ROI: the question this rail asks is "is there anything left at all once the
# jackpots are gone", not "is it still just as good".
TRIMMED_MIN_ROI = 0.0


def wallets() -> list[str]:
    """The wallets real money may follow. Empty is a valid, safe answer.

    Only gate-written records count. The store is a file on a box, so a
    hand-edited entry is possible; ignoring anything without a gate source
    means such an entry is inert rather than tradable.
    """
    # Subtract the eviction history. `evict()` removes AND records, but the
    # removal can fail on its own, and an evicted-but-still-listed wallet on
    # the money path is the failure this set exists to prevent. Belt and
    # braces, on the cheap side of the trade.
    evicted = evicted_set()
    if "*" in evicted:
        return []
    out = []
    for key, rec in promotion_state.promoted_map(SCOPE).items():
        if key.lower() in evicted:
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("source") not in GATE_SOURCES:
            # Warn ONCE per record, not per call: this runs per live trade and
            # every 3s in the prober, so a single hand-edited entry would have
            # produced a warning every three seconds forever.
            if key not in _warned_non_gate:
                _warned_non_gate.add(key)
                logger.warn(f"[zset] ignoring {key[:12]}: not written by the "
                            f"gate (source={rec.get('source')!r})")
            continue
        out.append(rec.get("wallet") or key)
    return out


def wallet_set() -> set:
    """Same population as `wallets()`, as a set.

    Derived, never read raw: reading `promoted_set(SCOPE)` directly skipped
    the gate-source filter, so this was the forgeable twin of the safe
    function and the more natural-looking name for "is this wallet in Z".
    """
    return {w.lower() for w in wallets()}


def _num(x) -> float:
    try:
        return float(x or 0.0)
    except (TypeError, ValueError):
        return 0.0


def trimmed_roi(settled: Iterable, *, min_opened_ts: Optional[float] = None,
                drop_n: int = DROP_TOP_N) -> tuple[Optional[float], int, int]:
    """At-their-price ROI after deleting the ``drop_n`` best copies.

    Returns ``(roi, n_kept, n_dropped)``; roi is None when nothing is left to
    measure, which fails closed at the call site.

    At-their-price, not realized: the trimmed number has to be comparable to
    the figure the honest-metrics gate already reads, and it must not be
    something the fill model can inflate.
    """
    from src.copy_trading.copy_paper import is_dust_fill

    rows = []
    for p in settled:
        if not getattr(p, "closed", False):
            continue
        try:
            if is_dust_fill(p):
                continue
        except AttributeError:
            pass
        if _num(getattr(p, "spent", 0.0)) <= 0:
            continue
        if (min_opened_ts is not None
                and _num(getattr(p, "opened_ts", 0.0)) < min_opened_ts):
            continue
        rows.append(p)

    if not rows:
        return (None, 0, 0)
    rows.sort(key=lambda p: _num(getattr(p, "ideal_pnl", 0.0)), reverse=True)
    kept = rows[drop_n:]
    n_dropped = len(rows) - len(kept)
    if not kept:
        # Fewer copies than the rail deletes: there is no record left to judge.
        return (None, 0, n_dropped)
    spent = sum(_num(p.spent) for p in kept)
    if spent <= 0:
        return (None, len(kept), n_dropped)
    ideal = sum(_num(getattr(p, "ideal_pnl", 0.0)) for p in kept)
    return (ideal / spent, len(kept), n_dropped)


def concentration_check(settled: Iterable, *,
                        min_opened_ts: Optional[float] = None
                        ) -> tuple[bool, str]:
    """The drop-top-3 rail. Returns ``(ok, human-readable detail)``."""
    roi, n_kept, n_dropped = trimmed_roi(settled, min_opened_ts=min_opened_ts)
    if roi is None:
        return (False, f"no record left after dropping its best {DROP_TOP_N} "
                       f"({n_kept} kept)")
    ok = roi >= TRIMMED_MIN_ROI
    return (ok, f"{roi * 100:+.0f}% over {n_kept} copies with its best "
                f"{n_dropped} deleted")


# Only records written by the gate count. A hand-added entry lacks this and is
# ignored by `wallets()`, so editing the JSON by hand cannot put a wallet in
# front of real money even though the file is on a box the owner can write.
GATE_SOURCES = ("gate", "telegram-gate")


def _blacklist_block(wallet: str, now: Optional[float] = None) -> Optional[str]:
    """Is this wallet under the bot's OWN active demotion?

    The bot auto-demotes wallets it has decided against and records that in
    `copy_blacklist.json`. Admitting one of those to the real-money set means
    the headline ROI overrode the system's own recorded verdict, which is
    exactly what happened: 0x4a3f86ed was admitted while carrying an active
    auto-demote (n=15, roi -6.4%) that ran to 2026-08-18. It slipped the
    contradiction rail because the demotion had removed its clean-era rows
    from book A, so the rail saw "not enough evidence to contradict".
    """
    import json
    import os

    from src.copy_trading import promotion_state
    for scope in ("", "b"):
        # FAIL CLOSED on unreadable evidence. `promotion_state._read` returns
        # {} for a corrupt file, which would silently turn "no active demote"
        # into a pass for every wallet. A blacklist we cannot read is not an
        # empty blacklist.
        path = promotion_state.blacklist_path(scope)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    json.load(f)
            except Exception as exc:
                return (f"the book {scope or 'A'} demote list is unreadable "
                        f"({exc}); refusing rather than assuming it is empty")
        try:
            if promotion_state.is_blacklisted(wallet, now=now, scope=scope):
                rec = promotion_state.blacklist_map(scope).get((wallet or "").lower(), {})
                return (f"under an active auto-demote in book "
                        f"{scope or 'A'} ({rec.get('reason', 'no reason')}, "
                        f"n={rec.get('n_closed', '?')}, roi={rec.get('roi', '?')})")
        except Exception as exc:
            return (f"could not check the book {scope or 'A'} demote list "
                    f"({exc}); refusing rather than assuming it is clear")
    return None


def contradiction_check(a_roi: Optional[float], a_n: int,
                        min_n: int = 10) -> tuple[bool, str]:
    """Does the OTHER book disagree in sign on the same window?

    Lives here rather than in the seeding script so it is a property of set Z
    itself, not of one script's run.
    """
    if a_n < min_n or a_roi is None:
        return (True, f"only {a_n} clean copies in the other book, "
                      f"not enough to contradict")
    if a_roi < 0:
        return (False, f"the other book is {a_roi * 100:+.0f}% over {a_n} "
                       f"clean copies")
    return (True, f"the other book agrees at {a_roi * 100:+.0f}% over {a_n} copies")


def admit(wallet: str, *, ready: bool, checks: list, settled: Iterable,
          era_floor: Optional[float] = None, tier: str = "1b",
          source: str = "gate",
          other_book_roi: Optional[float] = None,
          other_book_n: int = 0,
          rails_supplied: bool = False) -> tuple[bool, list]:
    """Admit a wallet to Z, but ONLY if the gate passed it and the rail holds.

    ``ready`` and ``checks`` come from ``promotion_gate.golive_check``. This
    function deliberately cannot be told "admit anyway": there is no force
    flag, because the entire point of Z is that the decision belongs to the
    gate. Returns ``(admitted, full_check_list)``.
    """
    # A caller that does not explicitly supply the rail evidence gets a
    # REFUSAL, not a free pass. `other_book_*` defaulting to (None, 0) made
    # the contradiction rail print PASS for a caller who simply omitted it,
    # and `era_floor=None` silently computed the concentration rail over the
    # pre-clean-era rows the whole repo has spent months invalidating.
    if not rails_supplied:
        logger.warn(f"[zset] {wallet[:12]} NOT admitted: caller did not supply "
                    f"the rail evidence (era_floor / other-book)")
        return (False, list(checks) + [
            ("rail evidence supplied by the caller", False,
             "admit() was called without era_floor and other-book evidence")])
    conc_ok, conc_detail = concentration_check(settled, min_opened_ts=era_floor)
    contra_ok, contra_detail = contradiction_check(other_book_roi, other_book_n)
    bl = _blacklist_block(wallet)
    all_checks = list(checks) + [
        (f"still positive with its best {DROP_TOP_N} copies deleted",
         conc_ok, conc_detail),
        ("the other book does not contradict it", contra_ok, contra_detail),
        ("not under the bot's own auto-demote", bl is None,
         bl or "no active demotion"),
    ]

    ev = evicted_set()
    if "*" in ev or (wallet or "").lower() in ev:
        detail = ("set Z's eviction history is unreadable" if "*" in ev
                  else "previously evicted from set Z; use zset.readmit first")
        all_checks = all_checks + [("not previously evicted", False, detail)]
        logger.warn(f"[zset] {wallet[:12]} NOT admitted, {detail}")
        return (False, all_checks)
    all_checks = all_checks + [("not previously evicted", True, "no eviction on record")]

    if bl is not None:
        logger.warn(f"[zset] {wallet[:12]} NOT admitted, {bl}")
        return (False, all_checks)
    if not contra_ok:
        logger.info(f"[zset] {wallet[:12]} NOT admitted, contradiction: "
                    f"{contra_detail}")
        return (False, all_checks)
    if not ready:
        failed = [lab for lab, ok, _ in checks if not ok]
        logger.info(f"[zset] {wallet[:12]} NOT admitted, gate: {failed}")
        return (False, all_checks)
    if not conc_ok:
        logger.info(f"[zset] {wallet[:12]} NOT admitted, concentration rail: "
                    f"{conc_detail}")
        return (False, all_checks)

    # Touch the eviction history so its later absence is detectable. Without
    # this, "missing" is ambiguous between "never used" and "deleted".
    try:
        import os as _os
        rp = promotion_state.retired_path(SCOPE)
        if not _os.path.exists(rp):
            promotion_state._write(rp, {})
    except Exception as exc:
        logger.error(f"[zset] could not initialise the eviction history: {exc}")
        return (False, all_checks + [
            ("eviction history is writable", False, str(exc)[:80])])

    promotion_state.add_promoted(wallet, tier=tier, source=source, scope=SCOPE)
    logger.warn(f"[zset] ADMITTED {wallet} to set Z ({conc_detail}). "
                f"Real money may now follow it once armed.")
    return (True, all_checks)


# An eviction lasts this long. Far past any auto-demote window, because the
# point is that re-entry needs a deliberate decision, not the passage of time.
EVICTION_YEARS_S = 10 * 365 * 24 * 3600


def evicted_set() -> set:
    """Wallets evicted from Z. Re-entry requires clearing this deliberately."""
    import json
    import os

    # Probe the RAW file. `promotion_state._read` catches JSONDecodeError and
    # OSError and returns {}, so a corrupt, truncated or deleted eviction
    # history was indistinguishable from "nobody was ever evicted" — which is
    # precisely the regression this record exists to prevent, and the disk on
    # this VM has run at 84% and been a day from full. The earlier
    # `except Exception` here could never fire in production.
    path = promotion_state.retired_path(SCOPE)
    if not os.path.exists(path):
        # MISSING is not the same as empty. `admit()` writes this file on
        # every successful admission, so once Z has ever been used its absence
        # means the history was deleted or the data dir was restored from
        # before an eviction. Corrupt already failed closed; missing did not,
        # which left the 2026-08-18 expiry bomb live on its third failure mode.
        if os.path.exists(promotion_state.promoted_path(SCOPE)):
            logger.warn("[zset] eviction history is MISSING while set Z exists; "
                        "treating set Z as closed")
            return {"*"}
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            json.load(f)
    except Exception as exc:
        logger.warn(f"[zset] eviction history unreadable ({exc}); "
                    f"treating set Z as closed")
        return {"*"}
    try:
        return {k.lower() for k in promotion_state.retired_map(SCOPE)}
    except Exception as exc:
        logger.warn(f"[zset] eviction history unreadable ({exc}); "
                    f"treating set Z as closed")
        return {"*"}


def evict(wallet: str, reason: str = "") -> bool:
    """Remove a wallet from Z, and keep it out.

    Asymmetry on purpose: getting in takes a gate and three rails, getting out
    takes one call, so anything that only reduces live exposure is reachable
    from anywhere including inside the loop.

    STICKY, and that is the point. The bot's own auto-demote is time-boxed:
    0x4a3f86ed was evicted for carrying an active demote that expires
    2026-08-18, and without a durable record the next run of the seeding
    script would have re-admitted it on the same evidence the bot demoted it
    for, days before the flip. Eviction is a decision, not a timeout.
    """
    import time as _t
    # Record FIRST. Removing first and recording second meant an ENOSPC left
    # the wallet out of Z with no durable record while `/zset drop` still told
    # the owner "real money can no longer follow it" — true only until the
    # next re-seed.
    try:
        promotion_state.add_retired(
            wallet, until=_t.time() + EVICTION_YEARS_S,
            reason=reason or "evicted from set Z", scope=SCOPE)
    except Exception as exc:
        logger.error(f"[zset] could NOT record the eviction of {wallet}: {exc}. "
                     f"Refusing to report an eviction that would not stick.")
        return False
    gone = promotion_state.remove_promoted(wallet, scope=SCOPE)
    if gone:
        logger.warn(f"[zset] EVICTED {wallet} from set Z: {reason or 'no reason given'}")
    return gone


def readmit(wallet: str, reason: str = "") -> bool:
    """Clear an eviction so the gate may consider the wallet again.

    Deliberately separate and deliberately explicit: this is the only way back
    in, and it should read like a decision in the log.
    """
    key = (wallet or "").lower()
    try:
        data = dict(promotion_state.retired_map(SCOPE))
        if key not in data:
            return False
        del data[key]
        promotion_state._write(promotion_state.retired_path(SCOPE), data)
    except Exception as exc:
        logger.error(f"[zset] could not clear the eviction of {wallet}: {exc}")
        return False
    logger.warn(f"[zset] eviction CLEARED for {wallet}: {reason or 'no reason given'}")
    return True
