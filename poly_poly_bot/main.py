#!/usr/bin/env python3
"""Polymarket copy-trading bot (Strategy #1).

Manages:
- Strategy #1 (Copy Trading): runs natively via asyncio
- Copy-paper validation harness (Strategy 1b): forward paper-copy measurement
- Wallet discovery: continuously hunts copyable wallets -> paper watchlist
- Unified Telegram bot for all commands

Usage:
  python main.py              # Run with defaults from .env
"""

import asyncio
import os
import sys
import signal
import logging
import threading
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import CONFIG
from src.logger import logger

import src.telegram_bot as telegram_bot

_shutdown_event = threading.Event()


def refresh_clob_client() -> None:
    """Rebuild the singleton CLOB client after the in-memory key changes.

    Called by the Telegram /setkey command. Drops the cached singleton and
    rebuilds it so a fresh key validates; Strategy #1's running loops hold the
    client reference obtained at startup, so a rotated key fully takes effect on
    the next container restart (which reloads PRIVATE_KEY from .env).
    """
    from src.copy_trading.clob_client import create_clob_client, reset_clob_client

    reset_clob_client()
    create_clob_client()  # may be None if key was cleared


def _log_copy_cycle_diagnostics(tag: str, summary) -> None:
    """One line of guardrail skips + one of detection-funnel rejects per cycle
    (only when nonzero) — shared by the A and B paper loops so the two books
    always report identically."""
    skips = (summary.skipped_fill_gate + summary.skipped_not_first_entry
             + summary.skipped_slate_cap + summary.skipped_category_gate
             + summary.skipped_event_cap + summary.skipped_category_evidence
             + summary.skipped_price_bucket_evidence)
    if skips:
        logger.info(
            f"[{tag}] guardrail skips: fill-gate={summary.skipped_fill_gate} "
            f"first-entry={summary.skipped_not_first_entry} "
            f"slate-cap={summary.skipped_slate_cap} "
            # the winning-markets gate is default-ON and the biggest behaviour
            # change — log it so a quieted book always shows a reason.
            f"category-gate={summary.skipped_category_gate} "
            f"event-cap={summary.skipped_event_cap} "
            f"already-copied={summary.skipped_already_copied}"
        )
    # P1-6 book-evidence gates get their own line: a book that goes quiet
    # because its own ledger proved a slice losing must say so loudly.
    if summary.skipped_category_evidence or summary.skipped_price_bucket_evidence:
        logger.info(
            f"[{tag}] evidence gates: category={summary.skipped_category_evidence} "
            f"price-bucket={summary.skipped_price_bucket_evidence} "
            "(unstamped wallet, slice proven-losing in this book)")
    if summary.opened_unkeyed_event:
        # the event cap could not group these opens (feed row had no eventSlug)
        logger.info(f"[{tag}] event-cap: {summary.opened_unkeyed_event} open(s) "
                    f"had no event key (ungroupable, admitted uncapped)")
    # starvation autopsy: rows the DETECTOR dropped before the engine saw them
    # (the 2026-07 A-book stall was invisible at guardrail level — watched
    # wallets traded, yet nothing reached the engine).
    rej = summary.detector_rejects
    if rej and (rej.get("rows", 0) - rej.get("emitted", 0)) > 0:
        detail = " ".join(
            f"{k.replace('_', '-')}={rej.get(k, 0)}"
            for k in ("rows", "not_buy", "stale", "price_band",
                      "below_min_usd", "missing_ids", "emitted"))
        logger.info(f"[{tag}] detection rejects: {detail}")


