import { ClobClient } from "@polymarket/clob-client";
import { telegram } from "./telegram-notifier";
import { CONFIG } from "./config";
import { logger } from "./logger";
import { evaluateTrade, recordPlacement, adjustPlacement } from "./risk-manager";
import { getUsdcBalance } from "./get-balance";
import { hasPosition, recordBuy, recordSell, syncInventoryFromApi } from "./inventory";
import {
  isSeenTrade,
  markTradeAsSeen,
  appendTradeHistory,
  isMaxRetries,
  incrementRetry,
  getCopyCount,
  incrementCopyCount,
} from "./trade-store";
import { errorMessage } from "./types";
import { shortAddress } from "./utils";
import { executeCopyOrder } from "./order-executor";
import { verifyOrderFill } from "./order-verifier";
import { fetchMarketSnapshot, computeDriftBps, MarketSnapshot } from "./market-price";
import {
  QueuedTrade,
  PendingOrder,
  enqueuePendingOrder,
  removePendingOrder,
  loadPendingOrdersFromDisk,
  clearPendingOrdersOnDisk,
  replacePendingOrders,
  updatePendingOrder,
} from "./trade-queue";
import { TIERED_MODE, getWalletTier, getTierConfig, StrategyTier } from "./strategy-config";
import { evaluateTieredTrade, recordTieredPlacement, releaseTieredExposure, TieredCopyDecision } from "./tiered-risk-manager";
import { analyzeTradeForPatterns } from "./pattern-detector";

/** Release tiered exposure when an order is resolved (filled/cancelled). */
function releaseTierExposure(order: PendingOrder): void {
  if (order.tier) {
    releaseTieredExposure(order.tier as StrategyTier, order.copySize);
  }
}

// --- Execution worker: evaluate risk + place orders (hot path) ---

