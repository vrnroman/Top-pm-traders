// Shared scan cache — prevents re-scanning recently analyzed traders (7-day TTL)

import fs from "fs";
import path from "path";

const SCAN_CACHE_FILE = path.resolve(process.cwd(), "data", "research", "scanned-cache.json");
const SCAN_CACHE_TTL_DAYS = 7;

export interface ScanCacheEntry {
  address: string;
  scannedAt: string;
  passed: boolean;
}

/** Load scan cache, filtering out entries older than TTL. */
export function loadScanCache(): Map<string, ScanCacheEntry> {
  try {
    if (!fs.existsSync(SCAN_CACHE_FILE)) return new Map();
    const data: ScanCacheEntry[] = JSON.parse(fs.readFileSync(SCAN_CACHE_FILE, "utf8"));
    const cutoff = Date.now() - SCAN_CACHE_TTL_DAYS * 86400000;
    const map = new Map<string, ScanCacheEntry>();
    for (const e of data) {
      if (new Date(e.scannedAt).getTime() > cutoff) map.set(e.address.toLowerCase(), e);
    }
    return map;
  } catch { return new Map(); }
}

/** Persist scan cache to disk. */
export function saveScanCache(cache: Map<string, ScanCacheEntry>): void {
  const dir = path.dirname(SCAN_CACHE_FILE);
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(SCAN_CACHE_FILE, JSON.stringify([...cache.values()], null, 2));
}

/** Add an address to the cache. */
export function addToCache(cache: Map<string, ScanCacheEntry>, address: string, passed: boolean): void {
  cache.set(address.toLowerCase(), { address, scannedAt: new Date().toISOString(), passed });
}
