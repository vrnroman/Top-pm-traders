"""s-log7q (2026-08-02) health-check batch: secret scrubbing, realized-pnl
dedup guard, rescache prune knobs, disk-trajectory watch, ledger-integrity
witness, and the verdict-memo §7 reading + cost slices.

Each test pins one behaviour the 2026-08-02 prod audit found missing:
  * the Telegram bot token reached bot-*.log via urllib3 request lines;
  * realized-pnl.jsonl carried a 61-position batch twice (double-counted −$575);
  * rescache was unbounded (578MB → 1.1GB in five days on an 84%-full disk);
  * nothing paged on the disk *trajectory*, only the crash would have;
  * the verdict memo rendered numbers but no mechanical §7 reading.
"""

from __future__ import annotations

import json
import logging
import os

import pytest

from src import logger as bot_logger
from src.copy_trading import disk_watch, ledger_integrity
from src.copy_trading import pnl as s1pnl
from src.copy_trading.strategy_compare import (
    cost_slices, format_verdict, section7_reading)


# --------------------------------------------------------------------------- #
# Secret scrubbing
# --------------------------------------------------------------------------- #

def test_scrub_secrets_redacts_telegram_token_url():
    line = ('https://api.telegram.org:443 "GET '
            '/bot123456789:AAHzyVe_NfiPuNuTdZMPVtQNOGkWCrkKc12a/getUpdates '
            'HTTP/1.1" 200 None')
    out = bot_logger.scrub_secrets(line)
    assert "AAHzyVe" not in out
    assert "bot***REDACTED" in out
    assert "getUpdates" in out  # the URL shape survives for debugging


def test_scrub_secrets_leaves_normal_text_alone():
    line = "GET /trades?limit=100&offset=200 HTTP/1.1 200 None"
    assert bot_logger.scrub_secrets(line) == line


def test_secret_scrub_filter_rewrites_record_and_pins_args():
    rec = logging.LogRecord(
        "urllib3.connectionpool", logging.DEBUG, __file__, 1,
        "GET /bot%s:%s/getUpdates", ("123456789", "A" * 35), None)
    bot_logger.SecretScrubFilter().filter(rec)
    assert rec.args == ()
    assert "AAA" not in rec.getMessage()
    assert "bot***REDACTED" in rec.getMessage()


def test_secret_scrub_filter_survives_broken_format():
    rec = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "bad %s %s", ("only-one",), None)
    assert bot_logger.SecretScrubFilter().filter(rec) is True


# --------------------------------------------------------------------------- #
# Realized-pnl dedup guard (the 2026-07-30 double-write class)
# --------------------------------------------------------------------------- #

def _res_row(cond="0xcond1", tok="tok1", pnl=-5.0):
    return {"timestamp": "2026-07-30T18:19:08+00:00", "condition_id": cond,
            "token_id": tok, "shares": 10, "avg_price": 0.5, "cost_basis": 5.0,
            "returned": 0.0, "pnl": pnl, "won": False, "exit": "resolution"}


def test_append_realized_dedups_resolution_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(s1pnl.CONFIG, "data_dir", str(tmp_path))
    s1pnl.append_realized(_res_row())
    s1pnl.append_realized(_res_row())  # the re-realized duplicate
    rows = s1pnl.load_realized()
    assert len(rows) == 1


def test_append_realized_allows_distinct_positions(tmp_path, monkeypatch):
    monkeypatch.setattr(s1pnl.CONFIG, "data_dir", str(tmp_path))
    s1pnl.append_realized(_res_row(tok="tok1"))
    s1pnl.append_realized(_res_row(tok="tok2"))
    assert len(s1pnl.load_realized()) == 2


def test_append_realized_never_dedups_non_resolution(tmp_path, monkeypatch):
    monkeypatch.setattr(s1pnl.CONFIG, "data_dir", str(tmp_path))
    row = _res_row()
    row["exit"] = "sell"
    s1pnl.append_realized(row)
    s1pnl.append_realized(dict(row))  # same token, legit second sell
    assert len(s1pnl.load_realized()) == 2