/** Place orders for queued trades. Does NOT verify fills — that's the verification worker's job. Returns count of orders placed. */
export async function placeTradeOrders(
  queued: QueuedTrade[],
  clobClient: ClobClient
): Promise<number> {
  // Sort by source detection time — oldest first
  const sorted = queued.sort(
    (a, b) => a.sourceDetectedAt - b.sourceDetectedAt
  );
  let placedCount = 0;
  let usdcBalance = CONFIG.previewMode ? -1 : await getUsdcBalance();

  for (const { trade, sourceDetectedAt, enqueuedAt, source } of sorted) {
    // Dedupe check — trade may have been enqueued twice between detection and execution
    if (isSeenTrade(trade.id) || isMaxRetries(trade.id)) continue;

    // Pattern detection (Strategy 1c) — runs for ALL trades regardless of tier
    analyzeTradeForPatterns(trade);

    const addr = shortAddress(trade.traderAddress);
    const marketKey = trade.conditionId || trade.tokenId;
    if (!trade.conditionId) {
      logger.debug(`Trade ${trade.id} missing conditionId — per-market cap uses tokenId fallback`);
    }

    // Route to tiered or legacy risk evaluation
    let decision: { shouldCopy: boolean; copySize: number; reason?: string };
    let tradeTier: StrategyTier | null = null;
    let tieredDecision: TieredCopyDecision | null = null;

    if (TIERED_MODE) {
      tradeTier = getWalletTier(trade.traderAddress);
    }

    if (tradeTier) {
      const tierConfig = getTierConfig(tradeTier);
      tieredDecision = evaluateTieredTrade(tierConfig, trade.size, trade.price, trade.timestamp);
      decision = tieredDecision;

      // Handle alert-only mode (1c pattern-detected wallets auto-followed with small size)
      if (tieredDecision.alertOnly) {
        markTradeAsSeen(trade.id);
        logger.info(`[${tradeTier}] ALERT: ${addr} ${trade.side} $${trade.size} on "${trade.market}" — ${tieredDecision.reason}`);
        appendTradeHistory({
          timestamp: new Date().toISOString(),
          traderAddress: trade.traderAddress,
          market: trade.market,
          side: trade.side,
          traderSize: trade.size,
          copySize: tieredDecision.copySize,
          price: trade.price,
          status: "skipped",
          reason: `[${tradeTier}] ${tieredDecision.reason}`,
          sourceDetectedAt,
          enqueuedAt,
        });
        continue;
      }
    } else {
      // Legacy evaluation path
      decision = evaluateTrade(
        trade.size,
        trade.price,
        trade.timestamp,
        marketKey,
        usdcBalance,
        trade.side
      );
    }

    if (!decision.shouldCopy) {
      markTradeAsSeen(trade.id);
      const tierLabel = tradeTier ? `[${tradeTier}] ` : "";
      logger.skip(`${tierLabel}${addr} ${trade.side} $${trade.size} on "${trade.market}" — ${decision.reason}`);
      appendTradeHistory({
        timestamp: new Date().toISOString(),
        traderAddress: trade.traderAddress,
        market: trade.market,
        side: trade.side,
        traderSize: trade.size,
        copySize: decision.copySize,
        price: trade.price,
        status: "skipped",
        reason: decision.reason,
        sourceDetectedAt,
        enqueuedAt,
      });
      continue;
    }

    // Max Duplicate Bets Check
    const copyCount = getCopyCount(trade.traderAddress, trade.market, trade.side);
    if (copyCount >= CONFIG.maxCopiesPerMarketSide) {
      markTradeAsSeen(trade.id);
      const reason = `Max ${CONFIG.maxCopiesPerMarketSide} copies reached for this trader on this outcome`;
      logger.skip(`${addr} ${trade.side} on "${trade.market}" — ${reason}`);
      appendTradeHistory({
        timestamp: new Date().toISOString(),
        traderAddress: trade.traderAddress,
        market: trade.market,
        side: trade.side,
        traderSize: trade.size,
        copySize: decision.copySize,
        price: trade.price,
        status: "skipped",
        reason,
        sourceDetectedAt,
        enqueuedAt,
      });
      continue;
    }

    // Market quality check — fetch live price, compute drift/spread
    let snapshot: MarketSnapshot | null = null;
    let driftBps: number | undefined;
    let spreadBps: number | undefined;
    if (trade.tokenId) {
      snapshot = await fetchMarketSnapshot(clobClient, trade.tokenId);
      if (snapshot) {
        driftBps = computeDriftBps(trade.price, snapshot, trade.side);
        spreadBps = snapshot.spreadBps;

        if (driftBps >= CONFIG.maxPriceDriftBps) {
          markTradeAsSeen(trade.id);
          const reason = `Price drifted ${driftBps}bps (max ${CONFIG.maxPriceDriftBps})`;
          logger.skip(`${addr} ${trade.side} on "${trade.market}" — ${reason}`);
          appendTradeHistory({
            timestamp: new Date().toISOString(),
            traderAddress: trade.traderAddress,
            market: trade.market,
            side: trade.side,
            traderSize: trade.size,
            copySize: decision.copySize,
            price: trade.price,
            status: "skipped",
            reason,
            sourceDetectedAt,
            enqueuedAt,
            driftBps,
            spreadBps,
          });
          continue;
        }

        if (spreadBps >= CONFIG.maxSpreadBps) {
          markTradeAsSeen(trade.id);
          const reason = `Spread ${spreadBps}bps too wide (max ${CONFIG.maxSpreadBps})`;
          logger.skip(`${addr} ${trade.side} on "${trade.market}" — ${reason}`);
          appendTradeHistory({
            timestamp: new Date().toISOString(),
            traderAddress: trade.traderAddress,
            market: trade.market,
            side: trade.side,
            traderSize: trade.size,
            copySize: decision.copySize,
            price: trade.price,
            status: "skipped",
            reason,
            sourceDetectedAt,
            enqueuedAt,
            driftBps,
            spreadBps,
          });
          continue;
        }
      } else {
        logger.debug(`${addr} no market snapshot for "${trade.market}" — using fixed price buffer`);
      }
    }

    if (CONFIG.previewMode) {
      markTradeAsSeen(trade.id);
      incrementCopyCount(trade.traderAddress, trade.market, trade.side);
      if (tradeTier) {
        recordTieredPlacement(tradeTier, decision.copySize);
      }

      const shares = decision.copySize / trade.price;
      if (trade.side === "BUY") {
        recordBuy(trade.tokenId, shares, trade.price, marketKey, trade.market);
      } else {
        recordSell(trade.tokenId, shares);
      }

      const tierLabel = tradeTier ? `[${tradeTier}] ` : "";
      logger.trade(
        `[PREVIEW] ${tierLabel}Would copy ${addr}: ${trade.side} $${decision.copySize} on "${trade.market}" @ ${trade.price}`
      );
      telegram.tradePlaced(trade.market, trade.side, decision.copySize, trade.price);
      appendTradeHistory({
        timestamp: new Date().toISOString(),
        traderAddress: trade.traderAddress,
        market: trade.market,
        side: trade.side,
        traderSize: trade.size,
        copySize: decision.copySize,
        price: trade.price,
        status: "preview",
        sourceDetectedAt,
        enqueuedAt,
        driftBps,
        spreadBps,
        conditionId: trade.conditionId,
        tokenId: trade.tokenId,
        outcome: trade.outcome,
      });
      continue;
    }

    // SELL: check local inventory, force sync if missing
    if (trade.side === "SELL" && !hasPosition(trade.tokenId)) {
      try { await syncInventoryFromApi(); } catch { /* logged internally */ }
      if (!hasPosition(trade.tokenId)) {
        markTradeAsSeen(trade.id);
        logger.skip(`${addr} SELL on "${trade.market}" — no position (confirmed after sync)`);
        appendTradeHistory({
          timestamp: new Date().toISOString(),
          traderAddress: trade.traderAddress,
          market: trade.market,
          side: trade.side,
          traderSize: trade.size,
          copySize: 0,
          price: trade.price,
          status: "skipped",
          reason: "No position to sell",
          sourceDetectedAt,
          enqueuedAt,
        });
        continue;
      }
    }

    try {
      const orderResult = await executeCopyOrder(clobClient, trade, decision.copySize, snapshot);

      if (!orderResult.orderId || orderResult.orderId === "undefined") {
        const noIdAttempt = incrementRetry(trade.id);
        if (isMaxRetries(trade.id)) {
          markTradeAsSeen(trade.id);
          telegram.tradeFailed(trade.market, "No orderId from CLOB");
        }
        logger.warn(`Order rejected by CLOB (no orderId) on "${trade.market}" — attempt ${noIdAttempt}/3`);
        appendTradeHistory({
          timestamp: new Date().toISOString(),
          traderAddress: trade.traderAddress,
          market: trade.market,
          side: trade.side,
          traderSize: trade.size,
          copySize: decision.copySize,
          price: orderResult.orderPrice,
          status: "failed",
          reason: "No orderId from CLOB",
          traderPrice: trade.price,
          sourceDetectedAt,
          enqueuedAt,
          driftBps,
          spreadBps,
        });
        continue;
      }

      const orderSubmittedAt = Date.now();

      // Critical operation order: recordPlacement → persist pending → markTradeAsSeen
      // 1. Optimistic risk accounting
      recordPlacement(marketKey, decision.copySize, trade.side);
      if (tradeTier) {
        recordTieredPlacement(tradeTier, decision.copySize);
      }
      // 2. Persist pending order to disk (crash recovery) — carry timing for verification records
      enqueuePendingOrder({
        trade,
        orderId: orderResult.orderId,
        orderPrice: orderResult.orderPrice,
        copySize: decision.copySize,
        placedAt: orderSubmittedAt,
        marketKey,
        side: trade.side,
        sourceDetectedAt,
        enqueuedAt,
        orderSubmittedAt,
        source,
        tier: tradeTier ?? undefined,
      });
      // 3. Mark as seen (dedup) & increment trader copy count limits
      markTradeAsSeen(trade.id);
      incrementCopyCount(trade.traderAddress, trade.market, trade.side);

      if (trade.side === "BUY" && usdcBalance >= 0) {
        usdcBalance -= decision.copySize;
      }
      placedCount++;

      appendTradeHistory({
        timestamp: new Date().toISOString(),
        traderAddress: trade.traderAddress,
        market: trade.market,
        side: trade.side,
        traderSize: trade.size,
        copySize: decision.copySize,
        price: orderResult.orderPrice,
        status: "placed",
        orderId: orderResult.orderId,
        traderPrice: trade.price,
        sourceDetectedAt,
        enqueuedAt,
        orderSubmittedAt,
        source,
        driftBps,
        spreadBps,
      });
    } catch (err: unknown) {
      const msg = errorMessage(err);
      const attempt = incrementRetry(trade.id);
      const maxedOut = isMaxRetries(trade.id);
      if (maxedOut) {
        markTradeAsSeen(trade.id);
        telegram.tradeFailed(trade.market, msg);
      }
      logger.error(
        `Failed to execute on "${trade.market}": ${msg}. Attempt ${attempt}/3${maxedOut ? " — giving up" : " — will retry"}`
      );
      appendTradeHistory({
        timestamp: new Date().toISOString(),
        traderAddress: trade.traderAddress,
        market: trade.market,
        side: trade.side,
        traderSize: trade.size,
        copySize: decision.copySize,
        price: trade.price,
        status: "failed",
        reason: msg,
        sourceDetectedAt,
        enqueuedAt,
        driftBps,
        spreadBps,
      });
    }
  }
  return placedCount;
}