def _copy_paper_loop():
    """Strategy 1b validation: forward paper-copy of watchlist wallets.

    Measures execution-realistic copy PnL (entries against the live book, net of
    drag) and tracks it to resolution. Places NO real orders — it is a
    measurement harness whose ledger gates whether any wallet earns real capital.
    """
    import time
    from src.copy_trading import governance
    from src.copy_trading.copy_paper import format_resolution_telegram, report
    from src.copy_trading.copy_paper_live import (
        TradeFeed, make_feed_detector, make_feed_exit_detector)
    from src.copy_trading.copy_paper_runner import CopyPaperRunner
    from src.copy_trading.outcome_names import DEFAULT_RESOLVER

    # Advisory Claude promotion review (annotates the offer; never blocks it).
    _promo_review = None
    if CONFIG.copy_promote_llm_review:
        from src.copy_trading.llm_review import review_promotion as _promo_review
    # promotion-gate-history lives beside the discovery gate-history log.
    _promo_history = os.path.join(
        os.path.dirname(CONFIG.wallet_discovery_state), "promotion-gate-history.jsonl")

    def _load_replay_by_wallet():
        """Per-wallet own-history copy-replay stats from the discovery watchlist,
        for the probation fast-track. Defensive: {} on any read/parse failure."""
        try:
            import json
            with open(CONFIG.copy_paper_watchlist, encoding="utf-8") as f:
                data = json.load(f)
            out = {}
            for row in (data.get("targets") or []):
                w = (row.get("wallet") or "").lower()
                if w:
                    out[w] = {"copy_roi": row.get("copy_roi", 0.0),
                              "copy_n": row.get("copy_n", 0),
                              "copy_tstat": row.get("copy_tstat", 0.0)}
            return out
        except Exception as e:
            # Fail safe to "no fast-track this cycle" on a missing/corrupt/wrong-shape
            # watchlist — but NOT silently: a persistent parse failure would disable
            # the probation path invisibly, so log it (RCA-visible) rather than swallow.
            logger.debug(f"[PROMOTE-GATE] fast-track replay read failed ({e}) — "
                         f"no probation this cycle")
            return {}

    def _send_demotion_a(d):
        telegram_bot.send_message(
            f"⛔ <b>Auto-demoted</b> <code>{d['wallet']}</code> — "
            f"{d['n_closed']} settled copies, ROI {d['roi'] * 100:+.0f}% "
            f"(≤ {CONFIG.copy_demote_max_roi * 100:+.0f}%). "
            f"Dropped from the watchlist for "
            f"{CONFIG.copy_demote_cooldown_days:.0f}d.")
        # A-vs-B race: a wallet A just proved-negative under LAGGED fills may be
        # strategy B's edge under instant fills (0x161a: -21% A / +9% B replay).
        # Offer it to B before it vanishes from the shared watchlist.
        _cross_route_a_exit(
            d["wallet"],
            reason=f"A auto-demote (ROI {d['roi'] * 100:+.0f}% @ n={d['n_closed']})")

    def _governance(ledger):
        """Auto promote-offer / demote off the System-B paper ledger each cycle."""
        if not CONFIG.copy_governance_enabled:
            return
        try:
            governance.run_governance_cycle(
                ledger.positions.values(),
                now=time.time(),
                promote_min_n=CONFIG.copy_promote_min_settled,
                promote_min_roi=CONFIG.copy_promote_min_roi,
                promote_min_tstat=CONFIG.copy_promote_min_tstat,
                promote_min_second_half_roi=CONFIG.copy_promote_min_second_half_roi,
                promote_min_conditions=CONFIG.copy_promote_min_conditions,
                promote_min_categories=CONFIG.copy_promote_min_categories,
                demote_min_n=CONFIG.copy_demote_min_settled,
                demote_max_roi=CONFIG.copy_demote_max_roi,
                demote_min_abs_loss=CONFIG.copy_demote_min_abs_loss,
                demote_max_wilson=CONFIG.copy_demote_max_wilson,
                cooldown_s=CONFIG.copy_demote_cooldown_days * 86400.0,
                default_tier=CONFIG.promote_default_tier,
                review_fn=_promo_review,
                llm_model=CONFIG.wallet_discovery_llm_model,
                history_path=_promo_history,
                send_offer=lambda o: telegram_bot.send_promotion_offer(
                    o["wallet"], o["n_closed"], o["roi"], o["net_pnl"],
                    o.get("tier", "1b"), extras=o),
                send_demotion=_send_demotion_a,
                # probation fast-track (rec 2a): strong own-history replay + a small
                # agreeing forward sample -> an early "probation"-tier offer.
                replay_by_wallet=_load_replay_by_wallet(),
                probation_enabled=CONFIG.copy_probation_enabled,
                probation_min_settled=CONFIG.copy_probation_min_settled,
                probation_min_replay_n=CONFIG.copy_probation_min_replay_n,
                probation_min_replay_roi=CONFIG.copy_probation_min_replay_roi,
                probation_min_replay_tstat=CONFIG.copy_probation_min_replay_tstat,
                # dead-band time-box (rec 2b): neutrally retire stuck wallets.
                time_box_enabled=CONFIG.copy_time_box_enabled,
                time_box_window_s=CONFIG.copy_time_box_window_days * 86400.0,
                retire_cooldown_s=CONFIG.copy_retire_cooldown_days * 86400.0,
                send_retirement=lambda r: telegram_bot.send_message(
                    f"🗄️ <b>Retired (inconclusive)</b> <code>{r['wallet']}</code> — "
                    f"{r['n_closed']} settled, ROI {r['roi'] * 100:+.0f}%, "
                    f"{r['age_days']:.0f}d on paper with no verdict. Removed from the "
                    f"watchlist (re-discoverable), not blacklisted."),
            )
        except Exception as e:
            logger.warning(f"[COPY-PAPER] governance cycle failed: {e}")

    # Dead-funnel alarm (starvation RCA 2026-07): paper opens/day fell 12→0 and
    # nothing said so — promotion evidence only accrues while copies open, so a
    # silent stall re-starves the funnel invisibly. Baseline falls back to boot
    # time on an empty ledger so a fresh install doesn't alarm instantly; alerts
    # re-arm every alarm window (daily heartbeat, not a one-shot), and any new
    # open resets the clock via the ledger itself (restart-proof).
    _stall = {"last_alert": 0.0, "boot": time.time()}

    def _stall_check(ledger):
        hours = CONFIG.copy_paper_stall_alarm_hours
        if hours <= 0:
            return
        now = time.time()
        last_open = max((getattr(p, "opened_ts", 0.0) or 0.0
                         for p in ledger.positions.values()), default=0.0)
        baseline = max(last_open, _stall["boot"])
        if now - baseline < hours * 3600.0:
            return
        if now - _stall["last_alert"] < hours * 3600.0:
            return  # already alerted this window
        n_watch = len(runner.wallets())
        if n_watch == 0:
            return  # empty watchlist is its own (already-logged) condition
        _stall["last_alert"] = now
        stalled_h = (now - baseline) / 3600.0
        logger.warning(
            f"[COPY-PAPER] FUNNEL STALLED — no paper opens in {stalled_h:.0f}h "
            f"with {n_watch} wallets on the watchlist; promotion evidence is "
            f"not accruing")
        telegram_bot.send_message(
            f"🚱 <b>Copy funnel stalled</b> — no paper copies opened in "
            f"{stalled_h:.0f}h with {n_watch} wallets watched. Evidence toward "
            f"promotion is not accruing; check the watchlist's trade activity "
            f"and the guardrail-skip mix.")

    def _golive_watch(ledger):
        """Edge-triggered GO-LIVE READY / dropped-back Telegram alert for
        promoted wallets — same gate as the manual /golive, fired on the
        crossing instead of waiting to be polled."""
        if not CONFIG.copy_golive_alert_enabled:
            return
        if not telegram_bot.is_configured():
            # No delivery channel: a crossing would "retry" (and warn) every
            # cycle forever. Skip cleanly; the transition fires once Telegram
            # is configured, since state only records DELIVERED alerts.
            return
        try:
            from src.copy_trading import (
                era_state, golive_watch, promotion_gate, promotion_state)
            golive_watch.run_golive_watch(
                ledger.positions.values(),
                promoted=promotion_state.promoted_wallets(),
                state_path=os.path.join(CONFIG.data_dir, "golive_watch.json"),
                send=telegram_bot.send_message,
                min_settled=CONFIG.copy_golive_min_settled,
                max_idle_days=CONFIG.copy_golive_max_idle_days,
                min_roi=CONFIG.copy_golive_min_roi,
                floor_kwargs=promotion_gate.floor_kwargs_from(CONFIG),
                # honest-metrics floors (owner ruling 2026-07-25): same single
                # derivation as /golive, evaluated on the clean era only.
                era_floor=era_state.era_floor_ts(
                    os.path.join(CONFIG.data_dir, "ab_race_state.json")),
                **promotion_gate.honest_kwargs_from(CONFIG))
        except Exception as e:
            # WARNING, not debug: this feature's whole job is telling the owner
            # what he isn't watching for — a silently dead watch is the worst
            # failure mode it has (2026-07-17 review catch).
            logger.warning(f"[GOLIVE-WATCH] cycle check failed ({e})")

    def _on_cycle(summary, ledger):
        if summary.opened or summary.resolved:
            logger.info(
                f"[COPY-PAPER] opened={summary.opened} resolved={summary.resolved} "
                f"open={len(ledger.open_positions())} closed={len(ledger.closed_positions())}"
            )
        _log_copy_cycle_diagnostics("COPY-PAPER", summary)
        if summary.resolved:
            telegram_bot.send_message(
                format_resolution_telegram(summary.resolved_positions, report(ledger),
                                           resolver=DEFAULT_RESOLVER)
            )
        try:
            _stall_check(ledger)
        except Exception as e:  # the alarm must never break the copy cycle
            logger.debug(f"[COPY-PAPER] stall check failed ({e})")
        _governance(ledger)
        _golive_watch(ledger)

    # A cap <= 0 disables that guardrail (engine treats None as off).
    def _cap(v):
        return v if v and v > 0 else None

    # Shared-feed detection (item 4): one global /trades poll per cycle, filtered
    # to watched wallets — detection cost is flat in the wallet count, so the
    # watchlist scales to hundreds. Falls back to per-wallet polling when off.
    detector_factory = None
    exit_detector_factory = None
    if CONFIG.copy_paper_feed_detection:
        _feed = TradeFeed()
        _feed_min = CONFIG.copy_paper_feed_min_usd

        def detector_factory(wallets, max_age_s, min_usd, flagged_by_map=None, **kw):
            return make_feed_detector(wallets, max_age_s, min_usd, flagged_by_map,
                                      feed=_feed, feed_min_usd=_feed_min, **kw)

        def exit_detector_factory(wallets, max_age_s):
            return make_feed_exit_detector(wallets, max_age_s,
                                           feed=_feed, feed_min_usd=_feed_min)

    runner = CopyPaperRunner(
        ledger_path=CONFIG.copy_paper_ledger,
        watchlist_path=CONFIG.copy_paper_watchlist,
        max_copy_usd=CONFIG.copy_paper_max_usd,
        copy_pct=CONFIG.copy_paper_copy_pct,
        max_slippage_bps=CONFIG.copy_paper_max_slippage_bps,
        max_age_s=CONFIG.copy_paper_max_age_s,
        min_usd=CONFIG.copy_paper_min_usd,
        cycle_interval_s=CONFIG.copy_paper_interval_s,
        fill_gate_bps=_cap(CONFIG.copy_paper_fill_gate_bps),
        first_entry_only=CONFIG.copy_paper_first_entry_only,
        max_copies_per_wallet_day=_cap(CONFIG.copy_paper_max_per_wallet_day),
        max_copies_per_category_day=_cap(CONFIG.copy_paper_max_per_category_day),
        # per-(wallet, event) correlated-slate cap + downward-only stake tiering
        # for low-confidence admits (2026-07 race RCA).
        max_copies_per_wallet_event=_cap(CONFIG.copy_paper_max_per_wallet_event),
        low_conf_stake_frac=CONFIG.copy_paper_low_conf_stake_frac,
        low_conf_until_n=CONFIG.copy_paper_low_conf_until_n,
        gate_history_path=os.path.join(
            # SAME derivation as the writer (discovery_runner puts gate-history
            # beside the discovery state file) — deriving from data_dir instead
            # would silently read a never-written path if the state file is
            # relocated, disabling the stake tiering with no warning.
            os.path.dirname(CONFIG.wallet_discovery_state), "gate-history.jsonl"),
        # evidence-throughput levers (starvation RCA): route the daily caps to
        # the coldest wallets + paper-only category-cap relief under the
        # evidence floor (fills stamped over_real_cap for promotion audit).
        starved_priority=CONFIG.copy_paper_starved_priority,
        relief_evidence_n=_cap(CONFIG.copy_paper_relief_evidence_n),
        relief_max_per_category_day=_cap(CONFIG.copy_paper_relief_max_per_category_day),
        # winning-markets-only gate (item A) + conviction sizing (item C)
        category_gate=CONFIG.copy_paper_category_gate,
        conviction_base_usd=(CONFIG.copy_paper_conviction_base_usd
                             if CONFIG.copy_paper_conviction_base_usd > 0 else None),
        conviction_min=CONFIG.copy_paper_conviction_min,
        conviction_max=CONFIG.copy_paper_conviction_max,
        # P1-6 book-evidence gates (unstamped wallets blocked from slices this
        # book proves losing) + P1-7 modeled costs (gas/fee vs realized, spread
        # vs at-price). Identical across books so the race variable stays
        # lagged-vs-instant.
        category_evidence_min_n=_cap(CONFIG.copy_paper_category_evidence_min_n),
        category_evidence_era_only=CONFIG.copy_paper_category_evidence_era_only,
        era_state_path=os.path.join(CONFIG.data_dir, "ab_race_state.json"),
        costs_enabled=CONFIG.copy_paper_costs_enabled,
        gas_usd_per_trade=CONFIG.copy_paper_gas_usd,
        trade_fee_bps=CONFIG.copy_paper_trade_fee_bps,
        # When Strategy 4 is on, this near-term book stops short-copying far-future
        # bets — they would lock paper capital for months and belong to the S4
        # book instead. Off => horizon-blind, so behaviour is unchanged.
        max_horizon_days=(CONFIG.strategy_4_long_horizon_days
                          if CONFIG.strategy_4_enabled else None),
        # NB: no mark_fetcher here on purpose. The near-term book cycles every
        # ~60s; marking in-cycle would (a) fire a full ledger re-serialize every
        # cycle (s.marked>0) and (b) burst N synchronous CLOB /book fetches,
        # stalling trade detection. Near-term opens are instead marked on-read in
        # /pnl (telegram_bot._compute_unified), exactly like System-A opens — the
        # mark only needs to be fresh when the owner looks. S4 (long-horizon, slow
        # cycle, months to resolution) still marks in-cycle below.
        detector_factory=detector_factory,
        exit_detector_factory=exit_detector_factory,
        on_cycle=_on_cycle,
    )
    n = len(runner.wallets())
    logger.info(
        f"Copy-paper harness started (wallets={n}, interval={CONFIG.copy_paper_interval_s}s, "
        f"feed-detection={CONFIG.copy_paper_feed_detection}, "
        f"max ${CONFIG.copy_paper_max_usd:.0f}/copy, PREVIEW measurement only)"
    )
    if n == 0:
        wl = CONFIG.copy_paper_watchlist
        logger.warning(
            f"[COPY-PAPER] no watchlist wallets at {wl} — generate one with "
            f"`python -m backtest.two_stage_watchlist --cache-dir data/wcache "
            f"--output {wl}` (skill ∩ copyability)"
        )
    runner.run_forever(_shutdown_event)