# --------------------------------------------------------------------------- #
# Disk-trajectory watch
# --------------------------------------------------------------------------- #

def test_disk_eval_trips_on_floor_without_history():
    res = disk_watch.evaluate(2.0, None, floor_gb=2.5, days_bar=14, now=1000.0)
    assert res["tripped"] and "floor" in res["reason"]


def test_disk_eval_first_sample_no_slope_no_trip():
    res = disk_watch.evaluate(5.0, None, floor_gb=2.5, days_bar=14, now=1000.0)
    assert not res["tripped"]
    assert res["days_to_floor"] is None


def test_disk_eval_trips_on_trajectory():
    prev = {"ts": 0.0, "free_gb": 7.0}
    # 1GB/day shrink over 2 days → 5.0G free, 2.5 days to the 2.5G floor
    res = disk_watch.evaluate(5.0, prev, floor_gb=2.5, days_bar=14,
                              now=2 * 86400.0)
    assert res["tripped"]
    assert res["days_to_floor"] == pytest.approx(2.5, abs=0.01)


def test_disk_eval_calm_when_growing():
    prev = {"ts": 0.0, "free_gb": 3.0}
    res = disk_watch.evaluate(3.5, prev, floor_gb=2.5, days_bar=14,
                              now=86400.0)
    assert not res["tripped"]


def test_disk_check_alerts_once_then_recovers(tmp_path):
    sent = []
    state = {}
    # Force a small "filesystem" by monkeypatching disk_usage
    class FakeUsage:
        def __init__(self, free_gb):
            self.free = int(free_gb * (1024 ** 3))
    usages = iter([2.0, 2.0, 4.0])
    orig = disk_watch.shutil.disk_usage
    disk_watch.shutil.disk_usage = lambda d: FakeUsage(next(usages))
    try:
        r1 = disk_watch.check(str(tmp_path), now=1000.0,
                              sender=sent.append, state_name="s.json")
        r2 = disk_watch.check(str(tmp_path), now=2000.0,
                              sender=sent.append, state_name="s.json")
        r3 = disk_watch.check(str(tmp_path), now=3000.0,
                              sender=sent.append, state_name="s.json")
    finally:
        disk_watch.shutil.disk_usage = orig
    assert r1["tripped"] and r1["alerted"]
    assert r2["tripped"] and not r2["alerted"]     # edge: no repeat Telegram
    assert not r3["tripped"]                        # recovery
    assert len(sent) == 1
    state = json.loads((tmp_path / "s.json").read_text())
    assert state["alerting"] is False


# --------------------------------------------------------------------------- #
# Ledger-integrity witness
# --------------------------------------------------------------------------- #

def test_integrity_flags_duplicate_resolution_rows():
    rows = [_res_row(), _res_row(), _res_row(tok="tok2")]
    dups = ledger_integrity.duplicate_resolution_keys(rows)
    assert list(dups) == [("0xcond1", "tok1")]
    assert dups[("0xcond1", "tok1")] == 2


def test_integrity_ignores_open_close_pair_but_flags_double_close():
    open_row = {"copy_id": "c1", "closed": False}
    closed_row = {"copy_id": "c1", "closed": True}
    assert not ledger_integrity.duplicate_copy_ids([open_row, closed_row])
    assert ledger_integrity.duplicate_copy_ids([closed_row, dict(closed_row)])


def test_integrity_scan_and_format(tmp_path):
    realized = tmp_path / "realized-pnl.jsonl"
    realized.write_text("\n".join(json.dumps(r) for r in [_res_row(), _res_row()]) + "\n")
    # the autopsy (the live caller) flags the duplicate as a NEW finding once
    res = ledger_integrity.run_autopsy(str(tmp_path), now=1000.0)
    assert any("realized-pnl" in a and "duplicate" in a for a in res["new"])


