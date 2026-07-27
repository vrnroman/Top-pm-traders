"""Claude gate on newly-qualified wallet candidates (Strategy 1c).

The statistical funnel (closed-position t-stat, lead-lag capture, entry
discipline, PnL-curve shape, copy-replay) is the gate that *narrows* the
universe. For the handful of wallets that survive it, this module assembles a
compact dossier and asks Claude for the qualitative judgment the heuristics
can't make — "is this a genuine informed/consistent trader worth copying, or an
artifact?" — plus its reasoning. The caller uses a ``skip`` verdict to block the
wallet from the paper watchlist (the final admission gate).

The call goes through the ``claude -p`` CLI, which runs on the operator's Claude
subscription (auth via ``CLAUDE_CODE_OAUTH_TOKEN`` — see ``claude setup-token``)
so no ``ANTHROPIC_API_KEY`` is required. It is deliberately defensive: it
degrades to ``None`` on any failure (CLI missing, not authenticated, timeout,
non-JSON output) and the caller treats ``None`` as fail-open (admit) so a broken
CLI never freezes discovery. The subprocess runner is injectable for tests, so
the suite never shells out.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable

from src.copy_trading import langfuse_telemetry

logger = logging.getLogger("poly_poly_bot")

DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_TIMEOUT_S = 180

# Sentinel distinct from ``None``: the ``claude -p`` call could not run because the
# subscription is spend/rate-limited (a *transient*, retriable condition), not a
# generic failure. Callers treat it specially — defer the wallet's gate check to a
# restart-surviving queue and re-run it once the limit clears — rather than
# fail-open-and-forget. Any caller that doesn't know about it simply sees a
# non-verdict (like ``None``), so it degrades to today's safe behaviour.
RATE_LIMITED = object()

# Markers that identify a spend/usage/rate-limit response (captured from a real
# `claude -p` limit reply: "You've hit your monthly spend limit · raise it at
# claude.ai/settings/usage"). Matched case-insensitively across stderr + stdout +
# the parsed envelope, so wording drift in any one field still trips detection.
_RATE_LIMIT_MARKERS = (
    "spend limit", "usage limit", "rate limit",
    "claude.ai/settings/usage",
)


def _looks_rate_limited(*texts: str | None) -> bool:
    blob = " ".join(t for t in texts if t).lower()
    return any(m in blob for m in _RATE_LIMIT_MARKERS)

_SYSTEM = (
    "You are a quantitative analyst vetting Polymarket wallets for a paper-trading "
    "copy bot. You are given a dossier of a wallet that already passed statistical "
    "filters. Judge whether it is a genuine, *copyable* informed/consistent trader "
    "or a likely artifact (variance, settlement-lag scooping near $1, in-play "
    "markets that move before a copier can follow).\n"
    "\n"
    "A wallet qualifies EITHER by a delayed-copy lead-lag edge OR by an independent "
    "theory (listed in `qualifying_theories`). Most theories do NOT require lead-lag "
    "capture (e.g. 1b consistent skill, 1e longshot calibration, 1g category "
    "specialist, 1i low-variance whale). Read the dossier accordingly:\n"
    "- If the `copyability` (lead-lag) block is ABSENT, the deep lead-lag stage "
    "simply never ran for this wallet — that is EXPECTED for a non-lead-lag theory "
    "and is NOT a defect. Do NOT skip a wallet merely because lead-lag is absent; "
    "judge it on its qualifying theory, the `copy_replay` copy-and-hold record, the "
    "closed-position `skill`, entry discipline, and PnL curve.\n"
    "- If the `copyability` block is PRESENT, it was measured. NEGATIVE capture/lead "
    "there means a copier enters worse than the wallet — a genuine disqualifier "
    "(settlement-lag scooping); weight it heavily toward skip.\n"
    "- `copy_replay` (copy-and-hold ROI over resolved markets) is the most "
    "decision-relevant copyability signal when present; a solid positive copy_replay "
    "over a real sample is strong evidence to follow even without lead-lag.\n"
    "\n"
    "Be skeptical of artifacts: a high ROI from a few lucky bets, tail-price entries, "
    "a spiky or deeply-negative PnL curve, or a large max drawdown should lower the "
    "verdict. Reward steady, low-drawdown edge that a delayed copier can actually "
    "capture at copyable prices."
)

_INSTRUCTION = (
    "Respond with ONLY a single JSON object (no prose, no markdown fences) with "
    "exactly these keys:\n"
    '{"verdict": "follow"|"watch"|"skip", '
    '"insider_likelihood": "low"|"medium"|"high", '
    '"copyable": true|false, '
    '"confidence": <number 0.0-1.0>, '
    '"reasoning": "<one or two sentences>"}\n'
    "Use \"skip\" only when the wallet looks like an artifact the bot should not "
    "add (variance-driven, tail-dominated, negative measured capture, or a losing/"
    "spiky curve) — NOT merely because the lead-lag copyability block is absent. "
    "Do not use any tools; answer directly from the dossier."
)


@dataclass(frozen=True)
class LLMVerdict:
    verdict: str            # follow | watch | skip
    insider_likelihood: str  # low | medium | high
    copyable: bool
    confidence: float
    reasoning: str


def build_dossier(
    wallet: str,
    *,
    metrics: Any = None,        # WalletMetrics-like (roi, tstat, n_closed, capital, hit_rate, concentration)
    evaluation: Any = None,     # Eval-like (capture_cents, lead_cents, hit_rate, n)
    entry: Any = None,          # EntryProfile-like (mean_entry, tail_ratio, copyable_ratio)
    curve: Any = None,          # CurveMetrics-like (net_pnl, max_drawdown_frac, up_ratio, sharpe)
    copy_replay: Any = None,    # Eval-like (copy_roi, copy_n, copy_hit, exit_roi) — copy-and-hold replay
    qualifying_theories: list[dict] | None = None,  # [{id, desc, needs_capture}, …]
    why_flagged: str | None = None,                 # human-readable "why follow"
    portfolio_value: float | None = None,
    recent_bets: list[dict] | None = None,
    paper_record: dict | None = None,   # REALIZED paper-copy record (n_closed, roi, net_pnl, wins)
) -> dict:
    """Assemble the compact, JSON-serializable dossier for one wallet.

    Pure (no network): every field is pulled defensively via getattr so callers
    can pass whatever signals they have. Missing pieces are simply omitted — in
    particular, pass ``evaluation=None`` when the deep lead-lag stage never ran
    (``n == 0``) so the ``copyability`` block is *absent* rather than a row of
    zeros the model would misread as a measured no-edge.
    """
    def g(obj, name):
        return getattr(obj, name, None) if obj is not None else None

    d: dict = {"wallet": wallet}
    if qualifying_theories:
        d["qualifying_theories"] = qualifying_theories
    if why_flagged:
        d["why_flagged"] = why_flagged
    if metrics is not None:
        d["skill"] = _drop_nones({
            "roi": _round(g(metrics, "roi")),
            "tstat": _round(g(metrics, "tstat"), 2),
            "n_closed": g(metrics, "n_closed"),
            "capital": _round(g(metrics, "capital"), 0),
            "hit_rate": _round(g(metrics, "hit_rate")),
            "concentration": _round(g(metrics, "concentration")),
        })
    if evaluation is not None:
        d["copyability"] = _drop_nones({
            "capture_cents": _round(g(evaluation, "capture_cents"), 2),
            "lead_cents": _round(g(evaluation, "lead_cents"), 2),
            "hit_rate": _round(g(evaluation, "hit_rate")),
            "n_trades": g(evaluation, "n"),
        })
    if copy_replay is not None and (g(copy_replay, "copy_n") or 0) > 0:
        d["copy_replay"] = _drop_nones({
            "copy_and_hold_roi": _round(g(copy_replay, "copy_roi")),
            "n_resolved": g(copy_replay, "copy_n"),
            "hit_rate": _round(g(copy_replay, "copy_hit")),
            "exit_follow_roi": _round(g(copy_replay, "exit_roi")),
        })
    if paper_record and (paper_record.get("n_closed") or 0) > 0:
        # REALIZED forward paper record — what actually happened when the live
        # harness copied this wallet (fill sim + slippage + resolution), as
        # opposed to the copy_replay backtest above. Present on paper-proven
        # re-entries so the gate weighs the measured outcome, not just the
        # own-history stats it may have already rejected once.
        d["paper_record_realized"] = _drop_nones({
            "n_settled": paper_record.get("n_closed"),
            "roi": _round(paper_record.get("roi")),
            "net_pnl_usd": _round(paper_record.get("net_pnl"), 2),
            "wins": paper_record.get("wins"),
        })
    if entry is not None:
        d["entry_profile"] = _drop_nones({
            "mean_entry_price": _round(g(entry, "mean_entry")),
            "tail_ratio": _round(g(entry, "tail_ratio")),
            "copyable_ratio": _round(g(entry, "copyable_ratio")),
        })
    if curve is not None:
        d["pnl_curve"] = _drop_nones({
            "net_pnl": _round(g(curve, "net_pnl"), 0),
            "max_drawdown_frac": _round(g(curve, "max_drawdown_frac")),
            "up_ratio": _round(g(curve, "up_ratio")),
            "sharpe": _round(g(curve, "sharpe"), 2),
        })
    if portfolio_value is not None:
        d["portfolio_value"] = round(float(portfolio_value), 0)
    if recent_bets:
        d["recent_bets"] = recent_bets[:15]
    return d


def _drop_nones(block: dict) -> dict:
    """Drop None-valued keys from a dossier block (P1-3): a field whose source
    data genuinely doesn't exist must be ABSENT, not an explicit null — the gate
    used to receive rows of nulls (n_closed, capital, up_ratio…) and read them
    as measured zeros or contradictions."""
    return {k: v for k, v in block.items() if v is not None}


def _round(v, ndigits: int = 4):
    return round(float(v), ndigits) if isinstance(v, (int, float)) else v


def _build_prompt(dossier: dict) -> str:
    return (
        f"{_SYSTEM}\n\n"
        "Vet this wallet dossier and decide whether the paper bot should add it:\n"
        f"{json.dumps(dossier, indent=2)}\n\n"
        f"{_INSTRUCTION}"
    )


def _claude_cli_runner(prompt: str, *, model: str, timeout_s: int) -> dict | None:
    """Run one ``claude -p`` call on the Claude subscription, return its envelope.

    Uses ``--output-format json`` (a stable envelope with a ``result`` string
    plus usage/cost/latency) and runs in a throwaway temp dir so it never loads
    the bot's own project context (CLAUDE.md, tools). Auth comes from the
    inherited environment (``CLAUDE_CODE_OAUTH_TOKEN``). Returns the parsed
    envelope dict; ``RATE_LIMITED`` (defer to the re-check queue) when the CLI
    is limited or keeps failing; ``None`` only for un-retriable non-success
    (CLI missing, bad envelope).
    """
    exe = shutil.which("claude")
    if not exe:
        logger.warning("[LLM-GATE] `claude` CLI not found on PATH — skipping (fail-open)")
        return None
    cmd = [exe, "-p", prompt, "--output-format", "json", "--model", model]
    # One bounded retry on a generic CLI failure (nonzero exit / timeout / exec
    # error), then DEFER via the retriable sentinel instead of failing open —
    # prod showed intermittent bare `exit 1` at sweep time (6× 2026-07-11..14),
    # and every one silently admitted an unvetted wallet. A transient hiccup
    # now costs one retry; a persistent one parks the wallet for the next
    # sweep's re-check, exactly like a rate limit. Fail-open remains only for
    # the truly un-retriable case (CLI missing above).
    proc = None
    for attempt in (1, 2):
        try:
            with tempfile.TemporaryDirectory(prefix="llm-gate-") as cwd:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout_s,
                    cwd=cwd, env=os.environ.copy(),
                )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning("[LLM-GATE] claude -p attempt %d/2 failed to run: %r",
                           attempt, e)
            proc = None
            continue
        if proc.returncode == 0:
            break
        # capture BOTH streams — the prod failures logged an empty stderr, so
        # stderr alone is a dead end for diagnosis.
        logger.warning(
            "[LLM-GATE] claude -p exit %s (attempt %d/2) stderr: %s | stdout: %s",
            proc.returncode, attempt,
            (proc.stderr or "")[:300] or "<empty>",
            (proc.stdout or "")[:300] or "<empty>")
        # A spend/rate limit is not worth a same-sweep retry — defer now.
        if _looks_rate_limited(proc.stderr, proc.stdout):
            logger.warning("[LLM-GATE] claude -p is rate/spend-limited — deferring")
            return RATE_LIMITED
    if proc is None or proc.returncode != 0:
        logger.warning(
            "[LLM-GATE] claude -p failed twice — deferring wallet to re-check "
            "queue (was fail-open before 2026-07-16)")
        return RATE_LIMITED
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # Non-JSON on a zero exit (e.g. a plain limit notice) — still catch a limit,
        # else warn (don't silently swallow a garbage CLI response like the old
        # unguarded json.loads did — the operator needs a diagnostic in bot.log).
        if _looks_rate_limited(proc.stdout, proc.stderr):
            return RATE_LIMITED
        logger.warning("[LLM-GATE] claude -p emitted non-JSON stdout: %s",
                       (proc.stdout or "")[:200])
        return None
    if envelope.get("is_error") or envelope.get("subtype") != "success":
        logger.warning("[LLM-GATE] claude -p returned error envelope: %s",
                       str(envelope.get("subtype")))
        # The limit may surface as an error envelope; scan the whole envelope.
        if _looks_rate_limited(json.dumps(envelope), proc.stderr):
            logger.warning("[LLM-GATE] claude -p error envelope is a rate/spend limit — deferring")
            return RATE_LIMITED
        return None
    return envelope


def _verdict_from_data(data: dict) -> LLMVerdict | None:
    try:
        return LLMVerdict(
            verdict=str(data["verdict"]),
            insider_likelihood=str(data["insider_likelihood"]),
            copyable=bool(data["copyable"]),
            confidence=float(data["confidence"]),
            reasoning=str(data["reasoning"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _parse_verdict(text: str) -> dict | None:
    """Pull the verdict JSON object out of the model's reply (tolerant of
    stray prose or ```json fences)."""
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)  # first balanced-ish object
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def review_wallet(
    dossier: dict,
    *,
    runner: Callable[..., Any] | None = None,
    model: str = DEFAULT_MODEL,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> LLMVerdict | None:
    """Ask Claude (via ``claude -p``) for a verdict on a dossier.

    Returns ``None`` on any failure so the caller can fail open. ``runner`` is
    injectable for tests; in production it shells out to the ``claude`` CLI and
    returns the full JSON envelope. Every call is recorded to Langfuse (a no-op
    unless ``LANGFUSE_*`` is configured) with the prompt, verdict, token usage,
    cost and latency.
    """
    runner = runner or _claude_cli_runner
    prompt = _build_prompt(dossier)
    wallet = dossier.get("wallet")
    start = time.time()
    res = None
    try:
        res = runner(prompt, model=model, timeout_s=timeout_s)
    except Exception:  # never let the gate call break the sweep
        logger.warning("[LLM-GATE] failed for %s", wallet, exc_info=True)
    end = time.time()

    # Rate/spend-limited: short-circuit BEFORE normalization (the sentinel is not a
    # dict/str, so it would otherwise fall through to a plain None and lose the
    # retriable signal). The caller defers this wallet to the re-check queue.
    if res is RATE_LIMITED:
        _record(dossier, wallet, model, prompt, None, None, None, start, end,
                "rate-limited")
        return RATE_LIMITED

    # The CLI runner returns the full envelope; an injected test runner may
    # return the result text directly. Normalize both.
    envelope = res if isinstance(res, dict) else None
    text = envelope.get("result") if envelope else (res if isinstance(res, str) else None)
    data = _parse_verdict(text) if text else None
    verdict = _verdict_from_data(data) if data else None

    error = None if verdict else ("no verdict returned" if res is None else "unparseable verdict")
    _record(dossier, wallet, model, prompt, text, envelope, verdict, start, end, error)
    return verdict


# --------------------------------------------------------------------------- #
# Promotion review — a SECOND Claude gate, on the paper-copy outcomes, run before
# a wallet is offered for promotion to real capital. Advisory only: it annotates
# the offer, it never blocks one (the statistical floor in promotion_gate.py is
# the block). Shares the `claude -p` runner + Langfuse plumbing above.
# --------------------------------------------------------------------------- #

_PROMO_SYSTEM = (
    "You are a quantitative risk reviewer deciding whether a Polymarket wallet, "
    "already measured on a paper-copy book, is a real, durable edge worth copying "
    "with REAL capital — or a wallet whose paper record is variance, decay, or "
    "concentration dressed up as skill.\n"
    "\n"
    "You are given the wallet's paper-copy performance (ROI over settled copies, "
    "the per-bet return t-stat, the chronological second-half ROI, how many "
    "distinct markets/categories the bets spread across, win/loss counts, the "
    "average entry price and a win-rate confidence bound). The statistical floor "
    "has ALREADY passed — your job is the qualitative judgment it can't make:\n"
    "- Judge the edge on RETURN, not hit rate. A wallet that wins <50% of the "
    "time but is profitable on longshots is legitimate; do NOT penalize a low win "
    "rate when the ROI and return t-stat are positive.\n"
    "- Be skeptical of: a second-half ROI far below the overall (fading edge), a "
    "return t-stat barely positive (thin significance), bets concentrated in a "
    "single market or category (correlated, not independent), or an ROI driven by "
    "one or two outsized wins.\n"
    "- Reward a steady, well-distributed, still-positive-recently edge.\n"
    "\n"
    "This is advice, not a veto — the owner still taps to accept and money only "
    "moves after a separate manual step. Give your honest read and the concerns."
)

_PROMO_INSTRUCTION = (
    "Respond with ONLY a single JSON object (no prose, no markdown fences) with "
    "exactly these keys:\n"
    '{"verdict": "promote"|"watch"|"reject", '
    '"confidence": <number 0.0-1.0>, '
    '"reasoning": "<one or two sentences>", '
    '"concerns": ["<short concern>", ...]}\n'
    'Use "reject" when the paper record looks like variance/decay/concentration '
    'rather than durable edge, "watch" when it is promising but you want more '
    'data, "promote" when it is a credible edge worth real capital. Do not use '
    "any tools; answer directly from the dossier."
)


@dataclass(frozen=True)
class PromotionVerdict:
    verdict: str            # promote | watch | reject
    confidence: float
    reasoning: str
    concerns: tuple = ()


def build_promotion_dossier(
    wallet: str,
    *,
    stats: Any = None,               # promotion_gate.PromotionStats-like
    theories: list | None = None,    # discovery theories that flagged the wallet
    floor_warnings: list | None = None,
    tier: str | None = None,
) -> dict:
    """Assemble the compact paper-performance dossier for a promotion review.

    Pure; every field pulled defensively so a partial ``stats`` still serializes."""
    def g(name):
        return getattr(stats, name, None) if stats is not None else None

    d: dict = {"wallet": wallet, "decision": "promote to real capital?"}
    if tier:
        d["target_tier"] = tier
    if theories:
        d["qualifying_theories"] = list(theories)
    d["paper_copy_record"] = {
        "settled_copies": g("n_closed"),
        "roi": _round(g("roi")),
        "net_pnl": _round(g("net_pnl"), 2),
        "return_tstat": _round(g("roi_tstat"), 2),
        "second_half_roi": _round(g("second_half_roi")),
        "wins": g("wins"),
        "losses": g("losses"),
        "distinct_markets": g("distinct_conditions"),
        "distinct_categories": g("distinct_categories"),
        "avg_entry_price": _round(g("avg_entry_price")),
        "breakeven_winrate": _round(g("breakeven_winrate")),
        "winrate_wilson_lb": _round(g("wilson_lb")),
    }
    if floor_warnings:
        d["statistical_flags"] = list(floor_warnings)
    return d


def _build_promotion_prompt(dossier: dict) -> str:
    return (
        f"{_PROMO_SYSTEM}\n\n"
        "Review this wallet's paper-copy record and decide whether to promote it "
        "to real capital:\n"
        f"{json.dumps(dossier, indent=2)}\n\n"
        f"{_PROMO_INSTRUCTION}"
    )


def _promotion_verdict_from_data(data: dict) -> PromotionVerdict | None:
    try:
        concerns = data.get("concerns") or []
        if not isinstance(concerns, (list, tuple)):
            concerns = [str(concerns)]
        return PromotionVerdict(
            verdict=str(data["verdict"]),
            confidence=float(data["confidence"]),
            reasoning=str(data["reasoning"]),
            concerns=tuple(str(c) for c in concerns),
        )
    except (KeyError, TypeError, ValueError):
        return None


def review_promotion(
    dossier: dict,
    *,
    runner: Callable[..., Any] | None = None,
    model: str = DEFAULT_MODEL,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> PromotionVerdict | None:
    """Ask Claude whether a paper-validated wallet should be promoted to real
    capital. Returns ``None`` on any failure (the caller treats that as "review
    unavailable — surface the offer statistical-only", never as a block).
    ``runner`` is injectable for tests. Recorded to Langfuse like the shortlist
    gate (no-op unless configured)."""
    runner = runner or _claude_cli_runner
    prompt = _build_promotion_prompt(dossier)
    wallet = dossier.get("wallet")
    start = time.time()
    res = None
    try:
        res = runner(prompt, model=model, timeout_s=timeout_s)
    except Exception:  # never let the review break the governance cycle
        logger.warning("[PROMOTE-GATE] LLM review failed for %s", wallet, exc_info=True)
    end = time.time()

    # Rate/spend-limited or persistently-failing CLI: the runner returns the
    # RATE_LIMITED sentinel (not a dict/str). Record it as what it is — without
    # this check it would fall through and be mislabeled "unparseable verdict".
    if res is RATE_LIMITED:
        _record_promotion(dossier, wallet, model, prompt, None, None, None,
                          start, end, "rate-limited")
        return None

    envelope = res if isinstance(res, dict) else None
    text = envelope.get("result") if envelope else (res if isinstance(res, str) else None)
    data = _parse_verdict(text) if text else None
    verdict = _promotion_verdict_from_data(data) if data else None

    error = None if verdict else ("no verdict returned" if res is None else "unparseable verdict")
    _record_promotion(dossier, wallet, model, prompt, text, envelope, verdict, start, end, error)
    return verdict


def _record_promotion(dossier, wallet, model, prompt, text, envelope, verdict, start, end, error) -> None:
    """Forward one promotion review to Langfuse (no-op unless configured; never raises)."""
    if not langfuse_telemetry.enabled():
        return
    meta: dict = {"wallet": wallet}
    if verdict is not None:
        meta.update(verdict=verdict.verdict, confidence=verdict.confidence,
                    concerns=list(verdict.concerns))
    if isinstance(dossier.get("paper_copy_record"), dict):
        meta["paper_copy_record"] = dossier["paper_copy_record"]
    theory_ids = [t for t in (dossier.get("qualifying_theories") or []) if t]
    if theory_ids:
        meta["qualifying_theories"] = theory_ids
    tags = ["promotion-gate", "claude-code", "strategy-1c-promote"] + [f"theory:{t}" for t in theory_ids]
    langfuse_telemetry.record_generation(
        name="promotion-gate",
        input=prompt,
        output=(text if text is not None else ""),
        model=model,
        start=start, end=end,
        usage=(envelope or {}).get("usage"),
        cost_usd=(envelope or {}).get("total_cost_usd"),
        duration_ms=(envelope or {}).get("duration_ms"),
        metadata=meta,
        tags=tags,
        error=error,
    )


def _record(dossier, wallet, model, prompt, text, envelope, verdict, start, end, error) -> None:
    """Forward one gate call to Langfuse (no-op unless configured; never raises)."""
    if not langfuse_telemetry.enabled():
        return
    meta: dict = {"wallet": wallet}
    if verdict is not None:
        meta.update(verdict=verdict.verdict, insider_likelihood=verdict.insider_likelihood,
                    copyable=verdict.copyable, confidence=verdict.confidence)
    if isinstance(dossier.get("skill"), dict):
        meta["skill"] = dossier["skill"]
    # Per-theory tags so Langfuse can slice accept/reject rate by qualifying theory
    # (e.g. spot "theory:1e admits ~0%") without a prod-log trawl.
    theory_ids = [t.get("id") for t in (dossier.get("qualifying_theories") or [])
                  if isinstance(t, dict) and t.get("id")]
    if theory_ids:
        meta["qualifying_theories"] = theory_ids
    tags = ["wallet-gate", "claude-code", "strategy-1c"] + [f"theory:{t}" for t in theory_ids]
    langfuse_telemetry.record_generation(
        name="wallet-gate",
        input=prompt,
        output=(text if text is not None else ""),
        model=model,
        start=start, end=end,
        usage=(envelope or {}).get("usage"),
        cost_usd=(envelope or {}).get("total_cost_usd"),
        duration_ms=(envelope or {}).get("duration_ms"),
        metadata=meta,
        tags=tags,
        error=error,
    )