def _s4_paper_loop():
    """Strategy 4: paper book for long-horizon bets, marked to market.

    Watches both the copy watchlist and the long-horizon watchlist (S1 ∪ S4
    wallets) and opens a paper position only on bets whose market resolves at or
    beyond the horizon cut — the far-future conviction bets the near-term copier
    now skips. Holds to resolution, marking each open position to the live mid so
    /pnl shows a running unrealized P&L instead of a blank for months. NO orders.
    """
    from src.copy_trading.copy_paper_live import fetch_mid
    from src.copy_trading.copy_paper_runner import CopyPaperRunner

    def _on_cycle(summary, ledger):
        if summary.opened or summary.resolved or summary.marked:
            logger.info(
                f"[S4-PAPER] opened={summary.opened} resolved={summary.resolved} "
                f"marked={summary.marked} skipped_horizon={summary.skipped_horizon} "
                f"open={len(ledger.open_positions())} closed={len(ledger.closed_positions())}"
            )

    runner = CopyPaperRunner(
        ledger_path=CONFIG.strategy_4_paper_ledger,
        watchlist_path=CONFIG.copy_paper_watchlist,
        extra_watchlist_paths=[CONFIG.wallet_discovery_long_horizon_watchlist],
        max_copy_usd=CONFIG.strategy_4_paper_max_usd,
        copy_pct=CONFIG.copy_paper_copy_pct,
        max_slippage_bps=CONFIG.copy_paper_max_slippage_bps,
        max_age_s=CONFIG.copy_paper_max_age_s,
        min_usd=CONFIG.strategy_4_paper_min_usd,
        cycle_interval_s=CONFIG.strategy_4_paper_interval_s,
        # this book takes ONLY long-horizon bets, marks them to market, and stamps
        # them strategy "4" for per-strategy P&L.
        min_horizon_days=CONFIG.strategy_4_long_horizon_days,
        mark_fetcher=fetch_mid,
        strategy="4",
        on_cycle=_on_cycle,
    )
    n = len(runner.wallets())
    logger.info(
        f"S4 long-horizon paper book started (wallets={n}, "
        f"interval={CONFIG.strategy_4_paper_interval_s}s, "
        f"horizon≥{CONFIG.strategy_4_long_horizon_days:.0f}d, "
        f"max ${CONFIG.strategy_4_paper_max_usd:.0f}/bet, PREVIEW measurement only)"
    )
    runner.run_forever(_shutdown_event)


