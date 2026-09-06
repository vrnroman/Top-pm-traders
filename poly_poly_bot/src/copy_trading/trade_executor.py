"""Trade execution orchestrator for Strategy #1 copy trading.

Two main hot paths:
  1. place_trade_orders — execution worker: dedup, pattern detection, tier routing,
     risk evaluation, market quality check, order placement.
  2. process_verifications — verification worker: fill checking, partial fills,
     cancel handling, inventory updates, risk accounting adjustments.

Plus crash recovery via recover_pending_orders.
"""

from __future__ import annotations

import time
from typing import Optional

from py_clob_client_v2 import ClobClient

from src.config import CONFIG
from src.copy_trading import live_mode, zset
from src.logger import logger
from src.models import (
    DetectedTrade,
    FillResult,
    OrderResult,
    PendingOrder,
    QueuedTrade,
    TradeRecord,
)
from src.utils import error_message, round_cents, short_address

# ---------------------------------------------------------------------------
# Lazy imports to avoid circular dependencies
# ---------------------------------------------------------------------------


def _risk_manager():
    from src.copy_trading.risk_manager import (
        evaluate_trade,
        record_placement,
        adjust_placement,
    )
    return evaluate_trade, record_placement, adjust_placement


def _tiered_risk():
    from src.copy_trading.tiered_risk_manager import (
        evaluate_tiered_trade,
        record_tiered_placement,
        release_tiered_exposure,
    )
    return evaluate_tiered_trade, record_tiered_placement, release_tiered_exposure


def _strategy_config():
    from src.copy_trading.strategy_config import (
        TIERED_MODE,
        get_wallet_tier,
        TIER_1C,
    )
    return TIERED_MODE, get_wallet_tier, TIER_1C


def _trade_store():
    from src.copy_trading.trade_store import (
        is_seen_trade,
        mark_trade_as_seen,
        increment_retry,
        is_max_retries,
        record_trade_history,
        get_duplicate_count,
    )
    return (
        is_seen_trade,
        mark_trade_as_seen,
        increment_retry,
        is_max_retries,
        record_trade_history,
        get_duplicate_count,
    )


def _trade_queue():
    from src.copy_trading.trade_queue import (
        enqueue_pending_order,
        remove_pending_order,
        load_pending_orders_from_disk,
    )
    return enqueue_pending_order, remove_pending_order, load_pending_orders_from_disk


def _inventory():
    from src.copy_trading.inventory import (
        record_buy,
        record_sell,
        has_position,
        sync_inventory_from_api,
    )
    return record_buy, record_sell, has_position, sync_inventory_from_api


def _telegram():
    from src.copy_trading.telegram_notifier import telegram
    return telegram


def _pattern_detector():
    from src.copy_trading.pattern_detector import analyze_trade_for_patterns
    return analyze_trade_for_patterns


# ---------------------------------------------------------------------------
# Market quality checks
# ---------------------------------------------------------------------------

async def _get_market_snapshot(
    clob_client: ClobClient,
    token_id: str,
) -> Optional[dict]:
    """Fetch best bid/ask from the CLOB for a token.

    Returns dict with best_bid, best_ask, midpoint, spread, spread_bps or None.
    """
    try:
        book = clob_client.get_order_book(token_id)
        bids = book.get("bids", [])
        asks = book.get("asks", [])

        # The CLOB returns bids ASCENDING and asks DESCENDING, so index 0 is
        # the WORST price on each side, not the best. Reading [0] gave
        # best_bid=0.01 / best_ask=0.99 on a real 0.67/0.68 book (probed on
        # the live VM, 2026-08-16): every copy was then rejected as
        # drift/spread, and any that slipped the gates would have posted a
        # real limit near 99c for a token trading at 68c. Take the extremes
        # and never trust the ordering.
        best_bid = max((float(b["price"]) for b in bids), default=0.0)
        best_ask = min((float(a["price"]) for a in asks), default=1.0)

        if best_bid <= 0 or best_ask <= 0:
            return None
        # A crossed book is bad data, not a tight spread. Without this the
        # spread goes NEGATIVE and `_check_market_quality`'s `spread_bps > max`
        # test passes it — the money path would accept a book that
        # market_price.fetch_market_snapshot rejects outright.
        if best_bid >= best_ask:
            return None

        midpoint = (best_bid + best_ask) / 2
        spread = best_ask - best_bid
        spread_bps = int((spread / midpoint) * 10000) if midpoint > 0 else 9999

        # The tick the book quotes in, so the executor can round a limit price
        # TOWARD crossing on it. A book without one falls back to 0.01.
        try:
            tick_size = float(book.get("tick_size") or 0) or None
        except (TypeError, ValueError, AttributeError):
            tick_size = None
        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "midpoint": midpoint,
            "spread": spread,
            "tick_size": tick_size,
            "spread_bps": spread_bps,
            "fetched_at": time.time(),
        }
    except Exception as exc:
        logger.warn(f"[exec] Failed to fetch order book for {token_id[:12]}...: {error_message(exc)}")
        return None


def _check_market_quality(
    trade: DetectedTrade,
    snapshot: Optional[dict],
) -> Optional[str]:
    """Check price drift and spread. Returns rejection reason or None if OK."""
    if snapshot is None:
        return "Could not fetch order book"

    # Drift check: how far has the market moved since the trader's execution?
    mid = snapshot["midpoint"]
    drift = abs(mid - trade.price)
    drift_bps = int((drift / trade.price) * 10000) if trade.price > 0 else 9999

    if drift_bps > CONFIG.max_price_drift_bps:
        return (
            f"Price drift too high: {drift_bps}bps > {CONFIG.max_price_drift_bps}bps "
            f"(trader @ {trade.price:.4f}, market @ {mid:.4f})"
        )

    # Spread check
    if snapshot["spread_bps"] > CONFIG.max_spread_bps:
        return (
            f"Spread too wide: {snapshot['spread_bps']}bps > {CONFIG.max_spread_bps}bps"
        )

    return None