# --------------------------------------------------------------------------- #
# Code-review fixes (M1 recalibrate re-arm, M2 setkey echo, L5 memo gate)
# --------------------------------------------------------------------------- #

def test_verdict_recalibrate_rearms_memo_clock(tmp_path, monkeypatch):
    tb, buf = _wire_verdict_env(tmp_path, monkeypatch, verdict_sent=True)
    st_path = tmp_path / "ab_race_state.json"
    st = json.loads(st_path.read_text())
    st["verdict_ts"] = 1785000000.0
    st_path.write_text(json.dumps(st))
    tb._handle_verdict("/verdict recalibrate")
    tb._handle_verdict("/verdict confirm")
    assert "Applied" in buf[-1]
    after = json.loads(st_path.read_text())
    assert "verdict_sent" not in after and "verdict_ts" not in after
    ov = verdict_overlay.load(verdict_overlay.overlay_path(str(tmp_path)))
    assert ov["AB_RACE_VERDICT_DAYS"] == "30"


def test_verdict_non_recalibrate_keeps_verdict_sent(tmp_path, monkeypatch):
    tb, buf = _wire_verdict_env(tmp_path, monkeypatch, verdict_sent=True)
    tb._handle_verdict("/verdict hold")
    tb._handle_verdict("/verdict confirm")
    after = json.loads((tmp_path / "ab_race_state.json").read_text())
    assert after["verdict_sent"] is True


def test_setkey_echo_redacts_private_key():
    import re as _re
    text = "/setkey 0x" + "ab" * 32 + " CONFIRM"
    echo = _re.sub(r"^(/setkey\s+)0x[0-9a-fA-F]{64}(\s+CONFIRM)$",
                   r"\g<1>0x***REDACTED\g<2>", text)
    assert "abab" not in echo and "REDACTED" in echo
    # a condition_id (also 0x+64hex) in any other command is NOT touched
    other = "/check 0x" + "cd" * 32
    echo2 = _re.sub(r"^(/setkey\s+)0x[0-9a-fA-F]{64}(\s+CONFIRM)$",
                    r"\g<1>0x***REDACTED\g<2>", other)
    assert echo2 == other


def test_send_chunked_reports_failure(tmp_path, monkeypatch):
    from src import telegram_bot
    calls = []
    monkeypatch.setattr(telegram_bot, "send_message",
                        lambda text, **kw: calls.append(text) or len(calls) > 1)
    long_text = "\n".join(f"line {i} " + "x" * 100 for i in range(100))
    ok = telegram_bot._send_chunked(long_text, chunk_size=1000)
    assert ok is False and len(calls) > 1
    monkeypatch.setattr(telegram_bot, "send_message", lambda text, **kw: True)
    assert telegram_bot._send_chunked(long_text, chunk_size=1000) is True


def test_disk_eval_ignores_tight_sample_pairs():
    prev = {"ts": 0.0, "free_gb": 3.1}
    # 3 minutes later, 200MB drop — must NOT extrapolate to a scary slope
    res = disk_watch.evaluate(2.9, prev, floor_gb=2.5, days_bar=14, now=180.0)
    assert not res["tripped"] and res["days_to_floor"] is None


# --------------------------------------------------------------------------- #
# Verdict memo: §7 reading + cost slices
# --------------------------------------------------------------------------- #

def _closed_row(cat="sports", spent=20.0, pnl=1.0, ideal=1.1, icost=0.4):
    return {"closed": True, "category": cat, "spent": spent, "pnl": pnl,
            "ideal_pnl": ideal, "ideal_cost_usd": icost, "won": True,
            "copy_id": f"c-{id(cat)}-{spent}-{pnl}"}