def _cross_route_a_exit(wallet: str, ev=None, reason: str = "") -> None:
    """Offer a wallet leaving strategy A's ecosystem to strategy B (A-vs-B race).

    Called from two exits: a governance auto-demote (no Eval — replay stats are
    read from the wallet's still-present watchlist row) and a discovery-sweep
    removal (the sweep's Eval carries fresh replay stats). The router does the
    B-fit gating; this wrapper only gathers stats and must never raise into the
    calling loop."""
    if not (CONFIG.copy_paper_b_enabled and CONFIG.copy_paper_b_cross_route):
        return
    try:
        import json as _json

        from src.copy_trading import cross_route

        replay_n, replay_roi, entry = 0, 0.0, None
        if ev is not None:
            replay_n = int(getattr(ev, "copy_n", 0) or 0)
            replay_roi = float(getattr(ev, "copy_roi", 0.0) or 0.0)
            entry = {
                "approved_categories": list(getattr(ev, "approved_categories", ()) or ()),
                "median_usd": getattr(ev, "median_usd", 0.0),
                "flagged_by": list(getattr(ev, "flagged_by", ()) or ()),
            }
        else:
            try:
                with open(CONFIG.copy_paper_watchlist, encoding="utf-8") as f:
                    for row in (_json.load(f).get("targets") or []):
                        if (row.get("wallet") or "").lower() == wallet.lower():
                            replay_n = int(row.get("copy_n", 0) or 0)
                            replay_roi = float(row.get("copy_roi", 0.0) or 0.0)
                            entry = row
                            break
            except (OSError, ValueError):
                pass  # no watchlist row — the router still checks B's own record
        routed, why = cross_route.route_to_b(
            wallet, extras_path=CONFIG.copy_paper_b_extra_watchlist,
            b_ledger_path=CONFIG.copy_paper_b_ledger, reason=reason,
            replay_n=replay_n, replay_roi=replay_roi,
            min_replay_n=CONFIG.wallet_discovery_min_copy_replay_n,
            min_replay_roi=CONFIG.wallet_discovery_min_copy_replay_roi,
            watchlist_entry=entry)
        if routed:
            telegram_bot.send_message(
                f"🅱️ <b>Cross-routed to strategy B</b> <code>{wallet}</code>\n"
                f"Left A ({reason}); B-fit: {why} "
                f"(replay {replay_roi * 100:+.1f}% @ n={replay_n}). "
                f"B keeps copying it at the target's own price.")
    except Exception as e:  # a routing failure must never break governance/discovery
        logger.warning(f"[COPY-PAPER-B] cross-route failed for {wallet}: {e}")


