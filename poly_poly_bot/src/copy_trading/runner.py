"""Strategy #1: Copy Trading main loop."""

import asyncio
import os
from pathlib import Path
from src.config import CONFIG, get_private_key
from src.logger import logger
from src.utils import error_message, async_sleep
from src.copy_trading.clob_client import create_clob_client
from src.copy_trading.trade_executor import place_trade_orders, process_verifications, recover_pending_orders
from src.copy_trading.risk_manager import get_risk_status
from src.copy_trading.get_balance import get_usdc_balance
from src.copy_trading.check_approvals import check_and_set_approvals
from src.copy_trading.inventory import sync_inventory_from_api, get_inventory_summary
from src.copy_trading.auto_redeemer import check_and_redeem_positions
from src.copy_trading.telegram_notifier import telegram
from src.copy_trading.trade_queue import drain_trades, peek_pending_orders
from src.copy_trading.trade_source import create_sources
from src.copy_trading.strategy_config import TIERED_MODE, TIER_1A, TIER_1B, TIER_1C
from src.copy_trading.tiered_risk_manager import get_tiered_risk_status
from src.copy_trading.trade_store import get_avg_reaction_latency
from src.constants import EXECUTION_LOOP_S, FILL_CHECK_DELAY_S

# Process lock
LOCKFILE = Path(CONFIG.data_dir) / "bot.lock"

_shutting_down = False
_daily_trade_count = 0
_daily_summary_date = ""


def _acquire_lock() -> None:
    """Acquire a filesystem-based process lock to prevent duplicate instances."""
    try:
        LOCKFILE.mkdir(parents=True)
    except FileExistsError:
        pid_file = LOCKFILE / "pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                if pid != os.getpid():
                    os.kill(pid, 0)  # Check if running
                    logger.error(f"Another instance is running (PID {pid}).")
                    raise SystemExit(1)
            except (ProcessLookupError, ValueError):
                pass
            logger.warn("Stale lock found. Reclaiming.")
    else:
        LOCKFILE.mkdir(parents=True, exist_ok=True)
    (LOCKFILE / "pid").write_text(str(os.getpid()))


def _release_lock() -> None:
    """Release the filesystem process lock."""
    import shutil
    try:
        shutil.rmtree(LOCKFILE, ignore_errors=True)
    except Exception:
        pass


async def _supervised(name: str, fn) -> str:
    """Run a worker coroutine, restarting it on crash with exponential backoff.

    Critical: without a restart loop, a crashed worker returns a string and the
    sibling `asyncio.gather(..., return_exceptions=False)` waits forever on the
    other workers — leaving the bot "running" but with that worker silently
    dead. The restart loop here keeps essential workers (pattern-scanner,
    detection, periodic) alive unless the whole bot is shutting down.
    """
    backoff = 5.0
    while not _shutting_down:
        try:
            await fn()
            # An infinite-loop worker returning means either _shutting_down
            # was flipped or the worker exited unexpectedly. In the latter
            # case we retry with backoff so transient errors don't go silent.
            if _shutting_down:
                return f"{name}: shutting down"
            logger.warn(f"[supervisor] {name} exited unexpectedly — restarting in {backoff:.0f}s")
        except Exception as err:
            logger.error(f"[supervisor] {name} crashed — {error_message(err)} — restarting in {backoff:.0f}s")
            try:
                await telegram.bot_error(f"{name} crashed: {error_message(err)}")
            except Exception:
                pass
        if _shutting_down:
            break
        await async_sleep(backoff)
        backoff = min(backoff * 2, 60.0)  # cap at 1 min so transient outages recover quickly
    return f"{name}: shutting down"


async def _detection_loop() -> None:
    """Start all configured trade detection sources."""
    sources = create_sources(CONFIG.trade_monitor_mode)
    logger.info(f"Monitor mode: {CONFIG.trade_monitor_mode} ({', '.join(s.name for s in sources)})")
    await asyncio.gather(*(s.start() for s in sources))