def _book_preview_exit(trade, sell_shares: float) -> None:
    """Book an early-exit realized-P&L row when a preview position is sold by
    mirroring the trader (paper analogue of an exit). Best-effort; attribution
    (tier/trader) is read from the held inventory position."""
    try:
        from datetime import datetime, timezone

        from src.copy_trading.inventory import get_position
        from src.copy_trading.pnl import append_realized

        held = get_position(trade.token_id)
        if not held:
            return
        avg = float(held.get("avg_price", 0) or 0)
        qty = min(sell_shares, float(held.get("shares", 0) or 0))
        if qty <= 0:
            return
        pnl = qty * (trade.price - avg)
        append_realized({
            "timestamp": trade.timestamp or datetime.now(timezone.utc).isoformat(),
            "title": trade.market,
            "condition_id": trade.condition_id,
            "token_id": trade.token_id,
            "shares": round(qty, 6),
            "avg_price": round(avg, 6),
            "cost_basis": round(qty * avg, 6),
            "returned": round(qty * trade.price, 6),
            "pnl": round(pnl, 6),
            "won": pnl > 0,
            "tier": held.get("tier", "") or "",
            "trader_address": trade.trader_address,
            "exit": "sell",
            "source": "preview",
        })
    except Exception as exc:
        logger.warn(f"[exec] preview exit booking failed: {error_message(exc)}")


# ---------------------------------------------------------------------------
# Order execution
# ---------------------------------------------------------------------------

async def _execute_copy_order(
    clob_client: ClobClient,
    trade: DetectedTrade,
    copy_size: float,
    snapshot: Optional[dict],
) -> Optional[OrderResult]:
    """Place a copy order on the CLOB.

    For BUY: limit order at best_ask (or trader price if no snapshot).
    For SELL: limit order at best_bid (or trader price if no snapshot).

    Returns OrderResult or None on failure.
    """
    try:
        # One pricing rule for the whole repo (see order_executor's docstring):
        # the same function the shadow-quote measurement runs, so "what we'd
        # pay" and "what we measured we'd pay" can never drift apart.
        from src.copy_trading.order_executor import quote_copy_order, shares_for

        order_price = quote_copy_order(trade.side, trade.price, snapshot)
        if order_price is None:
            logger.warn(f"[exec] Invalid order price for {trade.market}")
            return None

        shares = shares_for(copy_size, order_price)
        if shares <= 0:
            return None

        from py_clob_client_v2 import OrderArgs
        from py_clob_client_v2.order_builder.constants import BUY, SELL
        side = BUY if trade.side == "BUY" else SELL

        # A DATACLASS, not a dict. `create_and_post_order` reads
        # `order_args.token_id`, so the dict this used to pass raised
        # "'dict' object has no attribute 'token_id'" on every call and the
        # order path could never have placed anything. The suite never caught
        # it because its fakes accept a dict happily; the canary caught it on
        # the first real order, which is what the canary is for.
        order_args = OrderArgs(
            token_id=trade.token_id,
            price=order_price,
            size=round(shares, 2),
            side=side,
        )

        resp = clob_client.create_and_post_order(order_args)
        order_id = resp.get("orderID", "") or resp.get("id", "")

        if not order_id:
            logger.warn(f"[exec] No order ID returned: {resp}")
            return None

        return OrderResult(
            order_id=order_id,
            shares=round(shares, 2),
            order_price=order_price,
        )

    except Exception as exc:
        logger.error(f"[exec] Order placement failed: {error_message(exc)}")
        return None


async def _verify_order_fill(
    clob_client: ClobClient,
    order_id: str,
) -> FillResult:
    """Check the fill status of an order.

    Returns FillResult with status FILLED/PARTIAL/UNFILLED/UNKNOWN.
    """
    try:
        order = clob_client.get_order(order_id)
        if order is None:
            return FillResult(status="UNKNOWN")

        status = order.get("status", "").upper()
        size_matched = float(order.get("sizeMatched", 0) or order.get("size_matched", 0))
        original_size = float(order.get("originalSize", 0) or order.get("original_size", 0) or order.get("size", 0))
        avg_price = float(order.get("associatedTransactions", [{}])[0].get("price", 0)) if order.get("associatedTransactions") else 0

        # Calculate fill price from matched amount
        price = float(order.get("price", 0))
        filled_usd = size_matched * price if price > 0 else 0

        if status == "MATCHED" or (original_size > 0 and abs(size_matched - original_size) < 0.01):
            return FillResult(
                status="FILLED",
                filled_shares=size_matched,
                filled_usd=filled_usd,
                fill_price=price,
            )
        elif size_matched > 0:
            return FillResult(
                status="PARTIAL",
                filled_shares=size_matched,
                filled_usd=filled_usd,
                fill_price=price,
            )
        elif status in ("LIVE", "OPEN"):
            return FillResult(status="UNFILLED")
        else:
            return FillResult(status="UNKNOWN")

    except Exception as exc:
        logger.warn(f"[exec] Fill verification failed for {order_id}: {error_message(exc)}")
        return FillResult(status="UNKNOWN")


