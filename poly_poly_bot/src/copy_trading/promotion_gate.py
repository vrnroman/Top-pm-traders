"""Trustworthy promotion gate — the statistical floor a paper wallet must clear
*before* an offer to promote it to real capital (System A) is fired.

Why this exists: the promote decision used to be a bare threshold
(``n_settled >= 15 AND copy_ROI >= +10%``). Fifteen resolved paper bets is a
tiny sample: +10% over 15 bets can be pure variance, a handful of lucky
longshots, or fifteen correlated bets on a single event. This module adds the
rigor the shortlist gate (Strategy 1c) already has, but on the *paper-copy
outcomes* rather than the discovery dossier, so a wallet only reaches the owner's
"Promote?" button once its edge is statistically credible, durable, and spread
across independent markets.

Two design rules, both load-bearing:

* **Theory-agnostic by construction.** The significance test is on the *per-bet
  copy return* (t-stat of ``pnl/spent``), never on win-rate. A +EV longshot
  theory (e.g. 1e) wins well under 50% of the time by design; a win-rate floor
  would wrongly hold exactly the wallets such a theory is built to find. A return
  t-stat rewards a genuinely positive edge regardless of how often it hits, and
  it holds a high-variance edge for *more data* rather than blocking it forever —
  symmetric, not biased. The Wilson lower bound on win-rate is still computed, but
  only as a non-blocking honesty annotation next to the wallet's breakeven price.

* **Statistical floor blocks; the LLM review only advises.** Everything here is
  validated arithmetic and is allowed to *block* an auto-offer. The Claude
  promotion review (``llm_review.review_promotion``) runs on top but only
  annotates the surfaced offer — it never suppresses one, because the gate's own
  calibration is itself still unvalidated (see the holdout items ROADMAP P1-5 /
  P1-5b). Statistical = gate, LLM = surfaced advice.

Pure and defensive: ``compute_stats`` / ``evaluate_floor`` never raise on
malformed positions; a wallet with too little data simply fails the floor with a
reason rather than throwing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional

from src.copy_trading.copy_paper import is_dust_fill
from src.copy_trading.pnl_unified import wilson_lower_bound


@dataclass(frozen=True)
class PromotionStats:
    """Everything the floor and the Telegram annotation need, computed once from a
    wallet's *settled* paper-copy positions."""
    wallet: str
    n_closed: int
    wins: int
    losses: int
    roi: Optional[float]           # net_pnl / capital over settled copies
    net_pnl: float
    roi_tstat: float               # significance of the per-bet copy return (>0 = +edge)
    second_half_roi: Optional[float]   # chronological 2nd-half ROI (decay check)
    distinct_conditions: int       # independent markets among the settled bets
    distinct_categories: int
    avg_entry_price: float         # capital-weighted mean fill price
    breakeven_winrate: float       # == avg_entry_price (a bet at price p breaks even at prob p)
    wilson_lb: Optional[float]     # honesty annotation only; NOT a gate


@dataclass(frozen=True)
class FloorResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)    # why it was held (empty when passed)
    warnings: list[str] = field(default_factory=list)   # non-blocking flags to surface
    stats: Optional[PromotionStats] = None


# A finite sentinel for a degenerate (zero-variance) t-stat, so the value stays
# JSON-standard in the history log and readable in Telegram rather than ±inf.
_TSTAT_CLAMP = 99.0

# --------------------------------------------------------------------------- #
# The pre-registered falsification bar (ROADMAP §7, agreed 2026-07-25 — written
# down BEFORE the clean data accrues, so the result can't be negotiated after
# the fact): after the P0-1 fill fix, if ≥ 4 weeks of clean fills show combined
# at-their-price ROI still negative AND split-half correlation still <= 0
# across >= FALSIFY_MIN_WALLETS wallets with n >= FALSIFY_MIN_N settled copies
# each, then wallet-copying on Polymarket is falsified for this approach. Stop
# tuning it; pivot the search to rules/market structure, or stop.
# --------------------------------------------------------------------------- #
FALSIFY_MIN_WALLETS = 15
FALSIFY_MIN_N = 10


