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

from py_clob_client.client import ClobClient

from src.config import CONFIG
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

        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 1.0

        if best_bid <= 0 or best_ask <= 0:
            return None

        midpoint = (best_bid + best_ask) / 2
        spread = best_ask - best_bid
        spread_bps = int((spread / midpoint) * 10000) if midpoint > 0 else 9999

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "midpoint": midpoint,
            "spread": spread,
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
        if trade.side == "BUY":
            order_price = snapshot["best_ask"] if snapshot else trade.price
        else:
            order_price = snapshot["best_bid"] if snapshot else trade.price

        # Round price to 2 decimal places
        order_price = round(order_price, 2)
        if order_price <= 0 or order_price >= 1:
            logger.warn(f"[exec] Invalid order price {order_price} for {trade.market}")
            return None

        # Calculate shares from USD size
        shares = copy_size / order_price if order_price > 0 else 0
        if shares <= 0:
            return None

        from py_clob_client.order_builder.constants import BUY, SELL
        side = BUY if trade.side == "BUY" else SELL

        order_args = {
            "token_id": trade.token_id,
            "price": order_price,
            "size": round(shares, 2),
            "side": side,
        }

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
    """Attempt to cancel an order. Returns True if successful."""
    try:
        clob_client.cancel(order_id)
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

            # --- Tier routing ---
            tier: Optional[str] = None
            alert_only = False
            copy_size = 0.0

            if TIERED_MODE:
                tier = get_wallet_tier(trade.trader_address)
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
                            f"[exec] Tier {tier} skip: {decision.reason} — "
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
                        f"[exec] Skip: {decision.reason} — "
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
                    logger.skip(f"[exec] SELL skipped — no position after sync for {trade.token_id[:12]}...")
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
            if CONFIG.preview_mode:
                logger.trade(
                    f"[PREVIEW] {trade.side} ${copy_size:.2f} on '{trade.market[:40]}' "
                    f"@ {trade.price:.4f} (from {short_address(trade.trader_address)})"
                )

                # Record in inventory for preview tracking
                if trade.side == "BUY":
                    shares = copy_size / trade.price if trade.price > 0 else 0
                    record_buy(trade.token_id, shares, trade.price, market_key, trade.market)
                elif trade.side == "SELL":
                    shares = copy_size / trade.price if trade.price > 0 else 0
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

            # --- Live order placement ---
            order_submitted_at = time.time() * 1000
            result = await _execute_copy_order(clob_client, trade, copy_size, snapshot)

            if result is None:
                logger.error(f"[exec] Order placement returned None for '{trade.market[:40]}'")
                await tg.trade_failed(trade.market, "Order placement returned no result")
                increment_retry(trade.id)
                continue

            logger.trade(
                f"[LIVE] {trade.side} ${copy_size:.2f} on '{trade.market[:40]}' "
                f"@ {result.order_price:.4f} — order {result.order_id[:12]}..."
            )

            # Critical operation order: record → enqueue → mark seen
            # 1. Record placement in risk accounting
            if TIERED_MODE and tier is not None:
                record_tiered_placement(tier, copy_size)
            else:
                record_placement_fn(trade, copy_size)

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

        except Exception as exc:
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
                    logger.info(f"[verify] UNFILLED — cancelled order {po.order_id[:12]}...")

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

    pending = load_pending_orders_from_disk()
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
