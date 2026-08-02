"""Tests for the /golive pre-flip gate and its pure checker."""

from __future__ import annotations

import pytest

from src.copy_trading import promotion_gate as pg
from src.copy_trading import promotion_state as ps
from src.copy_trading.copy_paper import PaperPosition

FLOOR_KW = dict(min_n=15, min_roi=0.10, min_tstat=0.0,
                min_second_half_roi=-0.10, min_conditions=8, min_categories=3)


def pos(target, i, *, pnl, spent=10.0, entry=0.5):
    return PaperPosition(
        copy_id=f"{target}-{i}", target=target, condition_id=f"c{i}",
        token_id=f"T{i}", outcome_index=0, category=f"cat{i % 4}",
        their_price=entry, entry_price=entry, shares=spent / entry, spent=spent,
        drag_bps=0, opened_ts=float(i), closed=True, won=(pnl > 0), pnl=pnl,
        closed_ts=float(i))


def _ready_positions(n=30):
    return [pos("0xA", i, pnl=1.2) for i in range(n)]


def test_golive_ready_when_all_pass():
    s = pg.compute_stats("0xA", _ready_positions(30))
    ready, checks = pg.golive_check(
        s, last_trade_ts=1_000_000.0, now=1_000_000.0 + 86400,
        min_settled=30, max_idle_days=14.0, min_roi=0.0, floor_kwargs=FLOOR_KW)
    assert ready is True
    assert all(ok for _, ok, _ in checks)


def test_golive_holds_on_thin_sample():
    s = pg.compute_stats("0xA", _ready_positions(20))     # < 30 golive bar
    ready, checks = pg.golive_check(
        s, last_trade_ts=1_000_000.0, now=1_000_000.0,
        min_settled=30, max_idle_days=14.0, min_roi=0.0, floor_kwargs=FLOOR_KW)
    assert ready is False
    assert any(("settled" in label and not ok) for label, ok, _ in checks)


def test_golive_holds_on_stale_wallet():
    s = pg.compute_stats("0xA", _ready_positions(30))
    ready, checks = pg.golive_check(
        s, last_trade_ts=0.0, now=100 * 86400,             # 100 days idle
        min_settled=30, max_idle_days=14.0, min_roi=0.0, floor_kwargs=FLOOR_KW)
    assert ready is False
    assert any(("active" in label and not ok) for label, ok, _ in checks)


def test_golive_holds_when_roi_went_negative():
    s = pg.compute_stats("0xA", [pos("0xA", i, pnl=-0.5) for i in range(30)])
    ready, checks = pg.golive_check(
        s, last_trade_ts=1.0, now=1.0,
        min_settled=30, max_idle_days=14.0, min_roi=0.0, floor_kwargs=FLOOR_KW)
    assert ready is False


# --- honest-metrics floors (owner ruling 2026-07-25) ---

_BASE = dict(last_trade_ts=1_000_000.0, now=1_000_000.0 + 3600,
             min_settled=30, max_idle_days=14.0, min_roi=0.0, floor_kwargs=FLOOR_KW)


def test_golive_honest_ideal_roi_binds():
    s = pg.compute_stats("0xA", _ready_positions(30))
    ready, _ = pg.golive_check(
        s, min_ideal_roi=0.0, ideal_roi=0.05, n_ideal_settled=12, **_BASE)
    assert ready is True
    ready, checks = pg.golive_check(
        s, min_ideal_roi=0.0, ideal_roi=-0.03, n_ideal_settled=12, **_BASE)
    assert ready is False
    assert any("at-their-price" in label and not ok for label, ok, _ in checks)
    # fails CLOSED when there are no clean-era settles to measure
    ready, checks = pg.golive_check(
        s, min_ideal_roi=0.0, ideal_roi=None, n_ideal_settled=0, **_BASE)
    assert ready is False
    assert any("no clean-era settled copies" in str(detail)
               for _, ok, detail in checks if not ok)


def test_golive_honest_persistence_binds():
    s = pg.compute_stats("0xA", _ready_positions(30))
    ready, _ = pg.golive_check(
        s, min_split_half_corr=0.0, book_corr=(0.12, 7), **_BASE)
    assert ready is True
    ready, checks = pg.golive_check(
        s, min_split_half_corr=0.0, book_corr=(-0.05, 7), **_BASE)
    assert ready is False
    assert any("persistence" in label and not ok for label, ok, _ in checks)
    # fails CLOSED while the book is too young to measure
    ready, checks = pg.golive_check(
        s, min_split_half_corr=0.0, book_corr=(None, 2), **_BASE)
    assert ready is False
    assert any("unmeasurable" in str(detail) for _, ok, detail in checks if not ok)


