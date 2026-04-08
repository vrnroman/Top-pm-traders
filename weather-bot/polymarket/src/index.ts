import fs from "fs";
import path from "path";
import { CONFIG, getPrivateKey } from "./config";
import { logger } from "./logger";
import { createClobClient } from "./create-clob-client";
import { placeTradeOrders, processVerifications, recoverPendingOrders } from "./trade-executor";
import { getRiskStatus } from "./risk-manager";
import { getUsdcBalance } from "./get-balance";
import { checkAndSetApprovals } from "./check-approvals";
import { syncInventoryFromApi, getInventorySummary } from "./inventory";
import { checkAndRedeemPositions } from "./auto-redeemer";
import { telegram } from "./telegram-notifier";
import { errorMessage } from "./types";
import { sleep } from "./utils";
import { startTelegramCommands, stopTelegramCommands } from "./telegram-commands";
import { getAvgReactionLatency } from "./trade-store";
import { drainTrades, peekPendingOrders } from "./trade-queue";
import { EXECUTION_LOOP_MS, FILL_CHECK_DELAY_MS } from "./constants";
import { createSources } from "./trade-source";
import { TIERED_MODE, TIER_1A, TIER_1B, TIER_1C } from "./strategy-config";
import { getTieredRiskStatus } from "./tiered-risk-manager";

const LOCKFILE = path.resolve(process.cwd(), "data", "bot.lock");

function acquireLock(): void {
  try {
    fs.mkdirSync(LOCKFILE);
  } catch (err: unknown) {
    if ((err as NodeJS.ErrnoException).code === "EEXIST") {
      const pidFile = path.join(LOCKFILE, "pid");
      try {
        const pid = parseInt(fs.readFileSync(pidFile, "utf8").trim(), 10);
        if (pid !== process.pid) {
          process.kill(pid, 0);
          logger.error(`Another instance is running (PID ${pid}).`);
          process.exit(1);
        }
      } catch { /* stale lock */ }
      logger.warn("Stale lock found. Reclaiming.");
    } else {
      throw err;
    }
  }
  fs.writeFileSync(path.join(LOCKFILE, "pid"), String(process.pid));
}

function releaseLock(): void {
  try { fs.rmSync(LOCKFILE, { recursive: true, force: true }); } catch { /* ignore */ }
}

// --- Shared state between workers (single-threaded, no races) ---

let dailyTradeCount = 0;
let dailySummaryDate = new Date().toISOString().slice(0, 10);

// --- Fail-fast worker supervision ---

async function supervised(name: string, fn: () => Promise<void>): Promise<string> {
  try {
    await fn();
    return `${name}: exited normally (unexpected for infinite loop)`;
  } catch (err) {
    return `${name}: crashed — ${errorMessage(err)}`;
  }
}

// --- Workers ---

async function detectionLoop(): Promise<void> {
  const sources = createSources(CONFIG.tradeMonitorMode);
  logger.info(`Monitor mode: ${CONFIG.tradeMonitorMode} (${sources.map(s => s.name).join(", ")})`);

  // Fail-fast: if any source dies, detection worker dies → supervised() catches → bot shuts down.
  // A partially monitoring bot is dangerous — better to stop and restart cleanly.
  await Promise.race(sources.map(s => s.start()));
}

async function executionLoop(clobClient: ReturnType<typeof createClobClient> extends Promise<infer T> ? T : never): Promise<void> {
  while (!shuttingDown) {
    const queued = drainTrades();
    if (queued.length > 0) {
      const placed = await placeTradeOrders(queued, clobClient);
      dailyTradeCount += placed;
    }
    await sleep(EXECUTION_LOOP_MS);
  }
}

async function verificationLoop(clobClient: ReturnType<typeof createClobClient> extends Promise<infer T> ? T : never): Promise<void> {
  while (!shuttingDown) {
    const pending = peekPendingOrders();
    if (pending.length > 0) {
      await processVerifications(pending, clobClient);
    }
    await sleep(FILL_CHECK_DELAY_MS);
  }
}

