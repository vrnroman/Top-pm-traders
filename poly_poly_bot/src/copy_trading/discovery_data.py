"""Runtime data layer for continuous wallet discovery.

Network IO tuned for an always-on loop (rather than a one-shot backtest):
  * TTL-cached wallet activity on disk, so each sweep sees fresh trades without
    re-pulling everything every time;
  * bounded concurrency so discovery never starves the trading threads;
  * shutdown-aware so a stop request doesn't wait out a full sweep.

Pure analysis is reused from ``trader_scoring`` and ``lead_lag``; this module
only fetches and assembles ``Eval`` rows for ``discovery.run_discovery_cycle``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from src.copy_trading.copy_cost import CostModel
from src.copy_trading.copy_replay import (
    approved_category_set,
    proven_negative,
    score_copy_replay,
    select_copyable_categories,
)
from src.copy_trading.discovery import DiscoveryConfig, Eval
from src.copy_trading.entry_profile import EntryProfile, entry_profile, is_copyable_entry
from src.copy_trading.horizon_profile import (
    classify_strategy,
    horizon_profile,
    long_horizon_eligible,
)
from src.copy_trading.lead_lag import WalletLeadLag, analyze_buy
from src.copy_trading.market_resolution import fetch_open_end_dates, fetch_resolutions
from src.copy_trading.pnl_curve import CurveMetrics, curve_metrics, fetch_pnl_curve
from src.copy_trading.theories import REGISTRY, evaluate_all
from src.copy_trading.trader_scoring import compute_wallet_metrics, select_targets
from src.copy_trading.wallet_context import WalletContext, build_context

logger = logging.getLogger("poly_poly_bot")

# Activity-cache write failures already warned about this sweep, keyed by cache
# dir — the WARNING fires once per sweep per dir (first-failure style), never
# per failed row (a 400-wallet sweep of failures would bury the signal).
# Reset at the top of every evaluate_sweep.
_cache_write_warned_dirs: set = set()

# Wallets whose /activity fetch this sweep died mid-pagination (the API ran out
# of retries). Their result is incomplete, so it is neither cached nor trusted.
# Until now these were completely invisible: the discovery path logs nothing on
# a failed _get, so the only trace of a throttled sweep was the poisoned cache
# entry it left behind. list.append is atomic, which is all the thread-safety
# the ThreadPoolExecutor in fetch_all_activity needs. Reset per sweep.
_activity_fetch_failures: list = []

DATA_API = os.environ.get("DATA_API_URL", "https://data-api.polymarket.com")
CLOB = "https://clob.polymarket.com"
HISTORY_RETENTION_DAYS = 31  # CLOB price-history rolling window


def _stopping(ev: threading.Event | None) -> bool:
    return ev is not None and ev.is_set()


def _sleep_unless_stopped(ev: threading.Event | None, secs: float) -> None:
    """Pace the sweep without sleeping through a shutdown request.

    Used to space out the many requests of a wide (200k-wallet) scan so we stay
    under the data-API 429 ceiling. Returns immediately if the stop event fires,
    so a deploy/shutdown doesn't have to wait out a pacing pause."""
    if secs <= 0:
        return
    if ev is not None:
        ev.wait(secs)
    else:
        time.sleep(secs)


