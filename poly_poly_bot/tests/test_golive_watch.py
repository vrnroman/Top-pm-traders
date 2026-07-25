"""Edge-triggered go-live readiness alerts (owner ask 2026-07-17)."""

from __future__ import annotations

import json
import os
import tempfile

from src.copy_trading.copy_paper import PaperPosition
from src.copy_trading.golive_watch import run_golive_watch

# Small thresholds so fixtures stay compact — the gate math itself is covered
# by test_golive_gate.py; this file tests the WATCH semantics (edge-trigger,
# persistence, retry-on-send-failure).
GATE = dict(min_settled=3, max_idle_days=14.0, min_roi=0.0,
            floor_kwargs=dict(min_n=2, min_roi=0.05, min_tstat=-10.0,
                              min_second_half_roi=-1.0, min_conditions=1,
                              min_categories=1))
NOW = 1_000_000.0


def _pos(i, target="0xW", won=True, spent=10.0, pnl=None, opened=NOW - 3600,
         closed=True, ideal=None):
    if pnl is None:
        pnl = 2.0 if won else -spent
    return PaperPosition(
        copy_id=f"c{i}", target=target, condition_id=f"cond{i}",
        token_id=f"T{i}", outcome_index=0, category="sports",
        their_price=0.5, entry_price=0.5, shares=spent / 0.5, spent=spent,
        drag_bps=0, opened_ts=opened, closed=closed, won=won, pnl=pnl,
        closed_ts=opened + 60 if closed else 0.0,
        ideal_pnl=ideal if ideal is not None else pnl)


def _ready_book(n=4):
    """n settled winners -> ROI +20%, passes the small GATE above."""
    return [_pos(i) for i in range(n)]


class SendSpy:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    def __call__(self, msg):
        self.sent.append(msg)
        return self.ok


def test_crossing_alerts_once_and_persists():
    with tempfile.TemporaryDirectory() as d:
        state = os.path.join(d, "watch.json")
        send = SendSpy()
        t1 = run_golive_watch(_ready_book(), promoted=["0xW"],
                              state_path=state, send=send, now=NOW, **GATE)
        assert t1 == [("0xw", True)]
        assert len(send.sent) == 1 and "GO-LIVE READY" in send.sent[0]
        assert "/golive 0xw" in send.sent[0]
        # second pass, same state: no repeat
        t2 = run_golive_watch(_ready_book(), promoted=["0xW"],
                              state_path=state, send=send, now=NOW + 60, **GATE)
        assert t2 == [] and len(send.sent) == 1
        assert json.load(open(state))["0xw"]["ready"] is True


def test_first_sight_not_ready_is_silent():
    with tempfile.TemporaryDirectory() as d:
        state = os.path.join(d, "watch.json")
        send = SendSpy()
        t = run_golive_watch(_ready_book(n=1), promoted=["0xW"],
                             state_path=state, send=send, now=NOW, **GATE)
        assert t == [] and send.sent == []
        assert json.load(open(state))["0xw"]["ready"] is False


def test_drop_back_alerts_and_rearms():
    with tempfile.TemporaryDirectory() as d:
        state = os.path.join(d, "watch.json")
        send = SendSpy()
        run_golive_watch(_ready_book(), promoted=["0xW"], state_path=state,
                         send=send, now=NOW, **GATE)
        # wallet goes idle past max_idle_days -> not ready anymore
        later = NOW + 20 * 86400
        t2 = run_golive_watch(_ready_book(), promoted=["0xW"],
                              state_path=state, send=send, now=later, **GATE)
        assert t2 == [("0xw", False)]
        assert "No longer go-live ready" in send.sent[-1]
        # fresh activity again -> re-crosses -> re-alerts
        fresh = _ready_book() + [_pos(99, opened=later - 60)]
        t3 = run_golive_watch(fresh, promoted=["0xW"], state_path=state,
                              send=send, now=later + 3600, **GATE)
        assert t3 == [("0xw", True)]
        assert len(send.sent) == 3