async function periodicLoop(): Promise<void> {
  // Periodic position redemption (resolved markets)
  const REDEEM_INTERVAL_MS = CONFIG.redeemIntervalHours * 60 * 60 * 1000;
  let lastRedeemTime = 0;

  // Periodic inventory reconciliation
  const RECONCILE_INTERVAL_MS = 5 * 60 * 1000;
  let lastReconcileTime = Date.now();

  // Heartbeat
  const HEARTBEAT_INTERVAL_MS = 5 * 60 * 1000;
  let lastHeartbeatTime = Date.now();

  while (!shuttingDown) {
    try {
      // Daily summary check — uses shared dailyTradeCount from execution worker
      const today = new Date().toISOString().slice(0, 10);
      if (today !== dailySummaryDate) {
        const balance = await getUsdcBalance();
        telegram.dailySummary(dailyTradeCount, getRiskStatus(), balance >= 0 ? balance : 0);
        dailyTradeCount = 0;
        dailySummaryDate = today;
      }

      // Periodic redemption
      if (!CONFIG.previewMode && Date.now() - lastRedeemTime >= REDEEM_INTERVAL_MS) {
        lastRedeemTime = Date.now();
        try {
          const result = await checkAndRedeemPositions(getPrivateKey());
          if (result.count > 0) {
            logger.info(`Redeemed ${result.count} resolved position(s)`);
            telegram.positionsRedeemed(result.count, result.details);
          }
        } catch (err: unknown) {
          logger.warn(`Auto-redeem failed: ${errorMessage(err)}`);
        }
      }

      // Periodic inventory reconciliation
      if (Date.now() - lastReconcileTime >= RECONCILE_INTERVAL_MS) {
        try {
          await syncInventoryFromApi();
          lastReconcileTime = Date.now();
          logger.debug("Periodic inventory reconciliation complete");
        } catch (err: unknown) {
          logger.warn(`Periodic reconciliation failed: ${errorMessage(err)}`);
        }
      }

      // Heartbeat
      if (Date.now() - lastHeartbeatTime >= HEARTBEAT_INTERVAL_MS) {
        const avgLatency = getAvgReactionLatency();
        const latencyStr = avgLatency > 0 ? ` | avg reaction: ${avgLatency}ms` : "";
        const tieredStr = TIERED_MODE ? ` | ${getTieredRiskStatus()}` : "";
        logger.info(`Heartbeat: ${getRiskStatus()} | ${getInventorySummary()}${latencyStr}${tieredStr}`);
        lastHeartbeatTime = Date.now();
      }
    } catch (err: unknown) {
      logger.error(`Periodic job error: ${errorMessage(err)}`);
    }

    await sleep(60_000);
  }
}

// --- Main ---

async function main(): Promise<void> {
  acquireLock();
  logger.info("=== Polymarket Copy Trading Bot ===");
  logger.info(`Mode: ${CONFIG.previewMode ? "PREVIEW (no real trades)" : "LIVE"}`);
  logger.info(`Strategy: ${CONFIG.copyStrategy} (${CONFIG.copySize})`);
  logger.info(`Tracking ${CONFIG.userAddresses.length} wallet(s):`);
  for (const addr of CONFIG.userAddresses) {
    logger.info(`  - ${addr}`);
  }
  logger.info(`Poll interval: ${CONFIG.fetchInterval / 1000}s`);
  logger.info(`Limits: min $${CONFIG.minOrderSizeUsd}, max $${CONFIG.maxOrderSizeUsd}`);
  logger.info(`Risk: ${getRiskStatus()}`);

  if (TIERED_MODE) {
    logger.info("=== Tiered Strategy Mode ===");
    if (TIER_1A.enabled) logger.info(`  1a (Geopolitical Insiders): ${TIER_1A.wallets.length} wallets, ${TIER_1A.copyPercentage}% copy, max $${TIER_1A.maxBet}/bet, $${TIER_1A.maxTotalExposure} exposure`);
    if (TIER_1B.enabled) logger.info(`  1b (Whale/Leaderboard):     ${TIER_1B.wallets.length} wallets, ${TIER_1B.copyPercentage}% copy, max $${TIER_1B.maxBet}/bet, $${TIER_1B.maxTotalExposure} exposure`);
    if (TIER_1C.enabled) logger.info(`  1c (Pattern Detection):     ${TIER_1C.alertOnly ? "ALERT ONLY" : "AUTO-FOLLOW"}, max $${TIER_1C.maxBet}/bet`);
  }

  if (!CONFIG.previewMode) {
    const balance = await getUsdcBalance();
    logger.info(`USDC balance: $${balance >= 0 ? balance.toFixed(2) : "unknown"}`);
    logger.info("Checking token approvals...");
    await checkAndSetApprovals(getPrivateKey());
  }

  await syncInventoryFromApi();
  logger.info(`Inventory: ${getInventorySummary()}`);

  const clobClient = await createClobClient();

  // Crash recovery: verify pending orders from previous session before detection starts
  await recoverPendingOrders(clobClient);

  logger.info("Bot started. Monitoring trades...\n");

  startTelegramCommands(clobClient);

  // Fire-and-forget Telegram notification
  getUsdcBalance()
    .then(bal => telegram.botStarted(CONFIG.userAddresses.length, bal >= 0 ? bal : 0))
    .catch(() => {});

  // Fail-fast: if any worker dies, shut down immediately
  const deadWorker = await Promise.race([
    supervised("detection", () => detectionLoop()),
    supervised("execution", () => executionLoop(clobClient)),
    supervised("verification", () => verificationLoop(clobClient)),
    supervised("periodic", () => periodicLoop()),
  ]);
  logger.error(`Worker died: ${deadWorker}`);
  await telegram.botError(`Worker died: ${deadWorker}`);
  await shutdown(1);
}

// --- Graceful shutdown ---

let shuttingDown = false;
async function shutdown(code: number): Promise<void> {
  if (shuttingDown) return;
  shuttingDown = true;
  logger.info("Shutting down...");
  stopTelegramCommands();
  releaseLock();
  await logger.flush();
  process.exit(code);
}

process.on("SIGINT", () => shutdown(0));
process.on("SIGTERM", () => shutdown(0));

process.on("unhandledRejection", (err) => {
  logger.error(`Unhandled rejection: ${errorMessage(err)}`);
  shutdown(1);
});

main().catch(async (err: unknown) => {
  const msg = errorMessage(err);
  logger.error(`Fatal error: ${msg}`);
  await telegram.botError(msg);
  releaseLock();
  process.exit(1);
});