def _copy_paper_b_loop():
    """Strategy B: the borrowed-clock (instant-copy) paper book — A-vs-B race.

    Same feed detection and sizing as strategy A, ONE variable changed: every
    admitted copy fills at the TARGET'S OWN price (+COPY_PAPER_B_SLIPPAGE_BPS),
    with no fill-gate censoring and looser slate caps — the evidence a 2-3s
    copier would accrue, recorded without needing the on-chain feed. Own ledger,
    own promotion/blacklist stores (scope "b"), same promotion floors as A so
    the week's promotion counts compare one variable. NO real orders ever.
    """
    import time
    from collections import Counter

    from src.copy_trading import cross_route, governance, promotion_state
    from src.copy_trading.copy_paper import format_resolution_telegram, report
    from src.copy_trading.copy_paper_live import (
        TradeFeed, make_feed_detector, make_feed_exit_detector)
    from src.copy_trading.copy_paper_runner import CopyPaperRunner
    from src.copy_trading.outcome_names import DEFAULT_RESOLVER

    # One-time seed of the extras watchlist (A-demoted wallets that are B-fit).
    try:
        cross_route.seed_extras(CONFIG.copy_paper_b_extra_watchlist,
                                CONFIG.copy_paper_b_seed_wallets)
    except Exception as e:  # pragma: no cover - seeding must never kill the book
        logger.warning(f"[COPY-PAPER-B] extras seeding failed: {e}")

    _promo_review = None
    if CONFIG.copy_promote_llm_review:
        from src.copy_trading.llm_review import review_promotion as _promo_review
    _promo_history_b = os.path.join(
        os.path.dirname(CONFIG.wallet_discovery_state),
        "promotion-gate-history_b.jsonl")

    def _load_replay_by_wallet():
        """Same probation fast-track input as strategy A: replay stats from the
        shared discovery watchlist (B's extras wallets are simply absent -> no
        probation lane for them). Defensive {} on any failure."""
        try:
            import json
            with open(CONFIG.copy_paper_watchlist, encoding="utf-8") as f:
                data = json.load(f)
            out = {}
            for row in (data.get("targets") or []):
                w = (row.get("wallet") or "").lower()
                if w:
                    out[w] = {"copy_roi": row.get("copy_roi", 0.0),
                              "copy_n": row.get("copy_n", 0),
                              "copy_tstat": row.get("copy_tstat", 0.0)}
            return out
        except Exception as e:
            logger.debug(f"[PROMOTE-GATE-B] fast-track replay read failed ({e})")
            return {}

    def _send_offer_b(o) -> bool:
        # Plain tagged message — deliberately NO accept button: strategy B has
        # no live execution path yet (the on-chain feed is not wired), so an
        # accept must not be able to write into A's promoted store or the
        # fast-track path. Recorded in B's own offers store on delivery.
        tier = f" · tier {o.get('tier')}" if o.get("probation") else ""
        return telegram_bot.send_message(
            f"🅱️ <b>Strategy-B promote signal</b> <code>{o['wallet']}</code>\n"
            f"{o['n_closed']} settled instant-copies, ROI {o['roi'] * 100:+.0f}%, "
            f"${o['net_pnl']:+.0f}{tier}\n"
            f"Paper-only: B has no live execution until the on-chain feed ships. "
            f"No accept button by design.")

    def _governance_b(ledger):
        """B's auto promote-signal / demote off its own ledger, scope-\"b\" state."""
        if not CONFIG.copy_governance_enabled:
            return
        try:
            governance.run_governance_cycle(
                ledger.positions.values(),
                now=time.time(),
                promote_min_n=CONFIG.copy_promote_min_settled,
                promote_min_roi=CONFIG.copy_promote_min_roi,
                promote_min_tstat=CONFIG.copy_promote_min_tstat,
                promote_min_second_half_roi=CONFIG.copy_promote_min_second_half_roi,
                promote_min_conditions=CONFIG.copy_promote_min_conditions,
                promote_min_categories=CONFIG.copy_promote_min_categories,
                demote_min_n=CONFIG.copy_demote_min_settled,
                demote_max_roi=CONFIG.copy_demote_max_roi,
                demote_min_abs_loss=CONFIG.copy_demote_min_abs_loss,
                demote_max_wilson=CONFIG.copy_demote_max_wilson,
                cooldown_s=CONFIG.copy_demote_cooldown_days * 86400.0,
                default_tier=CONFIG.promote_default_tier,
                review_fn=_promo_review,
                llm_model=CONFIG.wallet_discovery_llm_model,
                history_path=_promo_history_b,
                state_scope="b",
                send_offer=_send_offer_b,
                send_demotion=lambda d: telegram_bot.send_message(
                    f"🅱️⛔ <b>B auto-demoted</b> <code>{d['wallet']}</code> — "
                    f"{d['n_closed']} settled instant-copies, ROI "
                    f"{d['roi'] * 100:+.0f}%. Dropped from B's book for "
                    f"{CONFIG.copy_demote_cooldown_days:.0f}d (A unaffected)."),
                replay_by_wallet=_load_replay_by_wallet(),
                probation_enabled=CONFIG.copy_probation_enabled,
                probation_min_settled=CONFIG.copy_probation_min_settled,
                probation_min_replay_n=CONFIG.copy_probation_min_replay_n,
                probation_min_replay_roi=CONFIG.copy_probation_min_replay_roi,
                probation_min_replay_tstat=CONFIG.copy_probation_min_replay_tstat,
                time_box_enabled=CONFIG.copy_time_box_enabled,
                time_box_window_s=CONFIG.copy_time_box_window_days * 86400.0,
                retire_cooldown_s=CONFIG.copy_retire_cooldown_days * 86400.0,
                send_retirement=lambda r: telegram_bot.send_message(
                    f"🅱️🗄️ <b>B retired (inconclusive)</b> <code>{r['wallet']}</code> — "
                    f"{r['n_closed']} settled, ROI {r['roi'] * 100:+.0f}%, "
                    f"{r['age_days']:.0f}d with no verdict. Removed from B "
                    f"(re-discoverable), not blacklisted."),
            )
        except Exception as e:
            logger.warning(f"[COPY-PAPER-B] governance cycle failed: {e}")

    _stall = {"last_alert": 0.0, "boot": time.time()}

    def _stall_check(ledger):
        hours = CONFIG.copy_paper_stall_alarm_hours
        if hours <= 0:
            return
        now = time.time()
        last_open = max((getattr(p, "opened_ts", 0.0) or 0.0
                         for p in ledger.positions.values()), default=0.0)
        baseline = max(last_open, _stall["boot"])
        if now - baseline < hours * 3600.0:
            return
        if now - _stall["last_alert"] < hours * 3600.0:
            return
        n_watch = len(runner.wallets())
        if n_watch == 0:
            return
        _stall["last_alert"] = now
        stalled_h = (now - baseline) / 3600.0
        logger.warning(
            f"[COPY-PAPER-B] FUNNEL STALLED — no B opens in {stalled_h:.0f}h "
            f"with {n_watch} wallets watched; the A-vs-B race is not accruing "
            f"B evidence (verdict window at risk)")
        telegram_bot.send_message(
            f"🅱️🚱 <b>Strategy-B funnel stalled</b> — no instant-copies opened "
            f"in {stalled_h:.0f}h with {n_watch} wallets watched. The A-vs-B "
            f"comparison window is compromised while B starves.")

    def _cap(v):
        return v if v and v > 0 else None

    def _on_cycle(summary, ledger):
        if summary.opened or summary.resolved:
            logger.info(
                f"[COPY-PAPER-B] opened={summary.opened} resolved={summary.resolved} "
                f"open={len(ledger.open_positions())} closed={len(ledger.closed_positions())}"
            )
        _log_copy_cycle_diagnostics("COPY-PAPER-B", summary)
        if summary.slate_cap_binds:
            # cap-bind autopsy (manager amendment): WHO the cap bound, per kind —
            # if B's caps bind non-degenerately they re-create A's censoring and
            # the race is contaminated; this line is how that gets caught in 48h.
            binds = Counter((t, kind) for t, _cat, kind in summary.slate_cap_binds)
            detail = ", ".join(f"{w[:8]}…×{n} ({kind})"
                               for (w, kind), n in binds.most_common())
            logger.info(f"[COPY-PAPER-B] cap-bind: {detail}")
        if summary.resolved:
            telegram_bot.send_message(
                "🅱️ " + format_resolution_telegram(
                    summary.resolved_positions, report(ledger),
                    resolver=DEFAULT_RESOLVER))
        try:
            _stall_check(ledger)
        except Exception as e:
            logger.debug(f"[COPY-PAPER-B] stall check failed ({e})")
        _governance_b(ledger)

    detector_factory = None
    exit_detector_factory = None
    if CONFIG.copy_paper_feed_detection:
        _feed = TradeFeed()   # B's own feed poll — no cross-thread cache sharing
        _feed_min = CONFIG.copy_paper_feed_min_usd

        def detector_factory(wallets, max_age_s, min_usd, flagged_by_map=None, **kw):
            return make_feed_detector(wallets, max_age_s, min_usd, flagged_by_map,
                                      feed=_feed, feed_min_usd=_feed_min, **kw)

        def exit_detector_factory(wallets, max_age_s):
            return make_feed_exit_detector(wallets, max_age_s,
                                           feed=_feed, feed_min_usd=_feed_min)

    runner = CopyPaperRunner(
        ledger_path=CONFIG.copy_paper_b_ledger,
        watchlist_path=CONFIG.copy_paper_watchlist,
        extra_watchlist_paths=[CONFIG.copy_paper_b_extra_watchlist],
        max_copy_usd=CONFIG.copy_paper_max_usd,
        copy_pct=CONFIG.copy_paper_copy_pct,
        max_slippage_bps=CONFIG.copy_paper_max_slippage_bps,
        max_age_s=CONFIG.copy_paper_max_age_s,
        min_usd=CONFIG.copy_paper_min_usd,
        cycle_interval_s=CONFIG.copy_paper_interval_s,
        # B's thesis: NO fill-gate censoring; fills at the target's own price.
        fill_gate_bps=None,
        fill_at_their_price_bps=CONFIG.copy_paper_b_slippage_bps,
        first_entry_only=CONFIG.copy_paper_first_entry_only,
        max_copies_per_wallet_day=_cap(CONFIG.copy_paper_b_max_per_wallet_day),
        max_copies_per_category_day=_cap(CONFIG.copy_paper_b_max_per_category_day),
        # same event cap + stake tiering as A (identical across books, so the
        # race variable stays lagged-vs-instant).
        max_copies_per_wallet_event=_cap(CONFIG.copy_paper_b_max_per_wallet_event),
        low_conf_stake_frac=CONFIG.copy_paper_low_conf_stake_frac,
        low_conf_until_n=CONFIG.copy_paper_low_conf_until_n,
        gate_history_path=os.path.join(
            # SAME derivation as the writer (discovery_runner puts gate-history
            # beside the discovery state file) — deriving from data_dir instead
            # would silently read a never-written path if the state file is
            # relocated, disabling the stake tiering with no warning.
            os.path.dirname(CONFIG.wallet_discovery_state), "gate-history.jsonl"),
        starved_priority=CONFIG.copy_paper_starved_priority,
        relief_evidence_n=None,   # caps already sized for take-all; no relief lane
        relief_max_per_category_day=None,
        category_gate=CONFIG.copy_paper_category_gate,
        # P1-6 book-evidence gates + P1-7 modeled costs — identical to A's, so
        # the race variable stays lagged-vs-instant. B's own ledger feeds its
        # evidence gates (its losing slices are its own: §1.5's −8.8% sports
        # and −61.5% 0.2-0.4 bucket are B-book records).
        category_evidence_min_n=_cap(CONFIG.copy_paper_category_evidence_min_n),
        category_evidence_era_only=CONFIG.copy_paper_category_evidence_era_only,
        era_state_path=os.path.join(CONFIG.data_dir, "ab_race_state.json"),
        costs_enabled=CONFIG.copy_paper_costs_enabled,
        gas_usd_per_trade=CONFIG.copy_paper_gas_usd,
        trade_fee_bps=CONFIG.copy_paper_trade_fee_bps,
        conviction_base_usd=(CONFIG.copy_paper_conviction_base_usd
                             if CONFIG.copy_paper_conviction_base_usd > 0 else None),
        conviction_min=CONFIG.copy_paper_conviction_min,
        conviction_max=CONFIG.copy_paper_conviction_max,
        max_horizon_days=(CONFIG.strategy_4_long_horizon_days
                          if CONFIG.strategy_4_enabled else None),
        # B's OWN blacklist — never A's. A-demoted wallets are B's thesis edge.
        blacklist_provider=lambda: promotion_state.active_blacklist(scope="b"),
        strategy="B",
        detector_factory=detector_factory,
        exit_detector_factory=exit_detector_factory,
        on_cycle=_on_cycle,
    )
    n = len(runner.wallets())
    logger.info(
        f"Strategy-B paper book started (wallets={n}, "
        f"interval={CONFIG.copy_paper_interval_s}s, fill=their-price"
        f"+{CONFIG.copy_paper_b_slippage_bps}bps, caps "
        f"{CONFIG.copy_paper_b_max_per_wallet_day}/wallet-day "
        f"{CONFIG.copy_paper_b_max_per_category_day}/category-day, "
        f"PREVIEW measurement only)"
    )
    runner.run_forever(_shutdown_event)