def _get(session, base, path, **params):
    for _ in range(4):
        try:
            r = session.get(base + path, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                # Empirically the data-API sends `Retry-After: 0` (no real hint),
                # so fall back to a fixed 1s cool-off; honour a larger value if
                # the server ever starts sending one.
                try:
                    wait = float(r.headers.get("Retry-After") or 0)
                except ValueError:
                    wait = 0.0
                time.sleep(max(wait, 1.0))
                continue
            if 400 <= r.status_code < 500:
                return None  # client error (e.g. offset cap exceeded) won't fix on retry
            # else 5xx — fall through to the backoff sleep and retry
        except requests.RequestException:
            pass
        time.sleep(0.25)
    return None


# ─── universe ────────────────────────────────────────────────────────────────
def build_universe(
    target: int,
    min_amounts=(3000, 1200, 500, 250, 100),
    max_offset: int | None = None,
    page_pause_s: float | None = None,
    window_s: float | None = None,
    expand_filters: bool | None = None,
) -> list[str]:
    """Wallets active in the recent trade feed (default: the last ``window_s``).

    NOTE: the data-API hard-caps ``/trades`` pagination at **offset 3000**
    (beyond it returns HTTP 400 "max historical activity offset of 3000
    exceeded"), so a single filter exposes only its ~3500 most-recent trades.
    We widen the net two ways:

    * **stake tiers** ($3000 → $100, high first so the strongest wallets seed
      the set and modest-bet insiders are still caught);
    * **filter expansion** (``expand_filters``): each tier is also queried per
      ``side`` (BUY/SELL) and ``takerOnly`` (true=takers, false=incl. makers).
      Each filter combination is its own newest-first feed, so the union covers
      more distinct wallets within the same recent window — at the cost of ~20×
      the requests. Off by default; turn on for a wider (but slower) sweep.

    ``window_s`` makes this an *active-in-the-last-N-seconds* universe: trades
    older than the cutoff are skipped, and because the feed is newest-first we
    stop paging a filter combo as soon as it crosses the cutoff (so a 24h sweep
    rarely needs the full offset budget). ``window_s=0`` disables the window and
    pages each combo to ``max_offset`` (the legacy top-stake behaviour).

    Stops a combo when it runs dry, the offset budget (``max_offset``, capped at
    the API's 3000) is hit, the window cutoff is crossed, or we've collected
    ``target`` unique wallets. A short ``page_pause_s`` between pages keeps us
    under the 429 ceiling.
    """
    if max_offset is None:
        max_offset = int(os.environ.get("WALLET_DISCOVERY_UNIVERSE_MAX_OFFSET", "3000"))
    if page_pause_s is None:
        page_pause_s = float(os.environ.get("WALLET_DISCOVERY_PAGE_PAUSE_S", "0.3"))
    if window_s is None:
        window_s = float(os.environ.get("WALLET_DISCOVERY_UNIVERSE_WINDOW_S", "86400"))
    if expand_filters is None:
        expand_filters = os.environ.get(
            "WALLET_DISCOVERY_EXPAND_FILTERS", "false").strip().lower() == "true"

    cutoff = (time.time() - window_s) if window_s and window_s > 0 else None
    sides = ("BUY", "SELL") if expand_filters else (None,)
    takers = ("true", "false") if expand_filters else ("true",)
    combos = [(amt, side, taker)
              for amt in min_amounts for side in sides for taker in takers]

    s = requests.Session()
    seen: set[str] = set()
    for amt, side, taker in combos:
        off = 0
        while off <= max_offset and len(seen) < target:  # offset 3000 itself is valid
            params = dict(limit=500, offset=off, filterType="CASH",
                          filterAmount=amt, takerOnly=taker)
            if side:
                params["side"] = side
            tr = _get(s, DATA_API, "/trades", **params)
            if not tr:
                break
            crossed = False
            for t in tr:
                if cutoff is not None and float(t.get("timestamp") or 0) < cutoff:
                    crossed = True  # newest-first: this and the rest are too old
                    continue
                w = t.get("proxyWallet")
                if w:
                    seen.add(w)
            if crossed:
                break  # remaining offsets are entirely outside the window
            off += 500
            if len(tr) < 500:
                break  # combo exhausted — no point paging an empty offset
            if page_pause_s > 0:
                time.sleep(page_pause_s)
    return list(seen)[:target]


def prune_cache(cache_dir: str | None, ttl_s: float, max_files: int | None = None,
                max_bytes: int | None = None, reserve_bytes: int | None = None,
                floor_bytes: int | None = None) -> int:
    """Bound the on-disk /activity cache; return how many files were removed.

    The universe churns every sweep, so wallets that drop out leave their
    ``{wallet}.json`` behind. Without pruning these orphans accumulate forever
    (measured ~2.5MB each at the 4000-record cap — 6.7GB in the first day after
    the 2026-07 cache resurrection), eventually filling a small VM's disk. We
    delete anything older than ``ttl_s`` (it would be re-fetched on
    use anyway), then drop the oldest by mtime until the directory is under
    BOTH ``max_files`` and ``max_bytes``. RAM is unaffected — this is purely a
    disk guard.

    ``max_bytes`` is the bound that actually holds. A file-count cap only
    bounds the disk if the mean file size is stable, and it is not: entry size
    is the wallet's activity length, so as the pool drifts toward whales the
    same 4000 files grow without limit. Prod 2026-08-02 measured the count-only
    cap at 4000 files / **8.2GB** of a 20G disk (mean 1.7MB, max 4.5MB, 2764
    files over 1MB) with disk-watch tripping "free 4.1G shrinking 1521MB/day →
    floor in ~1d". Bytes are what the disk runs out of, so bytes are what we
    budget; the count cap stays as a cheap secondary bound.
    """
    if not cache_dir or not os.path.isdir(cache_dir):
        return 0
    now = time.time()
    removed = 0
    fresh: list[tuple[float, str, int]] = []
    total_bytes = 0
    for name in os.listdir(cache_dir):
        if not name.endswith(".json"):
            continue
        path = os.path.join(cache_dir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        if ttl_s and (now - st.st_mtime) >= ttl_s:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
        else:
            fresh.append((st.st_mtime, path, st.st_size))
            total_bytes += st.st_size
    # ``reserve_bytes`` derives the real budget from the filesystem instead of
    # a hand-picked constant: this cache may grow into whatever space exists
    # beyond the reserve. A hand-derived GB number has now gone stale twice
    # (15000 files ≈ 37GB in P2, 4000 files ≈ 8.2GB today) because it silently
    # encodes a mean file size that drifts. Deriving it means the bound cannot
    # rot when the funnel widens or the disk is resized.
    if reserve_bytes:
        try:
            free = shutil.disk_usage(cache_dir).free
            # Space this cache could occupy while still leaving the reserve free.
            # FLOORED, never zero: an allowance of 0 is falsy, and the eviction
            # test below used to read it as "no budget configured" — so the
            # governor switched ITSELF OFF exactly when free space fell to the
            # reserve, i.e. at the one disk state it exists for, and
            # min(ceiling, 0) threw the explicit ceiling away too. The floor
            # also stops a tight disk from evicting the whole cache and forcing
            # a full refetch into an API that is already rate-limiting us.
            floor = _MIN_CACHE_BYTES if floor_bytes is None else floor_bytes
            allowance = max(floor, total_bytes + free - reserve_bytes)
            max_bytes = (min(max_bytes, allowance) if max_bytes is not None
                         else allowance)
        except OSError:
            pass

    fresh.sort()  # oldest first — evicted first by both bounds
    over_count = (len(fresh) - max_files) if max_files else 0
    idx = 0
    for mtime, path, size in fresh:
        # `is not None`, NOT truthiness: a computed budget of 0 means "evict
        # everything", not "no budget" (see the reserve block above).
        over_bytes = max_bytes is not None and total_bytes > max_bytes
        if idx >= over_count and not over_bytes:
            break
        try:
            os.remove(path)
            removed += 1
            total_bytes -= size
        except OSError:
            pass
        idx += 1
    return removed


# ─── activity (TTL-cached) ───────────────────────────────────────────────────
def fetch_activity(wallet: str, cache_dir: str | None, ttl_s: float, cap: int = 4000) -> list[dict]:
    """Wallet activity, served from disk cache if younger than ``ttl_s``.

    A failed fetch is NEVER cached. ``_get`` returns ``None`` once a page has
    exhausted its four attempts (429 / 5xx / timeout / connection error), which
    is a different fact from "the wallet has no more trades" — but both used to
    hit the same ``if not a: break`` and the truncated (often empty) result was
    then written to disk and served as truth for the whole TTL, i.e. four
    sweeps. A throttled wallet therefore scored as if it had no trade history,
    and that same record is what the skill screen ranks on and what the LLM
    gate's dossier is built from. Measured in prod 2026-08-02: 18 wallets held
    a cached ``[]``. Truncation mid-pagination is the same bug and leaves no
    trace at all, so we count both and let the sweep report them.
    """
    path = os.path.join(cache_dir, f"{wallet}.json") if cache_dir else None
    if path and os.path.exists(path):
        try:
            if (time.time() - os.path.getmtime(path)) < ttl_s:
                return json.load(open(path))
        except (json.JSONDecodeError, OSError):
            pass
    s = requests.Session()
    acts: list[dict] = []
    off = 0
    incomplete = False
    while off < cap:
        a = _get(s, DATA_API, "/activity", user=wallet, limit=500, offset=off)
        if a is None:
            incomplete = True  # the API failed — this result is not the truth
            break
        if not a:
            break              # genuine end of data
        acts += a
        off += 500
        if len(a) < 500:
            break
    if incomplete:
        _activity_fetch_failures.append(wallet)
        return acts
    if path:
        try:
            tmp = path + ".tmp"
            json.dump(acts, open(tmp, "w"))
            os.replace(tmp, path)
        except OSError as e:
            # A dead cache must be LOUD once, not silently off for weeks: prod
            # ran with wcache never created (2026-07-27 finding), so every sweep
            # re-fetched everything and the 10x pool (P1-1) would not have fit.
            if cache_dir not in _cache_write_warned_dirs:
                _cache_write_warned_dirs.add(cache_dir)
                logger.warning("[DISCOVERY] activity cache write failed (%s: %s) — "
                               "disk cache OFF, every sweep re-fetches", path, e)
    return acts


def fetch_all_activity(wallets, cache_dir, ttl_s, workers=8, stop=None) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_activity, w, cache_dir, ttl_s): w for w in wallets}
        for f in as_completed(futs):
            if _stopping(stop):
                break
            try:
                out[futs[f]] = f.result() or []
            except Exception:
                out[futs[f]] = []
    return out