async def _cancel_order(clob_client: ClobClient, order_id: str) -> bool:
    """Attempt to cancel an order. Returns True if successful.

    The v2 client has NO `cancel`; it has `cancel_order(OrderPayload)`. The
    old call raised AttributeError every time, so an unfilled order was never
    withdrawn and the live guard's stuck-order action could not act either.
    Same class as the order-args dict: a shape the fakes accepted and the real
    client does not.
    """
    try:
        from py_clob_client_v2 import OrderPayload
        clob_client.cancel_order(OrderPayload(orderID=order_id))
        return True
    except Exception as exc:
        logger.warn(f"[exec] Cancel failed for {order_id}: {error_message(exc)}")
        return False


# ---------------------------------------------------------------------------
# Execution worker — place_trade_orders
# ---------------------------------------------------------------------------

async def place_trade_orders(
    queued: list[QueuedTrade],
    clob_client: ClobClient,
) -> int:
    """Execute queued trades: dedup, risk check, market quality, place orders.

    Processes trades sorted by source_detected_at (oldest first).
    Returns the number of orders successfully placed.

    Critical operation order per trade:
      1. record_placement (risk accounting)
      2. enqueue_pending_order (verification queue)
      3. mark_trade_as_seen (dedup)
    """
    (
        is_seen_trade,
        mark_trade_as_seen,
        increment_retry,
        is_max_retries,
        record_trade_history,
        get_duplicate_count,
    ) = _trade_store()
    evaluate_trade, record_placement_fn, _ = _risk_manager()
    evaluate_tiered_trade, record_tiered_placement, _ = _tiered_risk()
    TIERED_MODE, get_wallet_tier, TIER_1C = _strategy_config()
    enqueue_pending_order, _, _ = _trade_queue()
    record_buy, record_sell, has_position, _ = _inventory()
    tg = _telegram()
    analyze_patterns = _pattern_detector()

    placed = 0

    # Sort by detection time — oldest first
    sorted_trades = sorted(queued, key=lambda q: q.source_detected_at)

    for qt in sorted_trades:
        trade = qt.trade
        now_ms = time.time() * 1000
        # Set before the try so the handler below can always ask whether a
        # one-shot was spent on this trade.
        canary_shot = False

        try:
            # --- Dedup ---
            if is_seen_trade(trade.id):
                logger.debug(f"[exec] Skipping seen trade: {trade.id[:20]}...")
                continue

            if is_max_retries(trade.id):
                logger.debug(f"[exec] Skipping max-retried trade: {trade.id[:20]}...")
                continue

            # --- Pattern detection (1c) ---
            if TIERED_MODE and TIER_1C.enabled:
                try:
                    await analyze_patterns(trade)
                except Exception as exc:
                    logger.warn(f"[exec] Pattern detection error: {error_message(exc)}")

            # --- SET Z: the unconditional membership check ---
            # Deliberately here, OUTSIDE the TIERED_MODE branch and before any
            # routing, so the Z invariant does not depend on which trade
            # source produced this trade or on how the tier lists happen to be
            # configured. Two live bypasses existed without it: `onchain_source`
            # still builds its own list from CONFIG.user_addresses, and the
            # legacy non-tiered branch consults no wallet set at all, so
            # emptying the env tier lists would have put 21 ungated wallets
            # back on the money path.
            if (trade.trader_address or "").lower() not in {
                    w.lower() for w in zset.wallets()}:
                logger.skip(f"[exec] not in set Z: "
                            f"{short_address(trade.trader_address)}")
                mark_trade_as_seen(trade.id)
                continue

            # --- Tier routing ---
            tier: Optional[str] = None
            alert_only = False
            copy_size = 0.0

            # A set-Z wallet always carries a tier (admit() writes one), so it
            # is sized by the tiered evaluator whatever the env tier lists say;
            # the legacy branch below has no governor, no exposure cap and no
            # trigger, and must not be the path a Z wallet takes.
            tier = get_wallet_tier(trade.trader_address)
            if TIERED_MODE or tier is not None:
                if tier is not None:
                    decision = evaluate_tiered_trade(trade, tier)
                    if decision.alert_only:
                        alert_only = True
                        logger.info(
                            f"[exec] Alert-only tier {tier}: {trade.side} ${trade.size:.2f} "
                            f"on '{trade.market[:40]}'"
                        )
                        # Record as seen but don't place
                        record_trade_history(TradeRecord(
                            timestamp=trade.timestamp,
                            trader_address=trade.trader_address,
                            market=trade.market,
                            side=trade.side,
                            trader_size=trade.size,
                            copy_size=decision.copy_size,
                            price=trade.price,
                            status="ALERT_ONLY",
                            reason=decision.reason,
                            source=qt.source,
                            source_detected_at=qt.source_detected_at,
                            enqueued_at=qt.enqueued_at,
                            condition_id=trade.condition_id,
                            token_id=trade.token_id,
                            outcome=trade.outcome,
                        ))
                        mark_trade_as_seen(trade.id)
                        continue

                    if not decision.should_copy:
                        logger.skip(
                            f"[exec] Tier {tier} skip: {decision.reason}: "
                            f"{trade.side} ${trade.size:.2f} on '{trade.market[:40]}'"
                        )
                        record_trade_history(TradeRecord(
                            timestamp=trade.timestamp,
                            trader_address=trade.trader_address,
                            market=trade.market,
                            side=trade.side,
                            trader_size=trade.size,
                            copy_size=0,
                            price=trade.price,
                            status="SKIPPED",
                            reason=decision.reason,
                            source=qt.source,
                            source_detected_at=qt.source_detected_at,
                            enqueued_at=qt.enqueued_at,
                            condition_id=trade.condition_id,
                            token_id=trade.token_id,
                            outcome=trade.outcome,
                        ))
                        mark_trade_as_seen(trade.id)
                        continue

                    copy_size = decision.copy_size
                else:
                    # Wallet not in any tier — skip in tiered mode
                    logger.debug(f"[exec] Wallet {short_address(trade.trader_address)} not in any tier")
                    mark_trade_as_seen(trade.id)
                    continue
            else:
                # Legacy (non-tiered) risk evaluation
                decision = evaluate_trade(trade)
                if not decision.should_copy:
                    logger.skip(
                        f"[exec] Skip: {decision.reason}: "
                        f"{trade.side} ${trade.size:.2f} on '{trade.market[:40]}'"
                    )
                    record_trade_history(TradeRecord(
                        timestamp=trade.timestamp,
                        trader_address=trade.trader_address,
                        market=trade.market,
                        side=trade.side,
                        trader_size=trade.size,
                        copy_size=0,
                        price=trade.price,
                        status="SKIPPED",
                        reason=decision.reason,
                        source=qt.source,
                        source_detected_at=qt.source_detected_at,
                        enqueued_at=qt.enqueued_at,
                        condition_id=trade.condition_id,
                        token_id=trade.token_id,
                        outcome=trade.outcome,
                    ))
                    mark_trade_as_seen(trade.id)
                    continue
                copy_size = decision.copy_size

            # --- Duplicate bet check ---
            market_key = trade.market or trade.condition_id
            dup_count = get_duplicate_count(market_key, trade.side)
            if dup_count >= CONFIG.max_copies_per_market_side:
                logger.skip(
                    f"[exec] Max copies reached ({dup_count}/{CONFIG.max_copies_per_market_side}) "
                    f"for {trade.side} on '{trade.market[:40]}'"
                )
                mark_trade_as_seen(trade.id)
                continue

            # --- SELL check: verify we have a position ---
            if trade.side == "SELL" and not has_position(trade.token_id):
                logger.info(f"[exec] SELL but no position for {trade.token_id[:12]}..., syncing inventory...")
                try:
                    _, _, _, sync_fn = _inventory()
                    await sync_fn(CONFIG.proxy_wallet)
                except Exception:
                    pass
                if not has_position(trade.token_id):
                    logger.skip(f"[exec] SELL skipped: no position after sync for {trade.token_id[:12]}...")
                    mark_trade_as_seen(trade.id)
                    continue

            # --- Market quality check ---
            snapshot = await _get_market_snapshot(clob_client, trade.token_id)
            quality_issue = _check_market_quality(trade, snapshot)
            if quality_issue is not None:
                logger.skip(f"[exec] Market quality: {quality_issue}")
                # Retry — don't mark as seen
                increment_retry(trade.id)
                continue

            # --- Preview mode ---
            # The single gate between this loop and real money. It reads the
            # two-key interlock (live_mode), not CONFIG.preview_mode directly:
            # PREVIEW_MODE=false alone is NOT enough — the owner's env key and
            # a runtime /live CONFIRM are both required, and it fails closed on
            # any error. Startup-time guards elsewhere still read
            # CONFIG.preview_mode: a live boot prepares (approvals, inventory
            # sync), the arm releases.
            if live_mode.is_preview():
                if not CONFIG.preview_mode:
                    # A LIVE process that is runtime-disarmed. No paper
                    # bookkeeping here: the inventory file is the REAL one in
                    # this process, the notifier would announce "[LIVE]", and a
                    # paper exit would write an untagged realized row. Log it,
                    # record it, and move on.
                    logger.trade(
                        f"[DISARMED] would {trade.side} ${copy_size:.2f} on "
                        f"'{trade.market[:40]}' @ {trade.price:.4f} "
                        f"(from {short_address(trade.trader_address)}); the arm is off")
                    # Say it ONCE per disarm episode: the last outage sat
                    # silent for 22 hours because this branch only logged.
                    announce, arm_rec = live_mode.note_disarmed_skip()
                    if announce:
                        try:
                            from src.copy_trading.telegram_notifier import _escape_html, _send_message
                            since = arm_rec.get("ts")
                            when = (time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(since)))
                                    if since else "?")
                            await _send_message(
                                f"⏸ <b>A followed wallet traded and the bot did not copy it: "
                                f"trading is OFF.</b>\n"
                                f"{short_address(trade.trader_address)} {trade.side} "
                                f"${trade.size:,.0f} on '{_escape_html(trade.market[:50])}' "
                                f"at {trade.price:.3f}; the copy would have been "
                                f"${copy_size:.2f}.\nOff since {when} (by "
                                f"{_escape_html(str(arm_rec.get('by') or '?'))}). Further "
                                f"skips are counted on the daily line, not announced. "
                                f"<code>/live CONFIRM</code> to trade again.", kind="deal")
                        except Exception as exc:
                            logger.warn(f"[exec] disarmed-skip notice failed: {exc}")
                    record_trade_history(TradeRecord(
                        timestamp=trade.timestamp,
                        trader_address=trade.trader_address,
                        market=trade.market,
                        side=trade.side,
                        trader_size=trade.size,
                        copy_size=copy_size,
                        price=trade.price,
                        status="DISARMED",
                        source=qt.source,
                        source_detected_at=qt.source_detected_at,
                        enqueued_at=qt.enqueued_at,
                        condition_id=trade.condition_id,
                        token_id=trade.token_id,
                        outcome=trade.outcome,
                    ))
                    mark_trade_as_seen(trade.id)
                    continue
                logger.trade(
                    f"[PREVIEW] {trade.side} ${copy_size:.2f} on '{trade.market[:40]}' "
                    f"@ {trade.price:.4f} (from {short_address(trade.trader_address)})"
                )

                # Record in inventory for preview tracking
                if trade.side == "BUY":
                    shares = copy_size / trade.price if trade.price > 0 else 0
                    record_buy(
                        trade.token_id, shares, trade.price, market_key, trade.market,
                        tier=tier or "", trader_address=trade.trader_address,
                    )
                elif trade.side == "SELL":
                    shares = copy_size / trade.price if trade.price > 0 else 0
                    if shares > 0 and CONFIG.preview_realize_enabled:
                        _book_preview_exit(trade, shares)
                    record_sell(trade.token_id, shares)

                record_trade_history(TradeRecord(
                    timestamp=trade.timestamp,
                    trader_address=trade.trader_address,
                    market=trade.market,
                    side=trade.side,
                    trader_size=trade.size,
                    copy_size=copy_size,
                    price=trade.price,
                    status="PREVIEW",
                    source=qt.source,
                    source_detected_at=qt.source_detected_at,
                    enqueued_at=qt.enqueued_at,
                    condition_id=trade.condition_id,
                    token_id=trade.token_id,
                    outcome=trade.outcome,
                ))

                await tg.trade_placed(trade.market, trade.side, copy_size, trade.price)
                mark_trade_as_seen(trade.id)
                placed += 1
                continue

            # --- The bankroll governor at the single order sink ---
            # Whatever branch sized this copy, a real order never leaves
            # without the governor: closed means refused, the per-copy cap
            # binds, and the copy trigger is the evidence base's. The tiered
            # branch already applied these; the legacy branch did not.
            from src.copy_trading import live_budget
            gov = live_budget.caps(live=True)
            if gov is None:
                logger.skip("[exec] bankroll governor closed (LIVE_BUDGET_USD is not "
                            "set): refusing a live copy")
                mark_trade_as_seen(trade.id)
                continue
            if trade.side == "BUY":
                from src.copy_trading.daily_spend_guard import can_copy_wallet
                _ok_w, _why_w = can_copy_wallet(trade.trader_address)
                if not _ok_w:
                    logger.skip(f"[exec] {_why_w}: not copied")
                    mark_trade_as_seen(trade.id)
                    continue
            if trade.side == "BUY" and trade.size < gov.min_trader_bet_usd:
                logger.skip(f"[exec] target bet ${trade.size:.0f} is under the evidence "
                            f"base's ${gov.min_trader_bet_usd:.0f}: not copied")
                mark_trade_as_seen(trade.id)
                continue
            if copy_size > gov.per_copy_usd:
                copy_size = gov.per_copy_usd

            # --- The canary: one minimum-size order, then the arm comes off ---
            # It only ever LOWERS the size of an order every rail above has
            # already admitted; it cannot admit one. The shot is spent and the
            # arm pulled BEFORE the post: an ambiguous post (accepted by the
            # CLOB, reply lost) or a failed state write must cost a re-arm,
            # never a second ticket.
            from src.copy_trading import canary
            # BUY only: the canary exists to price an ENTRY against the book,
            # and an exit measures nothing it was built to answer.
            canary_staged = canary.is_staged()
            canary_shot = canary_staged and trade.side == "BUY"
            if canary_staged and not canary_shot:
                # /live CONFIRM promises the FIRST live order is one ticket at
                # the minimum. A full-size SELL slipping out ahead of the shot
                # would break that promise silently, so it waits its turn
                # rather than being dropped.
                logger.skip(f"[canary] staged: holding this {trade.side} until the "
                            f"one shot fires")
                increment_retry(trade.id)
                continue
            if canary_shot:
                from src.copy_trading.order_executor import quote_copy_order
                _cp = quote_copy_order(trade.side, trade.price, snapshot)
                shot_size = canary.size_for(clob_client, trade.token_id, _cp or trade.price)
                # The market's own minimum is a THIRD-PARTY number on the one
                # path that would otherwise escape "one knob derives every live
                # cap". A book quoting a 500-share minimum would have posted
                # $255 against a $7.75 cap. It never raises the ticket: if this
                # market cannot be entered inside the cap, the shot stays
                # staged and waits for one that can.
                if shot_size > gov.per_copy_usd:
                    logger.skip(
                        f"[canary] {trade.market[:40]}: this market's minimum order "
                        f"${shot_size:.2f} is over the ${gov.per_copy_usd:.2f} per-copy "
                        f"cap; staying staged for a market inside the cap")
                    canary_shot = False
                    mark_trade_as_seen(trade.id)
                    continue
                copy_size = shot_size
                consumed = canary.consume(
                    market=trade.market, token_id=trade.token_id,
                    their_price=trade.price,
                    quoted_ask=(snapshot or {}).get("best_ask"), copy_size=copy_size,
                    notify_latency_s=(
                        (qt.received_at_ms - qt.source_detected_at) / 1000.0
                        if getattr(qt, "received_at_ms", None) else None))
                pulled = live_mode.disarm(by="canary")
                if not consumed or not pulled:
                    # Refusing to post is not enough: the arm record may still
                    # read ARMED, and the next cycle would place FULL-SIZE
                    # copies with no canary staged. Stop this process trading.
                    live_mode.hard_disarm(
                        "the canary's one-shot or its disarm could not be persisted")
                    logger.error("[canary] could not persist the one-shot or pull the "
                                 "arm; this process is hard-disarmed and will place "
                                 "no further real orders until it is restarted")
                    try:
                        from src.copy_trading.telegram_notifier import _send_message
                        await _send_message(
                            "🚨 <b>Canary could not be recorded.</b> No order was "
                            "placed and this process has stopped trading. The saved "
                            "arm may still read ARMED, so check disk space on the VM "
                            "and send <code>/live DISARM</code> before restarting.")
                    except Exception as exc:
                        logger.warn(f"[canary] hard-disarm message failed: {exc}")
                    break
                logger.warn(f"[exec] canary: one order at the minimum, ${copy_size:.2f}; "
                            f"the arm is off")

            # --- Live order placement ---
            order_submitted_at = time.time() * 1000
            result = await _execute_copy_order(clob_client, trade, copy_size, snapshot)

            if result is None:
                logger.error(f"[exec] Order placement returned None for '{trade.market[:40]}'")
                await tg.trade_failed(trade.market, "Order placement returned no result")
                if canary_shot:
                    canary.record_post_failed("order placement returned no result")
                    try:
                        from src.copy_trading.telegram_notifier import _send_message
                        await _send_message(canary.report_text())
                    except Exception as exc:
                        logger.warn(f"[canary] post-failed message failed: {exc}")
                    mark_trade_as_seen(trade.id)
                    break
                increment_retry(trade.id)
                continue

            logger.trade(
                f"[LIVE] {trade.side} ${copy_size:.2f} on '{trade.market[:40]}' "
                f"@ {result.order_price:.4f}, order {result.order_id[:12]}..."
            )
            if canary_shot:
                canary.record_fired(order_id=result.order_id, order_price=result.order_price)
                try:
                    from src.copy_trading.telegram_notifier import _escape_html, _send_message
                    await _send_message(
                        f"🐤 <b>Test copy placed</b>: ${copy_size:.2f} on "
                        f"'{_escape_html(trade.market[:50])}' at {result.order_price:.4f} "
                        f"(the wallet paid {trade.price:.4f}). Trading is paused until "
                        f"you send /live CONFIRM again; the fill report follows when "
                        f"the exchange confirms it.")
                except Exception as exc:
                    logger.warn(f"[canary] fired message failed: {exc}")

            # Critical operation order: record → enqueue → mark seen
            # 1. Record placement in risk accounting
            if TIERED_MODE and tier is not None:
                record_tiered_placement(tier, copy_size)
            else:
                record_placement_fn(trade, copy_size)

            # Global daily-spend cap accounting (BUY only)
            if trade.side == "BUY":
                from src.copy_trading.daily_spend_guard import record_spend
                record_spend(copy_size, source=f"copy:{tier or 'legacy'}")
                from src.copy_trading.daily_spend_guard import record_wallet_copy
                record_wallet_copy(trade.trader_address)

            # 2. Enqueue for verification
            pending = PendingOrder(
                trade=trade,
                order_id=result.order_id,
                order_price=result.order_price,
                copy_size=copy_size,
                placed_at=time.time() * 1000,
                market_key=market_key,
                side=trade.side,
                source_detected_at=qt.source_detected_at,
                enqueued_at=qt.enqueued_at,
                order_submitted_at=order_submitted_at,
                source=qt.source,
                tier=tier,
            )
            enqueue_pending_order(pending)

            # 3. Mark trade as seen (dedup)
            mark_trade_as_seen(trade.id)

            await tg.trade_placed(trade.market, trade.side, copy_size, result.order_price)

            record_trade_history(TradeRecord(
                timestamp=trade.timestamp,
                trader_address=trade.trader_address,
                market=trade.market,
                side=trade.side,
                trader_size=trade.size,
                copy_size=copy_size,
                price=trade.price,
                status="PLACED",
                order_id=result.order_id,
                trader_price=trade.price,
                source=qt.source,
                source_detected_at=qt.source_detected_at,
                enqueued_at=qt.enqueued_at,
                order_submitted_at=order_submitted_at,
                condition_id=trade.condition_id,
                token_id=trade.token_id,
                outcome=trade.outcome,
                drift_bps=(
                    int(abs(snapshot["midpoint"] - trade.price) / trade.price * 10000)
                    if snapshot and trade.price > 0 else None
                ),
                spread_bps=snapshot["spread_bps"] if snapshot else None,
            ))

            placed += 1
            if canary_shot:
                # The one shot is out and the arm is off. The rest of this
                # batch must not run: in a live process the preview branch
                # would write paper into the real inventory.
                break

        except Exception as exc:
            # A one-shot spent on a trade that then threw must not stay
            # half-open: the arm is already off, so say the order did not
            # post rather than leaving a fired record with no outcome.
            if canary_shot:
                try:
                    from src.copy_trading import canary as _canary
                    rec = (_canary.read().get("fired") or {})
                    if rec and not rec.get("posted"):
                        _canary.record_post_failed(error_message(exc))
                except Exception:
                    pass
            logger.error(
                f"[exec] Unexpected error processing trade {trade.id[:20]}...: "
                f"{error_message(exc)}"
            )
            increment_retry(trade.id)

    return placed