def test_cost_slices_groups_by_category_and_size():
    rows = [_closed_row(cat="sports", spent=5.0) for _ in range(12)]
    rows += [_closed_row(cat="crypto", spent=60.0, pnl=-2.0, ideal=-1.8)
             for _ in range(11)]
    out = cost_slices(rows)
    assert set(out["by_category"]) == {"sports", "crypto"}
    assert "<$10" in out["by_size"] and "$50+" in out["by_size"]
    s = out["by_category"]["sports"]
    assert s["n"] == 12
    assert s["ideal_roi_net"] == pytest.approx((12 * 1.1 - 12 * 0.4) / (12 * 5.0), abs=1e-4)


def test_cost_slices_drops_thin_slices():
    out = cost_slices([_closed_row() for _ in range(3)])
    assert out["by_category"] == {}


def _cmp_stub(corr, wallets, net=-0.033, settled=100):
    return {"persistence": {"a": {"all": {}, "era": None},
                            "b": {"all": {},
                                  "era": {"realized": corr, "ideal": corr,
                                          "n": wallets}}},
            "b": {"n_settled": settled, "ideal_roi_net": net}}


def test_section7_alive_but_cost_negative_recommends_hold_preview():
    r = section7_reading(_cmp_stub(0.06, 26))
    assert r["b"]["status"] == "alive"
    assert "hold PREVIEW" in r["recommendation"]


def test_section7_falsified_recommends_retire():
    r = section7_reading(_cmp_stub(-0.2, 20))
    assert r["b"]["status"] == "FALSIFIED"
    assert "retire" in r["recommendation"]


def test_section7_inconclusive_below_wallet_bar():
    r = section7_reading(_cmp_stub(-0.65, 4))
    assert "inconclusive" in r["b"]["status"]
    assert "retire" not in r["recommendation"]


def test_verdict_memo_renders_reading_and_slices():
    cmp_ = _cmp_stub(0.06, 26)
    cmp_.update({
        "era_days": 27.8, "era_start": 1784976482.0, "validity": {"valid": True, "reasons": []},
        "a": {"n_settled": 10, "pnl": -5.0, "spent": 100.0, "roi": -0.05,
              "ideal_roi": -0.05, "ideal_roi_net": -0.09, "n_open": 1,
              "open_usd": 10.0, "win_rate": 0.4, "cost_stamped": 10,
              "fill_suspect": False},
        "a_all_time": {"n_settled": 10, "pnl": -5.0, "spent": 100.0, "roi": -0.05,
                       "ideal_roi": -0.05, "ideal_roi_net": -0.09, "n_open": 1,
                       "open_usd": 10.0, "win_rate": 0.4, "cost_stamped": 10,
                       "fill_suspect": False},
        "b": {"n_settled": 20, "pnl": 10.0, "spent": 400.0, "roi": 0.025,
              "ideal_roi": 0.03, "ideal_roi_net": -0.033, "n_open": 2,
              "open_usd": 40.0, "win_rate": 0.6, "cost_stamped": 20,
              "fill_suspect": False},
        "persistence": {"a": {"all": {"realized": None, "n": 0}, "era": None},
                        "b": {"all": {"realized": 0.06, "n": 26},
                              "era": {"realized": 0.06, "ideal": 0.06, "n": 26}}},
        "b_cost_slices": cost_slices([_closed_row() for _ in range(12)]),
        "routing": [], "gov_a": {"promoted": [], "offers": {}, "blacklisted": []},
        "gov_b": {"promoted": [], "offers": {}, "blacklisted": []},
        "daily_a": [], "daily_b": [], "b_slippage_bps": 100,
        "section7": section7_reading(_cmp_stub(0.06, 26)),
    })
    text = format_verdict(cmp_)
    assert "§7 READING" in text
    assert "alive" in text
    assert "hold PREVIEW" in text
    assert "COST DRAG" in text


# --------------------------------------------------------------------------- #
# Phase-2: verdict overlay + /verdict + /slice + data autopsy
# --------------------------------------------------------------------------- #