// --- Verification worker: check fills asynchronously ---

const MAX_PENDING_UNCERTAIN_CYCLES = 5;

async function cancelOrderIfPossible(clobClient: ClobClient, orderId: string): Promise<boolean> {
  try {
    await clobClient.cancelOrder({ orderID: orderId });
    return true;
  } catch {
    return false;
  }
}

function applyUnaccountedFill(order: PendingOrder, filledShares: number, filledUsd: number, fillPrice: number): void {
  const accountedShares = order.accountedFilledShares || 0;
  const accountedUsd = order.accountedFilledUsd || 0;
  const deltaShares = Math.max(0, filledShares - accountedShares);
  const deltaUsd = Math.max(0, filledUsd - accountedUsd);

  if (deltaShares <= 0 || deltaUsd <= 0) {
    order.accountedFilledShares = Math.max(accountedShares, filledShares);
    order.accountedFilledUsd = Math.max(accountedUsd, filledUsd);
    return;
  }

  if (order.side === "BUY") {
    recordBuy(order.trade.tokenId, deltaShares, fillPrice, order.marketKey, order.trade.market);
  } else {
    recordSell(order.trade.tokenId, deltaShares);
  }

  order.accountedFilledShares = filledShares;
  order.accountedFilledUsd = filledUsd;
}

