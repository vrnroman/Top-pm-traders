"""Continuous copyable-wallet discovery — pure core (state machine).

The bot runs a discovery funnel on a schedule (see ``discovery_runner``):
universe -> robust skill score -> lead-lag copyability. This module holds the
*pure* part: given the wallets evaluated this sweep and the previous state,
decide which wallets are on the paper watchlist now, which are newly qualified
(-> Telegram ping), and which decayed out (-> removed from paper).

Kept free of network/file/Telegram IO so it is fully unit-testable; the runner
injects the evaluated metrics and performs the side effects.

Qualification uses **hysteresis** so wallets hovering at the bar don't flap on
and off paper each sweep:
  * enter  : capture >= min_capture_cents AND tstat >= min_tstat
  * stay   : already on, capture >= drop_capture_cents AND tstat >= min_tstat
A capped top-N (by theory-agreement, then capture) becomes the live paper
watchlist, so "pinged" always equals "now on paper".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.copy_trading.copy_replay import proven_negative, proven_positive


@dataclass(frozen=True)
class DiscoveryConfig:
    # universe / scoring
    category: str = "ALL"
    universe: int = 850
    lookback_days: float = 120.0
    method: str = "robust"
    min_capital: float = 5000.0
    min_closed: int = 10
    skill_pool: int = 40
    # lead-lag copyability
    ll_lookback_days: float = 28.0
    delay_min: float = 15.0
    horizon_min: float = 240.0
    min_usd: float = 500.0
    min_ll_trades: int = 4
    # qualification bar (strict default) + hysteresis lower band
    min_capture_cents: float = 1.5
    min_tstat: float = 10.0
    drop_capture_cents: float = 1.0
    # copy-replay selection gate — score each candidate on OUR copy action
    # (copy its copyable BUYs, hold to resolution) and DROP wallets whose
    # measured copy-and-hold edge is proven-negative, regardless of which theory
    # flagged them. This fixes the core leak: the legacy metric rewarded a
    # wallet's own closed-position ROI, but the live harness only copies the
    # 0.05-0.95 middle of the book and holds it — so favorite-buyers / scalpers
    # whose edge is in near-locks + early exits looked great yet lost when
    # copied. The watchlist now ranks copy-validated wallets first. Reversible:
    # set copy_replay_gate=False to fall back to the legacy theory-agreement rank.
    copy_replay_gate: bool = True
    min_copy_replay_n: int = 12        # resolved replayed copies before we judge a wallet
    min_copy_replay_roi: float = 0.0   # mean copy-and-hold ROI/$ bar to qualify / not be dropped
    fade_roi: float = -0.10            # at/below this (with enough n) -> FADE label (diagnostic)
    # winning-markets-only selection (item A). The lag-sweep kill-test proved
    # copy-and-hold is −EV in aggregate even at zero lag, but the loss is
    # categorical — a wallet +EV-to-copy in one market type bleeds in another. So
    # we approve each wallet only in the categories whose copy-and-hold ROI clears
    # real-money cost on >= min_category_n resolved copies, and DROP wallets with
    # no approved category. min_category_n is the n=10-crypto-trap guard: a lucky
    # tiny category is not promotable to real capital. Reversible: category_select
    # =False keeps the legacy whole-wallet gate.
    category_select: bool = True
    min_category_n: int = 8
    require_approved_category: bool = True   # drop a wallet with zero winning markets
    # Consensus-of-sharps signal (signal-only, no capital -> no slippage). When
    # >= consensus_min_wallets INDEPENDENT copy-validated wallets BUY the same
    # (market, outcome) within consensus_window_s, emit a Telegram signal. The
    # kill-test showed single-wallet copy-and-hold is -EV; cross-wallet agreement
    # is a different, slower, more reproducible object. On by default (no fill).
    consensus_enabled: bool = True
    consensus_min_wallets: int = 3
    consensus_window_s: float = 86400.0      # 24h — slow convergence survives lag
    consensus_min_usd: float = 500.0
    consensus_cooldown_s: float = 43200.0    # 12h before a cell re-pings (unless it grows)
    # entry-discipline gate: reject wallets whose buy $ is tail-dominated
    # (settlement-lag scooping near $1 — un-copyable). Lenient by default;
    # production tightens it via config (WALLET_DISCOVERY_MAX_TAIL_RATIO).
    max_tail_ratio: float = 0.5
    # Money-curve gates (RCA 2026-07): the legacy t-stat>=10 bar selected FOR
    # near-$1 scoopers — near-zero per-trade variance aces the t-stat, but the
    # dollar curve is a loser (huge drawdown, negative Sharpe, net loss). These
    # reject on the SAME money-curve signals the LLM gate was rejecting on, so the
    # cull happens upstream, cheaply, before an Opus call or a fail-open admit.
    # All default to OFF here (inf / 1.0) so a bare DiscoveryConfig() is byte-for-
    # byte the legacy funnel; production turns them on via config at conservative
    # thresholds. Both curve gates require >= min_curve_n resolved closed positions
    # first, so a thin, noisy book is never rejected on curve shape alone.
    max_curve_drawdown: float = float("inf")  # skip if curve_drawdown exceeds this (fraction; 1.5 = 150%)
    max_hit_rate: float = 1.0                 # active only when < 1.0: skip a high-hit wallet with a losing/spiky curve
    min_curve_n: int = 0                      # evidence floor (closed positions) before the two curve gates apply
    # independent strategy theories (1a..1j) that can qualify a wallet (OR'd with
    # the legacy capture+tstat gate). All ten run by default — discovery is
    # paper-only, so every theory earns or fails on measured paper PnL before any
    # manual promotion to real capital. 1a/1e need market-resolution data, which
    # the sweep now fetches on demand (see discovery_data.evaluate_sweep).
    enabled_theories: frozenset = frozenset(
        {"1a", "1b", "1c", "1d", "1e", "1f", "1g", "1h", "1i", "1j"})
    # paper watchlist size cap
    watchlist_cap: int = 25
    # auto-remove decayed wallets from paper (False = keep accumulating)
    auto_remove: bool = True
    # Strategy 4 — long-horizon bet tracking. When enabled, the sweep classifies
    # each wallet by how early it bets before resolution (see horizon_profile) and
    # ALSO adds wallets with a real long-horizon book to a SEPARATE long-horizon
    # watchlist. This is *dual membership*, not a partition: a long-horizon wallet
    # still flows through the copy funnel on its near-term bets — only its
    # far-future bets are routed (live, per bet) to the Strategy-4 paper book.
    # Off by default (zero added API cost): the open-market end-date fetch only
    # runs when this is on.
    s4_enabled: bool = False
    s4_long_horizon_days: float = 180.0
    s4_min_long_ratio: float = 0.5      # display label only (which horizon dominates $)
    s4_min_dated_buys: int = 5
    # A wallet joins the long-horizon track once it has this many distinct
    # long-horizon buys — independent of the copy funnel (dual membership).
    s4_min_long_buys: int = 3
    long_horizon_cap: int = 25
    # on-disk cache for market resolutions (1a/1e need how each market settled).
    # Resolved markets are immutable so they're cached permanently; None disables
    # the cache (every sweep re-fetches).
    res_cache_dir: str | None = None


@dataclass(frozen=True)
class Eval:
    """One wallet's evaluation from a sweep."""

    wallet: str
    roi: float = 0.0
    tstat: float = 0.0
    capture_cents: float = 0.0
    lead_cents: float = 0.0
    hit_rate: float = 0.0
    n: int = 0
    # entry-price discipline (F2) — share of buy $ at tail / in the copyable band
    tail_ratio: float = 0.0
    copyable_ratio: float = 1.0
    # PnL-curve shape (F1) — long-arc consistency, robust to the /activity cap
    curve_sharpe: float = 0.0
    curve_drawdown: float = 0.0
    net_pnl: float = 0.0
    # closed-position skill stats (from trader_scoring metrics), distinct from the
    # lead-lag `hit_rate`/`n` above. `closed_hit_rate` is the "98% hit" scooper
    # signal; `n_closed` is the evidence floor guarding the drawdown/hit gates so a
    # thin book can't be rejected on a noisy curve.
    closed_hit_rate: float = 0.0
    n_closed: int = 0
    # copy-replay (copy this wallet's copyable BUYs, hold to resolution) — the
    # selection signal that measures the SAME action the live harness takes.
    # exit_roi/exit_n are the two-horizon diagnostic (round-trip / exit-follow).
    copy_roi: float = 0.0
    copy_tstat: float = 0.0
    copy_n: int = 0
    copy_hit: float = 0.0
    exit_roi: float = 0.0
    exit_n: int = 0
    fade: bool = False
    # winning-markets-only (item A): the market categories where this wallet's
    # copy-and-hold edge clears real-money cost (cost + margin) on >= min_category_n
    # resolved copies — the only categories the live engine will copy it in.
    # ``category_edges`` keeps the per-category detail for the alert/why-string.
    approved_categories: tuple = ()
    category_edges: tuple = ()         # tuple[ (cat, n, net_roi, approved) ]
    # the wallet's own median copyable BUY size (USD), for conviction sizing.
    median_usd: float = 0.0
    # strategy theories that flagged this wallet + their reasons (why follow it)
    flagged_by: tuple = ()
    reason: str = ""
    # bet-horizon classification (Strategy 1 = near-term, Strategy 4 = long-horizon
    # conviction bets that won't resolve for months). `strategy` is a DISPLAY label
    # (which horizon dominates the wallet's $); "1" by default so wallets without
    # horizon data read as near-term. `long_horizon` is the ROUTING flag — True
    # when the wallet has a real long book — and is independent of `strategy`/the
    # copy funnel (dual membership: a wallet can be near-term-copyable and carry a
    # long book at once).
    strategy: str = "1"
    long_horizon: bool = False
    long_horizon_ratio: float = 0.0   # share of dated buy $ placed long before resolution
    horizon_days: float = 0.0         # USD-weighted mean days-before-resolution
    # Dossier completeness (ROADMAP P1-3): the gate's dossier shipped these as
    # explicit nulls because the sweep computed but never carried them. All four
    # are the fields the LLM rejections cite (a "+613% ROI" claim is only
    # legible next to the capital it was earned on).
    capital: float = 0.0              # closed-position capital (WalletMetrics)
    concentration: float = 0.0        # share of gross profit from the best market
    mean_entry: float = 0.0           # USD-weighted mean BUY entry price
    up_ratio: float = 0.0             # fraction of pnl-curve periods up


