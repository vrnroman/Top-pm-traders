"""Unified configuration for the copy-trading bot (Strategy #1)."""

from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv
from src.config_validators import parse_addresses, validate_private_key, validate_address

# Load .env from project root (poly_poly_bot/)
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)


def _required(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise ValueError(f"Missing required env var: {name}")
    return val


def _optional(name: str, fallback: str) -> str:
    return os.environ.get(name, "").strip() or fallback


def _opt_float(name: str, fallback: float) -> float:
    v = os.environ.get(name, "").strip()
    return float(v) if v else fallback


def _opt_int(name: str, fallback: int) -> int:
    v = os.environ.get(name, "").strip()
    return int(v) if v else fallback


def _opt_bool(name: str, fallback: bool) -> bool:
    v = os.environ.get(name, "").strip()
    if not v:
        return fallback
    return v.lower() == "true"


# --- Strategy 1 (Copy Trading) wallet loading ---

def _load_user_addresses() -> list[str]:
    """Load tracked wallets from USER_ADDRESSES or tiered strategy wallets."""
    raw = os.environ.get("USER_ADDRESSES", "").strip()
    if raw:
        addrs = parse_addresses(raw)
        for addr in addrs:
            validate_address(addr, "USER_ADDRESSES entry")
        return addrs
    tier1a = os.environ.get("STRATEGY_1A_WALLETS", "").strip()
    tier1b = os.environ.get("STRATEGY_1B_WALLETS", "").strip()
    if tier1a or tier1b:
        all_addrs: set[str] = set()
        if tier1a:
            for a in parse_addresses(tier1a):
                validate_address(a, "STRATEGY_1A_WALLETS")
                all_addrs.add(a)
        if tier1b:
            for a in parse_addresses(tier1b):
                validate_address(a, "STRATEGY_1B_WALLETS")
                all_addrs.add(a)
        return list(all_addrs)
    return []


def _load_private_key() -> str:
    """Load and validate private key, then remove from env."""
    raw = os.environ.get("PRIVATE_KEY", "").strip()
    if not raw:
        return ""
    validated = validate_private_key(raw)
    # Remove from env immediately — module const is the single source
    if "PRIVATE_KEY" in os.environ:
        del os.environ["PRIVATE_KEY"]
    return validated


_private_key = _load_private_key()


def get_private_key() -> str:
    """Access validated private key."""
    return _private_key


def set_private_key(raw: str) -> str:
    """Replace the in-memory private key at runtime.

    Used by the Telegram /setkey command as a safety lever: rotating to a
    different key (or clearing it with the empty string) immediately
    prevents the bot from signing further orders without redeploying.

    Returns the validated 64-hex key (or empty string when cleared).
    Raises ``ValueError`` on a bad hex.
    """
    global _private_key
    raw = (raw or "").strip()
    if not raw:
        _private_key = ""
        return ""
    _private_key = validate_private_key(raw)
    return _private_key


# Determine which strategies are enabled
_s1a_enabled = _opt_bool("STRATEGY_1A_ENABLED", bool(os.environ.get("STRATEGY_1A_WALLETS", "").strip()))
_s1b_enabled = _opt_bool("STRATEGY_1B_ENABLED", bool(os.environ.get("STRATEGY_1B_WALLETS", "").strip()))
_s1c_enabled = _opt_bool("STRATEGY_1C_ENABLED", False)
_strategy1_enabled = _s1a_enabled or _s1b_enabled or _s1c_enabled

_user_addresses = _load_user_addresses()

# Validate proxy wallet only when Strategy 1 is enabled
_proxy_wallet = ""
if _strategy1_enabled and os.environ.get("PROXY_WALLET", "").strip():
    _proxy_wallet = validate_address(_required("PROXY_WALLET"), "PROXY_WALLET")


class Config:
    """Unified configuration for all strategies."""

    # --- Strategy toggles ---
    strategy1_enabled: bool = _strategy1_enabled

    # --- Global ---
    preview_mode: bool = _opt_bool("PREVIEW_MODE", True)
    telegram_bot_token: str = _optional("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = _optional("TELEGRAM_CHAT_ID", "")

    # --- Strategy 1: Copy Trading ---
    user_addresses: list[str] = _user_addresses
    proxy_wallet: str = _proxy_wallet
    signature_type: int = _opt_int("SIGNATURE_TYPE", 0)

    copy_strategy: str = _optional("COPY_STRATEGY", "PERCENTAGE")
    copy_size: float = _opt_float("COPY_SIZE", 10.0)
    max_order_size_usd: float = _opt_float("MAX_ORDER_SIZE_USD", 100.0)
    min_order_size_usd: float = _opt_float("MIN_ORDER_SIZE_USD", 1.0)
    # Maximum slippage we accept on a live limit order, in basis points.
    # The order is posted at best_ask × (1 + slippage_bps/10000) for a BUY
    # (or best_bid × (1 - ...) for a SELL) and uses FAK so the unmatched
    # portion cancels rather than resting in the book. 50 bps = 0.5%.
    live_order_slippage_bps: int = _opt_int("LIVE_ORDER_SLIPPAGE_BPS", 50)
    max_position_per_market_usd: float = _opt_float("MAX_POSITION_PER_MARKET_USD", 500.0)
    max_daily_volume_usd: float = _opt_float("MAX_DAILY_VOLUME_USD", 1000.0)
    fetch_interval: float = _opt_int("FETCH_INTERVAL", 1) * 1.0  # seconds
    fetch_concurrency: int = max(1, _opt_int("FETCH_CONCURRENCY", 5))
    max_trade_age_hours: float = _opt_float("MAX_TRADE_AGE_HOURS", 1.0)
    max_price_drift_bps: int = _opt_int("MAX_PRICE_DRIFT_BPS", 300)
    max_spread_bps: int = _opt_int("MAX_SPREAD_BPS", 500)
    max_copies_per_market_side: int = _opt_int("MAX_COPIES_PER_MARKET_SIDE", 2)
    redeem_interval_hours: float = _opt_float("REDEEM_INTERVAL_HOURS", 0.5)
    # Paper-mode realization: in preview the on-chain redeemer is gated off, so
    # System-A copy positions would never realize. When enabled, the periodic
    # loop books realized P&L for resolved preview positions (and preview SELLs
    # book an early-exit P&L) so /pnl reflects a real paper track record.
    preview_realize_enabled: bool = _opt_bool("PREVIEW_REALIZE_ENABLED", True)
    trade_monitor_mode: str = _optional("TRADE_MONITOR_MODE", "data-api")

    # --- Copy-paper harness (Strategy 1b validation; PREVIEW-only, no orders) ---
    # Forward measurement of execution-realistic copy PnL on watchlist wallets.
    # Graduates a wallet to real capital only after positive net-of-drag PnL.
    # On by default: paper-only (places no real orders), so it's safe to run
    # everywhere. Set COPY_PAPER_ENABLED=false to stop it.
    copy_paper_enabled: bool = _opt_bool("COPY_PAPER_ENABLED", True)
    copy_paper_watchlist: str = _optional(
        "COPY_PAPER_WATCHLIST",
        str(Path(__file__).resolve().parent.parent / "data" / "copy_watchlist.json"),
    )
    copy_paper_ledger: str = _optional(
        "COPY_PAPER_LEDGER",
        str(Path(__file__).resolve().parent.parent / "data" / "copy_paper_ledger.jsonl"),
    )
    copy_paper_max_usd: float = _opt_float("COPY_PAPER_MAX_USD", 50.0)
    copy_paper_copy_pct: float = _opt_float("COPY_PAPER_COPY_PCT", 1.0)
    copy_paper_max_slippage_bps: int = _opt_int("COPY_PAPER_MAX_SLIPPAGE_BPS", 200)
    # Only copy a target BUY fresher than this. Was 6h (21600) — the lag-sweep
    # kill-test showed lag is NOT the main driver of the loss (copy-and-hold is
    # −EV even at zero lag), but filling a 6h-stale book is pure downside on real
    # money, and the live detection lag was a 2h p75 / 6h max under the old cap.
    # Cut to 1h: still generous for a 120s poll, without the multi-hour stale tail.
    copy_paper_max_age_s: float = _opt_float("COPY_PAPER_MAX_AGE_S", 3600.0)
    # Owner ruling 2026-07-16: 500 starved the A book of evidence — the new
    # detection-rejects histogram showed watchlist wallets betting below the
    # floor while A sat at zero opens for 48h+ (the voided race week). Lowered
    # to 300 for BOTH books (shared knob, so the A-vs-B race variable stays
    # lagged-vs-instant). The feed floor (COPY_PAPER_FEED_MIN_USD=100) already
    # sits below, so detection needs no change.
    copy_paper_min_usd: float = _opt_float("COPY_PAPER_MIN_USD", 300.0)
    # Poll cadence for the near-term copier. Dropped 120s -> 60s now that
    # detection runs off the shared global /trades feed (fixed cost regardless of
    # how many wallets are watched — see copy_paper_feed_detection), so we can
    # copy closer to real time. Lower is possible but each cycle also re-checks
    # every OPEN position for resolution (a Gamma call apiece), so the floor is a
    # trade-off, not free; 60s halves detection latency at ~2x the resolve load.
    copy_paper_interval_s: int = _opt_int("COPY_PAPER_INTERVAL_S", 60)
    # Shared-feed detection: instead of polling each watched wallet's /activity
    # (N calls/cycle — heavy and rate-limit-prone at 500 wallets), poll the global
    # data-api /trades feed ONCE per cycle and filter to watched wallets. Cost is
    # independent of wallet count, so the watchlist scales to hundreds. On by
    # default; set false to fall back to the legacy per-wallet detector.
    copy_paper_feed_detection: bool = _opt_bool("COPY_PAPER_FEED_DETECTION", True)
    # Cash floor for the shared feed's server-side filter (filterAmount). Trades
    # below this never enter the feed, bounding its size; set at/under
    # copy_paper_min_usd so no copyable BUY is filtered out, but low enough to
    # still catch a watched wallet's exits. The BUY min_usd gate still applies.
    copy_paper_feed_min_usd: float = _opt_float("COPY_PAPER_FEED_MIN_USD", 100.0)
    # Entry guardrails (cut the copies that historically leaked ROI). Reversible
    # via env; set a cap <= 0 to disable it. fill-gate: skip a copy whose
    # achievable fill is > this many bps from the target's price ON EITHER SIDE
    # (don't chase a moved book — and don't sweep a suspiciously cheap one:
    # two-sided since 2026-07-25, ROADMAP P0-1, because keeping only favourable
    # surprises manufactured the book's entire apparent edge). first-entry-only:
    # copy the opening trade per market, not
    # averaging-down adds. The per-day caps are the slate circuit-breaker —
    # one correlated same-day slate (the 82-copy World-Cup day that drove the
    # observed -25%) can't dominate the book.
    copy_paper_fill_gate_bps: int = _opt_int("COPY_PAPER_FILL_GATE_BPS", 150)
    copy_paper_first_entry_only: bool = _opt_bool("COPY_PAPER_FIRST_ENTRY_ONLY", True)
    copy_paper_max_per_wallet_day: int = _opt_int("COPY_PAPER_MAX_PER_WALLET_DAY", 3)
    copy_paper_max_per_category_day: int = _opt_int("COPY_PAPER_MAX_PER_CATEGORY_DAY", 8)
    # per-(wallet, event) concurrent-exposure cap (2026-07 race RCA: 0xa6fa lost
    # $345 on 3-4 same-match props that individually passed the day caps but
    # settled together). At most N OPEN copies of one wallet inside one event
    # (keyed by event slug); a resolution frees the slot. <=0 disables.
    copy_paper_max_per_wallet_event: int = _opt_int(
        "COPY_PAPER_MAX_PER_WALLET_EVENT", 1)
    # confidence-tiered stake, DOWNWARD only (same RCA: a band=low n=6 admit was
    # staked like a proven wallet): a wallet whose latest LLM-gate row says
    # confidence_band=low is staked at this fraction of normal until it has
    # low_conf_until_n settled copies in the book. Off when <=0 or >=1.
    copy_paper_low_conf_stake_frac: float = _opt_float(
        "COPY_PAPER_LOW_CONF_STAKE_FRAC", 0.5)
    copy_paper_low_conf_until_n: int = _opt_int("COPY_PAPER_LOW_CONF_UNTIL_N", 5)
    # Evidence-throughput levers (starvation RCA 2026-07): the slate caps above
    # are sized for a real-money book's correlation risk, but paper carries no
    # capital — so they were throttling promotion-evidence accrual (92.6% of all
    # copy attempts died on slate-cap while cold wallets sat at 0 copies).
    # PRIORITY processes each cycle coldest-wallet-first (same caps, same total
    # exposure — just routed to wallets that still need evidence). RELIEF lets a
    # wallet under the evidence floor exceed the CATEGORY cap up to the relief
    # ceiling; such fills are stamped over_real_cap in the ledger so promotion
    # review can audit them. Per-wallet cap always binds. RELIEF_EVIDENCE_N=0
    # disables relief; PRIORITY=false restores feed order.
    copy_paper_starved_priority: bool = _opt_bool("COPY_PAPER_STARVED_PRIORITY", True)
    copy_paper_relief_evidence_n: int = _opt_int("COPY_PAPER_RELIEF_EVIDENCE_N", 15)
    copy_paper_relief_max_per_category_day: int = _opt_int(
        "COPY_PAPER_RELIEF_MAX_PER_CATEGORY_DAY", 24)
    # Winning-markets-only gate (item A) + conviction sizing (item C). The
    # lag-sweep kill-test (backtest/copy_lag_backtest.py) proved copy-and-hold is
    # −EV in aggregate at ANY lag, but the loss is categorical, so the engine
    # copies a wallet only in the categories the watchlist marks approved for it
    # (its copy-and-hold edge cleared real-money spread there). Conviction sizing
    # replaces the flat $50 with size ∝ the target's bet vs its own median,
    # winsorized. Both ON by default — they only narrow/scale what we already do,
    # and the gate is what makes the paper book reproducible on real money.
    copy_paper_category_gate: bool = _opt_bool("COPY_PAPER_CATEGORY_GATE", True)
    copy_paper_conviction_base_usd: float = _opt_float("COPY_PAPER_CONVICTION_BASE_USD", 25.0)
    copy_paper_conviction_min: float = _opt_float("COPY_PAPER_CONVICTION_MIN", 0.25)
    copy_paper_conviction_max: float = _opt_float("COPY_PAPER_CONVICTION_MAX", 2.0)
    # Book-evidence gates (ROADMAP P1-6): a wallet with NO discovery-approved
    # stamp for a category may not copy into a slice this book's own ledger
    # already proves losing — >= N settled copies with realized ROI < 0, checked
    # per market category AND per entry-price bucket. The default flips from
    # "absent stamp -> don't block" to "absent -> require the slice unproven-or-
    # winning" (§1.5: sports is 333/415 copies and loses in both books; B's
    # 0.2-0.4 bucket is −61.5% at n=19). 15 catches the n=19 bucket and
    # everything bigger; 0 disables. Stamped wallets stay exempt (discovery
    # replay is the re-admission channel, so the block never deadlocks).
    copy_paper_category_evidence_min_n: int = _opt_int(
        "COPY_PAPER_CATEGORY_EVIDENCE_MIN_N", 15)
    # Evidence window for those gates: all-time by default — the pre-P0-1 fill
    # artifact flattered realized ROI UPWARD, so an all-time-losing slice is a
    # robust loser. Flip to clean-era-only later, once the era has accrued
    # enough settled rows for the gate to read.
    copy_paper_category_evidence_era_only: bool = _opt_bool(
        "COPY_PAPER_CATEGORY_EVIDENCE_ERA_ONLY", False)
    # Modeled real-money costs (ROADMAP P1-7): stamp every opened paper row with
    # gas (flat per trade) + trading fee (bps of spend) charged against realized
    # pnl, plus the category's full spread charged against the at-their-price
    # column (which otherwise assumes free fills). Fill mechanics untouched, so
    # the A-vs-B race variable stays lagged-vs-instant. Polymarket fees are ~0
    # and Polygon gas is cents — the spread is the honest cost.
    copy_paper_costs_enabled: bool = _opt_bool("COPY_PAPER_COSTS_ENABLED", True)
    copy_paper_gas_usd: float = _opt_float("COPY_PAPER_GAS_USD", 0.02)
    copy_paper_trade_fee_bps: float = _opt_float("COPY_PAPER_TRADE_FEE_BPS", 0.0)

    # --- Strategy B: the borrowed-clock (instant-copy) paper book ---
    # A SECOND paper book racing the one above (the 2026-07 A-vs-B experiment).
    # Same feed detection, same sizing, same promotion floors — one variable
    # changes: fills are recorded at the TARGET'S OWN price (+slippage bps)
    # instead of the live book minutes later, and there is no fill-gate censoring
    # (the counterfactual showed A's fill-gate admits only trades the market
    # moved against). Own ledger, own promotion/blacklist/offers stores
    # (scope "b"), own caps. A's demotions never filter B and vice versa.
    copy_paper_b_enabled: bool = _opt_bool("COPY_PAPER_B_ENABLED", True)
    copy_paper_b_ledger: str = _optional(
        "COPY_PAPER_B_LEDGER",
        str(Path(__file__).resolve().parent.parent / "data" / "copy_paper_ledger_b.jsonl"))
    copy_paper_b_slippage_bps: int = _opt_int("COPY_PAPER_B_SLIPPAGE_BPS", 100)
    # B's slate caps: looser than A's 3/8 because take-all IS the B thesis (the
    # counterfactual: capped B re-runs A's +6% regime; uncapped B is the +8%
    # regime whose gains concentrate in high-frequency wallets). Not unlimited —
    # a degenerate flood is still bounded. Cap binds are logged per wallet.
    copy_paper_b_max_per_wallet_day: int = _opt_int("COPY_PAPER_B_MAX_PER_WALLET_DAY", 25)
    # B's per-(wallet, event) cap — same default as A's so the race variable
    # stays lagged-vs-instant, not capped-vs-uncapped. <=0 disables.
    copy_paper_b_max_per_wallet_event: int = _opt_int(
        "COPY_PAPER_B_MAX_PER_WALLET_EVENT", 1)
    copy_paper_b_max_per_category_day: int = _opt_int(
        "COPY_PAPER_B_MAX_PER_CATEGORY_DAY", 75)
    # Extra wallets only B watches (beyond the shared discovery watchlist):
    # cross-routed A-demotions that are B-fit, plus the initial seed below.
    copy_paper_b_extra_watchlist: str = _optional(
        "COPY_PAPER_B_EXTRA_WATCHLIST",
        str(Path(__file__).resolve().parent.parent / "data" / "copy_watchlist_b_extra.json"))
    # Seeded into the extras file at first startup (comma-separated addresses).
    # Default: 0x161a… — auto-demoted from A (-21% realized under lagged fills)
    # but +9.1% over 99 trades under B's instant-copy counterfactual; the owner's
    # "demoted wallets which are good for the other strategy" case.
    copy_paper_b_seed_wallets: str = _optional(
        "COPY_PAPER_B_SEED_WALLETS",
        "0x161a7f666ca49d592848cf415b42f49a84714103")
    # Cross-route: when a wallet leaves A's ecosystem (governance demote,
    # discovery cull, retention drop), add it to B's extras if its replay or
    # B-ledger record clears the B-fit bar (replay n>=min_n & roi>=min_roi, or
    # B-ledger n>=5 & roi>0). Provenance-stamped; B's own blacklist still binds.
    copy_paper_b_cross_route: bool = _opt_bool("COPY_PAPER_B_CROSS_ROUTE", True)
    # A-vs-B race reporter: a daily snapshot at a KNOWN time (the owner's
    # scheduler principle: schedule the moment, never poll), and the verdict
    # memo once the era is this many days old. The era starts at B's first open.
    ab_race_daily_utc_hour: int = _opt_int("AB_RACE_DAILY_UTC_HOUR", 8)
    ab_race_verdict_days: float = _opt_float("AB_RACE_VERDICT_DAYS", 7.0)

    # --- Auto promote / demote governance (paper measurement -> action) ---
    # A background pass over the System-B paper ledger each copy cycle:
    #   * PROMOTE-offer a wallet once it has >= copy_promote_min_settled resolved
    #     copies AND copy ROI >= copy_promote_min_roi — sent to Telegram with a
    #     one-tap button; tapping adds it to the runtime promoted store (a wallet
    #     can then trade in System A at promote_default_tier, still PREVIEW).
    #   * DEMOTE (blacklist + drop from the watchlist) a wallet once it has
    #     >= copy_demote_min_settled resolved copies AND copy ROI <=
    #     copy_demote_max_roi, for copy_demote_cooldown_days (so it doesn't
    #     immediately re-qualify next sweep).
    # Advisory for promotion (you approve), automatic for demotion. On by default;
    # paper-only — promotion never moves real money (PREVIEW_MODE still gates that).
    copy_governance_enabled: bool = _opt_bool("COPY_GOVERNANCE_ENABLED", True)
    copy_promote_min_settled: int = _opt_int("COPY_PROMOTE_MIN_SETTLED", 15)
    copy_promote_min_roi: float = _opt_float("COPY_PROMOTE_MIN_ROI", 0.10)
    copy_demote_min_settled: int = _opt_int("COPY_DEMOTE_MIN_SETTLED", 15)
    copy_demote_max_roi: float = _opt_float("COPY_DEMOTE_MAX_ROI", -0.05)
    copy_demote_cooldown_days: float = _opt_float("COPY_DEMOTE_COOLDOWN_DAYS", 30.0)
    # --- Trustworthy promotion gate (statistical floor before an offer fires) ---
    # The bare n+ROI bar above is necessary but not sufficient: 15 bets at +10%
    # can be a decaying edge or 15 correlated bets on one market. Before an offer is
    # surfaced, promotion_gate.evaluate_floor also requires:
    #   * the chronological second-half ROI >= this floor (edge not reversed) — a
    #     small negative tolerance so a high-variance longshot's noisy recent half
    #     isn't read as decay; it catches a genuine reversal (+30% then -25%),
    #   * the settled bets spread across >= min_conditions markets OR
    #     >= min_categories categories (independent, not one bet measured N times),
    #   * a per-bet RETURN t-stat >= this. It is on the RETURN, never the win rate,
    #     so it never structurally penalizes a +EV longshot theory (which wins
    #     <50% by design). DEFAULT 0.0 = surfaced-and-tunable, not a hard floor:
    #     a positive value adds favorite-grade significance rigor but, because a
    #     high-variance longshot has low signal-per-bet, it needs many more samples
    #     to clear a positive t-stat — raise it only if you want that trade-off.
    copy_promote_min_tstat: float = _opt_float("COPY_PROMOTE_MIN_TSTAT", 0.0)
    copy_promote_min_second_half_roi: float = _opt_float("COPY_PROMOTE_MIN_SECOND_HALF_ROI", -0.10)
    copy_promote_min_conditions: int = _opt_int("COPY_PROMOTE_MIN_CONDITIONS", 8)
    copy_promote_min_categories: int = _opt_int("COPY_PROMOTE_MIN_CATEGORIES", 3)
    # Advisory Claude review layered on TOP of the statistical floor — it annotates
    # the offer with a promote/watch/reject read but never blocks one (the floor is
    # the block). ON by default: it's paper-side and advisory (fail-open), the
    # `claude -p` call runs on the subscription (CLAUDE_CODE_OAUTH_TOKEN, no metered
    # cost) and only fires on the rare candidate that clears the floor — so there's
    # nothing to gain by shipping it off. Set false to silence it.
    copy_promote_llm_review: bool = _opt_bool("COPY_PROMOTE_LLM_REVIEW", True)
    # --- Symmetric demote rigor (don't blacklist on small-sample noise) ---
    # A demote also needs a real absolute dollar loss (not a few cents of micro-
    # capital variance) AND a win rate that doesn't hold up (Wilson LB <= this).
    copy_demote_min_abs_loss: float = _opt_float("COPY_DEMOTE_MIN_ABS_LOSS", 5.0)
    copy_demote_max_wilson: float = _opt_float("COPY_DEMOTE_MAX_WILSON", 0.50)
    # Tier a one-tap-promoted wallet joins in System A (1a larger / 1b smaller).
    promote_default_tier: str = _optional("PROMOTE_DEFAULT_TIER", "1b")
    # --- /golive pre-flip gate ---
    # The manual PREVIEW_MODE=false flip is the ONLY step between a promoted wallet
    # and real money. `/golive <wallet>` re-checks the wallet live before that flip:
    # a DOUBLED settled bar, still-positive paper ROI now, recent activity, and the
    # promotion floor still holding. Advisory — it prints READY/HOLD, it never flips.
    copy_golive_min_settled: int = _opt_int("COPY_GOLIVE_MIN_SETTLED", 30)
    copy_golive_max_idle_days: float = _opt_float("COPY_GOLIVE_MAX_IDLE_DAYS", 14.0)
    copy_golive_min_roi: float = _opt_float("COPY_GOLIVE_MIN_ROI", 0.0)
    # Honest-metrics go-live floors (owner ruling 2026-07-25, desk item from the
    # P0 trust run): the go-live gate ALSO requires the at-their-price ROI and
    # the book's split-half persistence, both evaluated on CLEAN-ERA (post-P0-1)
    # fills — the realized-only checks it sits next to were reading the metric
    # the fill artifact inflated (A: +8.55% realized vs -0.11% at-price). Floor
    # values mirror the realized ones and are uncalibrated (ideal ROI is
    # structurally lower, so the same number is a stricter gate); recalibrate
    # at the 08-22 kill-criterion verdict. Both checks FAIL CLOSED while the
    # clean era is too young to measure. Master toggle false = checks off.
    copy_golive_honest_metrics: bool = _opt_bool("COPY_GOLIVE_HONEST_METRICS", True)
    copy_golive_min_ideal_roi: float = _opt_float("COPY_GOLIVE_MIN_IDEAL_ROI", 0.0)
    # Minimum CLEAN-ERA settled copies before the ideal-ROI check can pass at
    # all — one lucky settle must not clear the real-money gate (2026-07-27
    # review). Mirrors the repo's thin-sample band (MATURITY_THIN=5): below it,
    # an ROI is noise, not signal.
    copy_golive_min_ideal_settled: int = _opt_int("COPY_GOLIVE_MIN_IDEAL_SETTLED", 5)
    copy_golive_min_split_half_corr: float = _opt_float(
        "COPY_GOLIVE_MIN_SPLIT_HALF_CORR", 0.0)
    # Edge-triggered Telegram ping when a promoted wallet crosses (or drops
    # back off) the full /golive bar, so readiness never has to be polled by
    # hand (owner ask 2026-07-17). Advisory only — never flips anything.
    copy_golive_alert_enabled: bool = _opt_bool("COPY_GOLIVE_ALERT_ENABLED", True)
    # --- Probation fast-track (RCA 2026-07, rec 2a) ---
    # A wallet with STRONG own-history copy-and-hold replay (already computed by
    # discovery) AND a small AGREEING forward-paper sample gets an EARLY promote
    # offer at a lower settled bar, tagged tier "probation". This only lowers the
    # OFFER bar — the /golive real-money gate above is untouched, so probation never
    # weakens what real capital must clear. Tap-to-accept only, never auto-accepted.
    copy_probation_enabled: bool = _opt_bool("COPY_PROBATION_ENABLED", True)
    copy_probation_min_settled: int = _opt_int("COPY_PROBATION_MIN_SETTLED", 5)
    copy_probation_min_replay_n: int = _opt_int("COPY_PROBATION_MIN_REPLAY_N", 20)
    copy_probation_min_replay_roi: float = _opt_float("COPY_PROBATION_MIN_REPLAY_ROI", 0.05)
    copy_probation_min_replay_tstat: float = _opt_float("COPY_PROBATION_MIN_REPLAY_TSTAT", 2.0)
    # --- Dead-band time-box (RCA 2026-07, rec 2b) ---
    # A wallet stuck inconclusive (enough settled copies, ROI between the demote and
    # promote lines, never promotable nor demotable) past the observation window is
    # NEUTRALLY retired: removed from the watchlist so it stops squatting a slot, but
    # NOT blacklisted (it isn't a proven loser) — re-discoverable once the retire
    # window lapses. Distinct from the punitive auto-demote.
    copy_time_box_enabled: bool = _opt_bool("COPY_TIME_BOX_ENABLED", True)
    copy_time_box_window_days: float = _opt_float("COPY_TIME_BOX_WINDOW_DAYS", 45.0)
    copy_retire_cooldown_days: float = _opt_float("COPY_RETIRE_COOLDOWN_DAYS", 45.0)

    # --- Wallet discovery (continuously hunts copyable wallets -> paper) ---
    # Runs the discovery funnel on a schedule; pings Telegram on each new
    # qualifier and adds it to the paper watchlist. Never touches live capital.
    # On by default: discovery only writes a paper watchlist + Telegram pings,
    # never touches live capital. Set WALLET_DISCOVERY_ENABLED=false to stop it.
    wallet_discovery_enabled: bool = _opt_bool("WALLET_DISCOVERY_ENABLED", True)
    # Wide insider sweep: scan a large universe (take whatever the trade feed
    # yields up to this cap) on a slow multi-day cadence. The funnel paces its
    # requests (WALLET_DISCOVERY_PAGE_PAUSE_S / _BATCH_PAUSE_S) to stay under the
    # 429 ceiling and streams the scoring in chunks so RAM stays bounded.
    wallet_discovery_interval_s: int = _opt_int("WALLET_DISCOVERY_INTERVAL_S", 21600)  # 6h (ROADMAP P1-1: 4x/day)
    wallet_discovery_universe: int = _opt_int("WALLET_DISCOVERY_UNIVERSE", 200000)
    # Deep-eval pool per sweep. 400 (was 40, ROADMAP P1-1): a 40-wallet pool
    # re-ranked the same ~43 wallets every day (new=0..3/sweep — not a search).
    # Feasible on the e2-small only because the disk caches (wcache/rescache)
    # are alive again — see discovery_data.evaluate_sweep's makedirs.
    wallet_discovery_skill_pool: int = _opt_int("WALLET_DISCOVERY_SKILL_POOL", 400)
    # Paper watchlist size cap. 500 (was 25) so a single weak wallet can't squat a
    # scarce slot and starve out stronger candidates — discovery is paper-only, so
    # a wide shortlist just measures more wallets in parallel. The shared-feed
    # detector (copy_paper_feed_detection) makes per-cycle detection cost flat in
    # the wallet count, so a big watchlist no longer multiplies API load.
    wallet_discovery_cap: int = _opt_int("WALLET_DISCOVERY_CAP", 500)
    wallet_discovery_min_capture_cents: float = _opt_float("WALLET_DISCOVERY_MIN_CAPTURE_CENTS", 1.5)
    wallet_discovery_min_tstat: float = _opt_float("WALLET_DISCOVERY_MIN_TSTAT", 10.0)
    wallet_discovery_drop_capture_cents: float = _opt_float("WALLET_DISCOVERY_DROP_CAPTURE_CENTS", 1.0)
    wallet_discovery_auto_remove: bool = _opt_bool("WALLET_DISCOVERY_AUTO_REMOVE", True)
    wallet_discovery_category: str = _optional("WALLET_DISCOVERY_CATEGORY", "ALL")
    # Copy-replay selection gate: score each candidate on OUR copy action (copy
    # its copyable BUYs, hold to resolution) and DROP wallets whose measured
    # copy-and-hold edge is proven-negative, regardless of theory — so selection
    # measures the same action the live harness takes. Ranks copy-validated
    # wallets first. Set the gate false to fall back to the legacy theory rank.
    wallet_discovery_copy_replay_gate: bool = _opt_bool("WALLET_DISCOVERY_COPY_REPLAY_GATE", True)
    wallet_discovery_min_copy_replay_n: int = _opt_int("WALLET_DISCOVERY_MIN_COPY_REPLAY_N", 12)
    # Copy-and-hold ROI/$ bar to qualify (proven-positive) / not be dropped
    # (proven-negative). Raised 0.0 -> +0.02 (RCA 2026-07) so replay ROI is a real
    # cost-covering bar, not just "not a net-loser": a wallet at [0, 0.02) copy ROI
    # is now dropped rather than ranked first. Independent of the real-money promote
    # floor (copy_promote_min_roi=0.10) — this gates paper *discovery* only.
    wallet_discovery_min_copy_replay_roi: float = _opt_float("WALLET_DISCOVERY_MIN_COPY_REPLAY_ROI", 0.02)
    wallet_discovery_fade_roi: float = _opt_float("WALLET_DISCOVERY_FADE_ROI", -0.10)
    # Money-curve gates (RCA 2026-07): reject the near-$1 settlement-lag scooper
    # the legacy t-stat>=10 bar was admitting. Shipped ON at conservative
    # thresholds; each is a reversible env override (set drawdown huge + hit 1.0 to
    # disable). Both curve gates require >= MIN_CURVE_N resolved closed positions
    # first so a thin book is not rejected on a noisy curve.
    wallet_discovery_max_tail_ratio: float = _opt_float("WALLET_DISCOVERY_MAX_TAIL_RATIO", 0.4)
    wallet_discovery_max_curve_drawdown: float = _opt_float("WALLET_DISCOVERY_MAX_CURVE_DRAWDOWN", 1.5)
    wallet_discovery_max_hit_rate: float = _opt_float("WALLET_DISCOVERY_MAX_HIT_RATE", 0.95)
    wallet_discovery_min_curve_n: int = _opt_int("WALLET_DISCOVERY_MIN_CURVE_N", 20)
    # Paper-evidence retention override (starvation RCA 2026-07): wallets whose
    # REALIZED paper-copy record clears these floors are force-included in the
    # sweep and qualify while swept, bypassing the PREDICTIVE own-history gates
    # (tail/drawdown/hit/category) — retention was blind to paper results, so the
    # best earners (+80% ROI on settled copies) decayed off mid-accrual and the
    # promotion funnel starved. Proven-NEGATIVE signals still bind (auto-demote
    # blacklist, replay proven-negative), status is recomputed from the ledger
    # every sweep (never sticky), and re-entries still face the Claude gate WITH
    # their realized record in the dossier. Shipped ON (paper-side, reversible);
    # set PAPER_PROVEN_RETENTION_ENABLED=false to restore legacy behaviour.
    paper_proven_retention_enabled: bool = _opt_bool("PAPER_PROVEN_RETENTION_ENABLED", True)
    paper_proven_min_n: int = _opt_int("PAPER_PROVEN_MIN_N", 5)
    paper_proven_min_roi: float = _opt_float("PAPER_PROVEN_MIN_ROI", 0.0)
    # Dead-funnel alarm (starvation RCA 2026-07): WARN + one Telegram ping per
    # window when the paper harness has opened NOTHING for this many hours while
    # the watchlist is non-empty — a silent stall stops promotion evidence from
    # accruing, which is how the funnel starved invisibly. 0 disables.
    copy_paper_stall_alarm_hours: float = _opt_float("COPY_PAPER_STALL_ALARM_HOURS", 24.0)
    # Consensus-of-sharps signal (signal-only, no capital). When >= N independent
    # copy-validated wallets BUY the same (market, outcome) within the window, the
    # discovery sweep emits a Telegram signal (no fill -> no slippage). On by
    # default — the kill-test showed single-wallet copy-and-hold is -EV, but
    # cross-wallet agreement is a slower, more reproducible object worth surfacing.
    consensus_enabled: bool = _opt_bool("CONSENSUS_ENABLED", True)
    consensus_min_wallets: int = _opt_int("CONSENSUS_MIN_WALLETS", 3)
    consensus_window_hours: float = _opt_float("CONSENSUS_WINDOW_HOURS", 24.0)
    consensus_min_usd: float = _opt_float("CONSENSUS_MIN_USD", 500.0)
    consensus_cooldown_hours: float = _opt_float("CONSENSUS_COOLDOWN_HOURS", 12.0)
    # Claude gate (Strategy 1c): the final qualitative check before a NEWLY
    # qualified wallet is admitted to the paper watchlist. After the statistical
    # funnel passes, Claude vets a compact dossier and a "skip" verdict blocks
    # admission. Runs via the `claude -p` CLI on the Claude subscription
    # (CLAUDE_CODE_OAUTH_TOKEN) — no ANTHROPIC_API_KEY needed. Off by default;
    # fail-open (any LLM failure admits the wallet so a broken CLI never freezes
    # discovery). top_n caps how many new wallets are gated per sweep.
    wallet_discovery_llm_review_enabled: bool = _opt_bool("WALLET_DISCOVERY_LLM_REVIEW_ENABLED", False)
    wallet_discovery_llm_review_top_n: int = _opt_int("WALLET_DISCOVERY_LLM_REVIEW_TOP_N", 20)
    wallet_discovery_llm_model: str = _optional("WALLET_DISCOVERY_LLM_MODEL", "claude-opus-4-8")
    # Gate self-calibration holdout (ROADMAP P1-5b, Phase 1): to measure whether the LLM
    # shortlist gate is actually +EV, occasionally admit a wallet it would SKIP,
    # flag the gate-history row holdout:true (keeping the original skip verdict),
    # and let the paper harness accrue its outcome — so a later job can compare
    # would-have-rejected ROI vs admitted ROI instead of only ever measuring the
    # wallets we let in (a selection-biased self-congratulation loop). ON by
    # default (frac 0.1): it's paper-side and reversible, and the counterfactual it
    # accrues is the only way Phase 2 can ever prove the gate is +EV — so the clock
    # should be running. Exposure is capped per sweep (max 2) by construction, since
    # it is deliberately admitting wallets the gate thinks are bad. Set frac 0 to stop.
    gate_holdout_frac: float = _opt_float("GATE_HOLDOUT_FRAC", 0.1)
    gate_holdout_max_per_sweep: int = _opt_int("GATE_HOLDOUT_MAX_PER_SWEEP", 2)
    # P1-4: sweep-level fail-open alarm — WARNING + Telegram when the share of
    # gate calls admitting a wallet UNVETTED (error fail-open or exhausted
    # re-check) tops this fraction (with >= 3 calls that sweep). 0 disables.
    gate_failopen_alert_frac: float = _opt_float("GATE_FAILOPEN_ALERT_FRAC", 0.2)
    # Independent strategy theories that may qualify a wallet (OR'd). All ten on
    # by default — discovery is paper-only, so each theory proves out on measured
    # paper PnL before any manual promotion. 1a/1e need market-resolution data,
    # fetched on demand in the sweep. See research/THEORY_FINDINGS.md.
    wallet_discovery_theories: str = _optional(
        "WALLET_DISCOVERY_THEORIES", "1a,1b,1c,1d,1e,1f,1g,1h,1i,1j")
    # Activity-cache TTL: set ABOVE the sweep interval so a returning wallet is
    # served from cache instead of re-fetched (TTL < interval would expire every
    # time and never hit). 30h vs a 24h sweep. prune_cache deletes files older
    # than this, so it doubles as the disk-eviction horizon. Universe defaults to
    # wallets active in the last WALLET_DISCOVERY_UNIVERSE_WINDOW_S (24h); set
    # WALLET_DISCOVERY_EXPAND_FILTERS=true for a wider (BUY/SELL × taker) sweep.
    wallet_discovery_activity_ttl_s: int = _opt_int("WALLET_DISCOVERY_ACTIVITY_TTL_S", 108000)  # 30h
    wallet_discovery_cache_dir: str = _optional(
        "WALLET_DISCOVERY_CACHE_DIR",
        str(Path(__file__).resolve().parent.parent / "data" / "wcache"),
    )
    # Market-resolution cache (how each market settled) — feeds theories 1a/1e.
    # Resolved markets are immutable, so this grows slowly and is reused forever.
    wallet_discovery_res_cache: str = _optional(
        "WALLET_DISCOVERY_RES_CACHE",
        str(Path(__file__).resolve().parent.parent / "data" / "rescache"),
    )
    wallet_discovery_state: str = _optional(
        "WALLET_DISCOVERY_STATE",
        str(Path(__file__).resolve().parent.parent / "data" / "discovery_state.json"),
    )
    # Strategy 4 — long-horizon bet tracking. The Strategy-1 funnel scores
    # wallets on provably-closed markets, so it can't judge a wallet whose bets
    # resolve 6+ months out (nothing closes for a long time). When enabled, the
    # sweep classifies each candidate by how early it bets before resolution
    # (fetching end dates for its still-OPEN markets too, an extra Gamma cost)
    # and ALSO lists wallets with a real long book on a separate long-horizon
    # watchlist — dual membership, not a partition: a wallet still flows through
    # the copy funnel on its near-term bets, and only its far-future bets are
    # routed (live, per bet) to the Strategy-4 paper book. Off by default.
    strategy_4_enabled: bool = _opt_bool("STRATEGY_4_ENABLED", False)
    strategy_4_long_horizon_days: float = _opt_float("STRATEGY_4_LONG_HORIZON_DAYS", 180.0)
    strategy_4_min_long_ratio: float = _opt_float("STRATEGY_4_MIN_LONG_RATIO", 0.5)
    strategy_4_min_dated_buys: int = _opt_int("STRATEGY_4_MIN_DATED_BUYS", 5)
    # A wallet joins the long-horizon watchlist once it has this many distinct
    # long-horizon buys, independent of the copy funnel (dual membership).
    strategy_4_min_long_buys: int = _opt_int("STRATEGY_4_MIN_LONG_BUYS", 3)
    strategy_4_cap: int = _opt_int("STRATEGY_4_CAP", 25)
    wallet_discovery_long_horizon_watchlist: str = _optional(
        "WALLET_DISCOVERY_LONG_HORIZON_WATCHLIST",
        str(Path(__file__).resolve().parent.parent / "data" / "long_horizon_watchlist.json"),
    )
    # Strategy-4 paper book: paper-trades the long-horizon bets routed to it,
    # marking them to market over the months until they resolve. Separate ledger
    # and a smaller per-bet cap than the near-term copier (capital locks up for
    # months). Gated on strategy_4_enabled AND copy_paper_enabled. The near-term
    # copier, when strategy_4_enabled, SKIPS bets at/over the horizon cut so they
    # are no longer short-copied — they belong to this book instead.
    strategy_4_paper_ledger: str = _optional(
        "STRATEGY_4_PAPER_LEDGER",
        str(Path(__file__).resolve().parent.parent / "data" / "s4_paper_ledger.jsonl"),
    )
    strategy_4_paper_max_usd: float = _opt_float("STRATEGY_4_PAPER_MAX_USD", 25.0)
    strategy_4_paper_interval_s: int = _opt_int("STRATEGY_4_PAPER_INTERVAL_S", 600)
    strategy_4_paper_min_usd: float = _opt_float("STRATEGY_4_PAPER_MIN_USD", 500.0)

    # --- APIs ---
    clob_api_url: str = _optional("CLOB_API_URL", "https://clob.polymarket.com")
    data_api_url: str = _optional("DATA_API_URL", "https://data-api.polymarket.com")
    # polygon-rpc.com started returning 401 ("API key disabled, tenant disabled")
    # in 2026-06, which spammed the balance fetch every cycle. publicnode is a
    # keyless public Polygon RPC (full eth_call support) — override via RPC_URL.
    rpc_url: str = _optional("RPC_URL", "https://polygon-bor-rpc.publicnode.com")
    etherscan_api_key: str = _optional("ETHERSCAN_API_KEY", "")
    chain_id: int = 137

    # --- Directories ---
    data_dir: str = str(Path(__file__).resolve().parent.parent / "data")
    cache_dir: str = str(Path(__file__).resolve().parent.parent / "cache")
    results_dir: str = str(Path(__file__).resolve().parent.parent / "results")
    logs_dir: str = str(Path(__file__).resolve().parent.parent / "logs")


CONFIG = Config()