async def _execution_loop(clob_client) -> None:
    """Drain the trade queue and execute copy orders in a loop."""
    global _daily_trade_count
    while not _shutting_down:
        queued = drain_trades()
        if queued:
            placed = await place_trade_orders(queued, clob_client)
            _daily_trade_count += placed
        await async_sleep(EXECUTION_LOOP_S)


async def _verification_loop(clob_client) -> None:
    """Verify pending order fills in a loop."""
    while not _shutting_down:
        pending = peek_pending_orders()
        if pending:
            await process_verifications(pending, clob_client)
        await async_sleep(FILL_CHECK_DELAY_S)


async def _monitor_drain_loop() -> None:
    """Preview / monitor-only trade drain.

    When there is no CLOB client (no PRIVATE_KEY configured), the main
    execution loop doesn't start — but `_detection_loop` keeps enqueueing
    trades for every tracked 1a/1b wallet. Without this drain, the pending-
    trades queue grows unboundedly and OOMs the VM.

    This worker:
      1. drains the queue every EXECUTION_LOOP_S
      2. feeds every drained trade through `analyze_trade_for_patterns` when
         Strategy 1c is enabled, so 1c still gets coverage from the 1a/1b
         wallet intake even in monitor mode
      3. drops the trades afterward
    """
    from src.copy_trading.trade_store import is_seen_trade, mark_trade_as_seen
    run_patterns = TIER_1C.enabled
    while not _shutting_down:
        drained = drain_trades()
        if drained:
            logger.debug(f"[monitor-drain] draining {len(drained)} trades")
            if run_patterns:
                from src.copy_trading.pattern_detector import analyze_trade_for_patterns
                for qt in drained:
                    if is_seen_trade(qt.trade.id):
                        continue
                    try:
                        await analyze_trade_for_patterns(qt.trade)
                    except Exception as err:
                        logger.debug(f"[monitor-drain] pattern err: {error_message(err)}")
                    mark_trade_as_seen(qt.trade.id)
        await async_sleep(EXECUTION_LOOP_S)


async def _pattern_scanner_loop() -> None:
    """Strategy 1c: geo market discovery + market-scoped activity polling.

    Independent of the 1a/1b tracked-wallet intake — this path feeds the pattern
    detector with trades from any wallet participating in a geopolitical market.
    """
    from src.copy_trading.geo_market_scanner import (
        refresh_geo_markets,
        run_geo_market_scanner,
    )
    from src.copy_trading.market_activity_poller import run_market_activity_poller

    try:
        await refresh_geo_markets()
    except Exception as err:
        logger.warn(f"Initial geo scan failed: {error_message(err)}")

    await asyncio.gather(
        run_geo_market_scanner(),
        run_market_activity_poller(),
    )


async def _periodic_loop() -> None:
    """Periodic tasks: daily summary, auto-redeem, inventory reconciliation, heartbeat."""
    global _daily_trade_count, _daily_summary_date
    from datetime import datetime, timezone
    redeem_interval_s = CONFIG.redeem_interval_hours * 3600
    last_redeem = 0.0
    reconcile_interval_s = 300  # 5 min
    last_reconcile = asyncio.get_event_loop().time()
    heartbeat_interval_s = 300  # 5 min
    last_heartbeat = asyncio.get_event_loop().time()

    while not _shutting_down:
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today != _daily_summary_date:
                balance = await get_usdc_balance()
                await telegram.daily_summary(_daily_trade_count, get_risk_status(), max(balance, 0))
                _daily_trade_count = 0
                _daily_summary_date = today

            now = asyncio.get_event_loop().time()

            if not CONFIG.preview_mode and now - last_redeem >= redeem_interval_s:
                last_redeem = now
                try:
                    result = await check_and_redeem_positions(get_private_key())
                    if result.count > 0:
                        logger.info(f"Redeemed {result.count} resolved position(s)")
                        await telegram.positions_redeemed(result.count, result.details)
                except Exception as err:
                    logger.warn(f"Auto-redeem failed: {error_message(err)}")

            if now - last_reconcile >= reconcile_interval_s:
                try:
                    await sync_inventory_from_api(CONFIG.proxy_wallet)
                    last_reconcile = now
                except Exception as err:
                    logger.warn(f"Periodic reconciliation failed: {error_message(err)}")

            if now - last_heartbeat >= heartbeat_interval_s:
                avg_latency = get_avg_reaction_latency()
                latency_str = f" | avg reaction: {avg_latency}ms" if avg_latency > 0 else ""
                tiered_str = f" | {get_tiered_risk_status()}" if TIERED_MODE else ""
                logger.info(f"Heartbeat: {get_risk_status()} | {get_inventory_summary()}{latency_str}{tiered_str}")
                last_heartbeat = now
        except Exception as err:
            logger.error(f"Periodic job error: {error_message(err)}")

        await async_sleep(60)