# ─── lead-lag copyability ────────────────────────────────────────────────────
def fetch_recent_buys(wallet: str, since_ts: float, min_usd: float) -> list[dict]:
    s = requests.Session()
    out, offset = [], 0
    while offset < 1500:
        acts = _get(s, DATA_API, "/activity", user=wallet, limit=500, offset=offset)
        if not acts:
            break
        stop = False
        for a in acts:
            ts = float(a.get("timestamp") or 0)
            if ts < since_ts:
                stop = True
                continue
            if a.get("type") != "TRADE" or a.get("side") != "BUY":
                continue
            price = float(a.get("price") or 0)
            if not is_copyable_entry(price):  # skip tail entries (no copyable edge)
                continue
            usd = float(a.get("usdcSize") or 0) or float(a.get("size") or 0) * price
            if usd < min_usd:
                continue
            token = a.get("asset") or ""
            if token:
                out.append({"token": token, "ts": ts, "price": price, "usd": usd})
        if stop or len(acts) < 500:
            break
        offset += 500
    return out


def fetch_price_series(token: str, cache: dict) -> list[tuple[float, float]]:
    if token in cache:
        return cache[token]
    s = requests.Session()
    j = _get(s, CLOB, "/prices-history", market=token, interval="max", fidelity=10)
    series = []
    for pt in (j or {}).get("history", []) or []:
        t, p = pt.get("t"), pt.get("p")
        if t is not None and p is not None:
            series.append((float(t), float(p)))
    series.sort()
    cache[token] = series
    return series