# ---------------------------------------------------------------------------
# Verification worker — process_verifications
# ---------------------------------------------------------------------------

MAX_UNCERTAIN_CYCLES = 5


async def process_verifications(
    pending: list[PendingOrder],
    clob_client: ClobClient,
) -> None:
    """Verify fill status for pending orders and update inventory/risk.

    Handles FILLED, PARTIAL, UNFILLED, and UNKNOWN statuses.
    Cancels unfilled orders. Tracks uncertain cycles (max 5 before abandoning).
    """
    _, _, adjust_placement = _risk_manager()
    _, _, release_tiered_exposure = _tiered_risk()
    TIERED_MODE, _, _ = _strategy_config()
    _, remove_pending_order, _ = _trade_queue()
    record_buy, record_sell, _, _ = _inventory()
    _, mark_trade_as_seen, _, _, record_trade_history, _ = _trade_store()
    tg = _telegram()

    for po in pending:
        trade = po.trade

        try:
            fill = await _verify_order_fill(clob_client, po.order_id)

            # The canary's or the test order's fate, reported once.
            from src.copy_trading import canary
            _canary_report = (canary.record_fill(po.order_id, fill)
                              or canary.record_test_fill(po.order_id, fill))
            if _canary_report:
                try:
                    from src.copy_trading.telegram_notifier import _send_message
                    await _send_message(_canary_report)
                except Exception as exc:
                    logger.warn(f"[canary] report message failed: {exc}")

            if fill.status == "FILLED":
                # Full fill
                new_shares = fill.filled_shares - po.accounted_filled_shares
                new_usd = fill.filled_usd - po.accounted_filled_usd

                if new_shares > 0:
                    if trade.side == "BUY":
                        record_buy(
                            trade.token_id,
                            new_shares,
                            fill.fill_price,
                            po.market_key,
                            trade.market,
                            tier=po.tier or "",
                            trader_address=trade.trader_address,
                        )
                    elif trade.side == "SELL":
                        record_sell(trade.token_id, new_shares)

                logger.trade(
                    f"[verify] FILLED: {trade.side} {fill.filled_shares:.2f} shares "
                    f"on '{trade.market[:40]}' @ {fill.fill_price:.4f}"
                )

                now_ms = time.time() * 1000
                record_trade_history(TradeRecord(
                    timestamp=trade.timestamp,
                    trader_address=trade.trader_address,
                    market=trade.market,
                    side=trade.side,
                    trader_size=trade.size,
                    copy_size=po.copy_size,
                    price=trade.price,
                    status="FILLED",
                    order_id=po.order_id,
                    fill_price=fill.fill_price,
                    fill_shares=fill.filled_shares,
                    source=po.source,
                    source_detected_at=po.source_detected_at,
                    enqueued_at=po.enqueued_at,
                    order_submitted_at=po.order_submitted_at,
                    first_fill_seen_at=now_ms,
                    condition_id=trade.condition_id,
                    token_id=trade.token_id,
                    outcome=trade.outcome,
                ))

                await tg.trade_filled(trade.market, fill.filled_shares, fill.fill_price)
                remove_pending_order(po.order_id)

            elif fill.status == "PARTIAL":
                # Partial fill — account for new fills incrementally
                new_shares = fill.filled_shares - po.accounted_filled_shares
                new_usd = fill.filled_usd - po.accounted_filled_usd

                if new_shares > 0:
                    if trade.side == "BUY":
                        record_buy(
                            trade.token_id,
                            new_shares,
                            fill.fill_price,
                            po.market_key,
                            trade.market,
                            tier=po.tier or "",
                            trader_address=trade.trader_address,
                        )
                    elif trade.side == "SELL":
                        record_sell(trade.token_id, new_shares)

                    po.accounted_filled_shares = fill.filled_shares
                    po.accounted_filled_usd = fill.filled_usd

                    logger.info(
                        f"[verify] PARTIAL: {fill.filled_shares:.2f} shares filled so far "
                        f"on '{trade.market[:40]}'"
                    )

                # Don't remove from pending — wait for full fill or timeout

            elif fill.status == "UNFILLED":
                # Try to cancel the order
                cancelled = await _cancel_order(clob_client, po.order_id)

                if cancelled:
                    logger.info(f"[verify] UNFILLED, cancelled order {po.order_id[:12]}...")

                    # Adjust risk accounting: refund the unexecuted portion
                    unfilled_usd = po.copy_size - po.accounted_filled_usd
                    if unfilled_usd > 0:
                        if TIERED_MODE and po.tier is not None:
                            release_tiered_exposure(po.tier, unfilled_usd)
                        else:
                            adjust_placement(trade, -unfilled_usd)

                    record_trade_history(TradeRecord(
                        timestamp=trade.timestamp,
                        trader_address=trade.trader_address,
                        market=trade.market,
                        side=trade.side,
                        trader_size=trade.size,
                        copy_size=po.copy_size,
                        price=trade.price,
                        status="UNFILLED",
                        order_id=po.order_id,
                        fill_shares=po.accounted_filled_shares,
                        source=po.source,
                        condition_id=trade.condition_id,
                        token_id=trade.token_id,
                        outcome=trade.outcome,
                    ))

                    await tg.trade_unfilled(trade.market)
                    remove_pending_order(po.order_id)
                else:
                    # Cancel failed — track uncertain cycles
                    po.uncertain_cycles += 1
                    logger.warn(
                        f"[verify] Cancel failed for {po.order_id[:12]}... "
                        f"(uncertain cycle {po.uncertain_cycles}/{MAX_UNCERTAIN_CYCLES})"
                    )

                    if po.uncertain_cycles >= MAX_UNCERTAIN_CYCLES:
                        logger.error(
                            f"[verify] Abandoning order {po.order_id[:12]}... "
                            f"after {MAX_UNCERTAIN_CYCLES} uncertain cycles"
                        )
                        # Release full exposure as a safety measure
                        if TIERED_MODE and po.tier is not None:
                            release_tiered_exposure(po.tier, po.copy_size)
                        else:
                            adjust_placement(trade, -po.copy_size)

                        record_trade_history(TradeRecord(
                            timestamp=trade.timestamp,
                            trader_address=trade.trader_address,
                            market=trade.market,
                            side=trade.side,
                            trader_size=trade.size,
                            copy_size=po.copy_size,
                            price=trade.price,
                            status="ABANDONED",
                            order_id=po.order_id,
                            reason=f"Cancel failed {MAX_UNCERTAIN_CYCLES} times",
                            source=po.source,
                            condition_id=trade.condition_id,
                            token_id=trade.token_id,
                            outcome=trade.outcome,
                        ))

                        remove_pending_order(po.order_id)

            elif fill.status == "UNKNOWN":
                po.uncertain_cycles += 1
                logger.warn(
                    f"[verify] UNKNOWN status for {po.order_id[:12]}... "
                    f"(uncertain cycle {po.uncertain_cycles}/{MAX_UNCERTAIN_CYCLES})"
                )

                if po.uncertain_cycles >= MAX_UNCERTAIN_CYCLES:
                    logger.error(
                        f"[verify] Abandoning order {po.order_id[:12]}... "
                        f"after {MAX_UNCERTAIN_CYCLES} unknown cycles"
                    )
                    # Release full exposure
                    if TIERED_MODE and po.tier is not None:
                        release_tiered_exposure(po.tier, po.copy_size)
                    else:
                        adjust_placement(trade, -po.copy_size)

                    record_trade_history(TradeRecord(
                        timestamp=trade.timestamp,
                        trader_address=trade.trader_address,
                        market=trade.market,
                        side=trade.side,
                        trader_size=trade.size,
                        copy_size=po.copy_size,
                        price=trade.price,
                        status="ABANDONED",
                        order_id=po.order_id,
                        reason=f"Unknown status {MAX_UNCERTAIN_CYCLES} times",
                        source=po.source,
                        condition_id=trade.condition_id,
                        token_id=trade.token_id,
                        outcome=trade.outcome,
                    ))

                    remove_pending_order(po.order_id)

        except Exception as exc:
            logger.error(
                f"[verify] Error processing order {po.order_id[:12]}...: "
                f"{error_message(exc)}"
            )


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------

