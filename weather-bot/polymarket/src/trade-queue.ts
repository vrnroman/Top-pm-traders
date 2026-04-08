import fs from "fs";
import path from "path";
import { DetectedTrade } from "./trade-monitor";

// --- Types ---

export interface QueuedTrade {
  trade: DetectedTrade;
  enqueuedAt: number;        // Date.now() — for latency metrics
  sourceDetectedAt: number;  // parsed from trade.timestamp
  source: string;            // "data-api" | "onchain"
}

export interface PendingOrder {
  trade: DetectedTrade;
  orderId: string;
  orderPrice: number;
  copySize: number;
  placedAt: number;          // Date.now()
  marketKey: string;
  side: "BUY" | "SELL";
  // Timing fields carried from detection → execution for verification history records
  sourceDetectedAt: number;
  enqueuedAt: number;
  orderSubmittedAt: number;
  source: string;            // "data-api" | "onchain"
  accountedFilledShares?: number;
  accountedFilledUsd?: number;
  uncertainCycles?: number;
}

// --- Disk persistence for pending orders (crash recovery) ---

const DATA_DIR = path.resolve(process.cwd(), "data");
const PENDING_FILE = path.join(DATA_DIR, "pending-orders.json");

function writePendingToDisk(): void {
  if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
  const data = JSON.stringify(pendingOrders);
  const tmp = PENDING_FILE + ".tmp";
  fs.writeFileSync(tmp, data);
  try {
    fs.renameSync(tmp, PENDING_FILE);
  } catch {
    // Windows: rename fails if file is locked by indexer/antivirus
    fs.writeFileSync(PENDING_FILE, data);
    try { fs.unlinkSync(tmp); } catch { /* ignore */ }
  }
}

export function loadPendingOrdersFromDisk(): PendingOrder[] {
  try {
    if (fs.existsSync(PENDING_FILE)) {
      return JSON.parse(fs.readFileSync(PENDING_FILE, "utf8"));
    }
  } catch { /* corrupted — treat as empty */ }
  return [];
}

export function clearPendingOrdersOnDisk(): void {
  try { fs.unlinkSync(PENDING_FILE); } catch { /* ignore */ }
}

// --- In-memory queues ---

// Detection → Execution (volatile — re-detected on restart)
const pendingTrades: QueuedTrade[] = [];

// Execution → Verification (persisted to pending-orders.json)
const pendingOrders: PendingOrder[] = [];

// --- Trade queue (detection → execution) ---

export function enqueueTrade(trade: DetectedTrade, sourceDetectedAt: number, source = "data-api"): void {
  pendingTrades.push({ trade, enqueuedAt: Date.now(), sourceDetectedAt, source });
}

/** Drain all pending trades — returns current contents and clears the array. */
export function drainTrades(): QueuedTrade[] {
  return pendingTrades.splice(0);
}

// --- Pending order queue (execution → verification) ---

/** Enqueue a placed order for verification. Persists to disk for crash recovery. */
export function enqueuePendingOrder(order: PendingOrder): void {
  pendingOrders.push({
    accountedFilledShares: 0,
    accountedFilledUsd: 0,
    uncertainCycles: 0,
    ...order,
  });
  writePendingToDisk();
}

/** Return a snapshot of pending orders without clearing the shared array.
 *  Verification worker uses this to avoid race with execution worker's enqueuePendingOrder. */
export function peekPendingOrders(): PendingOrder[] {
  return [...pendingOrders];
}

/** Remove a specific pending order after verification. Removes from shared array + rewrites disk.
 *  Safe to call while execution worker enqueues new orders — both operate on the same array. */
export function removePendingOrder(orderId: string): void {
  const idx = pendingOrders.findIndex(o => o.orderId === orderId);
  if (idx !== -1) pendingOrders.splice(idx, 1);
  writePendingToDisk();
}

export function updatePendingOrder(orderId: string, patch: Partial<PendingOrder>): void {
  const idx = pendingOrders.findIndex(o => o.orderId === orderId);
  if (idx === -1) return;
  pendingOrders[idx] = { ...pendingOrders[idx], ...patch };
  writePendingToDisk();
}

export function replacePendingOrders(orders: PendingOrder[]): void {
  pendingOrders.splice(0, pendingOrders.length, ...orders);
  if (pendingOrders.length === 0) {
    clearPendingOrdersOnDisk();
    return;
  }
  writePendingToDisk();
}

export function getPendingOrderCount(): number {
  return pendingOrders.length;
}