function persistPendingState(order: PendingOrder): void {
  updatePendingOrder(order.orderId, {
    accountedFilledShares: order.accountedFilledShares || 0,
    accountedFilledUsd: order.accountedFilledUsd || 0,
    uncertainCycles: order.uncertainCycles || 0,
  });
}

function abandonPendingOrder(order: PendingOrder, reason: string): void {
  const details = `${reason}. Manual review required; optimistic risk accounting kept.`;
  logger.error(`Pending order ${order.orderId} on "${order.trade.market}" abandoned after ${order.uncertainCycles}/${MAX_PENDING_UNCERTAIN_CYCLES} uncertain cycles — ${details}`);
  telegram.tradeFailed(order.trade.market, details);
  appendTradeHistory({
    timestamp: new Date().toISOString(),
    traderAddress: order.trade.traderAddress,
    market: order.trade.market,
    side: order.side,
    traderSize: order.trade.size,
    copySize: order.copySize,
    price: order.orderPrice,
    status: "failed",
    orderId: order.orderId,
    traderPrice: order.trade.price,
    reason: details,
    sourceDetectedAt: order.sourceDetectedAt,
    enqueuedAt: order.enqueuedAt,
    orderSubmittedAt: order.orderSubmittedAt,
    source: order.source,
  });
}

