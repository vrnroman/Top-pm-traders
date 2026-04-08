import { describe, it, expect, vi, beforeEach } from "vitest";
import { placeTradeOrders } from "../trade-executor";
import type { DetectedTrade } from "../trade-monitor";
import type { QueuedTrade } from "../trade-queue";

// Mock all dependencies
vi.mock("../config", () => ({
  CONFIG: {
    previewMode: false,
    copyStrategy: "FIXED",
    copySize: 10,
    maxOrderSizeUsd: 100,
    minOrderSizeUsd: 1,
    maxPositionPerMarketUsd: 500,
    maxDailyVolumeUsd: 1000,
    maxTradeAgeHours: 1,
    fetchInterval: 5000,
    maxCopiesPerMarketSide: 2,
  },
}));

vi.mock("../logger", () => ({
  logger: {
    info: vi.fn(), warn: vi.fn(), error: vi.fn(),
    trade: vi.fn(), skip: vi.fn(), debug: vi.fn(),
  },
}));

vi.mock("../telegram-notifier", () => ({
  telegram: {
    tradePlaced: vi.fn(), tradeFilled: vi.fn(), tradeUnfilled: vi.fn(),
    tradeFailed: vi.fn(), botStarted: vi.fn(), botError: vi.fn(), dailySummary: vi.fn(),
  },
}));

const mockEvaluateTrade = vi.fn();
const mockRecordPlacement = vi.fn();
const mockAdjustPlacement = vi.fn();
vi.mock("../risk-manager", () => ({
  evaluateTrade: (...args: unknown[]) => mockEvaluateTrade(...args),
  recordPlacement: (...args: unknown[]) => mockRecordPlacement(...args),
  adjustPlacement: (...args: unknown[]) => mockAdjustPlacement(...args),
}));

const mockGetUsdcBalance = vi.fn().mockResolvedValue(500);
vi.mock("../get-balance", () => ({
  getUsdcBalance: () => mockGetUsdcBalance(),
}));

const mockHasPosition = vi.fn().mockReturnValue(false);
const mockRecordBuy = vi.fn();
const mockRecordSell = vi.fn();
const mockSyncInventoryFromApi = vi.fn().mockResolvedValue(undefined);
vi.mock("../inventory", () => ({
  hasPosition: (...args: unknown[]) => mockHasPosition(...args),
  recordBuy: (...args: unknown[]) => mockRecordBuy(...args),
  recordSell: (...args: unknown[]) => mockRecordSell(...args),
  syncInventoryFromApi: () => mockSyncInventoryFromApi(),
}));

const mockIsSeenTrade = vi.fn().mockReturnValue(false);
const mockMarkTradeAsSeen = vi.fn();
const mockAppendTradeHistory = vi.fn();
const mockIsMaxRetries = vi.fn().mockReturnValue(false);
const mockIncrementRetry = vi.fn().mockReturnValue(1);
const mockGetCopyCount = vi.fn().mockReturnValue(0);
const mockIncrementCopyCount = vi.fn();
vi.mock("../trade-store", () => ({
  isSeenTrade: (...args: unknown[]) => mockIsSeenTrade(...args),
  markTradeAsSeen: (...args: unknown[]) => mockMarkTradeAsSeen(...args),
  appendTradeHistory: (...args: unknown[]) => mockAppendTradeHistory(...args),
  isMaxRetries: (...args: unknown[]) => mockIsMaxRetries(...args),
  incrementRetry: (...args: unknown[]) => mockIncrementRetry(...args),
  getCopyCount: (...args: unknown[]) => mockGetCopyCount(...args),
  incrementCopyCount: (...args: unknown[]) => mockIncrementCopyCount(...args),
}));

const mockExecuteCopyOrder = vi.fn();
vi.mock("../order-executor", () => ({
  executeCopyOrder: (...args: unknown[]) => mockExecuteCopyOrder(...args),
}));

vi.mock("../order-verifier", () => ({
  verifyOrderFill: vi.fn(),
}));