def wallet_entry_profile(wallet: str, cache_dir: str | None, ttl_s: float) -> EntryProfile:
    """Entry-price discipline from a wallet's (cached) activity; never raises."""
    try:
        return entry_profile(fetch_activity(wallet, cache_dir, ttl_s))
    except Exception:
        return EntryProfile()


def wallet_curve_metrics(wallet: str) -> CurveMetrics:
    """PnL-curve shape from the user-pnl endpoint; never raises."""
    try:
        return curve_metrics(fetch_pnl_curve(wallet))
    except Exception:
        return CurveMetrics()


def build_wallet_context(wallet: str, cache_dir: str | None, ttl_s: float, *,
                         now: float, lookback_ts: float, category: str,
                         curve: CurveMetrics, capture_cents: float = 0.0,
                         lead_cents: float = 0.0, capture_hit_rate: float = 0.0,
                         n_capture: int = 0, resolutions: dict | None = None) -> WalletContext:
    """Build the theory feature bundle from a wallet's (cached) activity; the
    curve + lead-lag scalars are injected from the deep stage. ``resolutions``
    (conditionId -> MarketResolution) enriches each BUY with won/early so the
    resolution theories (1a/1e) can fire. Never raises."""
    try:
        acts = fetch_activity(wallet, cache_dir, ttl_s)
        return build_context(wallet, acts, now=now, lookback_ts=lookback_ts,
                             category=category, curve=curve, resolutions=resolutions,
                             capture_cents=capture_cents, lead_cents=lead_cents,
                             capture_hit_rate=capture_hit_rate, n_capture=n_capture)
    except Exception:
        return WalletContext(wallet=wallet, now=now, curve=curve)


def lead_lag_wallet(buys, delay_s, horizon_s, price_cache) -> WalletLeadLag:
    w = WalletLeadLag()
    tokens = {b["token"] for b in buys}
    series_by_token: dict[str, list] = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(fetch_price_series, t, price_cache): t for t in tokens}
        for f in as_completed(futs):
            series_by_token[futs[f]] = f.result()
    for b in buys:
        series = series_by_token.get(b["token"])
        if not series:
            continue
        r = analyze_buy(series, b["ts"], delay_s=delay_s, horizon_s=horizon_s)
        if r is not None:
            w.add(r, side_sign=1)
    return w


# ─── full sweep: universe -> skill -> copyability -> Eval rows ────────────────
# P1-2: a wallet whose copy-replay was proven negative on a recent sweep is
# excluded from the skill pool for this long before it may be re-evaluated —
# new resolutions can overturn the verdict, but re-judging every sweep just
# re-selects the same losers (they ace the own-history t-stat screen) and
# re-culls them downstream, which is exactly the loop that kept the cull
# histogram dominated by replay-proven-negative (§1.6).
COPY_STAT_REEVAL_S = 7 * 86400.0

# Hard bound on the shared per-token price-series cache used across the
# deep-eval loop. It exists to dedupe fetches when wallets share tokens, but
# at a 400-wallet pool it is the largest surviving per-sweep structure
# (per-token interval="max" series, thousands of points each). Full-clear
# past the cap — a few re-fetches are cheaper than an unbounded sweep peak.
PRICE_CACHE_MAX = 5000


def _screen_excluded(wallet: str, prior_copy_stats: dict, cfg: DiscoveryConfig,
                     now: float) -> bool:
    """True if the skill screen should skip this wallet this sweep: its persisted
    copy-replay from a RECENT deep-eval is already proven-negative under our
    copy action. must_include wallets bypass this entirely (retention/blacklist
    logic owns them)."""
    if not prior_copy_stats or not cfg.copy_replay_gate:
        return False
    rec = prior_copy_stats.get(wallet.lower())
    if not isinstance(rec, dict):
        return False  # absent — or a corrupt hand-edited row; never block on it
    if (now - float(rec.get("ts") or 0.0)) > COPY_STAT_REEVAL_S:
        return False  # stale evidence — re-evaluate, don't keep punishing
    return proven_negative(
        int(rec.get("copy_n") or 0), float(rec.get("copy_roi") or 0.0),
        min_n=cfg.min_copy_replay_n, min_roi=cfg.min_copy_replay_roi)