def _ab_race_reporter_loop():
    """A-vs-B race reporter: daily snapshot + day-7 verdict memo, on a clock.

    Fires at AB_RACE_DAILY_UTC_HOUR every day (a known time, scheduled — never
    polled) with the compact race snapshot, and sends the full verdict memo once
    the era (B's first open) is AB_RACE_VERDICT_DAYS old. Verdict-once semantics
    survive restarts via a small state file. The memo self-invalidates when
    either book sat starved 48h+ (a lopsided week must not sound confident).

    Since 2026-07-25 (ROADMAP P0-3): the race runs on the CLEAN ERA only. The
    2026-07-18 verdict was won on the artifact fill model (A's +8.55% was
    gifted fills; at the target's own price it was -0.11%), so the state file
    now carries ``era_floor_ts`` and both books are scored only on fills after
    it. The snapshot carries the at-their-price ROI and the fill-health
    witness, so the next verdict cannot be won on fills either.
    """
    import time
    from datetime import datetime, timedelta, timezone

    from src.copy_trading import era_state
    from src.copy_trading.strategy_compare import (
        compare, format_snapshot, format_verdict)

    state_path = os.path.join(CONFIG.data_dir, "ab_race_state.json")

    while not _shutdown_event.is_set():
        # sleep to the NEXT daily fire time (known time, one wake per day)
        now_dt = datetime.now(timezone.utc)
        fire = now_dt.replace(hour=CONFIG.ab_race_daily_utc_hour, minute=0,
                              second=0, microsecond=0)
        if fire <= now_dt:
            fire += timedelta(days=1)
        if _shutdown_event.wait((fire - now_dt).total_seconds()):
            return
        try:
            # The clean-era floor: written explicitly at the P0-1 deploy; seeded
            # here as a fallback so a wiped state file still scopes the race to
            # post-fix fills. Never moved once set — the era's start is a fact.
            era_floor = era_state.seed_era_floor(state_path)
            cmp_ = compare(CONFIG.copy_paper_ledger, CONFIG.copy_paper_b_ledger,
                           b_slippage_bps=CONFIG.copy_paper_b_slippage_bps,
                           era_floor=era_floor)
            sent_snap = telegram_bot.send_message(format_snapshot(cmp_))
            # Log every reporter post — Telegram is the only other trace, and an
            # unauditable daily job reads as "never ran" from the logs.
            logger.info(
                f"[AB-RACE] daily snapshot {'sent' if sent_snap else 'SEND FAILED'} "
                f"(era_day={cmp_.get('era_days', 0):.1f}, "
                f"era_floor={era_floor:.0f})")
            st = era_state.load(state_path)
            era = cmp_.get("era_start")
            if (era and not st.get("verdict_sent")
                    and time.time() - era >= CONFIG.ab_race_verdict_days * 86400.0):
                sent = telegram_bot.send_message(
                    "🏁 <b>A-vs-B verdict memo</b>\n<pre>"
                    + format_verdict(cmp_) + "</pre>")
                logger.info(
                    f"[AB-RACE] day-7 verdict memo "
                    f"{'sent' if sent else 'SEND FAILED (will retry next fire)'}")
                if sent:
                    st["verdict_sent"] = True
                    st["verdict_ts"] = time.time()
                    era_state.save(state_path, st)
        except Exception as e:  # the reporter must never die — next fire retries
            logger.warning(f"[AB-RACE] daily report failed: {e}")