@dataclass
class DiscoveryState:
    """Persisted across sweeps (and restarts) so pings fire once."""

    on_watchlist: dict[str, dict] = field(default_factory=dict)  # wallet -> last metrics
    last_run: float = 0.0
    initialized: bool = False  # first completed sweep done (suppresses ping storm)
    # P1-2: wallet(lower) -> latest deep-eval copy stats {copy_roi, copy_n,
    # copy_tstat, closed_hit_rate, n_closed, ts}. The next sweep's skill screen
    # reads this to EXCLUDE wallets already proven-negative under our copy
    # action (instead of re-selecting and re-culling the same scoopers every
    # sweep — the loop that dominated the cull histogram, §1.6) and to demote
    # the hit-rate-scooper signature at screen time. Rewritten each sweep from
    # the fresh evaluations; entries older than ~30d are pruned.
    copy_stats: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def from_json(cls, d: dict | None) -> "DiscoveryState":
        d = d or {}
        return cls(
            on_watchlist=dict(d.get("on_watchlist") or {}),
            last_run=float(d.get("last_run") or 0.0),
            initialized=bool(d.get("initialized") or False),
            copy_stats=dict(d.get("copy_stats") or {}),
        )

    def to_json(self) -> dict:
        return {
            "on_watchlist": self.on_watchlist,
            "last_run": self.last_run,
            "initialized": self.initialized,
            "copy_stats": self.copy_stats,
        }