def test_honest_kwargs_from_toggle():
    class C:
        copy_golive_honest_metrics = True
        copy_golive_min_ideal_roi = 0.0
        copy_golive_min_ideal_settled = 5
        copy_golive_min_split_half_corr = 0.0
        copy_golive_min_clean_settled = 30
    assert pg.honest_kwargs_from(C) == {
        "min_ideal_roi": 0.0, "min_ideal_settled": 5,
        "min_split_half_corr": 0.0, "min_clean_settled": 30}
    C.copy_golive_honest_metrics = False
    assert pg.honest_kwargs_from(C) == {
        "min_ideal_roi": None, "min_ideal_settled": None,
        "min_split_half_corr": None, "min_clean_settled": None}


def test_golive_honest_ideal_roi_needs_a_minimum_sample():
    # 2026-07-27 review: one lucky clean settle must not clear the real-money
    # gate — the ideal check carries the repo's thin-sample band (5).
    s = pg.compute_stats("0xA", _ready_positions(30))
    ready, checks = pg.golive_check(
        s, min_ideal_roi=0.0, min_ideal_settled=5,
        ideal_roi=0.90, n_ideal_settled=1, **_BASE)
    assert ready is False
    assert any("need ≥5" in str(detail) for _, ok, detail in checks if not ok)
    ready, _ = pg.golive_check(
        s, min_ideal_roi=0.0, min_ideal_settled=5,
        ideal_roi=0.03, n_ideal_settled=5, **_BASE)
    assert ready is True


def test_ideal_roi_for_scopes_quarantines_and_empties():
    # clean rows: entry == their, so ideal == pnl; dust row excluded
    good = [pos("0xA", i, pnl=1.0) for i in range(2)]
    for p in good:
        p.ideal_pnl = p.pnl
        p.opened_ts = 100.0
    old = [pos("0xA", 50, pnl=9.0)]
    old[0].ideal_pnl = 9.0
    old[0].opened_ts = 10.0                       # pre-floor
    dust = pos("0xA", 51, pnl=1000.0)
    dust.their_price, dust.entry_price = 0.60, 0.001
    dust.opened_ts = 100.0
    roi, n = pg.ideal_roi_for(good + old + [dust], min_opened_ts=50.0)
    assert n == 2 and roi == pytest.approx(0.1)   # only the clean rows
    assert pg.ideal_roi_for(good, min_opened_ts=10_000.0) == (None, 0)


# --- the Telegram handler ---

@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setenv("PROMOTED_WALLETS_STORE", str(tmp_path / "p.json"))
    ps.clear_cache()
    yield
    ps.clear_cache()


def test_golive_handler_reports(stores, tmp_path, monkeypatch):
    import json
    import time

    from src import telegram_bot
    wallet = "0x" + "a" * 40
    now = time.time()
    # a ready ledger for the wallet, with RECENT timestamps (active within 14d)
    ledger = tmp_path / "ledger.jsonl"
    with open(ledger, "w") as f:
        for i in range(30):
            p = pos(wallet, i, pnl=1.2)
            p.opened_ts = now - 3600 * i        # most recent bet ~now
            p.closed_ts = now - 3600 * i
            f.write(json.dumps(p.__dict__) + "\n")
    monkeypatch.setattr(telegram_bot.CONFIG, "copy_paper_ledger", str(ledger), raising=False)
    # legacy path: honest-metrics floors OFF (the honest path is covered below)
    monkeypatch.setattr(telegram_bot.CONFIG, "copy_golive_honest_metrics", False, raising=False)
    ps.add_promoted(wallet, tier="1b")

    sent = []
    monkeypatch.setattr(telegram_bot, "_send_chunked", lambda t, **k: sent.append(t))
    telegram_bot._handle_golive(f"/golive {wallet}")
    assert sent and "READY for live" in sent[0]


def _write_rows(f, target, pnls, now, *, spacing=1800):
    """Rows with strictly increasing recent timestamps (all clean-era)."""
    import json
    for i, pnl in enumerate(pnls):
        p = pos(target, i, pnl=pnl)
        p.ideal_pnl = pnl                    # entry == their: drag-free twin
        p.opened_ts = now - spacing * (len(pnls) - i)
        p.closed_ts = now - spacing * (len(pnls) - i)
        f.write(json.dumps(p.__dict__) + "\n")


