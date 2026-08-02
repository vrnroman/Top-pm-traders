"""Tests for the P1-1 cache prerequisite (2026-07-28).

Prod ran 2026-06..07 with the discovery disk caches (wcache/rescache) never
created — nothing mkdir'd them and the writers swallowed OSError, so every
sweep re-fetched all activity and re-resolved ~40k markets. These pin the
resurrection: the sweep creates its cache dirs, a dead cache warns LOUD once
per sweep per dir, and the resolution cid pre-pass is chunked so the widened
(400-wallet) pool can't pin RSS on the 2GB VM.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from src.copy_trading import discovery_data as dd
from src.copy_trading.discovery import DiscoveryConfig


def test_sweep_creates_missing_cache_dirs(monkeypatch, tmp_path):
    """evaluate_sweep must create its cache dirs itself (self-healing deploy)."""
    wcache = tmp_path / "wcache"
    rescache = tmp_path / "rescache"
    monkeypatch.setattr(dd, "build_universe", lambda target, **kw: [])
    cfg = DiscoveryConfig(res_cache_dir=str(rescache))
    dd.evaluate_sweep(cfg, cache_dir=str(wcache))
    assert wcache.is_dir() and rescache.is_dir()


def test_cache_write_failure_warns_once_per_sweep_per_dir(monkeypatch, caplog, tmp_path):
    """A dead cache must be LOUD once per sweep — not silent for weeks, and not
    one warning per failed row."""
    dd._cache_write_warned_dirs.clear()
    # `[]` is a SUCCESSFUL fetch of a wallet with no activity — that result is
    # cached, so the write is attempted and the dead-dir warning fires. (`None`
    # would mean the API failed; since s-r7m3qk that is deliberately never
    # cached, so it would never reach the write path at all.)
    monkeypatch.setattr(dd, "_get", lambda *a, **k: [])
    bad_dir = str(tmp_path / "does-not-exist" / "wcache")
    with caplog.at_level(logging.WARNING, logger="poly_poly_bot"):
        dd.fetch_activity("0xA", bad_dir, 3600)
        dd.fetch_activity("0xB", bad_dir, 3600)
    warns = [r for r in caplog.records if "cache write failed" in r.message]
    assert len(warns) == 1                      # once per dir per sweep
    # next sweep (the set is re-armed in evaluate_sweep) warns again
    caplog.clear()
    dd._cache_write_warned_dirs.clear()
    with caplog.at_level(logging.WARNING, logger="poly_poly_bot"):
        dd.fetch_activity("0xC", bad_dir, 3600)
    warns = [r for r in caplog.records if "cache write failed" in r.message]
    assert len(warns) == 1
    dd._cache_write_warned_dirs.clear()


def test_cid_prepass_is_chunked_at_50(monkeypatch):
    """The resolutions cid collection must not hold the whole pool's /activity
    at once (OOM risk at a 400-wallet pool): fetch_all_activity is called with
    <= 50 wallets per chunk in the cid pre-pass."""
    universe = [f"0x{i:04d}" for i in range(120)]
    monkeypatch.setattr(dd, "build_universe", lambda target, **kw: list(universe))

    calls: list[list[str]] = []

    def fake_fetch_all(wallets, *a, **k):
        calls.append(list(wallets))
        return {w: [{"type": "TRADE", "side": "BUY", "conditionId": f"0xCID{w}"}]
                for w in wallets}

    monkeypatch.setattr(dd, "fetch_all_activity", fake_fetch_all)
    monkeypatch.setattr(dd, "compute_wallet_metrics",
                        lambda a, **kw: SimpleNamespace(tstat=5.0, roi=0.1))
    monkeypatch.setattr(dd, "select_targets",
                        lambda scored, **kw: [SimpleNamespace(address=w, metrics=m)
                                              for w, m in scored.items()])
    monkeypatch.setattr(dd, "fetch_recent_buys", lambda *a, **k: [])
    monkeypatch.setattr(dd, "wallet_curve_metrics", lambda *a, **k: dd.CurveMetrics())
    monkeypatch.setattr(dd, "build_wallet_context",
                        lambda w, *a, **k: dd.WalletContext(wallet=w, now=0.0))
    monkeypatch.setattr(dd, "fetch_resolutions", lambda cids, cache_dir=None, **k: {})
    monkeypatch.setenv("WALLET_DISCOVERY_CHUNK", "100")   # score phase: 100 + 20
    monkeypatch.setenv("WALLET_DISCOVERY_BATCH_PAUSE_S", "0")

    dd.evaluate_sweep(DiscoveryConfig())

    sizes = [len(c) for c in calls]
    # first two calls are the score-chunk loop (100 + 20); the cid pre-pass
    # must follow in chunks of <= 50 (50 + 50 + 20).
    assert sizes[:2] == [100, 20]
    assert sizes[2:] == [50, 50, 20]