function incrementUncertainCycle(order: PendingOrder): boolean {
  order.uncertainCycles = (order.uncertainCycles || 0) + 1;
  return (order.uncertainCycles || 0) >= MAX_PENDING_UNCERTAIN_CYCLES;
}

/** Process pending orders — verify fills, update inventory/risk, handle cancellations.
 *  Receives a snapshot from peekPendingOrders(). After each processed order, removePendingOrder()
 *  removes it from the shared in-memory array and rewrites disk — safe against concurrent
 *  enqueuePendingOrder() from execution worker. */
export async function processVerifications(
  pending: PendingOrder[],
  clobClient: ClobClient
): Promise<void> {
  for (const order of pending) {
    let shouldRemove = false;
    try {
      const fillResult = await verifyOrderFill(clobClient, order.orderId);
      const firstFillSeenAt = Date.now();

      if (fillResult.status === "FILLED") {
        applyUnaccountedFill(order, fillResult.filledShares, fillResult.filledUsd, fillResult.fillPrice);
        adjustPlacement(order.marketKey, order.copySize, fillResult.filledUsd, order.side);
        logger.trade(`Order FILLED: ${fillResult.filledShares} shares @ ${fillResult.fillPrice} on "${order.trade.market}"`);
        telegram.tradeFilled(order.trade.market, fillResult.filledShares, fillResult.fillPrice);
        appendTradeHistory({
          timestamp: new Date().toISOString(),
          traderAddress: order.trade.traderAddress,
          market: order.trade.market,
          side: order.side,
          traderSize: order.trade.size,
          copySize: order.copySize,
          price: order.orderPrice,
          status: "filled",
          orderId: order.orderId,
          fillPrice: fillResult.fillPrice,
          fillShares: fillResult.filledShares,
          traderPrice: order.trade.price,
          sourceDetectedAt: order.sourceDetectedAt,
          enqueuedAt: order.enqueuedAt,
          orderSubmittedAt: order.orderSubmittedAt,
          firstFillSeenAt,
          source: order.source,
        });
        shouldRemove = true;
      } else if (fillResult.status === "PARTIAL") {
        applyUnaccountedFill(order, fillResult.filledShares, fillResult.filledUsd, fillResult.fillPrice);
        const cancelSucceeded = await cancelOrderIfPossible(clobClient, order.orderId);
        if (!cancelSucceeded) {
          const shouldAbandon = incrementUncertainCycle(order);
          if (shouldAbandon) {
            abandonPendingOrder(order, "Partial fill observed but cancel kept failing");
            removePendingOrder(order.orderId);
          } else {
            persistPendingState(order);
            logger.warn(`PARTIAL fill on "${order.trade.market}" — cancel failed, keeping pending order (${order.uncertainCycles}/${MAX_PENDING_UNCERTAIN_CYCLES})`);
          }
          continue;
        }
        adjustPlacement(order.marketKey, order.copySize, fillResult.filledUsd, order.side);
        logger.trade(`PARTIAL fill on "${order.trade.market}" — cancelled remainder`);
        logger.trade(`Order PARTIAL: ${fillResult.filledShares} shares @ ${fillResult.fillPrice} on "${order.trade.market}"`);
        telegram.tradeFilled(order.trade.market, fillResult.filledShares, fillResult.fillPrice);
        appendTradeHistory({
          timestamp: new Date().toISOString(),
          traderAddress: order.trade.traderAddress,
          market: order.trade.market,
          side: order.side,
          traderSize: order.trade.size,
          copySize: order.copySize,
          price: order.orderPrice,
          status: "partial",
          orderId: order.orderId,
          fillPrice: fillResult.fillPrice,
          fillShares: fillResult.filledShares,
          traderPrice: order.trade.price,
          sourceDetectedAt: order.sourceDetectedAt,
          enqueuedAt: order.enqueuedAt,
          orderSubmittedAt: order.orderSubmittedAt,
          firstFillSeenAt,
          source: order.source,
        });
        shouldRemove = true;
      } else if (fillResult.status === "UNKNOWN") {
        const cancelSucceeded = await cancelOrderIfPossible(clobClient, order.orderId);
        if (!cancelSucceeded) {
          const shouldAbandon = incrementUncertainCycle(order);
          if (shouldAbandon) {
            abandonPendingOrder(order, "Order status stayed UNKNOWN and cancel kept failing");
            removePendingOrder(order.orderId);
          } else {
            persistPendingState(order);
            logger.warn(`Order UNKNOWN on "${order.trade.market}" — cancel failed, keeping pending order (${order.uncertainCycles}/${MAX_PENDING_UNCERTAIN_CYCLES})`);
          }
          continue;
        }
        adjustPlacement(order.marketKey, order.copySize, 0, order.side);
        logger.warn(`Order UNKNOWN on "${order.trade.market}" — cancelled, risk reversed`);
        appendTradeHistory({
          timestamp: new Date().toISOString(),
          traderAddress: order.trade.traderAddress,
          market: order.trade.market,
          side: order.side,
          traderSize: order.trade.size,
          copySize: order.copySize,
          price: order.orderPrice,
          status: "unknown",
          orderId: order.orderId,
          traderPrice: order.trade.price,
          reason: "UNKNOWN — cancelled order, risk reversed",
          sourceDetectedAt: order.sourceDetectedAt,
          enqueuedAt: order.enqueuedAt,
          orderSubmittedAt: order.orderSubmittedAt,
          source: order.source,
        });
        shouldRemove = true;
      } else {
        const cancelSucceeded = await cancelOrderIfPossible(clobClient, order.orderId);
        if (!cancelSucceeded) {
          const shouldAbandon = incrementUncertainCycle(order);
          if (shouldAbandon) {
            abandonPendingOrder(order, "Order stayed UNFILLED and cancel kept failing");
            removePendingOrder(order.orderId);
          } else {
            persistPendingState(order);
            logger.warn(`Order UNFILLED on "${order.trade.market}" — cancel failed, keeping pending order (${order.uncertainCycles}/${MAX_PENDING_UNCERTAIN_CYCLES})`);
          }
          continue;
        }
        logger.warn(`Order UNFILLED on "${order.trade.market}" — cancelled to prevent stale fill`);
        adjustPlacement(order.marketKey, order.copySize, 0, order.side);
        telegram.tradeUnfilled(order.trade.market);
        appendTradeHistory({
          timestamp: new Date().toISOString(),
          traderAddress: order.trade.traderAddress,
          market: order.trade.market,
          side: order.side,
          traderSize: order.trade.size,
          copySize: order.copySize,
          price: order.orderPrice,
          status: "unfilled",
          orderId: order.orderId,
          traderPrice: order.trade.price,
          sourceDetectedAt: order.sourceDetectedAt,
          enqueuedAt: order.enqueuedAt,
          orderSubmittedAt: order.orderSubmittedAt,
          source: order.source,
        });
        shouldRemove = true;
      }
    } catch (err: unknown) {
      const cancelSucceeded = await cancelOrderIfPossible(clobClient, order.orderId);
      if (!cancelSucceeded) {
        const shouldAbandon = incrementUncertainCycle(order);
        if (shouldAbandon) {
          abandonPendingOrder(order, `Verification kept failing: ${errorMessage(err)}`);
          removePendingOrder(order.orderId);
        } else {
          persistPendingState(order);
          logger.error(`Verification error for order ${order.orderId} on "${order.trade.market}": ${errorMessage(err)} — cancel failed, keeping pending (${order.uncertainCycles}/${MAX_PENDING_UNCERTAIN_CYCLES})`);
        }
        continue;
      }
      adjustPlacement(order.marketKey, order.copySize, 0, order.side);
      logger.error(`Verification error for order ${order.orderId} on "${order.trade.market}": ${errorMessage(err)} — cancelled, risk reversed`);
      shouldRemove = true;
    }
    if (shouldRemove) {
      releaseTierExposure(order);
      // Remove from shared array + rewrite disk (safe against concurrent enqueuePendingOrder)
      removePendingOrder(order.orderId);
    }
  }
}