@dataclass
class CycleResult:
    new_state: DiscoveryState
    watchlist: list[Eval]          # ordered (capture desc), capped — write to file
    newly_qualified: list[Eval]    # entered the watchlist this sweep — ping these
    removed: list[str]             # wallets dropped from paper this sweep
    first_init: bool               # this sweep is the initial seed (send one summary)
    # Strategy 4 — long-horizon wallets tracked separately from the copy funnel.
    # A per-sweep snapshot (ranked by horizon), written to its own watchlist file;
    # empty unless DiscoveryConfig.s4_enabled and the sweep classified any wallet
    # as long-horizon. Kept out of `watchlist` so they never feed the paper copier.
    long_horizon: list[Eval] = field(default_factory=list)
    # Gate autopsy: wallet -> which gate culled it this sweep, with the metric
    # values that tripped it. The RCA drawdown/hit gates used to cull with a bare
    # `continue`, which made "did the 1.5 ceiling over-cull?" unanswerable from
    # logs — every removal is now attributable. Only wallets that were culled by
    # an explicit gate appear here; a wallet that simply failed to (re)enter shows
    # up in `removed` without an entry, which reads as plain decay.
    culled: dict[str, str] = field(default_factory=dict)
    # Wallets on the new watchlist that qualified through the paper-evidence
    # override (realized paper P&L, recomputed from the ledger every sweep).
    paper_proven: list[str] = field(default_factory=list)
    # Previously-on wallets NOT evaluated this cycle (interrupted/truncated
    # sweep) — kept on the watchlist untouched, never auto-removed. The runner
    # preserves their previous watchlist-file rows; the next sweep force-
    # evaluates them (must_include), so a real decay is deferred one cycle.
    carried: list[str] = field(default_factory=list)