def test_send_failure_leaves_state_for_retry():
    with tempfile.TemporaryDirectory() as d:
        state = os.path.join(d, "watch.json")
        failing = SendSpy(ok=False)
        t1 = run_golive_watch(_ready_book(), promoted=["0xW"],
                              state_path=state, send=failing, now=NOW, **GATE)
        assert t1 == []                       # nothing recorded as alerted
        assert (json.load(open(state)) if os.path.exists(state) else {}).get(
            "0xw") is None
        ok = SendSpy()
        t2 = run_golive_watch(_ready_book(), promoted=["0xW"],
                              state_path=state, send=ok, now=NOW + 60, **GATE)
        assert t2 == [("0xw", True)]          # retried and delivered


def test_only_promoted_wallets_watched_and_pruned():
    with tempfile.TemporaryDirectory() as d:
        state = os.path.join(d, "watch.json")
        send = SendSpy()
        book = _ready_book() + [_pos(50, target="0xOther")]
        run_golive_watch(book, promoted=["0xW"], state_path=state,
                         send=send, now=NOW, **GATE)
        assert list(json.load(open(state)).keys()) == ["0xw"]
        # 0xW demoted while ANOTHER wallet stays promoted -> its entry is
        # pruned (an all-empty promoted read keeps state — see the transient
        # wipe test)
        run_golive_watch(book, promoted=["0xOther"], state_path=state,
                         send=send, now=NOW + 60, **GATE)
        assert "0xw" not in json.load(open(state))
        assert len(send.sent) == 1            # no alert for the pruning


def test_corrupt_state_file_recovers():
    with tempfile.TemporaryDirectory() as d:
        state = os.path.join(d, "watch.json")
        with open(state, "w") as f:
            f.write("{not json")
        send = SendSpy()
        t = run_golive_watch(_ready_book(), promoted=["0xW"],
                             state_path=state, send=send, now=NOW, **GATE)
        assert t == [("0xw", True)]           # treated as first sight


def test_non_dict_state_value_recovers_without_raising():
    # {"0xw": true} parses as valid JSON but used to AttributeError every
    # cycle and never self-heal (2026-07-17 review catch).
    with tempfile.TemporaryDirectory() as d:
        state = os.path.join(d, "watch.json")
        with open(state, "w") as f:
            json.dump({"0xw": True}, f)
        send = SendSpy()
        t = run_golive_watch(_ready_book(), promoted=["0xW"],
                             state_path=state, send=send, now=NOW, **GATE)
        assert t == [("0xw", True)]           # entry dropped -> first sight
        assert json.load(open(state))["0xw"]["ready"] is True


def test_floor_decay_drop_back_message_is_html_safe():
    # The floor's reason strings carry a literal '<' ("copy ROI ... < floor
    # ...") — the drop-back alert must escape it or Telegram 400s the send
    # and the safety alert retries forever (2026-07-17 review catch).
    with tempfile.TemporaryDirectory() as d:
        state = os.path.join(d, "watch.json")
        send = SendSpy()
        run_golive_watch(_ready_book(), promoted=["0xW"], state_path=state,
                         send=send, now=NOW, **GATE)
        # same wallet decays: new settled losers pull ROI under the +5% floor
        # while activity stays fresh (so ONLY floor checks fail, whose details
        # contain '<')
        decayed = _ready_book() + [
            _pos(100 + i, won=False, opened=NOW + 60) for i in range(6)]
        t2 = run_golive_watch(decayed, promoted=["0xW"], state_path=state,
                              send=send, now=NOW + 3600, **GATE)
        assert t2 == [("0xw", False)]
        body = send.sent[-1]
        assert "No longer go-live ready" in body
        assert "&lt;" in body                 # escaped ...
        import re
        assert not re.search(r"<(?!/?(b|code|i|pre)>)", body)  # ... no raw '<'


def test_transient_empty_promoted_read_does_not_wipe_state():
    # promoted_wallets() degrades to [] on a missing/corrupt store read; that
    # transient must not wipe the edge state (a wipe re-fires duplicate READY
    # alerts on the next good read — 2026-07-17 review catch).
    with tempfile.TemporaryDirectory() as d:
        state = os.path.join(d, "watch.json")
        send = SendSpy()
        run_golive_watch(_ready_book(), promoted=["0xW"], state_path=state,
                         send=send, now=NOW, **GATE)
        assert len(send.sent) == 1
        run_golive_watch(_ready_book(), promoted=[], state_path=state,
                         send=send, now=NOW + 60, **GATE)
        assert json.load(open(state))["0xw"]["ready"] is True   # kept
        # store reads fine again: same READY state -> NO duplicate alert
        t3 = run_golive_watch(_ready_book(), promoted=["0xW"], state_path=state,
                              send=send, now=NOW + 120, **GATE)
        assert t3 == [] and len(send.sent) == 1


