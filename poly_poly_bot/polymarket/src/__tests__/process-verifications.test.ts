import { describe, it, expect, vi, beforeEach } from "vitest";
import { processVerifications, recoverPendingOrders } from "../trade-executor";
import type { PendingOrder } from "../trade-queue";

vi.mock("../config", () => ({
  CONFIG: { previewMode: false, copyStrategy: "FIXED", copySize: 10, maxOrderSizeUsd: 100,
    minOrderSizeUsd: 1, maxPositionPerMarketUsd: 500, maxDailyVolumeUsd: 1000, maxTradeAgeHours: 1, fetchInterval: 5000 },
}));

vi.mock("../logger", () => ({
  logger: { info: vi.fn(), warn: vi.fn(), error: vi.fn(), trade: vi.fn(), skip: vi.fn(), debug: vi.fn() },
}));

vi.mock("../telegram-notifier", () => ({
  telegram: { tradePlaced: vi.fn(), tradeFilled: vi.fn(), tradeUnfilled: vi.fn(), tradeFailed: vi.fn(), botStarted: vi.fn(), botError: vi.fn(), dailySummary: vi.fn() },
}));

const mockAdjustPlacement = vi.fn();
vi.mock("../risk-manager", () => ({
  evaluateTrade: vi.fn(), recordPlacement: vi.fn(),
  adjustPlacement: (...args: unknown[]) => mockAdjustPlacement(...args),
}));

vi.mock("../get-balance", () => ({ getUsdcBalance: vi.fn().mockResolvedValue(500) }));

const mockRecordBuy = vi.fn();
const mockRecordSell = vi.fn();
vi.mock("../inventory", () => ({
  hasPosition: vi.fn(), recordBuy: (...args: unknown[]) => mockRecordBuy(...args),
  recordSell: (...args: unknown[]) => mockRecordSell(...args), syncInventoryFromApi: vi.fn(),
}));

const mockMarkTradeAsSeen = vi.fn();
const mockAppendTradeHistory = vi.fn();
vi.mock("../trade-store", () => ({
  isSeenTrade: vi.fn().mockReturnValue(false), markTradeAsSeen: (...args: unknown[]) => mockMarkTradeAsSeen(...args),
  appendTradeHistory: (...args: unknown[]) => mockAppendTradeHistory(...args),
  isMaxRetries: vi.fn().mockReturnValue(false), incrementRetry: vi.fn().mockReturnValue(1),
}));

vi.mock("../order-executor", () => ({ executeCopyOrder: vi.fn() }));

const mockVerifyOrderFill = vi.fn();
vi.mock("../order-verifier", () => ({
  verifyOrderFill: (...args: unknown[]) => mockVerifyOrderFill(...args),
}));

const mockRemovePendingOrder = vi.fn();
const mockLoadFromDisk = vi.fn().mockReturnValue([]);
const mockClearDisk = vi.fn();
const mockReplacePendingOrders = vi.fn();
const mockUpdatePendingOrder = vi.fn();
vi.mock("../trade-queue", () => ({
  enqueuePendingOrder: vi.fn(),
  removePendingOrder: (...args: unknown[]) => mockRemovePendingOrder(...args),
  loadPendingOrdersFromDisk: () => mockLoadFromDisk(),
  clearPendingOrdersOnDisk: () => mockClearDisk(),
  replacePendingOrders: (...args: unknown[]) => mockReplacePendingOrders(...args),
  updatePendingOrder: (...args: unknown[]) => mockUpdatePendingOrder(...args),
}));

const makeTrade = (id = "t1") => ({
  id, traderAddress: "0x" + "a".repeat(40), timestamp: new Date().toISOString(),
  market: "Test", conditionId: "cond-1", tokenId: "tok-1",
  side: "BUY" as const, size: 100, price: 0.5, outcome: "Yes",
});

const makePending = (orderId = "o1", opts: Partial<PendingOrder> = {}): PendingOrder => ({
  trade: makeTrade(), orderId, orderPrice: 0.51, copySize: 10,
  placedAt: Date.now(), marketKey: "cond-1", side: "BUY",
  sourceDetectedAt: Date.now() - 1000, enqueuedAt: Date.now() - 500,
  orderSubmittedAt: Date.now() - 200, source: "data-api", ...opts,
});