def _meta(e: Eval) -> dict:
    return {
        "roi": round(e.roi, 4), "tstat": round(e.tstat, 3),
        "capture_cents": round(e.capture_cents, 3),
        "lead_cents": round(e.lead_cents, 3),
        "hit_rate": round(e.hit_rate, 3), "n": e.n,
        "tail_ratio": round(e.tail_ratio, 3),
        # copy-replay selection signal (+ two-horizon exit diagnostic + fade tag)
        "copy_roi": round(e.copy_roi, 4), "copy_tstat": round(e.copy_tstat, 3),
        "copy_n": e.copy_n, "copy_hit": round(e.copy_hit, 3),
        "exit_roi": round(e.exit_roi, 4), "exit_n": e.exit_n, "fade": e.fade,
        # winning-markets-only (item A): approved categories + per-category edge
        # detail, and the wallet's median copyable bet for conviction sizing (C).
        "approved_categories": list(e.approved_categories),
        "category_edges": [list(r) for r in e.category_edges],
        "median_usd": round(e.median_usd, 2),
        "flagged_by": list(e.flagged_by), "reason": e.reason,
        # bet-horizon classification (Strategy 1 near-term vs 4 long-horizon)
        "strategy": e.strategy,
        "long_horizon": e.long_horizon,
        "long_horizon_ratio": round(e.long_horizon_ratio, 3),
        "horizon_days": round(e.horizon_days, 1),
        # P1-3 dossier fields — also on the watchlist row so tooling that
        # rebuilds a dossier from the row (the shortlist audit) sees them too
        "n_closed": e.n_closed, "closed_hit_rate": round(e.closed_hit_rate, 3),
        "capital": round(e.capital, 2), "concentration": round(e.concentration, 3),
        "mean_entry": round(e.mean_entry, 4), "up_ratio": round(e.up_ratio, 3),
    }


