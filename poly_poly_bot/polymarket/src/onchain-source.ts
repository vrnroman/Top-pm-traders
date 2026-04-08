// On-chain trade source — polls OrderFilled events from CTF Exchange contracts via eth_getLogs.
// Converts raw logs to DetectedTrade[] and pushes to trade queue.

import fs from "fs";
import path from "path";
import { createPublicClient, http, parseAbi, formatUnits } from "viem";
import { polygon } from "viem/chains";
import { TradeSource } from "./trade-source";
import { DetectedTrade } from "./trade-monitor";
import { CONFIG } from "./config";
import { logger } from "./logger";
import { errorMessage } from "./types";
import { sleep } from "./utils";
import { isSeenTrade, isMaxRetries } from "./trade-store";
import { enqueueTrade } from "./trade-queue";
import { getMarketMeta } from "./market-cache";
import { CTF_EXCHANGE } from "./constants";

const NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a" as const;

const ORDER_FILLED_ABI = parseAbi([
  "event OrderFilled(bytes32 orderHash, address indexed maker, address indexed taker, uint256 makerAssetId, uint256 takerAssetId, uint256 makerAmountFilled, uint256 takerAmountFilled)",
]);

// Poll interval for on-chain source — ~5s covers 2-3 Polygon blocks
const ONCHAIN_POLL_MS = 5000;
// How many blocks behind latest to start when no persisted cursor
const INITIAL_LOOKBACK_BLOCKS = 5;
// Max blocks per getLogs request — Alchemy Free caps at 10, use 9 for safety
const MAX_BLOCK_RANGE = 9;

// Persisted block cursor — survives restarts so onchain mode doesn't miss trades during downtime
const DATA_DIR = path.resolve(process.cwd(), "data");
const CURSOR_FILE = path.join(DATA_DIR, "onchain-cursor.json");

function loadCursor(): bigint {
  try {
    if (fs.existsSync(CURSOR_FILE)) {
      const data = JSON.parse(fs.readFileSync(CURSOR_FILE, "utf8"));
      if (data.lastBlock) return BigInt(data.lastBlock);
    }
  } catch { /* corrupted — start fresh */ }
  return 0n;
}

function saveCursor(lastBlock: bigint): void {
  if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
  fs.writeFileSync(CURSOR_FILE, JSON.stringify({ lastBlock: lastBlock.toString() }));
}

export class OnchainSource implements TradeSource {
  name = "onchain";
  private running = false;
  private client = createPublicClient({ chain: polygon, transport: http(CONFIG.rpcUrl) });

  async start(): Promise<void> {
    this.running = true;
    let lastBlock = loadCursor();
    let consecutiveFailures = 0;

    // Build tracked address set (lowercase for comparison)
    const tracked = new Set(CONFIG.userAddresses.map(a => a.toLowerCase()));

    while (this.running) {
      try {
        const latestBlock = await this.client.getBlockNumber();

        if (lastBlock === 0n) {
          lastBlock = latestBlock - BigInt(INITIAL_LOOKBACK_BLOCKS);
          logger.info(`On-chain source: no cursor — starting from block ${lastBlock} (latest: ${latestBlock})`);
        } else if (lastBlock < latestBlock - 10000n) {
          // Cursor too old (>~5h of blocks) — skip to recent to avoid massive getLogs
          const old = lastBlock;
          lastBlock = latestBlock - BigInt(INITIAL_LOOKBACK_BLOCKS);
          logger.warn(`On-chain source: cursor too old (block ${old}), skipping to ${lastBlock}`);
        }

        if (latestBlock <= lastBlock) {
          await sleep(ONCHAIN_POLL_MS);
          continue;
        }

        const fromBlock = lastBlock + 1n;
        const trades = await this.pollLogs(fromBlock, latestBlock, tracked);

        for (const trade of trades) {
          if (!isSeenTrade(trade.id) && !isMaxRetries(trade.id)) {
            enqueueTrade(trade, new Date(trade.timestamp).getTime(), "onchain");
          }
        }

        lastBlock = latestBlock;
        saveCursor(lastBlock);
        consecutiveFailures = 0;
      } catch (err: unknown) {
        consecutiveFailures++;
        logger.error(`On-chain source error (${consecutiveFailures}x): ${errorMessage(err)}`);
        // Exponential backoff, cap at 60s
        await sleep(Math.min(ONCHAIN_POLL_MS * Math.pow(2, Math.min(consecutiveFailures, 4)), 60_000));
        continue;
      }

      await sleep(ONCHAIN_POLL_MS);
    }
  }

  stop(): void {
    this.running = false;
  }