const mockClobClient = {
  cancelOrder: vi.fn().mockResolvedValue(undefined),
} as unknown as import("@polymarket/clob-client").ClobClient;

describe("processVerifications", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockClobClient.cancelOrder = vi.fn().mockResolvedValue(undefined);
  });

  it("FILLED: adjusts risk, records inventory, sends telegram, removes from queue", async () => {
    mockVerifyOrderFill.mockResolvedValue({ status: "FILLED", filledShares: 20, filledUsd: 10.2, fillPrice: 0.51 });
    await processVerifications([makePending()], mockClobClient);

    expect(mockAdjustPlacement).toHaveBeenCalledWith("cond-1", 10, 10.2, "BUY");
    expect(mockRecordBuy).toHaveBeenCalledWith("tok-1", 20, 0.51, "cond-1", "Test");
    expect(mockRemovePendingOrder).toHaveBeenCalledWith("o1");
    expect(mockAppendTradeHistory).toHaveBeenCalledWith(expect.objectContaining({
      status: "filled", firstFillSeenAt: expect.any(Number),
      sourceDetectedAt: expect.any(Number), orderSubmittedAt: expect.any(Number), source: "data-api",
    }));
  });

  it("FILLED SELL: records sell not buy", async () => {
    mockVerifyOrderFill.mockResolvedValue({ status: "FILLED", filledShares: 5, filledUsd: 2.5, fillPrice: 0.5 });
    await processVerifications([makePending("o1", { side: "SELL" })], mockClobClient);

    expect(mockRecordSell).toHaveBeenCalledWith("tok-1", 5);
    expect(mockRecordBuy).not.toHaveBeenCalled();
  });

  it("PARTIAL: adjusts risk, cancels remainder, records partial inventory", async () => {
    mockVerifyOrderFill.mockResolvedValue({ status: "PARTIAL", filledShares: 5, filledUsd: 2.55, fillPrice: 0.51 });
    await processVerifications([makePending()], mockClobClient);

    expect(mockAdjustPlacement).toHaveBeenCalledWith("cond-1", 10, 2.55, "BUY");
    expect(mockRecordBuy).toHaveBeenCalledWith("tok-1", 5, 0.51, "cond-1", "Test");
    expect(mockClobClient.cancelOrder).toHaveBeenCalledWith({ orderID: "o1" });
    expect(mockAppendTradeHistory).toHaveBeenCalledWith(expect.objectContaining({ status: "partial" }));
    expect(mockRemovePendingOrder).toHaveBeenCalledWith("o1");
  });

  it("PARTIAL + cancel failure: accounts partial fill immediately and keeps pending", async () => {
    mockVerifyOrderFill.mockResolvedValue({ status: "PARTIAL", filledShares: 5, filledUsd: 2.55, fillPrice: 0.51 });
    mockClobClient.cancelOrder = vi.fn().mockRejectedValue(new Error("cancel failed"));

    await processVerifications([makePending()], mockClobClient);

    expect(mockAdjustPlacement).not.toHaveBeenCalled();
    expect(mockRecordBuy).toHaveBeenCalledWith("tok-1", 5, 0.51, "cond-1", "Test");
    expect(mockUpdatePendingOrder).toHaveBeenCalledWith("o1", expect.objectContaining({
      accountedFilledShares: 5,
      accountedFilledUsd: 2.55,
      uncertainCycles: 1,
    }));
    expect(mockRemovePendingOrder).not.toHaveBeenCalled();
  });

  it("FILLED after accounted partial: records only delta fill before removing", async () => {
    mockVerifyOrderFill.mockResolvedValue({ status: "FILLED", filledShares: 8, filledUsd: 4.08, fillPrice: 0.51 });

    await processVerifications([
      makePending("o1", { accountedFilledShares: 5, accountedFilledUsd: 2.55 }),
    ], mockClobClient);

    expect(mockRecordBuy).toHaveBeenCalledWith("tok-1", 3, 0.51, "cond-1", "Test");
    expect(mockAdjustPlacement).toHaveBeenCalledWith("cond-1", 10, 4.08, "BUY");
    expect(mockRemovePendingOrder).toHaveBeenCalledWith("o1");
  });

  it("UNFILLED: reverses full risk, cancels order, sends telegram", async () => {
    mockVerifyOrderFill.mockResolvedValue({ status: "UNFILLED", filledShares: 0, filledUsd: 0, fillPrice: 0 });
    const { telegram } = await import("../telegram-notifier");
    await processVerifications([makePending()], mockClobClient);

    expect(mockAdjustPlacement).toHaveBeenCalledWith("cond-1", 10, 0, "BUY");
    expect(mockClobClient.cancelOrder).toHaveBeenCalledWith({ orderID: "o1" });
    expect(telegram.tradeUnfilled).toHaveBeenCalledWith("Test");
    expect(mockRecordBuy).not.toHaveBeenCalled();
  });

  it("UNKNOWN: cancels order + reverses full risk (cancel-on-verification policy)", async () => {
    mockVerifyOrderFill.mockResolvedValue({ status: "UNKNOWN", filledShares: 0, filledUsd: 0, fillPrice: 0 });
    await processVerifications([makePending()], mockClobClient);

    expect(mockClobClient.cancelOrder).toHaveBeenCalledWith({ orderID: "o1" });
    expect(mockAdjustPlacement).toHaveBeenCalledWith("cond-1", 10, 0, "BUY");
    expect(mockAppendTradeHistory).toHaveBeenCalledWith(expect.objectContaining({ status: "unknown" }));
    expect(mockRemovePendingOrder).toHaveBeenCalledWith("o1");
  });

  it("UNFILLED + cancel failure: keeps pending and does not reverse risk", async () => {
    mockVerifyOrderFill.mockResolvedValue({ status: "UNFILLED", filledShares: 0, filledUsd: 0, fillPrice: 0 });
    mockClobClient.cancelOrder = vi.fn().mockRejectedValue(new Error("cancel failed"));

    await processVerifications([makePending()], mockClobClient);

    expect(mockAdjustPlacement).not.toHaveBeenCalled();
    expect(mockUpdatePendingOrder).toHaveBeenCalledWith("o1", expect.objectContaining({ uncertainCycles: 1 }));
    expect(mockRemovePendingOrder).not.toHaveBeenCalled();
  });

  it("verification error: cancels order + reverses risk", async () => {
    mockVerifyOrderFill.mockRejectedValue(new Error("RPC timeout"));
    await processVerifications([makePending()], mockClobClient);

    expect(mockClobClient.cancelOrder).toHaveBeenCalled();
    expect(mockAdjustPlacement).toHaveBeenCalledWith("cond-1", 10, 0, "BUY");
    expect(mockRemovePendingOrder).toHaveBeenCalledWith("o1");
  });

  it("verification error + cancel failure: keeps pending and does not reverse risk", async () => {
    mockVerifyOrderFill.mockRejectedValue(new Error("RPC timeout"));
    mockClobClient.cancelOrder = vi.fn().mockRejectedValue(new Error("cancel failed"));

    await processVerifications([makePending()], mockClobClient);

    expect(mockAdjustPlacement).not.toHaveBeenCalled();
    expect(mockRemovePendingOrder).not.toHaveBeenCalled();
  });

  it("stuck pending hits retry limit and is removed for manual review", async () => {
    mockVerifyOrderFill.mockResolvedValue({ status: "UNFILLED", filledShares: 0, filledUsd: 0, fillPrice: 0 });
    mockClobClient.cancelOrder = vi.fn().mockRejectedValue(new Error("cancel failed"));
    const { telegram } = await import("../telegram-notifier");

    await processVerifications([makePending("o1", { uncertainCycles: 4 })], mockClobClient);

    expect(mockRemovePendingOrder).toHaveBeenCalledWith("o1");
    expect(telegram.tradeFailed).toHaveBeenCalledWith("Test", expect.stringContaining("Manual review required"));
    expect(mockAppendTradeHistory).toHaveBeenCalledWith(expect.objectContaining({ status: "failed", orderId: "o1" }));
  });

  it("multiple orders: processes all, removes each from queue", async () => {
    mockVerifyOrderFill
      .mockResolvedValueOnce({ status: "FILLED", filledShares: 10, filledUsd: 5, fillPrice: 0.5 })
      .mockResolvedValueOnce({ status: "UNFILLED", filledShares: 0, filledUsd: 0, fillPrice: 0 });

    await processVerifications([makePending("o1"), makePending("o2")], mockClobClient);

    expect(mockRemovePendingOrder).toHaveBeenCalledTimes(2);
    expect(mockRemovePendingOrder).toHaveBeenCalledWith("o1");
    expect(mockRemovePendingOrder).toHaveBeenCalledWith("o2");
  });

  it("timing fields propagate to history records", async () => {
    mockVerifyOrderFill.mockResolvedValue({ status: "FILLED", filledShares: 10, filledUsd: 5, fillPrice: 0.5 });
    const order = makePending("o1", { source: "onchain" });
    await processVerifications([order], mockClobClient);

    expect(mockAppendTradeHistory).toHaveBeenCalledWith(expect.objectContaining({
      sourceDetectedAt: order.sourceDetectedAt,
      enqueuedAt: order.enqueuedAt,
      orderSubmittedAt: order.orderSubmittedAt,
      source: "onchain",
    }));
  });
});

