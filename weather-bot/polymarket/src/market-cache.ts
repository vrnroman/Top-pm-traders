// Market metadata cache — maps tokenId to market name/conditionId/outcome
// Used by on-chain source to enrich raw OrderFilled events with human-readable data.
// Cache is immutable (market metadata never changes) — persisted to disk, grows only.

import fs from "fs";
import path from "path";
import axios from "axios";
import { CONFIG } from "./config";
import { logger } from "./logger";
import { errorMessage } from "./types";

export interface MarketMeta {
  conditionId: string;
  market: string;       // human-readable title
  outcome: string;      // "Yes" / "No" / outcome name
  tokenId: string;
}

const DATA_DIR = path.resolve(process.cwd(), "data");
const CACHE_FILE = path.join(DATA_DIR, "market-cache.json");

// In-memory cache: tokenId → MarketMeta
const cache = new Map<string, MarketMeta>();

// Load disk cache on module init
function loadCache(): void {
  try {
    if (fs.existsSync(CACHE_FILE)) {
      const data: MarketMeta[] = JSON.parse(fs.readFileSync(CACHE_FILE, "utf8"));
      for (const m of data) cache.set(m.tokenId, m);
      logger.debug(`Market cache loaded: ${cache.size} entries`);
    }
  } catch { /* corrupted — start fresh */ }
}

function saveCache(): void {
  if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
  const data = JSON.stringify([...cache.values()]);
  const tmp = CACHE_FILE + ".tmp";
  fs.writeFileSync(tmp, data);
  try {
    fs.renameSync(tmp, CACHE_FILE);
  } catch {
    fs.writeFileSync(CACHE_FILE, data);
    try { fs.unlinkSync(tmp); } catch { /* ignore */ }
  }
}

loadCache();

/** Look up market metadata for a tokenId. Fetches from CLOB API on cache miss. Returns null on failure. */
export async function getMarketMeta(tokenId: string): Promise<MarketMeta | null> {
  const cached = cache.get(tokenId);
  if (cached) return cached;

  try {
    // CLOB API supports lookup by token asset ID
    const res = await axios.get(`${CONFIG.clobApiUrl}/markets`, {
      params: { asset_id: tokenId },
      timeout: 10000,
    });

    // Response is a single market object or array
    const market = Array.isArray(res.data) ? res.data[0] : res.data;
    if (!market || !market.condition_id) return null;

    // Find which outcome this token belongs to
    const tokens = market.tokens || [];
    const tokenEntry = tokens.find((t: { token_id?: string }) => t.token_id === tokenId);
    const outcome = tokenEntry?.outcome || "";

    const meta: MarketMeta = {
      conditionId: market.condition_id,
      market: market.question || market.title || "unknown",
      outcome,
      tokenId,
    };

    cache.set(tokenId, meta);
    saveCache();
    return meta;
  } catch (err: unknown) {
    logger.warn(`Market cache miss for tokenId ${tokenId}: ${errorMessage(err)}`);
    return null;
  }
}

/** Pre-warm cache for a batch of token IDs (skips already cached). */
export async function warmCache(tokenIds: string[]): Promise<void> {
  const uncached = tokenIds.filter(id => !cache.has(id));
  for (const id of uncached) {
    await getMarketMeta(id);
  }
}