const mockEnqueuePendingOrder = vi.fn();
const mockRemovePendingOrder = vi.fn();
vi.mock("../trade-queue", () => ({
  enqueuePendingOrder: (...args: unknown[]) => mockEnqueuePendingOrder(...args),
  removePendingOrder: (...args: unknown[]) => mockRemovePendingOrder(...args),
  loadPendingOrdersFromDisk: vi.fn().mockReturnValue([]),
  clearPendingOrdersOnDisk: vi.fn(),
}));

function makeTrade(overrides: Partial<DetectedTrade> = {}): DetectedTrade {
  return {
    id: "trade-1",
    traderAddress: "0x" + "a".repeat(40),
    timestamp: new Date().toISOString(),
    market: "Test Market",
    conditionId: "cond-1",
    tokenId: "tok-1",
    side: "BUY",
    size: 100,
    price: 0.5,
    outcome: "Yes",
    ...overrides,
  };
}

function makeQueued(overrides: Partial<DetectedTrade> = {}): QueuedTrade {
  const trade = makeTrade(overrides);
  return {
    trade,
    enqueuedAt: Date.now(),
    sourceDetectedAt: new Date(trade.timestamp).getTime(),
  };
}

const mockClobClient = {
  cancelOrder: vi.fn(),
} as unknown as import("@polymarket/clob-client").ClobClient;

