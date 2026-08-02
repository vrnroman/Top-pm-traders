"""s-r7m3qk (2026-08-02) discovery-plumbing batch: byte-budgeted disk caches
and never caching a failed /activity fetch.

Both pin a live prod finding from the 2026-08-02 inspection:

  * ``prune_cache`` bounded the wallet-activity cache by FILE COUNT only. Entry
    size is the wallet's history length, so as the widened (P1-1) pool drifted
    toward whales the same 4000 files grew to **8.2GB of a 20G disk** (mean
    1.7MB, max 4.5MB) and disk-watch tripped "free 4.1G shrinking 1521MB/day →
    floor in ~1d". Bytes are what the disk runs out of, so bytes are the bound.

  * ``fetch_activity`` could not tell "the API failed" from "no more trades":
    ``_get`` returns None after four failed attempts, the caller broke on it,
    and the empty/truncated list was written to the cache and served as truth
    for the full 24h TTL — four sweeps of scoring a throttled wallet as if it
    had no history, feeding both the skill ranking and the LLM gate dossier.
    18 wallets held a cached ``[]`` in prod when this was found.
"""

from __future__ import annotations

import json
import os

import pytest

from src.copy_trading import discovery_data as dd


@pytest.fixture(autouse=True)
def _clean_failure_tally():
    """The tally is module state cleared per sweep; isolate it per test."""
    dd._activity_fetch_failures.clear()
    yield
    dd._activity_fetch_failures.clear()


# --------------------------------------------------------------------------- #
# prune_cache: the byte budget
# --------------------------------------------------------------------------- #

def _write(d, name, size, mtime):
    p = d / name
    p.write_text("x" * size)
    os.utime(p, (mtime, mtime))
    return p


def test_prune_cache_evicts_oldest_until_under_byte_budget(tmp_path):
    # 5 files x 1000B = 5000B, budget 2500B -> the 3 oldest go.
    for i in range(5):
        _write(tmp_path, f"w{i}.json", 1000, 1_000_000 + i * 100)
    removed = dd.prune_cache(str(tmp_path), ttl_s=0, max_files=None,
                             max_bytes=2500)
    assert removed == 3
    survivors = sorted(p.name for p in tmp_path.glob("*.json"))
    assert survivors == ["w3.json", "w4.json"]   # newest kept


def test_prune_cache_byte_budget_binds_when_count_cap_would_not(tmp_path):
    """The exact prod shape: well under the file-count cap, way over on disk."""
    for i in range(10):
        _write(tmp_path, f"w{i}.json", 2_000_000, 1_000_000 + i * 100)
    removed = dd.prune_cache(str(tmp_path), ttl_s=0, max_files=4000,
                             max_bytes=5_000_000)
    assert removed == 8            # count cap alone would have removed nothing
    total = sum(p.stat().st_size for p in tmp_path.glob("*.json"))
    assert total <= 5_000_000


def test_prune_cache_count_cap_still_honoured_without_byte_budget(tmp_path):
    for i in range(6):
        _write(tmp_path, f"w{i}.json", 10, 1_000_000 + i * 100)
    assert dd.prune_cache(str(tmp_path), ttl_s=0, max_files=2) == 4
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_prune_cache_ttl_runs_before_the_budget(tmp_path):
    import time
    now = time.time()
    _write(tmp_path, "old.json", 5000, now - 10_000)     # past TTL
    _write(tmp_path, "new.json", 5000, now)
    removed = dd.prune_cache(str(tmp_path), ttl_s=100, max_bytes=6000)
    assert removed == 1                                   # TTL took the old one
    assert [p.name for p in tmp_path.glob("*.json")] == ["new.json"]


def test_prune_cache_no_bounds_is_ttl_only(tmp_path):
    for i in range(3):
        _write(tmp_path, f"w{i}.json", 1_000_000, 1_000_000 + i)
    assert dd.prune_cache(str(tmp_path), ttl_s=0) == 0
    assert len(list(tmp_path.glob("*.json"))) == 3