def run_discovery_cycle(
    evaluated: dict[str, Eval], prev: DiscoveryState, cfg: DiscoveryConfig,
    blacklisted: set | None = None, paper_proven: set | None = None,
) -> CycleResult:
    """Decide the new paper watchlist from this sweep's evaluations.

    ``evaluated`` must cover the freshly-scored skill pool *and* every wallet
    currently on the watchlist (the runner force-evaluates the latter so decay
    can be measured). ``blacklisted`` (lowercased addresses) is the set of
    auto-demoted wallets in their cooldown window — they're excluded so a wallet
    proven to lose under our copy action can't immediately re-qualify and squat a
    slot. ``paper_proven`` (lowercased addresses) is the set of wallets whose
    REALIZED paper-copy record is positive (recomputed from the ledger every
    sweep, never sticky): they bypass the predictive own-history gates
    (tail/drawdown/hit/category) and qualify while swept — the 2026-07 starvation
    RCA found retention was blind to paper evidence, so the best earners decayed
    off mid-accrual on the same own-history stats the money-curve RCA proved
    unreliable. Proven-NEGATIVE signals still bind: the blacklist and the
    copy-replay proven-negative gate drop a paper-proven wallet like any other
    (evidence must cut both ways or the override becomes a fail-open backdoor).
    Returns the capped, ordered watchlist plus the notify/remove deltas.
    """
    prev_on = set(prev.on_watchlist)
    blacklisted = blacklisted or set()
    paper_proven = paper_proven or set()

    # Strategy 4: collect wallets with a real long-horizon book into a SEPARATE
    # long-horizon track — but DO NOT remove them from the copy funnel. A wallet
    # can be near-term-copyable AND carry far-future conviction bets at the same
    # time (dual membership): its short-horizon bets keep flowing through the
    # Strategy-1 path below, while its long-horizon bets are tracked by the
    # Strategy-4 paper book. The per-bet split is made live, by each bet's own
    # resolution date — here we only decide which wallets each track *watches*.
    # Disabled (s4_enabled=False) leaves this empty, so behaviour is unchanged.
    long_horizon_evals: list[Eval] = []
    if cfg.s4_enabled:
        long_horizon_evals = [e for e in evaluated.values()
                              if e.long_horizon and e.wallet.lower() not in blacklisted]
        long_horizon_evals.sort(
            key=lambda e: (e.long_horizon_ratio, e.horizon_days), reverse=True)
        long_horizon_evals = long_horizon_evals[: cfg.long_horizon_cap]

    qualified: dict[str, Eval] = {}
    proven_here: set[str] = set()
    culled: dict[str, str] = {}
    for w, e in evaluated.items():
        if w.lower() in blacklisted:
            # auto-demoted (proven-negative copy ROI) — in cooldown, skip. Binds
            # even for paper-proven wallets: proven-negative beats proven-positive.
            culled[w] = "blacklist (auto-demote cooldown)"
            continue
        # copy-replay gate: drop wallets PROVEN to lose under our actual copy
        # action — enough resolved replayed copies AND a mean copy-and-hold
        # ROI/$ below the bar — no matter which theory flagged them. Wallets with
        # too little replay data are NOT dropped (insufficient evidence); they
        # just rank below copy-validated ones in the cap. Runs BEFORE the
        # paper-proven override — a proven-negative signal always binds.
        if cfg.copy_replay_gate and proven_negative(
                e.copy_n, e.copy_roi,
                min_n=cfg.min_copy_replay_n, min_roi=cfg.min_copy_replay_roi):
            culled[w] = (f"replay-proven-negative (copy_roi {e.copy_roi:+.3f} "
                         f"< {cfg.min_copy_replay_roi:+.3f} @ n={e.copy_n})")
            continue
        # paper-evidence override: a wallet whose REALIZED paper record is
        # positive qualifies while swept, bypassing the predictive gates below —
        # realized P&L is the territory those gates only map. Status is
        # recomputed from the ledger every sweep, so it lapses the moment the
        # record goes negative (then the normal gates — or the auto-demote
        # blacklist — take over).
        if w.lower() in paper_proven:
            proven_here.add(w)
            qualified[w] = e
            continue
        if e.tail_ratio > cfg.max_tail_ratio:
            # tail-dominated buy flow — un-copyable, skip regardless of theory
            culled[w] = f"tail-ratio ({e.tail_ratio:.3f} > {cfg.max_tail_ratio:.3f})"
            continue
        # money-curve gates (RCA fix): reject the scooper anti-pattern the legacy
        # t-stat bar was letting through. Guarded by min_curve_n so a thin book is
        # never rejected on a noisy curve (insufficient evidence -> keep accruing).
        # Both are no-ops at the legacy defaults (drawdown=inf, hit=1.0).
        if e.n_closed >= cfg.min_curve_n:
            if e.curve_drawdown > cfg.max_curve_drawdown:
                # catastrophic dollar drawdown — a copier eats the tail loss
                culled[w] = (f"curve-drawdown ({e.curve_drawdown:.2f} > "
                             f"{cfg.max_curve_drawdown:.2f} @ n_closed={e.n_closed})")
                continue
            if (cfg.max_hit_rate < 1.0 and e.closed_hit_rate >= cfg.max_hit_rate
                    and (e.net_pnl <= 0.0 or e.curve_sharpe <= 0.0)):
                # near-perfect hit rate on a losing/spiky curve = settlement-lag scooper
                culled[w] = (f"hit-rate-scooper (hit {e.closed_hit_rate:.2f} >= "
                             f"{cfg.max_hit_rate:.2f}, net {e.net_pnl:+.0f}, "
                             f"sharpe {e.curve_sharpe:+.2f})")
                continue
        # winning-markets-only gate (item A): drop a wallet with NO market category
        # whose copy-and-hold edge clears real-money cost — there's nowhere we can
        # profitably copy it. Scoped to PER-CATEGORY evidence: only drop once some
        # category has reached min_category_n (a fair chance to qualify) yet none
        # did. Gating on whole-wallet copy_n would wrongly drop a diversified
        # wallet whose copies are spread thin across categories (each still below
        # min_category_n) — it should keep accruing until one category matures.
        max_category_n = max((n for _c, n, _net, _ok in e.category_edges), default=0)
        if (cfg.category_select and cfg.require_approved_category
                and max_category_n >= cfg.min_category_n and not e.approved_categories):
            culled[w] = f"no-winning-category (max category n={max_category_n})"
            continue
        flagged = bool(e.flagged_by)
        # legacy lead-lag gate (== theory 1c) OR any independent theory fired
        legacy = e.tstat >= cfg.min_tstat and e.capture_cents >= cfg.min_capture_cents
        entered = legacy or flagged
        # retain a prior wallet while it's still flagged, or (legacy path) while
        # its t-stat holds the floor and capture hasn't decayed out — the t-stat
        # floor applies even in keep mode, so a collapsed wallet is dropped.
        retained_legacy = (w in prev_on and e.tstat >= cfg.min_tstat
                           and _retain_on_decay(cfg, e))
        retained = (w in prev_on and flagged) or retained_legacy
        if entered or retained:
            qualified[w] = e

    # Rank copy-VALIDATED wallets first (proven positive copy-and-hold edge),
    # ordered by their measured copy ROI — selection now leads with the same
    # action the live harness takes. Ties and unproven wallets fall back to the
    # legacy order: theory-agreement (flag count), then capture. Flag-count
    # before capture keeps each non-capture theory (1a/1b/1d/1e/1g/1i/1j)
    # represented rather than buried by the lead-lag-only signal. When no wallet
    # has replay data this reduces exactly to the legacy (flag-count, capture)
    # ranking, so behaviour is unchanged until copy-replay data accrues.
    # Paper-proven wallets rank FIRST: their zeroed own-history stats would
    # otherwise put them dead-last, so under cap pressure the override would
    # rank out exactly the wallets it exists to keep (realized evidence beats
    # every predictive signal below). No-op when paper_proven is empty, so the
    # legacy ordering is untouched.
    ranked = sorted(
        qualified.values(),
        key=lambda e: (1 if e.wallet.lower() in paper_proven else 0,
                       *_rank_key(cfg, e)),
        reverse=True)
    watchlist = ranked[: cfg.watchlist_cap]
    on_now = {e.wallet for e in watchlist}

    newly_qualified = [e for e in watchlist if e.wallet not in prev_on]
    removed = sorted(prev_on - on_now)

    # An interrupted sweep must never remove what it never looked at: a
    # previously-on wallet with NO evaluation this cycle (deploy restart landing
    # mid-sweep, fetch failure) is CARRIED — kept on the watchlist with its
    # previous stats — not culled. "Not-swept" used to count as a removal, so a
    # deploy landing mid-sweep could drop a third of the book on timing alone
    # (2026-07-10: 25 -> 18 on a restart), contaminating any comparison that
    # depends on watchlist continuity (the A-vs-B race verdict). Carried wallets
    # are force-evaluated next sweep (must_include), so a real decay is only
    # deferred one cycle, never hidden.
    carried = sorted(w for w in removed if w not in evaluated)
    if carried:
        removed = [w for w in removed if w in evaluated]

    # autopsy for removals that no explicit gate explains: ranked out of the cap,
    # never swept this cycle, or plain decay (failed to re-enter/retain).
    for w in removed:
        if w in culled:
            continue
        if w in qualified:
            culled[w] = f"ranked-out (cap {cfg.watchlist_cap})"
        elif w not in evaluated:
            culled[w] = "not-swept (no evaluation this cycle)"
        else:
            culled[w] = "decayed (no theory flag, capture/t-stat below retention)"

    on_watch_meta = {e.wallet: _meta(e) for e in watchlist}
    prev_meta = prev.on_watchlist if isinstance(prev.on_watchlist, dict) else {}
    for w in carried:
        on_watch_meta.setdefault(w, prev_meta.get(w) or {})
    new_state = DiscoveryState(
        on_watchlist=on_watch_meta,
        last_run=prev.last_run,  # runner stamps the real time
        initialized=True,
    )
    return CycleResult(
        new_state=new_state,
        watchlist=watchlist,
        newly_qualified=newly_qualified,
        removed=removed,
        first_init=not prev.initialized,
        long_horizon=long_horizon_evals,
        culled=culled,
        paper_proven=sorted(w for w in proven_here if w in on_now),
        carried=carried,
    )


