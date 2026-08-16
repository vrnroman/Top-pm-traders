"""In-bot runner for the forward paper-copy harness (Strategy 1b).

Wraps `CopyPaperEngine` with watchlist loading and a cycle loop so `main.py`
can start it as a daemon thread. It reloads the watchlist each cycle (so
regenerating the watchlist file takes effect without a restart) and places NO
real orders — it only accumulates the paper ledger whose net-of-drag PnL gates
real capital.

Live data access (detection/books/resolution) is injected from
`copy_paper_live` by default but overridable for tests.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from src.copy_trading import era_state
from src.copy_trading import promotion_state
from src.copy_trading.copy_cost import CostModel
from src.copy_trading.copy_paper import CopyPaperEngine, CycleSummary, PaperCopyLedger
from src.copy_trading.copy_paper_live import (
    fetch_asks,
    fetch_bids,
    load_watchlist_categories,
    load_watchlist_flagged_by,
    load_watchlist_median_usd,
    load_watchlist_wallets,
    make_detector,
    make_exit_detector,
    make_horizon_resolver,
    resolve,
)


class CopyPaperRunner:
    def __init__(
        self,
        *,
        ledger_path: str,
        watchlist_path: Optional[str] = None,
        wallets: Optional[list[str]] = None,
        max_copy_usd: float = 50.0,
        copy_pct: float = 1.0,
        max_slippage_bps: int = 200,
        max_age_s: float = 21600,
        min_usd: float = 500.0,
        cycle_interval_s: int = 120,
        # entry guardrails (forwarded to the engine; see CopyPaperEngine)
        fill_gate_bps: Optional[int] = None,
        first_entry_only: bool = False,
        max_copies_per_wallet_day: Optional[int] = None,
        max_copies_per_category_day: Optional[int] = None,
        # per-(wallet, event) concurrent-exposure cap (see CopyPaperEngine —
        # correlated same-match props settle together; None = off).
        max_copies_per_wallet_event: Optional[int] = None,
        # --- confidence-tiered stake (downward only) ---
        # A wallet whose LATEST LLM-gate row has confidence_band == "low" is
        # staked at ``low_conf_stake_frac`` of normal until it has
        # ``low_conf_until_n`` settled copies in THIS book's ledger. Reads
        # ``gate_history_path`` each cycle (same reload-without-restart contract
        # as the watchlist). Fraction None/<=0/>=1 = off.
        low_conf_stake_frac: Optional[float] = None,
        low_conf_until_n: int = 5,
        gate_history_path: Optional[str] = None,
        # --- winning-markets-only gate (item A) + conviction sizing (item C) ---
        # When ``category_gate`` is on, copy a wallet's BUY only in the categories
        # the watchlist marks approved for it (its copy-and-hold edge cleared
        # real-money cost there). When ``conviction_base_usd`` is set, size each
        # copy to the target's conviction (their bet vs its own median, winsorized
        # to [conviction_min, conviction_max] of the base). Both default-off so
        # the runner is unchanged until config switches them on.
        category_gate: bool = False,
        conviction_base_usd: Optional[float] = None,
        conviction_min: float = 0.25,
        conviction_max: float = 2.0,
        # evidence-throughput levers (starvation RCA 2026-07; forwarded to the
        # engine, both default-off — see CopyPaperEngine for semantics)
        starved_priority: bool = False,
        relief_evidence_n: Optional[int] = None,
        relief_max_per_category_day: Optional[int] = None,
        # --- bet-horizon routing (Strategy 1 near-term vs 4 long-horizon) ---
        # When set, every detected BUY is routed by its market's resolution date.
        # The near-term book passes ``max_horizon_days`` (skip far-future bets);
        # the long-horizon book passes ``min_horizon_days`` (take only them) plus a
        # ``mark_fetcher`` and ``strategy="4"``. Both None = horizon-blind (the
        # original copier). ``extra_watchlist_paths`` unions extra watchlists into
        # the watched set (the long-horizon book watches S1 ∪ S4 wallets).
        min_horizon_days: Optional[float] = None,
        max_horizon_days: Optional[float] = None,
        mark_fetcher: Optional[Callable[[str], Optional[float]]] = None,
        strategy: str = "1",
        extra_watchlist_paths: Optional[list[str]] = None,
        # borrowed-clock fill (strategy B) — forwarded to the engine; None keeps
        # the live-book walk (see CopyPaperEngine.fill_at_their_price_bps).
        fill_at_their_price_bps: Optional[int] = None,
        # --- P1-6 book-evidence gates + P1-7 modeled costs (forwarded) ---
        # Unstamped wallets are blocked from slices (market category, entry-price
        # bucket) THIS book's ledger proves losing (>= min_n settled, ROI < 0).
        # era_only scopes the evidence to clean-era rows via era_state_path.
        category_evidence_min_n: Optional[int] = None,
        category_evidence_era_only: bool = False,
        era_state_path: Optional[str] = None,
        # When on, each opened row is stamped with modeled real-money costs:
        # gas + fee against realized pnl, plus the category spread against the
        # at-their-price column (see CopyPaperEngine). Fill mechanics untouched.
        costs_enabled: bool = False,
        gas_usd_per_trade: float = 0.0,
        trade_fee_bps: float = 0.0,
        # which auto-demote blacklist binds this book. Default (None) reads the
        # legacy global store; a per-strategy book passes its own scoped reader
        # so strategy A's demotions never filter strategy B's watchlist (a wallet
        # demoted under one fill regime may be the other regime's edge).
        blacklist_provider: Optional[Callable[[], set]] = None,
        # injectable dependencies (defaults are the live ones)
        detector_factory: Optional[Callable[..., Callable]] = None,
        book_fetcher: Optional[Callable[[str], list[tuple[float, float]]]] = None,
        resolver: Optional[Callable[[str], Optional[int]]] = None,
        exit_detector_factory: Optional[Callable[[list[str], float], Callable]] = None,
        bid_fetcher: Optional[Callable[[str], list[tuple[float, float]]]] = None,
        horizon_resolver: Optional[Callable[[str], Optional[float]]] = None,
        on_cycle: Optional[Callable[[CycleSummary, "PaperCopyLedger"], None]] = None,
        # Pure observation of the raw detected list before any admission rule
        # (see CopyPaperEngine.observer). Carries the shadow-quote measurement;
        # None leaves the book's behaviour bit-for-bit unchanged.
        observer: Optional[Callable[[list], None]] = None,
    ):
        self._observer = observer
        self.ledger = PaperCopyLedger(ledger_path)
        self.watchlist_path = watchlist_path
        self._explicit_wallets = wallets
        self.max_copy_usd = max_copy_usd
        self.copy_pct = copy_pct
        self.max_slippage_bps = max_slippage_bps
        self.max_age_s = max_age_s
        self.min_usd = min_usd
        self.cycle_interval_s = cycle_interval_s
        self.fill_gate_bps = fill_gate_bps
        self.first_entry_only = first_entry_only
        self.max_copies_per_wallet_day = max_copies_per_wallet_day
        self.max_copies_per_category_day = max_copies_per_category_day
        self.max_copies_per_wallet_event = max_copies_per_wallet_event
        self.low_conf_stake_frac = (
            low_conf_stake_frac
            if low_conf_stake_frac and 0.0 < low_conf_stake_frac < 1.0 else None)
        self.low_conf_until_n = low_conf_until_n
        self.gate_history_path = gate_history_path
        self.category_gate = category_gate
        self.conviction_base_usd = conviction_base_usd
        self.conviction_min = conviction_min
        self.conviction_max = conviction_max
        self.starved_priority = starved_priority
        self.relief_evidence_n = relief_evidence_n
        self.relief_max_per_category_day = relief_max_per_category_day
        self.min_horizon_days = min_horizon_days
        self.max_horizon_days = max_horizon_days
        self.strategy = strategy
        self.fill_at_their_price_bps = fill_at_their_price_bps
        self.category_evidence_min_n = category_evidence_min_n
        self.category_evidence_era_only = category_evidence_era_only
        self.era_state_path = era_state_path
        self.gas_usd_per_trade = gas_usd_per_trade
        self.trade_fee_bps = trade_fee_bps
        self._cost_model = CostModel.from_env() if costs_enabled else None
        self._blacklist_provider = blacklist_provider
        self._mark_fetcher = mark_fetcher
        self._extra_watchlist_paths = list(extra_watchlist_paths or [])
        self._detector_factory = detector_factory or make_detector
        self._book_fetcher = book_fetcher or fetch_asks
        self._resolver = resolver or resolve
        # exit-following: mirror the target's SELLs instead of only resolving
        self._exit_detector_factory = exit_detector_factory or make_exit_detector
        self._bid_fetcher = bid_fetcher or fetch_bids
        self._on_cycle = on_cycle
        # One horizon resolver, built once so its end-date cache persists across
        # cycles (one Gamma call per new market, not per cycle). Only needed when a
        # horizon band is set; otherwise routing is blind and we never look dates up.
        horizon_enabled = min_horizon_days is not None or max_horizon_days is not None
        self._horizon_resolver = horizon_resolver or (
            make_horizon_resolver() if horizon_enabled else None)

    def wallets(self) -> list[str]:
        if self._explicit_wallets:
            return self._explicit_wallets
        # Union the primary watchlist with any extras (the long-horizon book
        # watches both the copy watchlist and the long-horizon watchlist), keeping
        # first-seen order and de-duping case-insensitively. Auto-demoted wallets
        # (proven-negative copy ROI) are dropped here so a demotion stops the copy
        # immediately, without waiting for the next discovery sweep to rewrite the
        # file. Empty blacklist (the default) -> no-op.
        blacklisted = (self._blacklist_provider() if self._blacklist_provider
                       else promotion_state.active_blacklist())
        seen: set[str] = set()
        out: list[str] = []
        for path in [self.watchlist_path or ""] + self._extra_watchlist_paths:
            for w in load_watchlist_wallets(path):
                key = w.lower()
                if key in seen or key in blacklisted:
                    continue
                seen.add(key)
                out.append(w)
        return out

    def flagged_by_map(self) -> dict:
        """Wallet -> discovery theories, reloaded from the watchlist each cycle
        so newly-flagged wallets are attributed without a restart. Empty when
        running on an explicit wallet list (no watchlist file)."""
        if self._explicit_wallets or not self.watchlist_path:
            return {}
        out = load_watchlist_flagged_by(self.watchlist_path)
        for path in self._extra_watchlist_paths:
            for w, fb in load_watchlist_flagged_by(path).items():
                out.setdefault(w, fb)
        return out

    def _load_bands(self) -> dict:
        """Latest vetted confidence band per wallet, mtime-cached — the gate
        history is append-only and the runner cycles every ~60s, so re-parsing
        the whole file each cycle (in BOTH books) is waste."""
        import os as _os

        from src.copy_trading import gate_history
        try:
            mtime = _os.path.getmtime(self.gate_history_path)
        except OSError:
            return {}
        cached = getattr(self, "_bands_cache", None)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        bands = gate_history.latest_band_by_wallet(
            gate_history.load(self.gate_history_path))
        self._bands_cache = (mtime, bands)
        return bands

    def _stake_frac_map(self) -> Optional[dict]:
        """Lowercased wallet -> stake multiplier (<1.0) for wallets whose latest
        LLM-gate verdict carried a "low" confidence band and whose own settled
        record in this ledger is still under ``low_conf_until_n``. Downward only;
        None when the feature is off or nothing qualifies."""
        if self.low_conf_stake_frac is None or not self.gate_history_path:
            return None
        bands = self._load_bands()
        if not bands:
            return None
        settled: dict[str, int] = {}
        for p in self.ledger.closed_positions():
            k = (p.target or "").lower()
            settled[k] = settled.get(k, 0) + 1
        out = {w: self.low_conf_stake_frac for w, band in bands.items()
               if band == "low" and settled.get(w, 0) < self.low_conf_until_n}
        return out or None

    def _evidence_floor_ts(self) -> float:
        """Floor for the P1-6 evidence window. era_only + a recorded clean-era
        floor -> that ts; era_only without a floor yet -> +inf (the gate is
        inert until clean evidence accrues — never all-time by accident);
        default -> 0 (all-time, the shipped setting: the pre-P0-1 artifact
        flattered realized ROI upward, so an all-time-losing slice is robust)."""
        if not self.category_evidence_era_only:
            return 0.0
        floor = era_state.era_floor_ts(self.era_state_path) if self.era_state_path else None
        return floor if floor is not None else float("inf")

    def run_once(self) -> CycleSummary:
        wallets = self.wallets()
        if not wallets:
            return CycleSummary()
        det_kwargs = {}
        if self._horizon_resolver is not None:
            det_kwargs["horizon_resolver"] = self._horizon_resolver
        detector = self._detector_factory(
            wallets, self.max_age_s, self.min_usd, self.flagged_by_map(), **det_kwargs
        )
        exit_detector = self._exit_detector_factory(wallets, self.max_age_s)
        # winning-markets gate + conviction sizing read from the watchlist each
        # cycle (so regenerating it takes effect without a restart). Off unless
        # the runner was configured for them AND a watchlist file is present.
        allowed_categories = None
        wallet_median_usd = None
        if self.watchlist_path and not self._explicit_wallets:
            if self.category_gate:
                allowed_categories = load_watchlist_categories(self.watchlist_path)
                for path in self._extra_watchlist_paths:
                    for w, c in load_watchlist_categories(path).items():
                        allowed_categories.setdefault(w, c)
            if self.conviction_base_usd:
                wallet_median_usd = load_watchlist_median_usd(self.watchlist_path)
                for path in self._extra_watchlist_paths:
                    for w, m in load_watchlist_median_usd(path).items():
                        wallet_median_usd.setdefault(w, m)
        engine = CopyPaperEngine(
            self.ledger, detector=detector, book_fetcher=self._book_fetcher,
            resolver=self._resolver, copy_pct=self.copy_pct,
            max_copy_usd=self.max_copy_usd, max_slippage_bps=self.max_slippage_bps,
            exit_detector=exit_detector,
            # borrowed-clock book: entries fill at the target's price, so exits
            # mirror at the target's exit price too (no bid-book walk) — one
            # regime end to end, matching the counterfactual estimate.
            bid_fetcher=(None if self.fill_at_their_price_bps is not None
                         else self._bid_fetcher),
            fill_at_their_price_bps=self.fill_at_their_price_bps,
            fill_gate_bps=self.fill_gate_bps, first_entry_only=self.first_entry_only,
            max_copies_per_wallet_day=self.max_copies_per_wallet_day,
            max_copies_per_category_day=self.max_copies_per_category_day,
            max_copies_per_wallet_event=self.max_copies_per_wallet_event,
            stake_frac=self._stake_frac_map(),
            min_horizon_days=self.min_horizon_days,
            max_horizon_days=self.max_horizon_days,
            mark_fetcher=self._mark_fetcher, strategy=self.strategy,
            allowed_categories=allowed_categories,
            wallet_median_usd=wallet_median_usd,
            conviction_base_usd=self.conviction_base_usd,
            conviction_min=self.conviction_min,
            conviction_max=self.conviction_max,
            starved_priority=self.starved_priority,
            relief_evidence_n=self.relief_evidence_n,
            relief_max_per_category_day=self.relief_max_per_category_day,
            category_evidence_min_n=self.category_evidence_min_n,
            category_evidence_floor_ts=self._evidence_floor_ts(),
            cost_model=self._cost_model,
            gas_usd_per_trade=self.gas_usd_per_trade,
            trade_fee_bps=self.trade_fee_bps,
            observer=self._observer,
        )
        summary = engine.run_cycle()
        # starvation autopsy: surface the detector's own reject mix (rows the
        # engine never saw) alongside the engine's guardrail skips.
        summary.detector_rejects = dict(getattr(detector, "stats", None) or {})
        if self._on_cycle:
            self._on_cycle(summary, self.ledger)
        return summary

    def run_forever(self, shutdown_event: threading.Event) -> None:
        while not shutdown_event.is_set():
            try:
                self.run_once()
            except Exception:  # pragma: no cover - defensive; loop must survive
                pass
            shutdown_event.wait(self.cycle_interval_s)