  private async pollLogs(
    fromBlock: bigint,
    toBlock: bigint,
    tracked: Set<string>
  ): Promise<DetectedTrade[]> {
    // Chunk getLogs requests to stay within RPC limits (Alchemy Free: 10 blocks max)
    // First chunk to infer the correct log type
    const firstEnd = fromBlock + BigInt(MAX_BLOCK_RANGE) - 1n > toBlock ? toBlock : fromBlock + BigInt(MAX_BLOCK_RANGE) - 1n;
    const logs = await this.client.getLogs({
      address: [CTF_EXCHANGE as `0x${string}`, NEG_RISK_CTF_EXCHANGE],
      event: ORDER_FILLED_ABI[0],
      fromBlock,
      toBlock: firstEnd,
    });
    // Remaining chunks
    for (let start = firstEnd + 1n; start <= toBlock; start += BigInt(MAX_BLOCK_RANGE)) {
      const end = start + BigInt(MAX_BLOCK_RANGE) - 1n > toBlock ? toBlock : start + BigInt(MAX_BLOCK_RANGE) - 1n;
      const chunk = await this.client.getLogs({
        address: [CTF_EXCHANGE as `0x${string}`, NEG_RISK_CTF_EXCHANGE],
        event: ORDER_FILLED_ABI[0],
        fromBlock: start,
        toBlock: end,
      });
      logs.push(...chunk);
    }

    if (logs.length === 0) return [];

    // Cache block timestamps to avoid repeated getBlock calls
    const blockTimestamps = new Map<bigint, number>();
    const trades: DetectedTrade[] = [];

    for (const log of logs) {
      const { maker, taker, makerAssetId, takerAssetId, makerAmountFilled, takerAmountFilled } = log.args!;
      if (!maker || !taker) continue;

      const makerLower = maker.toLowerCase();
      const takerLower = taker.toLowerCase();
      const isTrackedMaker = tracked.has(makerLower);
      const isTrackedTaker = tracked.has(takerLower);
      if (!isTrackedMaker && !isTrackedTaker) continue;

      const trackedAddress = isTrackedMaker ? maker : taker;

      // Determine side and tokenId
      // makerAssetId == 0 means maker pays USDC (buys tokens)
      // makerAssetId != 0 means maker pays tokens (sells tokens)
      let side: "BUY" | "SELL";
      let tokenId: string;
      let usdcAmount: bigint;
      let ctAmount: bigint;

      if (isTrackedMaker) {
        if (makerAssetId === 0n) {
          side = "BUY";
          tokenId = takerAssetId!.toString();
          usdcAmount = makerAmountFilled!;
          ctAmount = takerAmountFilled!;
        } else {
          side = "SELL";
          tokenId = makerAssetId!.toString();
          usdcAmount = takerAmountFilled!;
          ctAmount = makerAmountFilled!;
        }
      } else {
        // Tracked is taker
        if (takerAssetId === 0n) {
          side = "BUY";
          tokenId = makerAssetId!.toString();
          usdcAmount = takerAmountFilled!;
          ctAmount = makerAmountFilled!;
        } else {
          side = "SELL";
          tokenId = takerAssetId!.toString();
          usdcAmount = makerAmountFilled!;
          ctAmount = takerAmountFilled!;
        }
      }

      const size = parseFloat(formatUnits(usdcAmount, 6));
      const shares = parseFloat(formatUnits(ctAmount, 6));
      const price = shares > 0 ? size / shares : 0;

      if (size <= 0 || !tokenId || tokenId === "0") continue;

      // Get block timestamp (cached per block)
      let blockTs = blockTimestamps.get(log.blockNumber);
      if (blockTs === undefined) {
        try {
          const block = await this.client.getBlock({ blockNumber: log.blockNumber });
          blockTs = Number(block.timestamp);
          blockTimestamps.set(log.blockNumber, blockTs);
        } catch {
          blockTs = Math.floor(Date.now() / 1000);
        }
      }

      // Enrich with market metadata (non-blocking — trade still works without it)
      const meta = await getMarketMeta(tokenId);

      // Canonical trade key: txHash-tokenId-side — matches data-api-source for hybrid dedupe.
      // tokenId always available on-chain (no market cache dependency).
      const conditionId = meta?.conditionId || "";
      trades.push({
        id: `${log.transactionHash}-${tokenId}-${side}`,
        traderAddress: trackedAddress,
        timestamp: new Date(blockTs * 1000).toISOString(),
        market: meta?.market || `token:${tokenId.slice(0, 8)}...`,
        conditionId,
        tokenId,
        side,
        size,
        price: Math.round(price * 100) / 100,
        outcome: meta?.outcome || "",
      });
    }

    if (trades.length > 0) {
      logger.info(`On-chain: ${trades.length} trade(s) in blocks ${fromBlock}-${toBlock}`);
    }

    return trades;
  }
}