def _copy_validated(cfg: DiscoveryConfig, e: Eval) -> bool:
    """A wallet with enough resolved replayed copies AND a positive measured
    copy-and-hold edge — i.e. it earns under the action we actually take."""
    return cfg.copy_replay_gate and proven_positive(
        e.copy_n, e.copy_roi,
        min_n=cfg.min_copy_replay_n, min_roi=cfg.min_copy_replay_roi)


def _rank_key(cfg: DiscoveryConfig, e: Eval):
    """Watchlist priority: copy-validated wallets first (by measured copy ROI),
    then the legacy theory-agreement / capture order. Reduces exactly to
    (flag-count, capture) when no replay data exists, so it is backward-safe."""
    proven = _copy_validated(cfg, e)
    # Winning-markets wallets rank first (item A): a wallet with >=1 category whose
    # copy-and-hold edge clears real-money cost is the only kind we can actually
    # profit copying. Within that, prefer more approved categories, then the
    # legacy proven-copy-ROI / theory-agreement / capture order.
    has_winning = 1 if (cfg.category_select and e.approved_categories) else 0
    return (has_winning,
            len(e.approved_categories) if cfg.category_select else 0,
            1 if proven else 0,
            e.copy_roi if proven else 0.0,
            # P1-2: among the copy-UNPROVEN, a wallet whose replay evidence is
            # accruing non-negatively outranks one with no evidence at all —
            # copy_n=0 must never sit above a wallet the harness is actually
            # measuring (§1.6: the watchlist top was all copy_n=0 wallets).
            1 if (not proven and e.copy_n > 0 and e.copy_roi >= 0) else 0,
            len(e.flagged_by),
            e.capture_cents)