def split_half_corr(
    positions: Iterable,
    *,
    min_n: int = FALSIFY_MIN_N,
    min_wallets: int = 3,
    pnl_attr: str = "pnl",
    min_opened_ts: Optional[float] = None,
) -> Optional[tuple[float, int]]:
    """Split-half persistence of wallet edge — THE number that says whether
    copy-trading works at all (ROADMAP §1.3/P0-4).

    For every wallet with >= ``min_n`` settled copies: sort chronologically,
    split in half, compute each half's ROI; then Pearson-correlate (first-half,
    second-half) pairs ACROSS wallets. Positive = a wallet looking good is
    information about what it does next; <= 0 = the promotion gate is promoting
    noise (the 2026-07-25 external measurement: -0.18 book A, -0.10 book B,
    11 of 13 wallets flipped sign — never before computed by the bot itself).

    ``pnl_attr="ideal_pnl"`` recomputes it on at-their-price economics (the
    fill-model-proof variant); ``min_opened_ts`` restricts to the clean era.
    Dust fills are excluded (same quarantine as every other trust surface): one
    pre-fix absurd fill (~$50 swept into ~50k shares) would put +99,900% in a
    wallet's half and dominate the correlation — the 2026-07-25 review caught
    this number disagreeing with the dust-quarantined book stats printed right
    beside it on dust-bearing backup ledgers.

    Returns ``(corr, n_qualifying_wallets)``; ``corr`` is None when undefined —
    fewer than ``min_wallets`` qualify (a correlation over 2 points is always
    ±1, so it is reported as "not measurable yet", never as a number) or a
    side has zero variance. ``n`` is still returned so surfaces can show how
    far off measurability the book is.
    """
    by_wallet: dict = {}
    for p in positions:
        if _num(getattr(p, "spent", 0.0)) <= 0.0:
            continue
        if not getattr(p, "closed", False):
            continue
        try:
            if is_dust_fill(p):
                continue
        except AttributeError:
            pass  # row lacks price fields: can't classify as dust — keep it
        if (min_opened_ts is not None
                and _num(getattr(p, "opened_ts", 0.0)) < min_opened_ts):
            continue
        w = (getattr(p, "target", "") or "").lower()
        by_wallet.setdefault(w, []).append(p)

    first_rois: list[float] = []
    second_rois: list[float] = []
    for rows in by_wallet.values():
        if len(rows) < min_n:
            continue
        ordered = sorted(rows, key=_closed_key)
        half = len(ordered) // 2
        fh, sh = ordered[:half], ordered[half:]
        fh_spent = sum(_num(p.spent) for p in fh)
        sh_spent = sum(_num(p.spent) for p in sh)
        if fh_spent <= 0 or sh_spent <= 0:
            continue
        first_rois.append(sum(_num(getattr(p, pnl_attr, 0.0)) for p in fh) / fh_spent)
        second_rois.append(sum(_num(getattr(p, pnl_attr, 0.0)) for p in sh) / sh_spent)

    n = len(first_rois)
    if n < min_wallets:
        return (None, n)
    mx = sum(first_rois) / n
    my = sum(second_rois) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(first_rois, second_rois))
    vx = sum((x - mx) ** 2 for x in first_rois)
    vy = sum((y - my) ** 2 for y in second_rois)
    # Scale-aware degenerate guard, NOT vx <= 0: float summation leaves
    # residuals like 5.8e-34 on bit-identical halves, which slips a zero check
    # and then noise/noise FABRICATES a maximal ±1.0 (or a garbage 0.0 that
    # would count toward the ROADMAP §7 kill bar) — verifier catch 2026-07-25
    # (three flat-stake wallets at ROI 0.1 read as perfect +1.0 persistence).
    # 1e-18 sits far above float noise and far below any real cross-wallet
    # variance (ROIs differing by >=1e-4 give vx >= 1e-8). This is the
    # kill-criterion number: it must never be manufactured.
    eps = 1e-18 * max(1.0, mx * mx, my * my)
    if vx <= eps or vy <= eps:
        return (None, n)    # no measurable cross-wallet variance: undefined
    return (round(cov / math.sqrt(vx * vy), 4), n)


def _return_tstat(returns: list[float]) -> float:
    """One-sample t-stat of per-bet copy returns against zero.

    ``> 0`` means a positive average edge; the magnitude says how many standard
    errors it sits above break-even. Zero-variance-but-positive (every bet the
    same positive return) is treated as strongly significant (clamped, not inf).
    Needs >= 2 bets."""
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    if var <= 0.0:
        # no dispersion: significance is degenerate — strongly + if mean>0, else 0.
        return _TSTAT_CLAMP if mean > 0 else (0.0 if mean == 0 else -_TSTAT_CLAMP)
    return mean / (math.sqrt(var) / math.sqrt(n))