def _merge_topk(pool: list, chunk_scored: dict, cfg: DiscoveryConfig,
                prior_copy_stats: dict | None = None, now: float | None = None) -> list:
    """Fold a chunk's scored wallets into the running top-K skill pool.

    ``pool`` is the prior winners (RankedMetrics). We re-run the same
    filter+rank over the union of the pool and the chunk, capped at
    ``skill_pool``. Because each input already holds at least the global top-K
    it can contribute, the top-K of the union is exact — so this streams 200k
    wallets through fixed memory without changing which wallets are selected.

    P1-2: wallets whose persisted copy-replay is already proven-negative are
    excluded before ranking, and the hit-rate-scooper signature is demoted to
    the bottom of the pool (wired from the same config the curve gates use, so
    it is ON in prod where WALLET_DISCOVERY_MAX_HIT_RATE < 1.0)."""
    combined = {rm.address: rm.metrics for rm in pool}
    combined.update(chunk_scored)
    if prior_copy_stats:
        now = time.time() if now is None else now
        combined = {w: m for w, m in combined.items()
                    if not _screen_excluded(w, prior_copy_stats, cfg, now)}
    return select_targets(
        combined, method=cfg.method, min_capital=cfg.min_capital,
        min_closed=cfg.min_closed, top_k=cfg.skill_pool,
        demote_hit_rate=(cfg.max_hit_rate if cfg.max_hit_rate < 1.0 else None),
        demote_min_closed=cfg.min_curve_n,
    )


# Disk budgets for the two on-disk caches. BYTES, not file counts, are the
# bound that holds — see prune_cache. Measured 2026-08-02: at the count-only
# cap wcache held 8.2G of a 20G disk (mean 1.7MB/file, max 4.5MB, 2764 files
# over 1MB) and disk-watch tripped at ~1 day to full.
#
# These ceilings are the *upper* bound; the operative budget is derived from
# free space at prune time via ``reserve_bytes`` (whichever is smaller wins),
# so the caches yield when something else on the disk grows and expand again
# when it shrinks. RESERVE is what must stay free for everything else: a
# ~1.5G image plus the same again for a pull, the ledgers/sqlite, and the
# disk-watch floor (2.5G) with room to spare.
_WCACHE_MAX_BYTES = 5.0 * 1024 ** 3
_RESCACHE_MAX_BYTES = 1.5 * 1024 ** 3
_DISK_RESERVE_BYTES = 7.0 * 1024 ** 3
# Floor on the derived allowance. Under real disk pressure the cache shrinks to
# this rather than to nothing: emptying it entirely would force a full refetch
# on the next sweep into a data API that is already 429-ing us, trading a disk
# problem for a data-quality one.
_MIN_CACHE_BYTES = 0.5 * 1024 ** 3


def _env_bytes(name: str, default: float) -> int:
    """Read a GiB-valued env override; fall back to ``default`` bytes."""
    try:
        return int(float(os.environ[name]) * 1024 ** 3)
    except (KeyError, ValueError):
        return int(default)


def _report_incomplete_fetches() -> None:
    """Surface this sweep's incomplete /activity fetches. Never raises.

    A throttled sweep silently scores wallets on partial history; that is a
    data-quality fact the funnel line should carry, not something to discover
    later from an empty cache file. WARNING (not INFO) because it degrades the
    inputs the LLM gate and the skill ranking read — note WARNING+ lands in
    ``signals-*.log``, not ``bot-*.log`` (src/logger.py:104)."""
    try:
        n = len(_activity_fetch_failures)
        if n:
            sample = ", ".join(_activity_fetch_failures[:3])
            logger.warning("[DISCOVERY] %d wallet(s) had an INCOMPLETE /activity "
                           "fetch this sweep — not cached, scored on partial "
                           "history (e.g. %s)", n, sample)
    except Exception:  # noqa: BLE001
        pass


def _prune_disk_caches(cache_dir: str | None, cfg: DiscoveryConfig,
                       activity_ttl_s: float, *, when: str) -> None:
    """Bound both on-disk caches. Called at sweep START and sweep END.

    Pruning only at the start bounded the *floor* and left the peak free: a
    sweep writes its whole working set (~830 activity files, ~1.4GB, plus ~90k
    resolution files) after the prune has already run, so the disk sat at peak
    between sweeps — which is where prod was when disk-watch tripped. Running
    it again on the way out means the idle-state disk is the pruned state.
    Never raises: a cache guard must not break the sweep it rides.
    """
    try:
        reserve = _env_bytes("DISK_CACHE_RESERVE_GB", _DISK_RESERVE_BYTES)
        if cache_dir:
            # Logged, not silent: wcache is the biggest thing on the disk and
            # this is the lever that evicts gigabytes of it. An invisible lever
            # is how the 8.2GB cache grew unnoticed in the first place.
            removed = prune_cache(
                cache_dir, activity_ttl_s,
                max_files=int(os.environ.get("WALLET_DISCOVERY_CACHE_MAX_FILES", "4000")),
                max_bytes=_env_bytes("WALLET_DISCOVERY_CACHE_MAX_GB", _WCACHE_MAX_BYTES),
                reserve_bytes=reserve)
            if removed:
                logger.info("[DISCOVERY] wcache pruned (%s-sweep): %d file(s) removed",
                            when, removed)
        # Resolution cache: files are tiny (~4KB) and immutable, but there are
        # ~90k written per sweep. Resolutions are facts, so a pruned file is
        # never *wrong* to lose — re-querying costs one batched Gamma call. The
        # prune is mtime-based (reads don't touch), so a still-hot file older
        # than the TTL is deleted and refetched within the same sweep.
        #
        # The count cap was 120k against ~90k writes per sweep, so an entry
        # survived ~1.3 sweeps and the cache thrashed: prod measured 139,878
        # then 86,936 files evicted on consecutive sweeps, i.e. it was mostly
        # paying to write files it deleted before reuse. At ~4KB the bytes are
        # cheap, so the count cap is now well clear of one sweep's working set
        # and the byte budget above is what actually bounds it.
        if cfg.res_cache_dir:
            removed = prune_cache(
                cfg.res_cache_dir,
                ttl_s=float(os.environ.get("WALLET_DISCOVERY_RES_CACHE_TTL_DAYS", "7")) * 86400.0,
                max_files=int(os.environ.get("WALLET_DISCOVERY_RES_CACHE_MAX_FILES", "400000")),
                max_bytes=_env_bytes("WALLET_DISCOVERY_RES_CACHE_MAX_GB", _RESCACHE_MAX_BYTES),
                reserve_bytes=reserve)
            if removed:
                logger.info("[DISCOVERY] rescache pruned (%s-sweep): %d file(s) removed",
                            when, removed)
    except Exception as e:  # noqa: BLE001
        logger.warning("[DISCOVERY] cache prune (%s-sweep) failed: %s", when, e)


