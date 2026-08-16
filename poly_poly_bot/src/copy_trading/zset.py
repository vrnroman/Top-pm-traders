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
    out = []
    for key, rec in promotion_state.promoted_map(SCOPE).items():
        if not isinstance(rec, dict):
            continue
        if rec.get("source") not in GATE_SOURCES:
            logger.warn(f"[zset] ignoring {key[:12]}: not written by the gate "
                        f"(source={rec.get('source')!r})")
            continue
        out.append(rec.get("wallet") or key)
    return out


def wallet_set() -> set:
    return promotion_state.promoted_set(SCOPE)


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
    from src.copy_trading import promotion_state
    for scope in ("", "b"):
        try:
            if promotion_state.is_blacklisted(wallet, now=now, scope=scope):
                rec = promotion_state.blacklist_map(scope).get((wallet or "").lower(), {})
                return (f"under an active auto-demote in book "
                        f"{scope or 'A'} ({rec.get('reason', 'no reason')}, "
                        f"n={rec.get('n_closed', '?')}, roi={rec.get('roi', '?')})")
        except Exception:
            continue
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
          other_book_n: int = 0) -> tuple[bool, list]:
    """Admit a wallet to Z, but ONLY if the gate passed it and the rail holds.

    ``ready`` and ``checks`` come from ``promotion_gate.golive_check``. This
    function deliberately cannot be told "admit anyway": there is no force
    flag, because the entire point of Z is that the decision belongs to the
    gate. Returns ``(admitted, full_check_list)``.
    """
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

    promotion_state.add_promoted(wallet, tier=tier, source=source, scope=SCOPE)
    logger.warn(f"[zset] ADMITTED {wallet} to set Z ({conc_detail}). "
                f"Real money may now follow it once armed.")
    return (True, all_checks)


def evict(wallet: str, reason: str = "") -> bool:
    """Remove a wallet from Z. Always allowed, never gated.

    Asymmetry on purpose: getting in takes a gate and a rail, getting out
    takes one call. Anything that can only reduce live exposure should be
    reachable from anywhere, including from inside the loop.
    """
    gone = promotion_state.remove_promoted(wallet, scope=SCOPE)
    if gone:
        logger.warn(f"[zset] EVICTED {wallet} from set Z: {reason or 'no reason given'}")
    return gone