def _num(v, default: float = 0.0) -> float:
    """Coerce to float, defaulting on anything non-numeric — so a malformed ledger
    row can never raise out of the pure stats path."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _closed_key(p) -> float:
    """Chronological sort key for a settled position (resolution time, falling
    back to open time so rows predating ``closed_ts`` still order sanely)."""
    return _num(getattr(p, "closed_ts", 0.0)) or _num(getattr(p, "opened_ts", 0.0))


def compute_stats(wallet: str, settled_positions: Iterable) -> PromotionStats:
    """Reduce one wallet's *settled* copy positions to the promotion statistics.

    ``settled_positions`` are ``PaperPosition``-like objects with ``spent``,
    ``pnl``, ``won``, ``entry_price``, ``condition_id`` and ``category``. Dust /
    zero-capital rows are ignored so they can't dilute the ratios."""
    rows = [p for p in settled_positions if _num(getattr(p, "spent", 0.0)) > 0.0]
    n = len(rows)
    if n == 0:
        return PromotionStats(
            wallet=wallet, n_closed=0, wins=0, losses=0, roi=None, net_pnl=0.0,
            roi_tstat=0.0, second_half_roi=None, distinct_conditions=0,
            distinct_categories=0, avg_entry_price=0.0, breakeven_winrate=0.0,
            wilson_lb=None,
        )

    spent = [_num(p.spent) for p in rows]
    pnl = [_num(getattr(p, "pnl", 0.0)) for p in rows]
    total_spent = sum(spent)
    total_pnl = sum(pnl)
    wins = sum(1 for p in rows if bool(getattr(p, "won", False)))
    losses = n - wins
    returns = [pnl[i] / spent[i] for i in range(n)]

    # chronological second half — is the edge still there in the recent bets?
    ordered = sorted(rows, key=_closed_key)
    half = n // 2
    tail = ordered[half:] if half > 0 else []
    tail_spent = sum(_num(p.spent) for p in tail)
    tail_pnl = sum(_num(getattr(p, "pnl", 0.0)) for p in tail)
    second_half_roi = (tail_pnl / tail_spent) if tail_spent > 0 else None

    conditions = {getattr(p, "condition_id", None) for p in rows if getattr(p, "condition_id", None)}
    categories = {getattr(p, "category", None) for p in rows if getattr(p, "category", None)}
    avg_entry = (sum(_num(getattr(p, "entry_price", 0.0)) * _num(p.spent) for p in rows)
                 / total_spent) if total_spent > 0 else 0.0

    return PromotionStats(
        wallet=wallet,
        n_closed=n,
        wins=wins,
        losses=losses,
        roi=(total_pnl / total_spent) if total_spent > 0 else None,
        net_pnl=round(total_pnl, 2),
        roi_tstat=round(_return_tstat(returns), 2),
        second_half_roi=(round(second_half_roi, 4) if second_half_roi is not None else None),
        distinct_conditions=len(conditions),
        distinct_categories=len(categories),
        avg_entry_price=round(avg_entry, 4),
        breakeven_winrate=round(avg_entry, 4),
        wilson_lb=wilson_lower_bound(wins, n),
    )


def evaluate_floor(
    stats: PromotionStats,
    *,
    min_n: int,
    min_roi: float,
    min_tstat: float,
    min_second_half_roi: float,
    min_conditions: int,
    min_categories: int,
) -> FloorResult:
    """Decide whether a wallet clears the statistical floor for a promote offer.

    ALL of the hard checks must pass; each failure adds a human-readable reason.
    Non-blocking observations (e.g. a Wilson lower bound below the wallet's
    breakeven price) are returned as ``warnings`` to surface on the offer, not to
    block it. Pure — no I/O."""
    reasons: list[str] = []
    warnings: list[str] = []

    if stats.n_closed < min_n:
        reasons.append(f"only {stats.n_closed} settled copies (need ≥{min_n})")
    if stats.roi is None:
        reasons.append("no settled capital to measure ROI")
    elif stats.roi < min_roi:
        reasons.append(f"copy ROI {stats.roi * 100:+.0f}% < floor {min_roi * 100:+.0f}%")

    # Significance on the per-bet RETURN (theory-agnostic; longshot-safe).
    if stats.roi_tstat < min_tstat:
        reasons.append(
            f"return t-stat {stats.roi_tstat:.2f} < {min_tstat:.2f} "
            f"(edge not distinguishable from variance yet)")

    # Durability — the recent half must not have gone negative.
    if stats.second_half_roi is not None and stats.second_half_roi < min_second_half_roi:
        reasons.append(
            f"2nd-half ROI {stats.second_half_roi * 100:+.0f}% < "
            f"{min_second_half_roi * 100:+.0f}% (edge decaying)")

    # Independence — 15 bets on one market is one bet measured 15 times.
    if (stats.distinct_conditions < min_conditions
            and stats.distinct_categories < min_categories):
        reasons.append(
            f"concentrated: {stats.distinct_conditions} markets / "
            f"{stats.distinct_categories} categories "
            f"(need ≥{min_conditions} markets or ≥{min_categories} categories)")

    # Non-blocking honesty: win-rate CI vs the price the wallet paid to enter.
    if (stats.wilson_lb is not None and stats.breakeven_winrate > 0
            and stats.wilson_lb < stats.breakeven_winrate):
        warnings.append(
            f"win-rate CI low ({stats.wilson_lb:.0%}) vs breakeven "
            f"{stats.breakeven_winrate:.0%} — edge rides on payout size, not hit rate")

    return FloorResult(passed=not reasons, reasons=reasons, warnings=warnings, stats=stats)


