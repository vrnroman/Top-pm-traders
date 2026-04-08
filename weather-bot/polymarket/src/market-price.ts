import { ClobClient } from "@polymarket/clob-client";
import { logger } from "./logger";

export interface MarketSnapshot {
  bestBid: number;
  bestAsk: number;
  midpoint: number;
  spread: number;
  spreadBps: number; // spread in basis points relative to midpoint
  fetchedAt: number;
}

const FETCH_TIMEOUT_MS = 200;
const CACHE_TTL_MS = 5000; // 5s — avoids redundant API calls for same token in a batch

const snapshotCache = new Map<string, { snapshot: MarketSnapshot | null; expiresAt: number }>();

/** Clear snapshot cache. Exported for testing only. */
export function _clearSnapshotCache(): void { snapshotCache.clear(); }

/** Fetch live best bid/ask from CLOB. Returns cached result if <5s old. Returns null on timeout/error. */
export async function fetchMarketSnapshot(
  clobClient: ClobClient,
  tokenId: string
): Promise<MarketSnapshot | null> {
  const cached = snapshotCache.get(tokenId);
  if (cached && Date.now() < cached.expiresAt) return cached.snapshot;
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const result = await Promise.race([
      fetchPrices(clobClient, tokenId),
      new Promise<null>((resolve) => {
        timer = setTimeout(() => resolve(null), FETCH_TIMEOUT_MS);
      }),
    ]);
    snapshotCache.set(tokenId, { snapshot: result, expiresAt: Date.now() + CACHE_TTL_MS });
    return result;
  } catch (err) {
    logger.warn(`Market snapshot fetch failed for ${tokenId}: ${err instanceof Error ? err.message : err}`);
    return null;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function fetchPrices(clobClient: ClobClient, tokenId: string): Promise<MarketSnapshot | null> {
  // getPrice returns best executable price for each side
  const [bidRaw, askRaw] = await Promise.all([
    clobClient.getPrice(tokenId, "BUY"),
    clobClient.getPrice(tokenId, "SELL"),
  ]);

  // SDK getPrice returns string, number, or { price: string } — normalize
  const bestBid = parsePrice(bidRaw);
  const bestAsk = parsePrice(askRaw);

  if (isNaN(bestBid) || isNaN(bestAsk) || bestBid <= 0 || bestAsk <= 0) {
    logger.warn(`Invalid market prices for ${tokenId}: bid=${bidRaw}, ask=${askRaw}`);
    return null;
  }

  // Crossed book (bid > ask) = no real liquidity, unsafe to trade
  if (bestBid > bestAsk) {
    logger.warn(`Crossed book for ${tokenId}: bid=${bestBid} > ask=${bestAsk}`);
    return null;
  }

  const midpoint = (bestBid + bestAsk) / 2;
  const spread = bestAsk - bestBid;
  const spreadBps = midpoint > 0 ? Math.round((spread / midpoint) * 10000) : 0;

  return { bestBid, bestAsk, midpoint, spread, spreadBps, fetchedAt: Date.now() };
}

/** Normalize getPrice response — SDK returns string, number, or { price: string }. */
function parsePrice(raw: unknown): number {
  if (typeof raw === "number") return raw;
  if (typeof raw === "string") return parseFloat(raw);
  if (raw && typeof raw === "object" && "price" in raw) {
    return parseFloat(String((raw as Record<string, unknown>).price));
  }
  return NaN;
}

/** Compute price drift in basis points. Positive = market moved against us (worse entry). */
export function computeDriftBps(
  traderPrice: number,
  snapshot: MarketSnapshot,
  side: "BUY" | "SELL"
): number {
  if (traderPrice <= 0) return 0;
  // BUY: we pay ask. Drift = how much ask moved above trader's price
  // SELL: we receive bid. Drift = how much bid moved below trader's price
  const currentPrice = side === "BUY" ? snapshot.bestAsk : snapshot.bestBid;
  const drift = side === "BUY"
    ? (currentPrice - traderPrice) / traderPrice
    : (traderPrice - currentPrice) / traderPrice;
  return Math.round(drift * 10000);
}