describe("recoverPendingOrders", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockClobClient.cancelOrder = vi.fn().mockResolvedValue(undefined);
  });

  it("no pending orders: does nothing", async () => {
    mockLoadFromDisk.mockReturnValue([]);
    await recoverPendingOrders(mockClobClient);

    expect(mockVerifyOrderFill).not.toHaveBeenCalled();
    expect(mockClearDisk).not.toHaveBeenCalled();
  });

  it("FILLED recovery: adjusts risk, records inventory, marks seen", async () => {
    mockLoadFromDisk.mockReturnValue([makePending("crashed-1")]);
    mockVerifyOrderFill.mockResolvedValue({ status: "FILLED", filledShares: 10, filledUsd: 5, fillPrice: 0.5 });

    await recoverPendingOrders(mockClobClient);

    expect(mockAdjustPlacement).toHaveBeenCalledWith("cond-1", 10, 5, "BUY");
    expect(mockRecordBuy).toHaveBeenCalled();
    expect(mockMarkTradeAsSeen).toHaveBeenCalledWith("t1");
    expect(mockClearDisk).toHaveBeenCalled();
  });

  it("UNFILLED recovery: cancels + reverses + marks seen", async () => {
    mockLoadFromDisk.mockReturnValue([makePending("crashed-2")]);
    mockVerifyOrderFill.mockResolvedValue({ status: "UNFILLED", filledShares: 0, filledUsd: 0, fillPrice: 0 });

    await recoverPendingOrders(mockClobClient);

    expect(mockClobClient.cancelOrder).toHaveBeenCalledWith({ orderID: "crashed-2" });
    expect(mockAdjustPlacement).toHaveBeenCalledWith("cond-1", 10, 0, "BUY");
    expect(mockMarkTradeAsSeen).toHaveBeenCalledWith("t1");
    expect(mockClearDisk).toHaveBeenCalled();
  });

  it("unverifiable recovery: cancels + reverses (best effort)", async () => {
    mockLoadFromDisk.mockReturnValue([makePending("crashed-3")]);
    mockVerifyOrderFill.mockRejectedValue(new Error("API down"));

    await recoverPendingOrders(mockClobClient);

    expect(mockClobClient.cancelOrder).toHaveBeenCalled();
    expect(mockAdjustPlacement).toHaveBeenCalledWith("cond-1", 10, 0, "BUY");
    expect(mockMarkTradeAsSeen).toHaveBeenCalledWith("t1");
    expect(mockClearDisk).toHaveBeenCalled();
  });

  it("recovery keeps survivor when cancel fails", async () => {
    const order = makePending("crashed-4");
    mockLoadFromDisk.mockReturnValue([order]);
    mockVerifyOrderFill.mockResolvedValue({ status: "UNFILLED", filledShares: 0, filledUsd: 0, fillPrice: 0 });
    mockClobClient.cancelOrder = vi.fn().mockRejectedValue(new Error("cancel failed"));

    await recoverPendingOrders(mockClobClient);

    expect(mockAdjustPlacement).not.toHaveBeenCalled();
    expect(mockMarkTradeAsSeen).not.toHaveBeenCalled();
    expect(mockClearDisk).not.toHaveBeenCalled();
    expect(mockReplacePendingOrders).toHaveBeenCalledWith([expect.objectContaining({ uncertainCycles: 1 })]);
  });

  it("recovery keeps accounted partial fill when cancel fails", async () => {
    mockLoadFromDisk.mockReturnValue([makePending("crashed-5")]);
    mockVerifyOrderFill.mockResolvedValue({ status: "PARTIAL", filledShares: 5, filledUsd: 2.55, fillPrice: 0.51 });
    mockClobClient.cancelOrder = vi.fn().mockRejectedValue(new Error("cancel failed"));

    await recoverPendingOrders(mockClobClient);

    expect(mockRecordBuy).toHaveBeenCalledWith("tok-1", 5, 0.51, "cond-1", "Test");
    expect(mockReplacePendingOrders).toHaveBeenCalledWith([
      expect.objectContaining({
        accountedFilledShares: 5,
        accountedFilledUsd: 2.55,
        uncertainCycles: 1,
      }),
    ]);
  });

  it("recovery partial abandon at retry limit marks trade as seen", async () => {
    mockLoadFromDisk.mockReturnValue([makePending("crashed-5b", { uncertainCycles: 4 })]);
    mockVerifyOrderFill.mockResolvedValue({ status: "PARTIAL", filledShares: 5, filledUsd: 2.55, fillPrice: 0.51 });
    mockClobClient.cancelOrder = vi.fn().mockRejectedValue(new Error("cancel failed"));

    await recoverPendingOrders(mockClobClient);

    expect(mockMarkTradeAsSeen).toHaveBeenCalledWith("t1");
    expect(mockReplacePendingOrders).not.toHaveBeenCalled();
    expect(mockClearDisk).toHaveBeenCalledTimes(1);
  });

  it("recovery terminal-unknown abandon at retry limit marks trade as seen", async () => {
    mockLoadFromDisk.mockReturnValue([makePending("crashed-5c", { uncertainCycles: 4 })]);
    mockVerifyOrderFill.mockResolvedValue({ status: "UNFILLED", filledShares: 0, filledUsd: 0, fillPrice: 0 });
    mockClobClient.cancelOrder = vi.fn().mockRejectedValue(new Error("cancel failed"));

    await recoverPendingOrders(mockClobClient);

    expect(mockMarkTradeAsSeen).toHaveBeenCalledWith("t1");
    expect(mockReplacePendingOrders).not.toHaveBeenCalled();
    expect(mockClearDisk).toHaveBeenCalledTimes(1);
  });

  it("recovery abandons stuck pending at retry limit", async () => {
    mockLoadFromDisk.mockReturnValue([makePending("crashed-6", { uncertainCycles: 4 })]);
    mockVerifyOrderFill.mockRejectedValue(new Error("API down"));
    mockClobClient.cancelOrder = vi.fn().mockRejectedValue(new Error("cancel failed"));
    const { telegram } = await import("../telegram-notifier");

    await recoverPendingOrders(mockClobClient);

    expect(mockReplacePendingOrders).not.toHaveBeenCalled();
    expect(mockClearDisk).toHaveBeenCalledTimes(1);
    expect(mockMarkTradeAsSeen).toHaveBeenCalledWith("t1");
    expect(telegram.tradeFailed).toHaveBeenCalledWith("Test", expect.stringContaining("Manual review required"));
  });

  it("multiple pending: recovers all, clears disk once at end", async () => {
    mockLoadFromDisk.mockReturnValue([makePending("c1"), makePending("c2")]);
    mockVerifyOrderFill
      .mockResolvedValueOnce({ status: "FILLED", filledShares: 10, filledUsd: 5, fillPrice: 0.5 })
      .mockResolvedValueOnce({ status: "UNFILLED", filledShares: 0, filledUsd: 0, fillPrice: 0 });

    await recoverPendingOrders(mockClobClient);

    expect(mockMarkTradeAsSeen).toHaveBeenCalledTimes(2);
    expect(mockClearDisk).toHaveBeenCalledTimes(1);
    expect(mockReplacePendingOrders).not.toHaveBeenCalled();
  });
});