# --------------------------------------------------------------------------- #
# /golive pre-flip gate — re-check a promoted wallet live before real money
# --------------------------------------------------------------------------- #

def floor_kwargs_from(cfg) -> dict:
    """The promotion-floor kwargs from a CONFIG-like object — ONE derivation
    shared by the manual ``/golive`` handler and the automatic go-live watch,
    so the two gates can never drift apart (2026-07-17 review catch)."""
    return dict(
        min_n=cfg.copy_promote_min_settled,
        min_roi=cfg.copy_promote_min_roi,
        min_tstat=cfg.copy_promote_min_tstat,
        min_second_half_roi=cfg.copy_promote_min_second_half_roi,
        min_conditions=cfg.copy_promote_min_conditions,
        min_categories=cfg.copy_promote_min_categories)


def honest_kwargs_from(cfg) -> dict:
    """The honest-metrics go-live floors (at-their-price ROI + book split-half
    persistence, both evaluated on clean-era fills) from a CONFIG-like object.
    ONE derivation shared by ``/golive`` and the go-live watch — same parity
    rule as ``floor_kwargs_from`` above. Owner ruling 2026-07-25: wire these
    now at mirrored values, recalibrate at the 08-22 kill-criterion verdict.
    ``None`` (= the check is off) when the master toggle is off."""
    if not getattr(cfg, "copy_golive_honest_metrics", True):
        return dict(min_ideal_roi=None, min_ideal_settled=None,
                    min_split_half_corr=None)
    return dict(
        min_ideal_roi=cfg.copy_golive_min_ideal_roi,
        min_ideal_settled=cfg.copy_golive_min_ideal_settled,
        min_split_half_corr=cfg.copy_golive_min_split_half_corr)


def ideal_roi_for(positions: Iterable, *, min_opened_ts: Optional[float] = None
                  ) -> tuple[Optional[float], int]:
    """(at-their-price ROI, n) over settled, non-dust rows, optionally floored
    to the clean era — the go-live gate's honest-ROI input. The ROI is None
    when there are no qualifying rows: the gate fails CLOSED on it (real money
    is never blessed on artifact-era or absent evidence)."""
    rows = [p for p in positions
            if getattr(p, "closed", False)
            and _num(getattr(p, "spent", 0.0)) > 0
            and (min_opened_ts is None or _num(getattr(p, "opened_ts", 0.0))
                 >= min_opened_ts)]
    clean = []
    for p in rows:
        try:
            if is_dust_fill(p):
                continue
        except AttributeError:
            pass
        clean.append(p)
    if not clean:
        return (None, 0)
    spent = sum(_num(p.spent) for p in clean)
    ideal = sum(_num(getattr(p, "ideal_pnl", 0.0)) for p in clean)
    return ((ideal / spent) if spent > 0 else None, len(clean))


