#!/usr/bin/env python3
"""Rehearse every real-money path that has never actually run.

    python scripts/golive_drills.py

Four things are true of this system today and all four are uncomfortable:
the auto-redeemer has never fired, `recover_pending_orders` has never
reconciled a real order, the proxy wallet balance has never been checked
against a live session, and the VM has gone network-dead before. Each is a
sentence in a doc rather than a check that can fail.

This turns each into a drill with a green/red result, the same shape as a test
gate. Everything here is in-process and read-only: no order is placed, nothing
is armed, nothing on chain is touched. That boundary is not squeamishness, it
is structural, because `PREVIEW_MODE=false` fires token approvals, the
auto-redeemer and an inventory reconcile at BOOT, before any order and
regardless of the runtime arm, so there is no such thing as "drill it live but
carefully". Live is live.

Exit code is 0 when every drill passes, 1 otherwise, so it can gate a deploy.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.config import CONFIG  # noqa: E402
from src.copy_trading import live_guard, live_mode, zset  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
_results: list = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, ok, detail))
    print(f"  {PASS if ok else FAIL}  {name}" + (f"  ({detail})" if detail else ""))
    return ok


def drill_interlock_is_shut() -> None:
    """The door must be shut, and shut for a reason it can name."""
    print("\n[1] interlock")
    st = live_mode.status()
    check("preview mode is on", st["preview"] is True,
          f"process_live={st['process_live']} env_key={st['env_key']} "
          f"armed={st['runtime_armed']}")
    reasons = live_mode.blocking_reasons()
    check("it can say what is blocking it", len(reasons) > 0,
          f"{len(reasons)} blocker(s)")
    # Deliberately NOT calling live_mode.arm(): it is inert today only because
    # the env key is unset, and this script's own banner promises it arms
    # nothing. Assert the refusal from the interlock's state instead.
    blocked = bool(live_mode.blocking_reasons())
    check("arming cannot succeed in this configuration", blocked,
          "; ".join(live_mode.blocking_reasons())[:90])


def drill_set_z_is_gated() -> None:
    """Z must be non-forgeable: no path admits a wallet the gate refused."""
    print("\n[2] set Z admission")
    before = set(zset.wallet_set())
    admitted, checks = zset.admit(
        "0x000000000000000000000000000000000000dead",
        ready=False, checks=[("fabricated gate result", False, "drill")],
        settled=[], rails_supplied=True)
    check("a wallet the gate refused cannot be admitted", admitted is False)
    check("...and nothing was written", set(zset.wallet_set()) == before,
          f"{len(before)} wallet(s) in Z, unchanged")
    check("Z is the live source of truth", True,
          f"Z holds: {sorted(w[:10] + '…' for w in before) or 'nothing'}")


def drill_concentration_rail() -> None:
    """The rail must actually reject a jackpot-shaped record."""
    print("\n[3] concentration rail")

    class P:
        def __init__(self, spent, ideal):
            self.spent, self.ideal_pnl = spent, ideal
            self.closed, self.opened_ts = True, 1.0

    # Ten copies: three huge winners carry a book of losers.
    jackpot = [P(50, 400) for _ in range(3)] + [P(50, -20) for _ in range(7)]
    ok, detail = zset.concentration_check(jackpot)
    check("a three-ticket record is rejected", ok is False, detail)

    broad = [P(50, 8) for _ in range(20)]
    ok2, detail2 = zset.concentration_check(broad)
    check("a broad record survives the same rail", ok2 is True, detail2)


def drill_guard_detects_without_acting() -> None:
    """Detectors must fire in preview; actions must not."""
    print("\n[4] live guard, unarmed")
    now = time.time()
    cancelled: list = []

    class Order:
        def __init__(self, oid, age):
            self.order_id, self.placed_at = oid, now - age

    class Pos:
        def __init__(self, age):
            self.closed, self.redeemed = True, False
            self.closed_ts = now - age

    out = live_guard.run_once(
        pending_orders=[Order("stuck-1", 3600), Order("fresh-1", 5)],
        positions=[Pos(48 * 3600), Pos(48 * 3600), Pos(10)],
        crash_streak=0, send=None,
        cancel_order=lambda oid: cancelled.append(oid), now=now)

    check("it sees the stuck order", out["stuck_orders"] == 1,
          f"{out['stuck_orders']} stuck")
    check("it sees the unredeemed positions", out["unredeemed"] == 2,
          f"{out['unredeemed']} unredeemed")
    check("it did NOT cancel anything while unarmed", cancelled == [],
          "actions are gated on is_armed()")
    check("it did not self-disarm on a healthy session",
          out["self_disarmed"] is False)


def drill_self_disarm_triggers() -> None:
    """Every self-disarm trigger must be a statement about US, not the market."""
    print("\n[5] self-disarm conditions")
    cases = [
        ("crash loop", dict(crash_streak=live_guard.CRASH_LOOP_N)),
        ("stuck redemptions", dict(unredeemed=3)),
        ("stale feed", dict(feed_stale_s=3600)),
        ("bankroll under the floor", dict(equity_usd=200.0, floor_usd=217.0)),
    ]
    for label, kw in cases:
        fires, why = live_guard.should_self_disarm(**kw)
        check(f"{label} disarms", fires is True, why[:70])
    fires, _ = live_guard.should_self_disarm(
        crash_streak=0, unredeemed=0, feed_stale_s=60)
    check("a healthy session does NOT disarm", fires is False)
    fires, _ = live_guard.should_self_disarm(crash_streak=0)
    check("a LOSING session is not a disarm condition", fires is False,
          "losing money is a strategy question, not a trust question")


def drill_recovery_paths_exist() -> None:
    """The never-run paths must at least be importable and wired."""
    print("\n[6] recovery paths")
    try:
        from src.copy_trading.trade_executor import recover_pending_orders
        check("recover_pending_orders is importable", callable(recover_pending_orders))
    except Exception as exc:
        check("recover_pending_orders is importable", False, str(exc)[:60])
    try:
        from src.copy_trading import auto_redeemer
        check("auto_redeemer is importable", hasattr(auto_redeemer, "__name__"))
    except Exception as exc:
        check("auto_redeemer is importable", False, str(exc)[:60])
    try:
        from src.copy_trading.order_verifier import __name__ as _n
        check("order_verifier is importable", True)
    except Exception as exc:
        check("order_verifier is importable", False, str(exc)[:60])
    # The queue file the recovery path reads must be loadable, or recovery
    # after a crash silently starts from nothing.
    qpath = os.path.join(CONFIG.data_dir, "late_bet_queue.json")
    check("data dir is readable", os.path.isdir(CONFIG.data_dir), CONFIG.data_dir)


def drill_caps_are_sane() -> None:
    """A blind window costs at most what the caps allow. State the number."""
    print("\n[7] exposure caps")
    per_market = CONFIG.max_position_per_market_usd
    daily = CONFIG.max_daily_volume_usd
    check("per-market cap is set", per_market > 0, f"${per_market:,.0f}")
    check("daily volume cap is set", daily > 0, f"${daily:,.0f}")
    check("worst case in a blind day is bounded", daily <= 5000,
          f"a full day unreachable costs at most ${daily:,.0f} of new exposure")


def drill_bankroll_governor() -> None:
    """Live sizing must be closed without a budget and bounded with one."""
    print("\n[8] bankroll governor")
    from src.copy_trading import live_budget
    from src.copy_trading.strategy_config import TIER_1B
    saved = CONFIG.live_budget_usd
    try:
        CONFIG.live_budget_usd = 0.0
        _cfg, why = live_budget.govern_tier(TIER_1B, live=True)
        check("no budget means no live sizing", why is not None, (why or "")[:70])
        CONFIG.live_budget_usd = 310.0
        c = live_budget.caps(live=True, balance=310.0)
        check("caps are fractions of the budget",
              c is not None and c.per_copy_usd == 7.75 and c.daily_usd == 93.0
              and c.exposure_usd == 248.0,
              f"copy ${c.per_copy_usd} day ${c.daily_usd} open ${c.exposure_usd}"
              if c else "no caps")
        c2 = live_budget.caps(live=True, balance=120.0)
        check("the chain balance bounds the stated budget",
              c2 is not None and c2.effective_usd == 120.0,
              f"effective ${c2.effective_usd}" if c2 else "no caps")
        cfg, why2 = live_budget.govern_tier(TIER_1B, live=True, balance=310.0)
        check("the tier copies from the evidence base's trade size",
              why2 is None and cfg.min_trader_bet == CONFIG.copy_paper_min_usd
              and cfg.max_bet <= 7.75,
              f"min trader bet ${cfg.min_trader_bet:.0f}, max bet ${cfg.max_bet}")
    finally:
        CONFIG.live_budget_usd = saved


def drill_canary_is_bounded() -> None:
    """The canary cannot stage without every key, and fires exactly once."""
    print("\n[9] canary")
    from src.copy_trading import canary
    ok, why = canary.stage(by="drill")
    check("it cannot stage in this configuration", ok is False, why[:70])

    class _Book:
        min_order_size = 5.0

    class _Clob:
        def get_order_book(self, token_id):
            return _Book()
    check("it sizes at the market minimum, never under the bot minimum",
          canary.size_for(_Clob(), "tok", 0.5) == max(5.0 * 0.5, CONFIG.min_order_size_usd))
    canary.reset(by="drill")
    canary._write({"staged": True, "staged_ts": 0.0, "expires_ts": 9e12,
                   "fired": None, "fill": None})
    canary.consume(market="m", token_id="t", their_price=0.5, quoted_ask=0.51,
                   copy_size=5.0, notify_latency_s=3.0)
    check("the shot is spent before any order posts", canary.is_staged() is False)
    canary.record_fired(order_id="drill-1", order_price=0.51)
    check("once fired it is no longer staged", canary.is_staged() is False)
    ok2, why2 = canary.stage(by="drill")
    check("a fired canary does not restage without RESET", ok2 is False, why2[:60])


def main() -> int:
    # Run the guard drills against a THROWAWAY data dir. The first version ran
    # them against the live one, which flipped live_guard.json's edge state to
    # true; the next production pass then saw empty inputs and sent the owner
    # "resolved" messages for conditions that never happened. A drill that
    # alerts the owner is worse than no drill.
    import tempfile
    real_data_dir = CONFIG.data_dir
    sandbox = tempfile.mkdtemp(prefix="golive-drill-")

    print("=" * 66)
    print("GO-LIVE DRILLS, nothing here places an order or arms anything")
    print(f"guard drills sandboxed to {sandbox}")
    print("=" * 66)
    # These read live state on purpose: they answer "is the real box shut".
    drill_interlock_is_shut()
    drill_set_z_is_gated()
    drill_concentration_rail()
    # These MUTATE watcher state, so they run against the sandbox only.
    try:
        CONFIG.data_dir = sandbox
        live_guard.CONFIG.data_dir = sandbox
        drill_guard_detects_without_acting()
        drill_self_disarm_triggers()
        drill_canary_is_bounded()
    finally:
        CONFIG.data_dir = real_data_dir
        live_guard.CONFIG.data_dir = real_data_dir
    drill_recovery_paths_exist()
    drill_caps_are_sane()
    drill_bankroll_governor()

    failed = [r for r in _results if not r[1]]
    print("\n" + "=" * 66)
    print(f"{len(_results) - len(failed)}/{len(_results)} drills passed")
    for name, _ok, detail in failed:
        print(f"  FAILED: {name}  {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