async def recover_pending_orders(clob_client: ClobClient) -> None:
    """Recover pending orders from disk after a crash/restart.

    Loads persisted pending orders, verifies each against the CLOB,
    reconciles risk state, and marks recovered trades as seen.
    """
    _, _, load_pending = _trade_queue()
    _, mark_trade_as_seen, _, _, record_trade_history, _ = _trade_store()
    _, _, adjust_placement = _risk_manager()
    _, _, release_tiered_exposure = _tiered_risk()
    TIERED_MODE, _, _ = _strategy_config()
    record_buy, record_sell, _, _ = _inventory()
    _, remove_pending_order, _ = _trade_queue()

    pending = load_pending()
    if not pending:
        logger.info("[recovery] No pending orders to recover")
        return

    logger.info(f"[recovery] Recovering {len(pending)} pending order(s)...")

    for po in pending:
        trade = po.trade

        try:
            fill = await _verify_order_fill(clob_client, po.order_id)

            if fill.status == "FILLED":
                # Order was filled while we were down
                new_shares = fill.filled_shares - po.accounted_filled_shares
                if new_shares > 0:
                    if trade.side == "BUY":
                        record_buy(
                            trade.token_id,
                            new_shares,
                            fill.fill_price,
                            po.market_key,
                            trade.market,
                            tier=po.tier or "",
                            trader_address=trade.trader_address,
                        )
                    elif trade.side == "SELL":
                        record_sell(trade.token_id, new_shares)

                logger.info(
                    f"[recovery] Order {po.order_id[:12]}... was FILLED "
                    f"({fill.filled_shares:.2f} shares)"
                )
                remove_pending_order(po.order_id)

            elif fill.status == "PARTIAL":
                # Partially filled — account for fills, leave in pending
                new_shares = fill.filled_shares - po.accounted_filled_shares
                if new_shares > 0:
                    if trade.side == "BUY":
                        record_buy(
                            trade.token_id,
                            new_shares,
                            fill.fill_price,
                            po.market_key,
                            trade.market,
                            tier=po.tier or "",
                            trader_address=trade.trader_address,
                        )
                    elif trade.side == "SELL":
                        record_sell(trade.token_id, new_shares)
                    po.accounted_filled_shares = fill.filled_shares
                    po.accounted_filled_usd = fill.filled_usd

                logger.info(
                    f"[recovery] Order {po.order_id[:12]}... PARTIAL "
                    f"({fill.filled_shares:.2f} shares), keeping in pending"
                )

            elif fill.status in ("UNFILLED", "UNKNOWN"):
                # Try to cancel
                cancelled = await _cancel_order(clob_client, po.order_id)

                # Release risk exposure for unfilled portion
                unfilled_usd = po.copy_size - po.accounted_filled_usd
                if unfilled_usd > 0:
                    if TIERED_MODE and po.tier is not None:
                        release_tiered_exposure(po.tier, unfilled_usd)
                    else:
                        adjust_placement(trade, -unfilled_usd)

                status_str = "cancelled" if cancelled else "cancel attempt"
                logger.info(
                    f"[recovery] Order {po.order_id[:12]}... {fill.status} "
                    f"({status_str}), released ${unfilled_usd:.2f} exposure"
                )
                remove_pending_order(po.order_id)

            # Mark trade as seen after recovery
            mark_trade_as_seen(trade.id)

        except Exception as exc:
            logger.error(
                f"[recovery] Error recovering order {po.order_id[:12]}...: "
                f"{error_message(exc)}"
            )

    logger.info("[recovery] Pending order recovery complete")