def evaluate_sweep(
    cfg: DiscoveryConfig,
    *,
    must_include: set[str] | None = None,
    cache_dir: str | None = None,
    activity_ttl_s: float = 86400.0,
    stop: threading.Event | None = None,
    prior_copy_stats: dict | None = None,
) -> dict[str, Eval]:
    """Run the funnel, then re-bound the disk caches on the way out.

    The sweep writes its entire working set *after* the entry prune, so without
    this the disk idles at the sweep's peak. ``finally`` so an early
    ``return {}`` (shutdown) or an exception still leaves the disk pruned.
    """
    try:
        return _evaluate_sweep(
            cfg, must_include=must_include, cache_dir=cache_dir,
            activity_ttl_s=activity_ttl_s, stop=stop,
            prior_copy_stats=prior_copy_stats)
    finally:
        _report_incomplete_fetches()
        _prune_disk_caches(cache_dir, cfg, activity_ttl_s, when="post")


def _evaluate_sweep(
    cfg: DiscoveryConfig,
    *,
    must_include: set[str] | None = None,
    cache_dir: str | None = None,
    activity_ttl_s: float = 86400.0,
    stop: threading.Event | None = None,
    prior_copy_stats: dict | None = None,
) -> dict[str, Eval]:
    """Run the funnel and return wallet -> Eval.

    ``must_include`` wallets (e.g. those already on the watchlist) are always
    lead-lag evaluated so decay can be measured even if they fall out of the
    fresh skill pool. ``prior_copy_stats`` (wallet(lower) -> last sweep's copy
    stats) feeds the P1-2 screen: wallets already proven-negative under our
    copy action are excluded from the pool instead of re-selected and re-culled
    every sweep.
    """
    must_include = must_include or set()

    # Fresh sweep: re-arm the once-per-sweep cache-write warnings and the
    # incomplete-fetch tally.
    _cache_write_warned_dirs.clear()
    _activity_fetch_failures.clear()

    # The disk caches only work if the directories exist. Nothing created them
    # until now (prod ran 2026-06..07 with both silently dead: every sweep
    # re-fetched all activity and re-resolved ~40k markets). Create them here,
    # once per sweep, so a fresh deploy self-heals instead of needing a manual
    # mkdir on the VM.
    for d in (cache_dir, cfg.res_cache_dir):
        if d:
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:
                logger.warning("[DISCOVERY] cannot create cache dir %s — caching off", d)

    # Keep the on-disk caches bounded before we add this sweep's files.
    _prune_disk_caches(cache_dir, cfg, activity_ttl_s, when="pre")

    universe = build_universe(cfg.universe)
    for w in must_include:
        if w not in universe:
            universe.append(w)
    if _stopping(stop):
        return {}

    # Fetch + score the universe in CHUNKS so we never hold every wallet's raw
    # /activity in memory at once. The unchunked version held all wallets at once
    # and peaked ~2.5GB (OOM on a 2GB VM). Activity is disk-cached, so chunking
    # changes only *when* data is resident, not the network volume; and
    # `activity` is not needed past scoring (lead-lag below re-fetches via
    # fetch_recent_buys).
    #
    # We also keep only a STREAMING top-K skill pool rather than every wallet's
    # metrics: at 200k wallets the compact metrics dict (a per-market PnL list
    # each) would itself dwarf RAM. After each chunk we fold its survivors into a
    # running pool capped at ``skill_pool`` (top-K of a union == top-K of each
    # part's top-K), then drop the chunk entirely. Peak is bounded to ~one chunk
    # + the pool regardless of universe size, so the scan scales to 200k+.
    lookback = time.time() - cfg.lookback_days * 86400
    chunk_size = max(1, int(os.environ.get("WALLET_DISCOVERY_CHUNK", "100")))
    # 8 workers (fetch_all_activity default) sustained ~148 /activity req/s with
    # zero 429s in testing; 16 workers started drawing 429s. So 8 workers is the
    # real governor and a 0.5s pause between 100-wallet batches leaves margin.
    batch_pause_s = float(os.environ.get("WALLET_DISCOVERY_BATCH_PAUSE_S", "0.5"))
    pool: list = []                              # running top-K RankedMetrics
    must_metrics: dict = {}                      # retained metrics for watchlist wallets
    for i in range(0, len(universe), chunk_size):
        if _stopping(stop):
            return {}
        chunk = universe[i:i + chunk_size]
        activity = fetch_all_activity(chunk, cache_dir, activity_ttl_s, stop=stop)
        chunk_scored: dict = {}
        for w, a in activity.items():
            m = compute_wallet_metrics(a, start_ts=lookback, category=cfg.category)
            chunk_scored[w] = m
            if w in must_include:
                must_metrics[w] = m
        del activity  # release this chunk's raw activity before the next fetch
        pool = _merge_topk(pool, chunk_scored, cfg,
                           prior_copy_stats=prior_copy_stats)
        del chunk_scored
        # Pause between batches so a wide sweep paces its /activity calls under
        # the data-API 429 ceiling (skip after the last chunk).
        if i + chunk_size < len(universe):
            _sleep_unless_stopped(stop, batch_pause_s)
    if _stopping(stop):
        return {}
    skilled = pool
    # metrics lookup for roi/tstat by wallet
    metric_by_wallet = {rm.address: rm.metrics for rm in skilled}

    # deep-evaluate the skill pool plus any wallets we must re-check
    to_eval = [rm.address for rm in skilled]
    for w in must_include:
        if w not in metric_by_wallet:
            to_eval.append(w)
            if w in must_metrics:
                metric_by_wallet[w] = must_metrics[w]

    since = time.time() - min(cfg.ll_lookback_days, HISTORY_RETENTION_DAYS - 1) * 86400
    delay_s, horizon_s = cfg.delay_min * 60, cfg.horizon_min * 60
    price_cache: dict[str, list] = {}

    # Theories 1a/1e judge trades by how each market settled (which outcome won,
    # how early the bet was placed). Fetch those resolutions ONCE for the
    # deep-eval wallets' BUYs — batched + disk-cached (resolved markets are
    # immutable) — but only when an enabled theory actually needs them, so the
    # default-off case adds zero API cost. Re-reads from the activity cache the
    # chunk loop already populated (fetch_all_activity is the test seam).
    # Resolutions are needed by 1a/1e AND by the copy-replay selection gate
    # (which labels each BUY won/lost to replay copying it held to resolution).
    needs_res = cfg.copy_replay_gate or any(
        t in REGISTRY and REGISTRY[t].needs_resolution for t in cfg.enabled_theories)
    resolutions: dict = {}
    if to_eval and (needs_res or cfg.s4_enabled):
        # Collect the deep-eval wallets' BUY conditionIds in CHUNKS, keeping only
        # the cid set: holding every wallet's full /activity at once (the old
        # fetch_all_activity(to_eval) call) peaks at ~1-2MB x pool size — fine at
        # a 40-wallet pool, an OOM risk at the P1-1 400-wallet pool on the 2GB
        # VM. Chunking keeps the parallelism for cache misses while bounding
        # peak RSS to ~50 wallets' activity at a time.
        cids: set = set()
        cid_chunk = 50
        for i in range(0, len(to_eval), cid_chunk):
            if _stopping(stop):
                break
            res_acts = fetch_all_activity(to_eval[i:i + cid_chunk], cache_dir,
                                          activity_ttl_s, stop=stop)
            for acts in res_acts.values():
                for ev in acts:
                    if (ev.get("type") == "TRADE" and ev.get("side") == "BUY"
                            and ev.get("conditionId")):
                        cids.add(ev.get("conditionId"))
            del res_acts  # only the cid set is needed downstream — free the chunk
        if cids and needs_res and not _stopping(stop):
            resolutions = fetch_resolutions(cids, cfg.res_cache_dir)
            logger.info("[DISCOVERY] resolutions: %d/%d markets settled "
                        "(copy-replay gate + 1a/1e)", len(resolutions), len(cids))
        # Strategy 4: the resolved set above only dates CLOSED markets, but a
        # long-horizon bettor's positions are mostly still OPEN. Fetch end dates
        # for the as-yet-unresolved markets so a far-future bet's horizon is
        # measurable; merge them in as unresolved (winning_index=None) rows.
        if cids and cfg.s4_enabled and not _stopping(stop):
            open_cids = [c for c in cids if c not in resolutions]
            open_dates = fetch_open_end_dates(open_cids) if open_cids else {}
            resolutions.update(open_dates)
            logger.info("[DISCOVERY] long-horizon: dated %d/%d open markets "
                        "(Strategy 4)", len(open_dates), len(open_cids))

    evaluated: dict[str, Eval] = {}
    cost_model = CostModel.from_env()  # winning-markets cost floor (item A/B)
    for w in to_eval:
        if _stopping(stop):
            break
        m = metric_by_wallet.get(w)
        tstat = m.tstat if m else 0.0
        roi = m.roi if m else 0.0
        # lead-lag copyability (capture) first — it feeds theories 1c/1h
        capture = lead = hit = 0.0
        n_cap = 0
        buys = fetch_recent_buys(w, since, cfg.min_usd)
        if len(buys) >= cfg.min_ll_trades:
            # Bound the shared price-series cache (the biggest per-sweep
            # structure at the 400-pool) before this wallet adds up to 60 more.
            if len(price_cache) > PRICE_CACHE_MAX:
                price_cache.clear()
            agg = lead_lag_wallet(buys[:60], delay_s, horizon_s, price_cache)
            capture, lead = agg.avg_capture * 100, agg.avg_lead * 100
            hit, n_cap = agg.capture_hit_rate, agg.n
        # PnL curve + full feature context, then run the independent theories.
        cm = wallet_curve_metrics(w)
        ctx = build_wallet_context(
            w, cache_dir, activity_ttl_s, now=time.time(), lookback_ts=lookback,
            category=cfg.category, curve=cm, capture_cents=capture, lead_cents=lead,
            capture_hit_rate=hit, n_capture=n_cap, resolutions=resolutions)
        flags = evaluate_all(ctx, enabled=cfg.enabled_theories)
        ep = ctx.entry
        # copy-replay: replay copying this wallet's copyable BUYs (first entry
        # per market) held to resolution — the SAME action the live harness
        # takes — so selection measures what we actually do, not the wallet's
        # own closed-position ROI. exit_* is the two-horizon diagnostic.
        crs = score_copy_replay(ctx.buys, ctx.round_trips, min_usd=cfg.min_usd)
        fade = crs.fade_label(min_n=cfg.min_copy_replay_n, fade_roi=cfg.fade_roi) is not None
        # winning-markets-only (item A): score copy-and-hold per category and keep
        # only the categories whose net-of-cost edge clears the floor on enough
        # resolved copies. ``approved_categories`` is what the live engine gates on.
        cat_edges = select_copyable_categories(
            ctx.buys, cost_model, min_n=cfg.min_category_n, min_usd=cfg.min_usd)
        approved_cats = tuple(sorted(approved_category_set(cat_edges)))
        cat_edge_rows = tuple(
            (c, e.n, e.net_roi, e.approved) for c, e in sorted(cat_edges.items()))
        # the wallet's own median copyable BUY size, for conviction sizing (item C)
        copyable_usd = [
            float(b.usd) for b in ctx.buys
            if is_copyable_entry(float(getattr(b, "price", 0.0) or 0.0))
            and float(getattr(b, "usd", 0.0) or 0.0) >= cfg.min_usd]
        median_usd = statistics.median(copyable_usd) if copyable_usd else 0.0
        # Strategy 1 vs 4 — NOT exclusive (dual membership). `strategy` is a
        # display label (which horizon dominates the wallet's $); `long_horizon`
        # is the routing flag that ALSO adds the wallet to the Strategy-4 track
        # when it has a real long book. The copy funnel below is unaffected — it
        # scores every wallet on its near-term bets as before; s4 only adds the
        # long-horizon list. Defaults keep behaviour unchanged when s4 is off.
        strategy = "1"
        long_horizon = False
        hp = horizon_profile(ctx.buys, long_horizon_days=cfg.s4_long_horizon_days)
        if cfg.s4_enabled:
            label = classify_strategy(
                hp, min_dated_buys=cfg.s4_min_dated_buys,
                long_ratio_threshold=cfg.s4_min_long_ratio)
            strategy = label or "1"
            long_horizon = long_horizon_eligible(hp, min_long_buys=cfg.s4_min_long_buys)
        evaluated[w] = Eval(
            wallet=w, roi=roi, tstat=tstat,
            capture_cents=capture, lead_cents=lead, hit_rate=hit, n=n_cap,
            tail_ratio=ep.tail_ratio, copyable_ratio=ep.copyable_ratio,
            curve_sharpe=cm.sharpe, curve_drawdown=cm.max_drawdown_frac, net_pnl=cm.net_pnl,
            closed_hit_rate=(getattr(m, "hit_rate", 0.0) if m else 0.0),
            n_closed=(getattr(m, "n_closed", 0) if m else 0),
            copy_roi=crs.mean_roi, copy_tstat=crs.tstat, copy_n=crs.n,
            copy_hit=crs.hit_rate, exit_roi=crs.exit_mean_roi, exit_n=crs.exit_n, fade=fade,
            approved_categories=approved_cats, category_edges=cat_edge_rows,
            median_usd=median_usd,
            flagged_by=tuple(f.theory for f in flags),
            reason=" | ".join(f.reason for f in flags),
            strategy=strategy,
            long_horizon=long_horizon,
            long_horizon_ratio=hp.long_ratio,
            horizon_days=hp.mean_horizon_days,
            # P1-3 dossier fields (computed above, previously dropped)
            capital=(getattr(m, "capital", 0.0) if m else 0.0),
            concentration=(getattr(m, "concentration", 0.0) if m else 0.0),
            mean_entry=ep.mean_entry,
            up_ratio=cm.up_ratio,
        )
    return evaluated