def test_env_bytes_reads_gib_and_falls_back(monkeypatch):
    monkeypatch.setenv("PM_TEST_GB", "0.5")
    assert dd._env_bytes("PM_TEST_GB", 999) == int(0.5 * 1024 ** 3)
    monkeypatch.setenv("PM_TEST_GB", "not-a-number")
    assert dd._env_bytes("PM_TEST_GB", 777) == 777        # bad value -> default
    monkeypatch.delenv("PM_TEST_GB")
    assert dd._env_bytes("PM_TEST_GB", 777) == 777        # absent -> default


def test_default_budgets_fit_the_20g_disk():
    """The whole point of the fix: both caches together must leave room for the
    ~1G of ledgers/sqlite, the ~1.5G image, and an image pull's headroom."""
    total_gb = (dd._WCACHE_MAX_BYTES + dd._RESCACHE_MAX_BYTES) / 1024 ** 3
    assert total_gb + (dd._DISK_RESERVE_BYTES / 1024 ** 3) >= 12.0  # sized to a 20G disk
    assert total_gb <= 8.0


def test_reserve_derives_the_budget_from_free_space(monkeypatch, tmp_path):
    """The class-killer: a hand-picked GB ceiling encodes a mean file size that
    drifts (it has gone stale twice). With a reserve, the cache yields to
    whatever else is on the disk instead of rotting."""
    for i in range(10):
        _write(tmp_path, f"w{i}.json", 1_000_000, 1_000_000 + i * 100)   # 10MB total

    class _DU:
        free = 5_000_000            # only 5MB free
    monkeypatch.setattr(dd.shutil, "disk_usage", lambda p: _DU())

    # allowance = 10MB (cache) + 5MB (free) - 12MB (reserve) = 3MB
    removed = dd.prune_cache(str(tmp_path), ttl_s=0, max_bytes=None,
                             reserve_bytes=12_000_000)
    assert removed == 7
    total = sum(p.stat().st_size for p in tmp_path.glob("*.json"))
    assert total <= 3_000_000


def test_reserve_never_loosens_a_tighter_static_cap(monkeypatch, tmp_path):
    for i in range(10):
        _write(tmp_path, f"w{i}.json", 1_000_000, 1_000_000 + i * 100)

    class _DU:
        free = 500_000_000          # plenty free
    monkeypatch.setattr(dd.shutil, "disk_usage", lambda p: _DU())

    dd.prune_cache(str(tmp_path), ttl_s=0, max_bytes=4_000_000,
                   reserve_bytes=1_000_000)
    total = sum(p.stat().st_size for p in tmp_path.glob("*.json"))
    assert total <= 4_000_000       # the static cap still binds


def test_reserve_survives_an_unstatable_filesystem(monkeypatch, tmp_path):
    _write(tmp_path, "w.json", 1_000_000, 1_000_000)
    monkeypatch.setattr(dd.shutil, "disk_usage",
                        lambda p: (_ for _ in ()).throw(OSError("no fs")))
    assert dd.prune_cache(str(tmp_path), ttl_s=0, reserve_bytes=1) == 0


# --------------------------------------------------------------------------- #
# _prune_disk_caches: runs on the way out too, and never breaks the sweep
# --------------------------------------------------------------------------- #

class _Cfg:
    res_cache_dir = None


def test_prune_disk_caches_swallows_errors(monkeypatch, caplog):
    def boom(*a, **k):
        raise OSError("disk gone")
    monkeypatch.setattr(dd, "prune_cache", boom)
    dd._prune_disk_caches("/nonexistent", _Cfg(), 86400.0, when="post")  # no raise


