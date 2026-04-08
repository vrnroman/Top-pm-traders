import fs from "fs";
import path from "path";
import axios from "axios";
import { CONFIG } from "./config";
import { logger } from "./logger";
import { errorMessage } from "./types";

const DATA_DIR = path.resolve(process.cwd(), "data");
if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
const INVENTORY_FILE = CONFIG.previewMode 
  ? path.join(DATA_DIR, "preview-inventory.json")
  : path.join(DATA_DIR, "inventory.json");

// tokenId → { shares, avgPrice, marketKey, market }
interface Position {
  shares: number;
  avgPrice: number;
  marketKey: string; // conditionId
  market: string;    // human-readable title
}

let inventory: Record<string, Position> = {};

function load(): void {
  try {
    if (fs.existsSync(INVENTORY_FILE)) {
      inventory = JSON.parse(fs.readFileSync(INVENTORY_FILE, "utf8"));
    }
  } catch {
    // Corrupted file — start fresh
    inventory = {};
  }
}

function save(): void {
  const data = JSON.stringify(inventory, null, 2);
  const tmp = INVENTORY_FILE + ".tmp";
  try {
    fs.writeFileSync(tmp, data);
    fs.renameSync(tmp, INVENTORY_FILE);
  } catch {
    fs.writeFileSync(INVENTORY_FILE, data);
    try { fs.unlinkSync(tmp); } catch { /* ignore */ }
  }
}

load();

/** Calculate weighted average price for combining positions. */
export function weightedAvgPrice(existingShares: number, existingPrice: number, newShares: number, newPrice: number): number {
  const totalShares = existingShares + newShares;
  if (totalShares === 0) return 0;
  return (existingPrice * existingShares + newPrice * newShares) / totalShares;
}

/** Record a BUY fill — creates or updates position with weighted average price. */
export function recordBuy(tokenId: string, shares: number, price: number, marketKey: string, market: string): void {
  const existing = inventory[tokenId];
  if (existing) {
    const totalShares = existing.shares + shares;
    existing.avgPrice = weightedAvgPrice(existing.shares, existing.avgPrice, shares, price);
    existing.shares = totalShares;
  } else {
    inventory[tokenId] = { shares, avgPrice: price, marketKey, market };
  }
  save();
  logger.debug(`Inventory BUY: ${shares.toFixed(2)} shares of "${market}" @ ${price} (total: ${inventory[tokenId].shares.toFixed(2)})`);
}

export function hasPosition(tokenId: string): boolean {
  return !!inventory[tokenId] && inventory[tokenId].shares > 0;
}

export function getPosition(tokenId: string): (Position & { tokenId: string }) | null {
  const p = inventory[tokenId];
  return p ? { ...p, tokenId } : null;
}

export function recordSell(tokenId: string, shares: number): void {
  const pos = inventory[tokenId];
  if (!pos) return;
  pos.shares = Math.max(0, pos.shares - shares);
  if (pos.shares <= 0) {
    delete inventory[tokenId];
  }
  save();
}

/** Reconcile local inventory with real positions from the Polymarket Data API. */
export async function syncInventoryFromApi(): Promise<void> {
  if (CONFIG.previewMode) {
    logger.debug("Inventory sync skipped in preview mode");
    return;
  }
  try {
    const res = await axios.get(`${CONFIG.dataApiUrl}/positions`, {
      params: { user: CONFIG.proxyWallet },
      timeout: 15000,
    });
    if (!Array.isArray(res.data)) return;

    const real: Record<string, Position> = {};
    for (const p of res.data) {
      const size = typeof p.size === "string" ? parseFloat(p.size) : Number(p.size);
      const avgPrice = typeof p.avgPrice === "string" ? parseFloat(p.avgPrice) : Number(p.avgPrice || 0);
      if (!p.asset || !isFinite(size) || size <= 0) continue;
      real[p.asset] = {
        shares: size,
        avgPrice: isFinite(avgPrice) ? avgPrice : 0,
        marketKey: p.conditionId || "",
        market: p.title || "unknown",
      };
    }

    // Count diffs
    const localKeys = Object.keys(inventory);
    const realKeys = Object.keys(real);
    const phantoms = localKeys.filter((k) => !real[k]);
    const missing = realKeys.filter((k) => !inventory[k]);

    if (phantoms.length > 0) logger.warn(`Inventory sync: removing ${phantoms.length} phantom position(s)`);
    if (missing.length > 0) logger.info(`Inventory sync: adding ${missing.length} missing position(s)`);

    inventory = real;
    save();
    logger.info(`Inventory synced: ${realKeys.length} position(s) from API`);
  } catch (err: unknown) {
    logger.warn(`Inventory sync failed: ${errorMessage(err)} — keeping local state`);
  }
}

export function getInventorySummary(): string {
  const entries = Object.entries(inventory);
  if (entries.length === 0) return "No open positions";
  return entries
    .map(([, pos]) => `${pos.market}: ${pos.shares.toFixed(2)} shares @ ${pos.avgPrice.toFixed(2)}`)
    .join(", ");
}

/** Return all positions as an array for external consumers (e.g. Telegram /status). */
export function getPositions(): (Position & { tokenId: string })[] {
  return Object.entries(inventory)
    .filter(([, p]) => p.shares > 0)
    .map(([tokenId, p]) => ({ ...p, tokenId }));
}
