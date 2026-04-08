import axios from "axios";
import { CONFIG } from "./config";
import { logger } from "./logger";
import { DataApiActivityItem, errorMessage } from "./types";

export interface DetectedTrade {
  id: string;
  traderAddress: string;
  timestamp: string;
  market: string;
  conditionId: string;
  tokenId: string;
  side: "BUY" | "SELL";
  size: number;
  price: number;
  outcome: string;
}

/** Reject trades with missing market identifiers or invalid sizes. */
function isValidTrade(trade: DetectedTrade): boolean {
  if (!trade.tokenId) {
    logger.debug(`Rejected trade ${trade.id}: no tokenId`);
    return false;
  }
  if (trade.size <= 0 || isNaN(trade.size)) {
    logger.debug(`Rejected trade ${trade.id}: invalid size ${trade.size}`);
    return false;
  }
  return true;
}

// --- Per-trader cursor (optimization hint, NOT correctness mechanism) ---

const CURSOR_OVERLAP_MS = 5000; // 5s overlap to catch same-second trades
const traderCursors = new Map<string, string>();

/** Reset all per-trader cursors — used in tests to prevent cross-test state leakage. */
export function resetCursors(): void {
  traderCursors.clear();
}

function updateCursor(address: string, trades: DetectedTrade[]): void {
  if (trades.length === 0) return;
  const newest = trades.reduce(
    (max, t) => (t.timestamp > max ? t.timestamp : max),
    trades[0].timestamp
  );
  const current = traderCursors.get(address);
  if (!current || newest > current) {
    traderCursors.set(address, newest);
  }
}

/** Filter trades to those near or after cursor. Uses overlap window for safety.
 *  This is an OPTIMIZATION — isSeenTrade() is the authoritative dedup. */
function filterByCursor(address: string, trades: DetectedTrade[]): DetectedTrade[] {
  const cursor = traderCursors.get(address);
  if (!cursor) return trades;
  const cursorTime = new Date(cursor).getTime() - CURSOR_OVERLAP_MS;
  return trades.filter(t => new Date(t.timestamp).getTime() > cursorTime);
}

// Track per-address fetch failures to log recovery
const failedAddresses = new Set<string>();

/** Fetch recent trade activity for a single wallet from the Polymarket Data API. */
export async function fetchTraderActivity(
  traderAddress: string
): Promise<DetectedTrade[]> {
  try {
    const url = `${CONFIG.dataApiUrl}/activity`;
    const response = await axios.get(url, {
      params: {
        user: traderAddress,
        type: "TRADE",
        limit: 100,
      },
      timeout: 8000,
      headers: { "User-Agent": "PolymarketCopyBot/1.0" },
    });

    if (!Array.isArray(response.data)) {
      logger.warn(`Unexpected response from Data API for ${traderAddress}`);
      return [];
    }

    if (failedAddresses.has(traderAddress)) {
      failedAddresses.delete(traderAddress);
      logger.info(`Data API recovered for ${traderAddress.slice(0, 10)}...`);
    }

    const allTrades = response.data.map((item: DataApiActivityItem) => ({
      // Canonical trade key: txHash-tokenId-side — matches onchain-source for hybrid dedupe.
      // tokenId (not conditionId) distinguishes outcome tokens within same market.
      id: item.transactionHash && (item.asset || item.assetId || item.tokenId)
        ? `${item.transactionHash}-${item.asset || item.assetId || item.tokenId}-${item.side || "BUY"}`
        : (item.id || `${item.transactionHash}-${item.conditionId}-${item.asset || item.tokenId}-${item.side}-${item.size}-${item.price}`),
      traderAddress,
      timestamp: typeof item.timestamp === "number"
        ? new Date(item.timestamp * 1000).toISOString()
        : (item.timestamp || item.createdAt || new Date().toISOString()),
      market: item.title || item.market || item.slug || "unknown",
      conditionId: item.conditionId || "",
      tokenId: item.asset || item.assetId || item.tokenId || "",
      side: item.side === "SELL" ? "SELL" as const : "BUY" as const,
      size: parseFloat(item.usdcSize || item.amount || "0") ||
        parseFloat(item.size || "0") * parseFloat(item.price || "0"),
      price: parseFloat(item.price || "0"),
      outcome: item.outcome || item.outcomeName || "",
    })).filter(isValidTrade);

    const newTrades = filterByCursor(traderAddress, allTrades);
    updateCursor(traderAddress, allTrades);
    return newTrades;
  } catch (err: unknown) {
    if (axios.isAxiosError(err) && err.response?.status === 429) {
      logger.warn(`Rate limited on Data API for ${traderAddress}, backing off`);
    } else {
      logger.error(`Failed to fetch activity for ${traderAddress}: ${errorMessage(err)}`);
      failedAddresses.add(traderAddress);
    }
    return [];
  }
}

/** Fetch all trader activities with bounded concurrency (batch parallel requests). */
export async function fetchAllTraderActivities(): Promise<DetectedTrade[]> {
  const results: DetectedTrade[] = [];
  const { userAddresses } = CONFIG;
  const concurrency = CONFIG.fetchConcurrency;

  for (let i = 0; i < userAddresses.length; i += concurrency) {
    const batch = userAddresses.slice(i, i + concurrency);
    const settled = await Promise.allSettled(
      batch.map(addr => fetchTraderActivity(addr))
    );
    for (const result of settled) {
      if (result.status === "fulfilled") {
        results.push(...result.value);
      }
      // Rejected: already logged inside fetchTraderActivity
    }
  }

  return results;
}