def test_evaluate_sweep_prunes_on_the_way_out_even_when_stopped(monkeypatch, tmp_path):
    """A sweep writes its whole working set after the entry prune, so the exit
    prune is what bounds the idle disk. It must run on the early-return path."""
    calls = []
    monkeypatch.setattr(dd, "_prune_disk_caches",
                        lambda *a, **k: calls.append(k.get("when")))
    monkeypatch.setattr(dd, "build_universe", lambda *a, **k: [])

    import threading
    stop = threading.Event()
    stop.set()                                   # force the early `return {}`

    cfg = dd.DiscoveryConfig(universe=None) if hasattr(dd, "DiscoveryConfig") else None
    out = dd.evaluate_sweep(cfg, cache_dir=str(tmp_path), stop=stop)
    assert out == {}
    assert calls == ["pre", "post"]


# --------------------------------------------------------------------------- #
# fetch_activity: a failed fetch is never cached
# --------------------------------------------------------------------------- #

def _stub_get(monkeypatch, pages):
    """pages: list of what successive _get calls return (None == API failure)."""
    seq = iter(pages)
    monkeypatch.setattr(dd, "_get", lambda *a, **k: next(seq, None))


def test_failed_first_page_is_not_cached(monkeypatch, tmp_path):
    _stub_get(monkeypatch, [None])
    out = dd.fetch_activity("0xdead", str(tmp_path), ttl_s=86400.0)
    assert out == []
    assert not (tmp_path / "0xdead.json").exists()      # the whole bug
    assert dd._activity_fetch_failures == ["0xdead"]


def test_truncated_pagination_is_not_cached(monkeypatch, tmp_path):
    full = [{"t": i} for i in range(500)]
    _stub_get(monkeypatch, [full, None])                # page 2 dies
    out = dd.fetch_activity("0xbeef", str(tmp_path), ttl_s=86400.0)
    assert len(out) == 500                              # partial is returned...
    assert not (tmp_path / "0xbeef.json").exists()      # ...but never pinned


def test_genuinely_empty_wallet_is_still_cached(monkeypatch, tmp_path):
    """`[]` from a healthy API is a fact worth caching — don't over-correct."""
    _stub_get(monkeypatch, [[]])
    out = dd.fetch_activity("0xcafe", str(tmp_path), ttl_s=86400.0)
    assert out == []
    assert json.loads((tmp_path / "0xcafe.json").read_text()) == []
    assert dd._activity_fetch_failures == []


def test_complete_short_page_is_cached(monkeypatch, tmp_path):
    _stub_get(monkeypatch, [[{"t": 1}, {"t": 2}]])      # <500 == end of data
    out = dd.fetch_activity("0xfeed", str(tmp_path), ttl_s=86400.0)
    assert len(out) == 2
    assert len(json.loads((tmp_path / "0xfeed.json").read_text())) == 2


def test_a_poisoned_cache_entry_is_never_written_over_a_good_one(monkeypatch, tmp_path):
    """The damaging case: a good entry exists, its TTL lapses, the refetch is
    throttled. The stale-but-real history must survive rather than be replaced
    by an empty list that then reads as truth for another full TTL."""
    good = [{"t": i} for i in range(10)]
    p = tmp_path / "0xaaa.json"
    p.write_text(json.dumps(good))
    os.utime(p, (1_000_000, 1_000_000))                 # long expired
    _stub_get(monkeypatch, [None])
    dd.fetch_activity("0xaaa", str(tmp_path), ttl_s=1.0)
    assert json.loads(p.read_text()) == good


def test_incomplete_fetch_reporter_warns_and_never_raises(caplog):
    dd._activity_fetch_failures.clear()
    dd._activity_fetch_failures.extend(["0x1", "0x2", "0x3", "0x4"])
    with caplog.at_level("WARNING", logger="poly_poly_bot"):
        dd._report_incomplete_fetches()
    assert "4 wallet(s) had an INCOMPLETE" in caplog.text
    dd._activity_fetch_failures.clear()
    dd._report_incomplete_fetches()                     # silent when clean