def _discovery_loop():
    """Continuously hunt for copyable wallets and feed them to the paper harness.

    Runs the funnel (universe -> robust skill -> lead-lag copyability) on a
    schedule. Each new qualifier is Telegram-pinged and written to the paper
    watchlist (auto-paper) so measurement starts while you analyze. Never places
    real orders and never edits the live `.env` tiers — promotion stays manual.
    """
    from src.copy_trading.discovery import DiscoveryConfig
    from src.copy_trading.discovery_runner import DiscoveryRunner

    cfg = DiscoveryConfig(
        category=CONFIG.wallet_discovery_category,
        universe=CONFIG.wallet_discovery_universe,
        skill_pool=CONFIG.wallet_discovery_skill_pool,
        watchlist_cap=CONFIG.wallet_discovery_cap,
        min_capture_cents=CONFIG.wallet_discovery_min_capture_cents,
        min_tstat=CONFIG.wallet_discovery_min_tstat,
        drop_capture_cents=CONFIG.wallet_discovery_drop_capture_cents,
        auto_remove=CONFIG.wallet_discovery_auto_remove,
        enabled_theories=frozenset(
            t.strip() for t in CONFIG.wallet_discovery_theories.split(",") if t.strip()),
        res_cache_dir=CONFIG.wallet_discovery_res_cache,
        copy_replay_gate=CONFIG.wallet_discovery_copy_replay_gate,
        min_copy_replay_n=CONFIG.wallet_discovery_min_copy_replay_n,
        min_copy_replay_roi=CONFIG.wallet_discovery_min_copy_replay_roi,
        fade_roi=CONFIG.wallet_discovery_fade_roi,
        max_tail_ratio=CONFIG.wallet_discovery_max_tail_ratio,
        max_curve_drawdown=CONFIG.wallet_discovery_max_curve_drawdown,
        max_hit_rate=CONFIG.wallet_discovery_max_hit_rate,
        min_curve_n=CONFIG.wallet_discovery_min_curve_n,
        s4_enabled=CONFIG.strategy_4_enabled,
        s4_long_horizon_days=CONFIG.strategy_4_long_horizon_days,
        s4_min_long_ratio=CONFIG.strategy_4_min_long_ratio,
        s4_min_dated_buys=CONFIG.strategy_4_min_dated_buys,
        s4_min_long_buys=CONFIG.strategy_4_min_long_buys,
        long_horizon_cap=CONFIG.strategy_4_cap,
        consensus_enabled=CONFIG.consensus_enabled,
        consensus_min_wallets=CONFIG.consensus_min_wallets,
        consensus_window_s=CONFIG.consensus_window_hours * 3600.0,
        consensus_min_usd=CONFIG.consensus_min_usd,
        consensus_cooldown_s=CONFIG.consensus_cooldown_hours * 3600.0,
    )
    runner = DiscoveryRunner(
        config=cfg,
        watchlist_path=CONFIG.copy_paper_watchlist,   # feeds the paper harness
        state_path=CONFIG.wallet_discovery_state,
        long_horizon_watchlist_path=CONFIG.wallet_discovery_long_horizon_watchlist,
        cache_dir=CONFIG.wallet_discovery_cache_dir,
        activity_ttl_s=CONFIG.wallet_discovery_activity_ttl_s,
        cycle_interval_s=CONFIG.wallet_discovery_interval_s,
        notify=lambda msg: telegram_bot.send_message(msg),
        llm_review_enabled=CONFIG.wallet_discovery_llm_review_enabled,
        llm_review_top_n=CONFIG.wallet_discovery_llm_review_top_n,
        llm_model=CONFIG.wallet_discovery_llm_model,
        holdout_frac=CONFIG.gate_holdout_frac,
        holdout_max_per_sweep=CONFIG.gate_holdout_max_per_sweep,
        failopen_alert_frac=CONFIG.gate_failopen_alert_frac,
        # Paper-evidence retention override (starvation RCA): None disables.
        paper_ledger_path=(CONFIG.copy_paper_ledger
                           if CONFIG.paper_proven_retention_enabled else None),
        paper_proven_min_n=CONFIG.paper_proven_min_n,
        paper_proven_min_roi=CONFIG.paper_proven_min_roi,
        # A-vs-B race: every wallet removed from the watchlist this sweep (cull,
        # retention drop, demote exclusion) is offered to strategy B with its
        # fresh replay stats — B-fit gating happens inside the router.
        on_removed=(
            (lambda w, ev: _cross_route_a_exit(
                w, ev, reason="removed from A watchlist (sweep)"))
            if CONFIG.copy_paper_b_enabled and CONFIG.copy_paper_b_cross_route
            else None),
    )
    runner.run_forever(_shutdown_event)


