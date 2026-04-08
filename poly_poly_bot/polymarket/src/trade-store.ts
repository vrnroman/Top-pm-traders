import fs from "fs";
import path from "path";
import { todayUtc } from "./utils";

const DATA_DIR = path.resolve(process.cwd(), "data");
const SEEN_FILE = path.join(DATA_DIR, "seen-trades.json");
const HISTORY_FILE = path.join(DATA_DIR, "trade-history.jsonl");
const COUNTS_FILE = path.join(DATA_DIR, "trader-counts.json");

if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });

function loadSeen(): Set<string> {
  try {
    if (fs.existsSync(SEEN_FILE)) {
      const data = JSON.parse(fs.readFileSync(SEEN_FILE, "utf8"));
      return new Set(data);
    }
  } catch {
    // Corrupted file — start fresh
  }
  return new Set();
}

// Safe write: try atomic rename, fallback to direct write on Windows EPERM
function atomicWrite(filePath: string, data: string): void {
  const tmp = filePath + ".tmp";
  fs.writeFileSync(tmp, data);
  try {
    fs.renameSync(tmp, filePath);
  } catch {
    // Windows: rename fails if file is locked by indexer/antivirus
    fs.writeFileSync(filePath, data);
    try { fs.unlinkSync(tmp); } catch { /* ignore */ }
  }
}

const MAX_SEEN_TRADES = 10000; // Cap seen-trades set to prevent unbounded growth

function saveSeen(): void {
  const arr = [...seenTrades].slice(-MAX_SEEN_TRADES);
  atomicWrite(SEEN_FILE, JSON.stringify(arr));
}

const seenTrades = loadSeen();

function loadTraderCounts(): Record<string, number> {
  try {
    if (fs.existsSync(COUNTS_FILE)) {
      return JSON.parse(fs.readFileSync(COUNTS_FILE, "utf8"));
    }
  } catch {
    // Corrupt file
  }
  return {};
}

const traderCounts = loadTraderCounts();
const MAX_TRADER_COUNTS = 20000;

function saveTraderCounts(): void {
  // Prevent unbounded memory growth over months of running
  const keys = Object.keys(traderCounts);
  if (keys.length > MAX_TRADER_COUNTS) {
    const toRemove = keys.slice(0, keys.length - MAX_TRADER_COUNTS);
    for (const k of toRemove) delete traderCounts[k];
  }
  atomicWrite(COUNTS_FILE, JSON.stringify(traderCounts));
}

export function getCopyCount(trader: string, market: string, side: string): number {
  const key = `${trader}|${market}|${side}|${todayUtc()}`;
  return traderCounts[key] || 0;
}

export function incrementCopyCount(trader: string, market: string, side: string): void {
  const key = `${trader}|${market}|${side}|${todayUtc()}`;
  traderCounts[key] = (traderCounts[key] || 0) + 1;
  saveTraderCounts();
}

const retryCount = new Map<string, number>();
const MAX_RETRIES = 3; // Max retry attempts before giving up on a failed trade
const MAX_RETRY_ENTRIES = 1000; // Cap retryCount map to prevent unbounded memory growth

export function isSeenTrade(tradeId: string): boolean {
  return seenTrades.has(tradeId);
}

export function incrementRetry(tradeId: string): number {
  // Evict oldest entries if map exceeds cap — preserves in-flight retry state
  if (retryCount.size > MAX_RETRY_ENTRIES) {
    const evictCount = Math.floor(MAX_RETRY_ENTRIES / 2);
    let i = 0;
    for (const key of retryCount.keys()) {
      if (i++ >= evictCount) break;
      retryCount.delete(key);
    }
  }
  const count = (retryCount.get(tradeId) || 0) + 1;
  retryCount.set(tradeId, count);
  return count;
}

export function isMaxRetries(tradeId: string): boolean {
  return (retryCount.get(tradeId) || 0) >= MAX_RETRIES;
}

/** Mark a trade as processed — persists to JSON to survive restarts. */
export function markTradeAsSeen(tradeId: string): void {
  seenTrades.add(tradeId);
  retryCount.delete(tradeId); // Clean up retry state to prevent unbounded growth
  saveSeen();
}

export interface TradeRecord {
  timestamp: string;
  traderAddress: string;
  market: string;
  side: string;
  traderSize: number;
  copySize: number;
  price: number;
  status: "placed" | "filled" | "partial" | "unfilled" | "unknown" | "skipped" | "failed" | "preview";
  reason?: string;
  orderId?: string;
  fillPrice?: number;
  fillShares?: number;
  traderPrice?: number; // Original price trader got — for slippage calculation
  // Stage timing (ms epoch) — for latency instrumentation
  sourceDetectedAt?: number;
  enqueuedAt?: number;
  orderSubmittedAt?: number;
  firstFillSeenAt?: number;
  source?: string; // "data-api" | "onchain"
  // Market quality metrics — for execution quality analysis
  driftBps?: number;   // price drift from trader's fill to current market (basis points)
  spreadBps?: number;  // current bid-ask spread (basis points)
  // Market identifiers
  conditionId?: string;
  tokenId?: string;
  outcome?: string;
}

// Rolling window of recent reaction latencies (sourceDetectedAt → orderSubmittedAt) for heartbeat reporting
const recentLatencies: number[] = [];
const MAX_LATENCY_SAMPLES = 50;

/** Append trade record to JSONL history. Swallows IO errors to prevent bot crash on non-critical logging. */
export function appendTradeHistory(record: TradeRecord): void {
  try {
    const line = JSON.stringify(record) + "\n";
    fs.appendFileSync(HISTORY_FILE, line);
  } catch {
    // Non-critical: disk full or file locked should not crash the bot
  }
  // Track reaction latency for heartbeat — only from terminal statuses to avoid double-counting
  // "placed" is intermediate; terminal = filled/partial/unfilled/unknown (verification result)
  const terminal = ["filled", "partial", "unfilled", "unknown"];
  if (terminal.includes(record.status) && record.sourceDetectedAt && record.orderSubmittedAt) {
    recentLatencies.push(record.orderSubmittedAt - record.sourceDetectedAt);
    if (recentLatencies.length > MAX_LATENCY_SAMPLES) recentLatencies.shift();
  }
}

/** Get average reaction latency from recent trades (ms). Returns 0 if no data. */
export function getAvgReactionLatency(): number {
  if (recentLatencies.length === 0) return 0;
  return Math.round(recentLatencies.reduce((a, b) => a + b, 0) / recentLatencies.length);
}