async def run_copy_trading() -> None:
    """Main entry point for Strategy #1."""
    global _shutting_down, _daily_summary_date
    from datetime import datetime, timezone

    _acquire_lock()
    _daily_summary_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    logger.info("=== Polymarket Copy Trading Bot ===")
    logger.info(f"Mode: {'PREVIEW (no real trades)' if CONFIG.preview_mode else 'LIVE'}")
    logger.info(f"Strategy: {CONFIG.copy_strategy} ({CONFIG.copy_size})")
    logger.info(f"Tracking {len(CONFIG.user_addresses)} wallet(s)")
    for addr in CONFIG.user_addresses:
        logger.info(f"  - {addr}")
    logger.info(f"Poll interval: {CONFIG.fetch_interval}s")
    logger.info(f"Risk: {get_risk_status()}")

    if TIERED_MODE:
        logger.info("=== Tiered Strategy Mode ===")
        if TIER_1A.enabled:
            logger.info(f"  1a: {len(TIER_1A.wallets)} wallets, {TIER_1A.copy_percentage}% copy, max ${TIER_1A.max_bet}/bet")
        if TIER_1B.enabled:
            logger.info(f"  1b: {len(TIER_1B.wallets)} wallets, {TIER_1B.copy_percentage}% copy, max ${TIER_1B.max_bet}/bet")
        if TIER_1C.enabled:
            logger.info(f"  1c: {'ALERT ONLY' if TIER_1C.alert_only else 'AUTO-FOLLOW'}")

    if not CONFIG.preview_mode:
        balance = await get_usdc_balance()
        logger.info(f"USDC balance: ${balance:.2f}" if balance >= 0 else "USDC balance: unknown")
        logger.info("Checking token approvals...")
        check_and_set_approvals(get_private_key())

    await sync_inventory_from_api(CONFIG.proxy_wallet)
    logger.info(f"Inventory: {get_inventory_summary()}")

    clob_client = create_clob_client()
    if clob_client:
        await recover_pending_orders(clob_client)

    logger.info("Bot started. Monitoring trades...")

    try:
        balance = await get_usdc_balance() if clob_client else 0
        await telegram.bot_started(len(CONFIG.user_addresses), max(balance, 0))
    except Exception:
        pass

    # In preview mode without CLOB client, only run detection + periodic (monitoring only)
    workers = [_supervised("detection", _detection_loop), _supervised("periodic", _periodic_loop)]
    if TIER_1C.enabled:
        workers.append(_supervised("pattern-scanner", _pattern_scanner_loop))
    if clob_client:
        workers.append(_supervised("execution", lambda: _execution_loop(clob_client)))
        workers.append(_supervised("verification", lambda: _verification_loop(clob_client)))
    else:
        # No CLOB client → no executor. We still need to drain the detection
        # queue, otherwise it grows unboundedly. The drain worker also feeds
        # 1c pattern detection so 1a/1b wallet trades are still scanned.
        logger.info("No CLOB client — running in monitor-only mode (detection + alerts + pattern drain)")
        workers.append(_supervised("monitor-drain", _monitor_drain_loop))

    try:
        dead_worker = await asyncio.gather(
            *workers,
            return_exceptions=False,
        )
    except Exception as err:
        logger.error(f"Worker died: {error_message(err)}")
        await telegram.bot_error(f"Worker died: {error_message(err)}")
    finally:
        _shutting_down = True
        _release_lock()
        logger.flush()