# -- Main --

def _setup_logging():
    """Configure logging to console and file.

    The file handler must ROLL at UTC midnight: the old plain FileHandler
    baked the start date into the filename once, so a long container run
    appended everything (incl. all WARNINGs and urllib3 DEBUG) to the boot
    day's file — bot-2026-07-10.log grew to 99MB over six days while the
    per-day files stayed near-empty, and the 07-15 FUNNEL-STALLED warning
    "lived in" the 07-10 file (2026-07-16 RCA). Reuse the same rolling
    handler the BotLogger files use.
    """
    from src.logger import _DailyRotatingFileHandler

    os.makedirs(CONFIG.logs_dir, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)

    # File handler — rolls to bot-<new-date>.log when the UTC date changes.
    # It shares that file with BotLogger's ops handler, and the app logger
    # ("poly_poly_bot") PROPAGATES to root — without the filter below every
    # app record would be written twice to the same file by two unsynchronized
    # handlers. Root keeps only third-party records (urllib3, web3, ...);
    # BotLogger already files the app's own records (bot- ops, signals- WARN+).
    fh = _DailyRotatingFileHandler(Path(CONFIG.logs_dir), "bot")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    fh.addFilter(lambda rec: not (rec.name == "poly_poly_bot"
                                  or rec.name.startswith("poly_poly_bot.")))

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(ch)
    root.addHandler(fh)


def _signal_handler(sig, frame):
    logger.info(f"Received signal {sig}, shutting down...")
    _shutdown_event.set()


async def main():
    """Main entry point — runs Strategy #1 plus its measurement harnesses."""
    _setup_logging()

    logger.info("=" * 60)
    logger.info("  Polymarket Copy-Trading Bot")
    logger.info(f"  Strategy #1 (Copy Trading): {'ENABLED' if CONFIG.strategy1_enabled else 'DISABLED'}")
    logger.info(f"  Preview mode: {CONFIG.preview_mode}")
    logger.info("=" * 60)

    # Register CLOB-client refresher so /setkey can rotate the in-memory
    # private key and have the singleton rebuilt.
    telegram_bot.on_refresh_clob_client = refresh_clob_client

    # Signal handlers
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Start Telegram polling
    if telegram_bot.is_configured():
        telegram_bot.start_polling()
        logger.info("Telegram bot started")
    else:
        logger.info("Telegram not configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)")

    # Startup notification
    telegram_bot.send_message(
        "<b>Bot Started</b>\n"
        f"Strategy #1 (Copy): {'ON' if CONFIG.strategy1_enabled else 'OFF'}\n"
        f"Mode: {'PREVIEW' if CONFIG.preview_mode else 'LIVE'}"
    )

    # Start the copy-paper validation harness (Strategy 1b) in a thread.
    # Measurement only — never places real orders — so it is always safe to run.
    if CONFIG.copy_paper_enabled:
        copy_paper_thread = threading.Thread(
            target=_copy_paper_loop, daemon=True, name="copy-paper"
        )
        copy_paper_thread.start()
        logger.info("Copy-paper harness thread started")

        # Strategy 4: the long-horizon paper book runs alongside the near-term
        # copier (same measurement-only guarantee), taking the far-future bets the
        # copier now skips. Gated on both copy-paper AND strategy_4 being enabled.
        if CONFIG.strategy_4_enabled:
            s4_paper_thread = threading.Thread(
                target=_s4_paper_loop, daemon=True, name="s4-paper"
            )
            s4_paper_thread.start()
            logger.info("S4 long-horizon paper book thread started")

        # Strategy B: the borrowed-clock (instant-copy) paper book races the
        # near-term copier above — same measurement-only guarantee, own ledger
        # and governance state. Gated on both books being enabled.
        if CONFIG.copy_paper_b_enabled:
            b_paper_thread = threading.Thread(
                target=_copy_paper_b_loop, daemon=True, name="copy-paper-b"
            )
            b_paper_thread.start()
            logger.info("Strategy-B paper book thread started")

            ab_reporter_thread = threading.Thread(
                target=_ab_race_reporter_loop, daemon=True, name="ab-race-reporter"
            )
            ab_reporter_thread.start()
            logger.info("A-vs-B race reporter thread started "
                        f"(daily {CONFIG.ab_race_daily_utc_hour:02d}:00 UTC, "
                        f"verdict at day {CONFIG.ab_race_verdict_days:.0f})")
    else:
        logger.info("Copy-paper harness disabled (set COPY_PAPER_ENABLED=true)")

    # Start the continuous wallet-discovery hunter (feeds the paper watchlist).
    # Measurement/selection only — never places real orders or edits live tiers.
    if CONFIG.wallet_discovery_enabled:
        discovery_thread = threading.Thread(
            target=_discovery_loop, daemon=True, name="wallet-discovery"
        )
        discovery_thread.start()
        logger.info("Wallet-discovery thread started")
    else:
        logger.info("Wallet discovery disabled (set WALLET_DISCOVERY_ENABLED=true)")

    # Start Strategy #1 (Copy Trading) natively via asyncio
    s1_crashed = False
    if CONFIG.strategy1_enabled:
        logger.info("Starting Strategy #1 (Copy Trading) via asyncio...")
        from src.copy_trading.runner import run_copy_trading
        try:
            # Run copy trading as the main async task; it blocks until shutdown.
            # The copy-paper and discovery harnesses run in daemon threads.
            await run_copy_trading()
        except KeyboardInterrupt:
            pass
        except Exception as e:
            logger.exception(f"Strategy #1 crashed: {e}")
            telegram_bot.send_message(f"Strategy #1 crashed: <code>{e}</code>\n<i>Bot continues — paper/discovery harnesses still running.</i>")
            s1_crashed = True
    else:
        logger.info("Strategy #1 disabled, skipping copy-trader bot")

    # Keep alive whenever Strategy #1 isn't the main task — either it's
    # disabled, or it crashed. The copy-paper and discovery harnesses run in
    # daemon threads and need the main thread to stay up so the container
    # doesn't exit.
    if not CONFIG.strategy1_enabled or s1_crashed:
        try:
            while not _shutdown_event.is_set():
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            pass

    # Shutdown
    logger.info("Shutting down...")
    _shutdown_event.set()
    telegram_bot.send_message("Bot shutting down.")
    telegram_bot.stop_polling()
    logger.info("Goodbye.")


if __name__ == "__main__":
    asyncio.run(main())