def _honest_handler_setup(stores, tmp_path, monkeypatch, *, era_floor):
    """A promoted wallet whose book has measurable positive persistence."""
    import json
    import time

    from src import telegram_bot
    wallet = "0x" + "a" * 40
    now = time.time()
    ledger = tmp_path / "ledger.jsonl"
    with open(ledger, "w") as f:
        # the promoted wallet: 30 clean settles, ROI +13%, halves 0.14/0.12
        _write_rows(f, wallet, [1.0] * 5 + [2.0] * 5 + [1.2] * 20, now)
        # two more wallets so the book corr is measurable: halves (0.2, 0.4)
        # and (0.3, 0.6) -> strongly positive split-half corr across the book
        _write_rows(f, "0x" + "b" * 40, [2.0] * 5 + [4.0] * 5, now)
        _write_rows(f, "0x" + "c" * 40, [3.0] * 5 + [6.0] * 5, now)
    monkeypatch.setattr(telegram_bot.CONFIG, "copy_paper_ledger", str(ledger), raising=False)
    state = tmp_path / "ab_race_state.json"
    state.write_text(json.dumps({"era_floor_ts": era_floor(now)}))
    monkeypatch.setattr(telegram_bot, "_ab_race_state_path", lambda: str(state))
    ps.add_promoted(wallet, tier="1b")
    return telegram_bot, wallet


def test_golive_handler_honest_floors_pass_on_clean_era(stores, tmp_path, monkeypatch):
    telegram_bot, wallet = _honest_handler_setup(
        stores, tmp_path, monkeypatch, era_floor=lambda now: now - 86400)
    sent = []
    monkeypatch.setattr(telegram_bot, "_send_chunked", lambda t, **k: sent.append(t))
    telegram_bot._handle_golive(f"/golive {wallet}")
    out = sent[0]
    assert "READY for live" in out
    assert "at-their-price ROI" in out and "✅" in out
    assert "book persistence" in out
    # the new floors default ON (mirrored values) — no CONFIG monkeypatching
    assert telegram_bot.CONFIG.copy_golive_honest_metrics is True


def test_golive_handler_honest_floors_fail_closed_when_era_too_young(
        stores, tmp_path, monkeypatch):
    # floor ~a minute ago: the wallet's whole record is pre-floor -> the honest
    # checks cannot bless it (fail CLOSED, never on artifact-era evidence)
    telegram_bot, wallet = _honest_handler_setup(
        stores, tmp_path, monkeypatch, era_floor=lambda now: now - 60)
    sent = []
    monkeypatch.setattr(telegram_bot, "_send_chunked", lambda t, **k: sent.append(t))
    telegram_bot._handle_golive(f"/golive {wallet}")
    out = sent[0]
    assert "HOLD" in out and "READY for live" not in out
    assert "no clean-era settled copies" in out
    assert "unmeasurable" in out


def test_golive_handler_usage_without_arg(monkeypatch):
    from src import telegram_bot
    sent = []
    monkeypatch.setattr(telegram_bot, "send_message", lambda *a, **k: sent.append(a))
    telegram_bot._handle_golive("/golive")
    assert sent and "Usage" in sent[0][0]


# --- s-r7m3qk (2026-08-02): the clean-era settled bar -----------------------

def test_golive_blocks_a_wallet_whose_record_is_all_artifact_era():
    """compute_stats has NO era parameter, so the settled-count, paper-ROI and
    promotion-floor bars are all-time realized — the very number P0-1 voided.
    Before this bar, 40 artifact-era settles at +14% plus 5 lucky clean settles
    and a 3-wallet correlation read 🟢 READY on evidence the repo itself
    declared invalid."""
    s = pg.compute_stats("0xA", _ready_positions(40))
    ready, checks = pg.golive_check(
        s, min_ideal_roi=0.0, min_ideal_settled=5, min_clean_settled=30,
        ideal_roi=0.02, n_ideal_settled=5,
        min_split_half_corr=0.0, book_corr=(0.10, 3), **_BASE)
    assert ready is False
    assert any("CLEAN ERA" in label and not ok for label, ok, _ in checks)
    # and it opens once the clean era actually carries the evidence
    ready, _ = pg.golive_check(
        s, min_ideal_roi=0.0, min_ideal_settled=5, min_clean_settled=30,
        ideal_roi=0.02, n_ideal_settled=30,
        min_split_half_corr=0.0, book_corr=(0.10, 3), **_BASE)
    assert ready is True


def test_clean_era_bar_fails_closed_with_no_era_floor():
    """No recorded era floor means the caller passes n_ideal_settled=0."""
    s = pg.compute_stats("0xA", _ready_positions(40))
    ready, _ = pg.golive_check(
        s, min_ideal_roi=0.0, min_clean_settled=30,
        ideal_roi=None, n_ideal_settled=0, **_BASE)
    assert ready is False


def test_clean_era_bar_absent_when_honest_metrics_off():
    s = pg.compute_stats("0xA", _ready_positions(30))
    _, checks = pg.golive_check(s, min_clean_settled=None, **_BASE)
    assert not any("CLEAN ERA" in label for label, _, _ in checks)