// --- Crash recovery: runs at startup before detection ---

/** Recover pending orders from a previous session. Verifies each via CLOB API, reconciles risk state. */
export async function recoverPendingOrders(clobClient: ClobClient): Promise<void> {
  const pending = loadPendingOrdersFromDisk();
  if (pending.length === 0) return;
  logger.info(`Recovering ${pending.length} pending order(s) from previous session`);
  const survivors: PendingOrder[] = [];

  for (const order of pending) {
    try {
      const fillResult = await verifyOrderFill(clobClient, order.orderId);

      if (fillResult.status === "FILLED") {
        applyUnaccountedFill(order, fillResult.filledShares, fillResult.filledUsd, fillResult.fillPrice);
        adjustPlacement(order.marketKey, order.copySize, fillResult.filledUsd, order.side);
        logger.info(`Recovery: order ${order.orderId} FILLED — ${fillResult.filledShares} shares on "${order.trade.market}"`);
      } else if (fillResult.status === "PARTIAL") {
        applyUnaccountedFill(order, fillResult.filledShares, fillResult.filledUsd, fillResult.fillPrice);
        const cancelSucceeded = await cancelOrderIfPossible(clobClient, order.orderId);
        if (!cancelSucceeded) {
          const shouldAbandon = incrementUncertainCycle(order);
          if (shouldAbandon) {
            abandonPendingOrder(order, "Recovery saw partial fill but cancel kept failing");
            markTradeAsSeen(order.trade.id);
          } else {
            logger.warn(`Recovery: order ${order.orderId} PARTIAL but cancel failed — keeping pending on "${order.trade.market}" (${order.uncertainCycles}/${MAX_PENDING_UNCERTAIN_CYCLES})`);
            survivors.push(order);
          }
          continue;
        }
        adjustPlacement(order.marketKey, order.copySize, fillResult.filledUsd, order.side);
        logger.info(`Recovery: order ${order.orderId} PARTIAL — cancelled remainder on "${order.trade.market}"`);
      } else {
        // UNFILLED or UNKNOWN — cancel + reverse full optimistic amount
        const cancelSucceeded = await cancelOrderIfPossible(clobClient, order.orderId);
        if (!cancelSucceeded) {
          const shouldAbandon = incrementUncertainCycle(order);
          if (shouldAbandon) {
            abandonPendingOrder(order, `Recovery saw ${fillResult.status} but cancel kept failing`);
            markTradeAsSeen(order.trade.id);
          } else {
            logger.warn(`Recovery: order ${order.orderId} ${fillResult.status} and cancel failed — keeping pending on "${order.trade.market}" (${order.uncertainCycles}/${MAX_PENDING_UNCERTAIN_CYCLES})`);
            survivors.push(order);
          }
          continue;
        }
        adjustPlacement(order.marketKey, order.copySize, 0, order.side);
        logger.info(`Recovery: order ${order.orderId} ${fillResult.status} — cancelled, risk reversed on "${order.trade.market}"`);
      }
    } catch {
      // Can't verify at all — cancel to be safe, reverse optimistic accounting
      const cancelSucceeded = await cancelOrderIfPossible(clobClient, order.orderId);
      if (!cancelSucceeded) {
        const shouldAbandon = incrementUncertainCycle(order);
        if (shouldAbandon) {
          abandonPendingOrder(order, "Recovery could not verify order and cancel kept failing");
          markTradeAsSeen(order.trade.id);
        } else {
          logger.warn(`Recovery: order ${order.orderId} unverifiable and cancel failed — keeping pending on "${order.trade.market}" (${order.uncertainCycles}/${MAX_PENDING_UNCERTAIN_CYCLES})`);
          survivors.push(order);
        }
        continue;
      }
      adjustPlacement(order.marketKey, order.copySize, 0, order.side);
      logger.warn(`Recovery: order ${order.orderId} unverifiable — cancelled, risk reversed on "${order.trade.market}"`);
    }
    // Close crash gap: mark trade as seen so detection doesn't re-process
    markTradeAsSeen(order.trade.id);
  }
  if (survivors.length === 0) {
    clearPendingOrdersOnDisk();
    return;
  }
  replacePendingOrders(survivors);
}