def golive_check(
    stats: PromotionStats,
    *,
    last_trade_ts: Optional[float],
    now: float,
    min_settled: int,
    max_idle_days: float,
    min_roi: float,
    floor_kwargs: dict,
    ideal_roi: Optional[float] = None,
    n_ideal_settled: int = 0,
    min_ideal_roi: Optional[float] = None,
    min_ideal_settled: Optional[int] = None,
    book_corr: Optional[tuple] = None,
    min_split_half_corr: Optional[float] = None,
) -> tuple[bool, list[tuple]]:
    """Re-verify a promoted wallet is still worth REAL capital, right now.

    The manual ``PREVIEW_MODE=false`` flip is the one true one-way door; this is
    the checklist the owner sees before crossing it. Stricter than the promote
    offer (a doubled settled bar) and time-aware (a wallet that went silent since
    it was promoted is not ready). Returns ``(ready, checks)`` where each check is
    ``(label, ok, detail)``. Pure — the caller supplies the wallet's stats and
    last-trade time.

    Honest-metrics floors (owner ruling 2026-07-25, additive; each None = off):
    the realized checks above were reading the ROI the fill artifact inflated,
    so the gate also requires the AT-THEIR-PRICE ROI and the BOOK's split-half
    persistence, both evaluated by the caller on clean-era (post-P0-1) fills.
    Both fail CLOSED: a wallet with no clean-era settled copies, or a book
    whose persistence is not yet measurable, is not ready — real money is
    never blessed on artifact-era or absent evidence. Recalibrate the floor
    values at the 08-22 kill-criterion verdict.
    """
    checks: list[tuple] = []

    checks.append((
        f"≥{min_settled} settled copies",
        stats.n_closed >= min_settled,
        f"{stats.n_closed}"))

    roi_ok = stats.roi is not None and stats.roi >= min_roi
    checks.append((
        f"paper ROI ≥ {min_roi * 100:+.0f}% now",
        roi_ok,
        (f"{stats.roi * 100:+.0f}%" if stats.roi is not None else "no data")))

    if min_ideal_roi is not None:
        # The at-their-price check needs a minimum SAMPLE, not just a sign:
        # without it one lucky clean settle clears the real-money gate while
        # the wallet's clean record is still thin (2026-07-27 review catch —
        # the corr check has a real minimum via split_half_corr, this one did
        # not). min_ideal_settled mirrors the repo's thin-sample band (5).
        thin = min_ideal_settled is not None and n_ideal_settled < min_ideal_settled
        ideal_ok = (ideal_roi is not None and not thin
                    and ideal_roi >= min_ideal_roi)
        if ideal_roi is None:
            detail = "no clean-era settled copies"
        elif thin:
            detail = (f"{ideal_roi * 100:+.0f}% over {n_ideal_settled} clean "
                      f"settles (need ≥{min_ideal_settled})")
        else:
            detail = f"{ideal_roi * 100:+.0f}% over {n_ideal_settled} clean settles"
        checks.append((
            f"at-their-price ROI ≥ {min_ideal_roi * 100:+.0f}% (clean era)",
            ideal_ok, detail))

    if min_split_half_corr is not None:
        corr = book_corr[0] if book_corr else None
        n_w = book_corr[1] if (book_corr and len(book_corr) > 1) else 0
        corr_ok = corr is not None and corr >= min_split_half_corr
        checks.append((
            f"book persistence ≥ {min_split_half_corr:+.2f} (split-half, clean era)",
            corr_ok,
            (f"{corr:+.2f} ({n_w}w)" if corr is not None
             else f"unmeasurable ({n_w}w with n≥{FALSIFY_MIN_N} settled)")))

    idle_days = ((now - last_trade_ts) / 86400.0) if last_trade_ts else float("inf")
    checks.append((
        f"active within {max_idle_days:.0f}d",
        idle_days <= max_idle_days,
        (f"{idle_days:.0f}d ago" if idle_days != float("inf") else "never")))

    floor = evaluate_floor(stats, **floor_kwargs)
    checks.append((
        "promotion floor still holds",
        floor.passed,
        ("ok" if floor.passed else "; ".join(floor.reasons))))

    ready = all(ok for _, ok, _ in checks)
    return ready, checks


# --------------------------------------------------------------------------- #
# Symmetric demote rigor
# --------------------------------------------------------------------------- #

def should_demote(
    stats: PromotionStats,
    *,
    min_n: int,
    max_roi: float,
    min_abs_loss: float,
    max_wilson: float,
) -> tuple[bool, str]:
    """Whether a wallet's paper record is bad enough to auto-blacklist it.

    Symmetric rigor with the promote side: a wallet is demoted only when it has
    enough settled copies AND a negative ROI past the floor AND a real absolute
    dollar loss (so a string of micro-capital bets that net a few cents negative
    can't trigger a 30-day blacklist) AND its win-rate upper credibility is weak.
    Returns ``(demote, reason)``. Pure."""
    if stats.n_closed < min_n:
        return (False, "")
    if stats.roi is None or stats.roi > max_roi:
        return (False, "")
    if stats.net_pnl > -abs(min_abs_loss):
        return (False, f"ROI {stats.roi * 100:+.0f}% but only {stats.net_pnl:+.2f} lost "
                       f"(< {min_abs_loss:.2f} floor) — noise, not a proven loser")
    # A genuinely negative ROI with a real loss AND a weak win rate: demote.
    if stats.wilson_lb is not None and stats.wilson_lb > max_wilson:
        return (False, f"ROI {stats.roi * 100:+.0f}% but win-rate holds up "
                       f"(Wilson {stats.wilson_lb:.0%}) — hold, not demote")
    return (True, f"{stats.n_closed} settled, ROI {stats.roi * 100:+.0f}%, "
                  f"net {stats.net_pnl:+.2f}")