def _retain_on_decay(cfg: DiscoveryConfig, e: Eval) -> bool:
    """Should a wallet *already* on the list be kept this sweep?

    With auto_remove off it always stays (keeps accumulating paper PnL). With
    auto_remove on it stays only while capture holds above the lower drop band
    (hysteresis) — so a wallet sitting between drop and entry bands doesn't flap
    on and off every sweep."""
    if not cfg.auto_remove:
        return True
    return e.capture_cents >= cfg.drop_capture_cents


def watchlist_to_targets(watchlist: list[Eval], cfg: DiscoveryConfig) -> dict:
    """Serialize to the copy_watchlist.json shape the paper harness reads."""
    return {
        "category": cfg.category,
        "method": cfg.method,
        "source": "discovery",
        "targets": [
            {"rank": i + 1, "wallet": e.wallet, **_meta(e)}
            for i, e in enumerate(watchlist)
        ],
    }


def long_horizon_to_targets(long_horizon: list[Eval], cfg: DiscoveryConfig) -> dict:
    """Serialize the Strategy-4 long-horizon snapshot to its own watchlist file.

    Same row shape as ``watchlist_to_targets`` (so tooling can read either), but
    a distinct ``source`` so it's never confused with the copy watchlist the
    paper harness consumes — these wallets are tracked, not copied.
    """
    return {
        "category": cfg.category,
        "source": "discovery_long_horizon",
        "long_horizon_days": cfg.s4_long_horizon_days,
        "targets": [
            {"rank": i + 1, "wallet": e.wallet, **_meta(e)}
            for i, e in enumerate(long_horizon)
        ],
    }