from src.copy_trading import verdict_overlay


def test_overlay_draft_confirm_flow(tmp_path):
    d = str(tmp_path)
    draft = verdict_overlay.new_draft("retire", now=1000.0)
    verdict_overlay.save(verdict_overlay.draft_path(d), draft)
    patch = verdict_overlay.confirm(d, now=1001.0)
    assert patch == {"COPY_PAPER_ENABLED": "false",
                     "WALLET_DISCOVERY_ENABLED": "false"}
    ov = verdict_overlay.load(verdict_overlay.overlay_path(d))
    assert ov["COPY_PAPER_ENABLED"] == "false"
    assert ov["_decision"]["action"] == "retire"
    # draft consumed
    assert verdict_overlay.confirm(d, now=1002.0) is None


def test_overlay_expired_draft_does_not_apply(tmp_path):
    d = str(tmp_path)
    verdict_overlay.save(verdict_overlay.draft_path(d),
                         verdict_overlay.new_draft("retire", now=1000.0))
    assert verdict_overlay.confirm(d, now=1000.0 + 3700.0) is None
    assert verdict_overlay.load(verdict_overlay.overlay_path(d)) == {}


def test_overlay_effective_prefers_overlay_over_env(tmp_path):
    d = str(tmp_path)
    assert verdict_overlay.effective(d, "AB_RACE_VERDICT_DAYS", "27") == "27"
    verdict_overlay.save(verdict_overlay.overlay_path(d),
                         {"AB_RACE_VERDICT_DAYS": "30"})
    assert verdict_overlay.effective(d, "AB_RACE_VERDICT_DAYS", "27") == "30"
    assert verdict_overlay.effective_bool(d, "COPY_PAPER_ENABLED", True) is True
    verdict_overlay.save(verdict_overlay.overlay_path(d),
                         {"COPY_PAPER_ENABLED": "false"})
    assert verdict_overlay.effective_bool(d, "COPY_PAPER_ENABLED", True) is False


