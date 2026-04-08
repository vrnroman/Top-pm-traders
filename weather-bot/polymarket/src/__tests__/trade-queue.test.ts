import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import fs from "fs";
import path from "path";

// Mock fs for disk persistence tests
const DATA_DIR = path.resolve(process.cwd(), "data");
const PENDING_FILE = path.join(DATA_DIR, "pending-orders.json");

// We need to test the actual module, but with controlled fs
// Import fresh module for each test to reset in-memory state
async function freshImport() {
  // Clear module cache to get fresh state
  vi.resetModules();
  return await import("../trade-queue");
}

vi.mock("../trade-monitor", () => ({
  DetectedTrade: {},
}));

const makeTrade = (id = "t1") => ({
  id,
  traderAddress: "0x" + "a".repeat(40),
  timestamp: new Date().toISOString(),
  market: "Test Market",
  conditionId: "cond-1",
  tokenId: "tok-1",
  side: "BUY" as const,
  size: 100,
  price: 0.5,
  outcome: "Yes",
});

describe("trade-queue", () => {
  beforeEach(() => {
    // Clean up pending file
    try { fs.unlinkSync(PENDING_FILE); } catch { /* ok */ }
  });

  afterEach(() => {
    try { fs.unlinkSync(PENDING_FILE); } catch { /* ok */ }
  });

  it("enqueueTrade + drainTrades: enqueue returns in drain, drain clears", async () => {
    const { enqueueTrade, drainTrades } = await freshImport();
    const trade = makeTrade("t1");
    enqueueTrade(trade, Date.now(), "data-api");
    enqueueTrade(makeTrade("t2"), Date.now(), "onchain");

    const drained = drainTrades();
    expect(drained).toHaveLength(2);
    expect(drained[0].trade.id).toBe("t1");
    expect(drained[0].source).toBe("data-api");
    expect(drained[1].source).toBe("onchain");

    // Second drain returns empty
    expect(drainTrades()).toHaveLength(0);
  });

  it("enqueuePendingOrder persists to disk", async () => {
    const { enqueuePendingOrder } = await freshImport();
    const order = {
      trade: makeTrade(),
      orderId: "order-1",
      orderPrice: 0.51,
      copySize: 10,
      placedAt: Date.now(),
      marketKey: "cond-1",
      side: "BUY" as const,
      sourceDetectedAt: Date.now(),
      enqueuedAt: Date.now(),
      orderSubmittedAt: Date.now(),
      source: "data-api",
    };
    enqueuePendingOrder(order);

    expect(fs.existsSync(PENDING_FILE)).toBe(true);
    const diskData = JSON.parse(fs.readFileSync(PENDING_FILE, "utf8"));
    expect(diskData).toHaveLength(1);
    expect(diskData[0].orderId).toBe("order-1");
  });

  it("peekPendingOrders returns copy without clearing", async () => {
    const { enqueuePendingOrder, peekPendingOrders } = await freshImport();
    const order = {
      trade: makeTrade(),
      orderId: "order-1",
      orderPrice: 0.51,
      copySize: 10,
      placedAt: Date.now(),
      marketKey: "cond-1",
      side: "BUY" as const,
      sourceDetectedAt: Date.now(),
      enqueuedAt: Date.now(),
      orderSubmittedAt: Date.now(),
      source: "data-api",
    };
    enqueuePendingOrder(order);

    const peek1 = peekPendingOrders();
    expect(peek1).toHaveLength(1);

    // Peek again — still there (not drained)
    const peek2 = peekPendingOrders();
    expect(peek2).toHaveLength(1);
  });

  it("removePendingOrder removes from memory and rewrites disk", async () => {
    const { enqueuePendingOrder, removePendingOrder, peekPendingOrders } = await freshImport();
    const makeOrder = (id: string) => ({
      trade: makeTrade(id),
      orderId: id,
      orderPrice: 0.51,
      copySize: 10,
      placedAt: Date.now(),
      marketKey: "cond-1",
      side: "BUY" as const,
      sourceDetectedAt: Date.now(),
      enqueuedAt: Date.now(),
      orderSubmittedAt: Date.now(),
      source: "data-api",
    });

    enqueuePendingOrder(makeOrder("o1"));
    enqueuePendingOrder(makeOrder("o2"));
    enqueuePendingOrder(makeOrder("o3"));
    expect(peekPendingOrders()).toHaveLength(3);

    removePendingOrder("o2");
    expect(peekPendingOrders()).toHaveLength(2);
    expect(peekPendingOrders().map(o => o.orderId)).toEqual(["o1", "o3"]);

    // Disk reflects removal
    const diskData = JSON.parse(fs.readFileSync(PENDING_FILE, "utf8"));
    expect(diskData).toHaveLength(2);
  });

  it("loadPendingOrdersFromDisk recovers after simulated crash", async () => {
    // Write directly to disk (simulating a crash where in-memory is lost)
    const order = {
      trade: makeTrade(),
      orderId: "crashed-order",
      orderPrice: 0.5,
      copySize: 5,
      placedAt: Date.now(),
      marketKey: "cond-1",
      side: "BUY",
      sourceDetectedAt: Date.now(),
      enqueuedAt: Date.now(),
      orderSubmittedAt: Date.now(),
      source: "data-api",
    };
    if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
    fs.writeFileSync(PENDING_FILE, JSON.stringify([order]));

    const { loadPendingOrdersFromDisk } = await freshImport();
    const loaded = loadPendingOrdersFromDisk();
    expect(loaded).toHaveLength(1);
    expect(loaded[0].orderId).toBe("crashed-order");
  });

  it("clearPendingOrdersOnDisk removes file", async () => {
    if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
    fs.writeFileSync(PENDING_FILE, "[]");

    const { clearPendingOrdersOnDisk } = await freshImport();
    clearPendingOrdersOnDisk();
    expect(fs.existsSync(PENDING_FILE)).toBe(false);
  });

  it("concurrent enqueue during verification: new order survives remove", async () => {
    const { enqueuePendingOrder, peekPendingOrders, removePendingOrder } = await freshImport();
    const makeOrder = (id: string) => ({
      trade: makeTrade(id),
      orderId: id,
      orderPrice: 0.51,
      copySize: 10,
      placedAt: Date.now(),
      marketKey: "cond-1",
      side: "BUY" as const,
      sourceDetectedAt: Date.now(),
      enqueuedAt: Date.now(),
      orderSubmittedAt: Date.now(),
      source: "data-api",
    });

    // Simulate: verification peeks [o1, o2], then execution adds o3 during verification
    enqueuePendingOrder(makeOrder("o1"));
    enqueuePendingOrder(makeOrder("o2"));
    const snapshot = peekPendingOrders(); // [o1, o2]

    // Execution worker adds new order during verification
    enqueuePendingOrder(makeOrder("o3"));

    // Verification processes o1, removes it
    removePendingOrder(snapshot[0].orderId);
    // o3 should still be in memory and on disk
    const remaining = peekPendingOrders();
    expect(remaining.map(o => o.orderId)).toEqual(["o2", "o3"]);

    const diskData = JSON.parse(fs.readFileSync(PENDING_FILE, "utf8"));
    expect(diskData.map((o: { orderId: string }) => o.orderId)).toEqual(["o2", "o3"]);
  });

  it("burst: 10 trades enqueued, all returned in single drain", async () => {
    const { enqueueTrade, drainTrades } = await freshImport();
    for (let i = 0; i < 10; i++) {
      enqueueTrade(makeTrade(`burst-${i}`), Date.now(), "data-api");
    }
    const drained = drainTrades();
    expect(drained).toHaveLength(10);
    expect(drained[0].trade.id).toBe("burst-0");
    expect(drained[9].trade.id).toBe("burst-9");
  });
});