describe("placeTradeOrders", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockIsSeenTrade.mockReturnValue(false);
    mockIsMaxRetries.mockReturnValue(false);
    mockEvaluateTrade.mockReturnValue({ shouldCopy: true, copySize: 10 });
  });

  it("returns 0 for empty trade list", async () => {
    const result = await placeTradeOrders([], mockClobClient);
    expect(result).toBe(0);
  });

  it("skips already seen trades", async () => {
    mockIsSeenTrade.mockReturnValue(true);
    const result = await placeTradeOrders([makeQueued()], mockClobClient);
    expect(result).toBe(0);
    expect(mockExecuteCopyOrder).not.toHaveBeenCalled();
  });

  it("skips trades that fail risk evaluation", async () => {
    mockEvaluateTrade.mockReturnValue({ shouldCopy: false, copySize: 0, reason: "Too old" });
    const result = await placeTradeOrders([makeQueued()], mockClobClient);
    expect(result).toBe(0);
    expect(mockMarkTradeAsSeen).toHaveBeenCalledWith("trade-1");
    expect(mockAppendTradeHistory).toHaveBeenCalledWith(expect.objectContaining({ status: "skipped" }));
  });

  it("SELL triggers immediate sync when no local position, marks seen if still empty", async () => {
    mockHasPosition.mockReturnValue(false);

    const result = await placeTradeOrders([makeQueued({ side: "SELL" })], mockClobClient);
    expect(result).toBe(0);
    expect(mockSyncInventoryFromApi).toHaveBeenCalled();
    expect(mockMarkTradeAsSeen).toHaveBeenCalledWith("trade-1");
  });

  it("places order and enqueues for verification — does NOT call verifyOrderFill", async () => {
    mockExecuteCopyOrder.mockResolvedValue({ orderId: "order-1", shares: 20, orderPrice: 0.51 });

    const result = await placeTradeOrders([makeQueued()], mockClobClient);

    expect(result).toBe(1);
    // Optimistic risk accounting at placement time
    expect(mockRecordPlacement).toHaveBeenCalledWith("cond-1", 10, "BUY");
    // Pending order enqueued for verification worker
    expect(mockEnqueuePendingOrder).toHaveBeenCalledWith(
      expect.objectContaining({ orderId: "order-1", copySize: 10, marketKey: "cond-1", side: "BUY" })
    );
    // Trade marked as seen
    expect(mockMarkTradeAsSeen).toHaveBeenCalledWith("trade-1");
    // History recorded with "placed" status (not "filled")
    expect(mockAppendTradeHistory).toHaveBeenCalledWith(expect.objectContaining({ status: "placed" }));
  });

  it("critical operation order: recordPlacement → enqueuePendingOrder → markTradeAsSeen", async () => {
    mockExecuteCopyOrder.mockResolvedValue({ orderId: "order-1", shares: 20, orderPrice: 0.51 });
    const callOrder: string[] = [];
    mockRecordPlacement.mockImplementation(() => callOrder.push("recordPlacement"));
    mockEnqueuePendingOrder.mockImplementation(() => callOrder.push("enqueuePendingOrder"));
    mockMarkTradeAsSeen.mockImplementation(() => callOrder.push("markTradeAsSeen"));

    await placeTradeOrders([makeQueued()], mockClobClient);

    expect(callOrder).toEqual(["recordPlacement", "enqueuePendingOrder", "markTradeAsSeen"]);
  });

  it("handles no orderId from CLOB — uses retry, not immediate mark-seen", async () => {
    mockExecuteCopyOrder.mockResolvedValue({ orderId: "", shares: 3.03, orderPrice: 0.33 });
    mockIncrementRetry.mockReturnValue(1);
    mockIsMaxRetries.mockReturnValue(false);

    await placeTradeOrders([makeQueued()], mockClobClient);

    expect(mockIncrementRetry).toHaveBeenCalledWith("trade-1");
    expect(mockEnqueuePendingOrder).not.toHaveBeenCalled();
    expect(mockAppendTradeHistory).toHaveBeenCalledWith(expect.objectContaining({ status: "failed", reason: "No orderId from CLOB" }));
  });

  it("handles executeCopyOrder throwing — retries up to max", async () => {
    mockExecuteCopyOrder.mockRejectedValue(new Error("Network timeout"));
    mockIncrementRetry.mockReturnValue(1);
    mockIsMaxRetries.mockReturnValue(false);

    await placeTradeOrders([makeQueued()], mockClobClient);

    expect(mockIncrementRetry).toHaveBeenCalledWith("trade-1");
    expect(mockMarkTradeAsSeen).not.toHaveBeenCalled();
    expect(mockAppendTradeHistory).toHaveBeenCalledWith(expect.objectContaining({ status: "failed", reason: "Network timeout" }));
  });

  it("handles executeCopyOrder throwing at max retries — marks seen, alerts telegram", async () => {
    mockExecuteCopyOrder.mockRejectedValue(new Error("CLOB down"));
    mockIncrementRetry.mockReturnValue(3);
    mockIsMaxRetries.mockReturnValueOnce(false).mockReturnValueOnce(true);

    const { telegram } = await import("../telegram-notifier");
    await placeTradeOrders([makeQueued()], mockClobClient);

    expect(mockMarkTradeAsSeen).toHaveBeenCalledWith("trade-1");
    expect(telegram.tradeFailed).toHaveBeenCalledWith("Test Market", "CLOB down");
  });

  it("processes multiple trades, counts only placed orders", async () => {
    const queued = [
      makeQueued({ id: "t1" }),
      makeQueued({ id: "t2" }),
      makeQueued({ id: "t3" }),
    ];

    // t1 = placed, t2 = skipped by risk, t3 = placed
    mockEvaluateTrade
      .mockReturnValueOnce({ shouldCopy: true, copySize: 10 })
      .mockReturnValueOnce({ shouldCopy: false, copySize: 0, reason: "Too old" })
      .mockReturnValueOnce({ shouldCopy: true, copySize: 10 });

    mockExecuteCopyOrder
      .mockResolvedValueOnce({ orderId: "o1", shares: 20, orderPrice: 0.51 })
      .mockResolvedValueOnce({ orderId: "o3", shares: 20, orderPrice: 0.51 });

    const result = await placeTradeOrders(queued, mockClobClient);
    expect(result).toBe(2); // both t1 and t3 placed (verification is separate)
    expect(mockEnqueuePendingOrder).toHaveBeenCalledTimes(2);
  });
});