# --- honest-metrics floors (owner ruling 2026-07-25): evaluated on the clean
# era, fail closed while the era is too young to measure ---

def _clean_book():
    """Three wallets with 10 clean-era settles each, positive split-half corr
    (halves: -0.04/-0.28/-0.52 first, +0.20/-0.04/-0.28 second -> +1.0)."""
    patterns = {"0xW": (4, 5), "0xW2": (3, 4), "0xW3": (2, 3)}
    positions = []
    for target, (fw, sw) in patterns.items():
        for i in range(10):
            won = (i < fw) if i < 5 else (i - 5 < sw)
            positions.append(_pos(f"{target}-{i}", target=target, won=won,
                                  opened=NOW - 1000 + i * 10))
    return positions


def test_honest_floors_ready_alert_fires_on_clean_era():
    with tempfile.TemporaryDirectory() as d:
        send = SendSpy()
        t = run_golive_watch(
            _clean_book(), promoted=["0xW"], state_path=os.path.join(d, "s.json"),
            send=send, now=NOW, era_floor=NOW - 2000,
            min_ideal_roi=0.0, min_split_half_corr=0.0, **GATE)
        assert t == [("0xw", True)]
        assert "GO-LIVE READY" in send.sent[-1]


def test_honest_floors_fail_closed_without_clean_era():
    # all-time READY record but every row is PRE-floor: the honest checks must
    # not bless it (fail closed; first sight is silent, state remembers).
    with tempfile.TemporaryDirectory() as d:
        state = os.path.join(d, "s.json")
        send = SendSpy()
        t = run_golive_watch(
            _ready_book(), promoted=["0xW"], state_path=state,
            send=send, now=NOW, era_floor=NOW - 60,   # rows opened NOW-3600 < floor
            min_ideal_roi=0.0, min_split_half_corr=0.0, **GATE)
        assert t == [] and send.sent == []
        assert json.load(open(state))["0xw"]["ready"] is False


def test_honest_floors_fail_closed_when_no_floor_recorded():
    # era_floor=None means there IS no clean era: both checks fail closed.
    with tempfile.TemporaryDirectory() as d:
        send = SendSpy()
        t = run_golive_watch(
            _clean_book(), promoted=["0xW"], state_path=os.path.join(d, "s.json"),
            send=send, now=NOW, era_floor=None,
            min_ideal_roi=0.0, min_split_half_corr=0.0, **GATE)
        assert t == [] and send.sent == []


def test_honest_floors_negative_persistence_holds():
    # the 2026-07 flip signature: every wallet's second half inverts its first
    # -> corr -1.0 -> nobody goes live on a falsified approach.
    positions = []
    for j, (fw, sw) in enumerate(((5, 0), (4, 1), (3, 2))):
        target = "0xW" if j == 0 else f"0xW{j}"
        for i in range(10):
            won = (i < fw) if i < 5 else (i - 5 < sw)
            positions.append(_pos(f"{target}-{i}", target=target, won=won,
                                  opened=NOW - 1000 + i * 10))
    with tempfile.TemporaryDirectory() as d:
        send = SendSpy()
        t = run_golive_watch(
            positions, promoted=["0xW"], state_path=os.path.join(d, "s.json"),
            send=send, now=NOW, era_floor=NOW - 2000,
            min_ideal_roi=0.0, min_split_half_corr=0.0, **GATE)
        assert t == [] and send.sent == []


def test_honest_floors_off_keeps_legacy_behavior():
    # floors OFF (kwargs absent): the all-time record decides, as before.
    with tempfile.TemporaryDirectory() as d:
        send = SendSpy()
        t = run_golive_watch(
            _ready_book(), promoted=["0xW"], state_path=os.path.join(d, "s.json"),
            send=send, now=NOW, **GATE)
        assert t == [("0xw", True)]