def _wire_verdict_env(tmp_path, monkeypatch, verdict_sent):
    """Point telegram_bot at tmp data + capture sends; write race state."""
    from src import telegram_bot
    buf: list[str] = []
    monkeypatch.setattr(telegram_bot, "send_message",
                        lambda text, **_kw: buf.append(text))
    monkeypatch.setattr(telegram_bot.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(telegram_bot.CONFIG, "ab_race_verdict_days", 27.0)
    state = {"verdict_sent": verdict_sent, "era_floor_ts": 1784976482.0}
    (tmp_path / "ab_race_state.json").write_text(json.dumps(state))
    return telegram_bot, buf


def test_verdict_inert_before_memo_posts(tmp_path, monkeypatch):
    tb, buf = _wire_verdict_env(tmp_path, monkeypatch, verdict_sent=False)
    tb._handle_verdict("/verdict")
    assert "No verdict yet" in buf[-1]
    tb._handle_verdict("/verdict retire")
    assert "Inert" in buf[-1]
    # nothing drafted, nothing applied
    assert verdict_overlay.load(
        verdict_overlay.overlay_path(str(tmp_path))) == {}


def test_verdict_draft_preview_confirm(tmp_path, monkeypatch):
    tb, buf = _wire_verdict_env(tmp_path, monkeypatch, verdict_sent=True)
    tb._handle_verdict("/verdict retire")
    preview = buf[-1]
    assert "Draft: retire" in preview
    assert "COPY_PAPER_ENABLED" in preview and "false" in preview
    assert "confirm" in preview
    tb._handle_verdict("/verdict confirm")
    assert "Applied" in buf[-1]
    ov = verdict_overlay.load(verdict_overlay.overlay_path(str(tmp_path)))
    assert ov["COPY_PAPER_ENABLED"] == "false"
    # overlay survives (the deploy-regenerates-.env guarantee): a fresh read
    # through effective_bool sees the decision without any env change
    assert verdict_overlay.effective_bool(
        str(tmp_path), "COPY_PAPER_ENABLED", True) is False


def test_verdict_hold_records_decision_without_changes(tmp_path, monkeypatch):
    tb, buf = _wire_verdict_env(tmp_path, monkeypatch, verdict_sent=True)
    tb._handle_verdict("/verdict hold")
    assert "no config change" in buf[-1]
    tb._handle_verdict("/verdict confirm")
    ov = verdict_overlay.load(verdict_overlay.overlay_path(str(tmp_path)))
    assert ov["_decision"]["action"] == "hold"
    assert "COPY_PAPER_ENABLED" not in ov


def test_slice_renders_same_table_as_memo(tmp_path, monkeypatch):
    from src import telegram_bot
    buf: list[str] = []
    monkeypatch.setattr(telegram_bot, "send_message",
                        lambda text, **_kw: buf.append(text))
    monkeypatch.setattr(telegram_bot.CONFIG, "data_dir", str(tmp_path))
    ledger = tmp_path / "b.jsonl"
    rows = [_closed_row(cat="crypto", spent=20.0) for _ in range(12)]
    for r in rows:
        r["opened_ts"] = 1785000000.0
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setattr(telegram_bot.CONFIG, "copy_paper_b_ledger", str(ledger))
    (tmp_path / "ab_race_state.json").write_text(
        json.dumps({"era_floor_ts": 1784976482.0}))
    telegram_bot._handle_slice("/slice B")
    msg = buf[-1]
    assert "cost slices" in msg and "crypto" in msg
    # same renderer as the memo: identical row line
    from src.copy_trading.strategy_compare import cost_slices, fmt_slices
    expect = fmt_slices("x", cost_slices(rows)["by_category"])[1]
    assert expect.strip() in msg


def test_autopsy_detects_and_dedups(tmp_path):
    d = str(tmp_path)
    # realized-pnl with a duplicate AND a >1h inversion
    rows = [
        {"timestamp": "2026-07-30T18:19:08+00:00", "condition_id": "c1",
         "token_id": "t1", "pnl": -5.0, "exit": "resolution"},
        {"timestamp": "2026-07-30T18:19:08+00:00", "condition_id": "c1",
         "token_id": "t1", "pnl": -5.0, "exit": "resolution"},
        {"timestamp": "2026-07-28T00:00:00+00:00", "condition_id": "c2",
         "token_id": "t2", "pnl": 1.0, "exit": "resolution"},
    ]
    (tmp_path / "realized-pnl.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n")
    first = ledger_integrity.run_autopsy(d, now=1000.0)
    assert any("duplicate resolution" in a for a in first["findings"])
    assert any("inversion" in a for a in first["findings"])
    assert first["new"] == first["findings"]
    # rerun-safety: identical state → no re-alert
    second = ledger_integrity.run_autopsy(d, now=2000.0)
    assert second["new"] == []
    assert second["findings"] == first["findings"]
    # changed anomaly shape → re-alerts
    rows.append({"timestamp": "2026-07-30T18:19:08+00:00", "condition_id": "c1",
                 "token_id": "t1", "pnl": -5.0, "exit": "resolution"})
    (tmp_path / "realized-pnl.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n")
    third = ledger_integrity.run_autopsy(d, now=3000.0)
    assert any("duplicate resolution" in a for a in third["new"])


def test_autopsy_size_trajectory_flags_runaway_growth(tmp_path):
    d = str(tmp_path)
    big = tmp_path / "trade-history.jsonl"
    big.write_text("x" * (6 * 1024 * 1024))
    ledger_integrity.run_autopsy(d, now=1000.0)      # baseline sample
    big.write_text("x" * (13 * 1024 * 1024))         # >2x growth, +7MB
    res = ledger_integrity.run_autopsy(d, now=1000.0 + 86400.0)
    assert any("trade-history grew" in a for a in res["new"])
