import { describe, it, expect, vi, beforeEach } from "vitest";
import type { ClobClient } from "@polymarket/clob-client";
import type { DetectedTrade } from "../trade-monitor";

vi.mock("../logger", () => ({
  logger: { trade: vi.fn(), debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

const mockGetPosition = vi.fn();
vi.mock("../inventory", () => ({
  getPosition: (...args: unknown[]) => mockGetPosition(...args),
}));

import { executeCopyOrder } from "../order-executor";
import type { MarketSnapshot } from "../market-price";

function makeTrade(overrides: Partial<DetectedTrade> = {}): DetectedTrade {
  return {
    id: "t1",
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

function makeClobClient(overrides: Record<string, unknown> = {}) {
  return {
    createOrder: vi.fn().mockResolvedValue({ signedOrder: true }),
    postOrder: vi.fn().mockResolvedValue({ orderID: "order-1" }),
    ...overrides,
  } as unknown as ClobClient;
}

describe("order-executor executeCopyOrder", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("BUY: calculates shares = copySize / buffered price", async () => {
    const client = makeClobClient();
    const result = await executeCopyOrder(client, makeTrade({ price: 0.5 }), 10);
    // orderPrice = roundCents(min(0.99, 0.5 * 1.02)) = 0.51
    // shares = roundCents(10 / 0.51) = 19.61
    expect(result.orderPrice).toBeCloseTo(0.51, 1);
    expect(result.shares).toBeCloseTo(19.61, 0);
  });

  it("BUY: caps order price at 0.99", async () => {
    const client = makeClobClient();
    const result = await executeCopyOrder(client, makeTrade({ price: 0.98 }), 10);
    // 0.98 * 1.02 = 0.9996 → min(0.99, 0.9996) = 0.99
    expect(result.orderPrice).toBe(0.99);
  });

  it("SELL: applies 2% discount, floors at 0.01", async () => {
    mockGetPosition.mockReturnValue({ shares: 100, avgPrice: 0.5 });
    const client = makeClobClient();
    const result = await executeCopyOrder(client, makeTrade({ price: 0.5, side: "SELL" }), 10);
    // orderPrice = max(0.01, 0.5 * 0.98) = 0.49
    expect(result.orderPrice).toBe(0.49);
  });

  it("SELL: caps shares at position size", async () => {
    mockGetPosition.mockReturnValue({ shares: 5, avgPrice: 0.5 });
    const client = makeClobClient();
    const result = await executeCopyOrder(client, makeTrade({ price: 0.5, side: "SELL" }), 100);
    expect(result.shares).toBe(5);
  });

  it("SELL: throws when no position exists", async () => {
    mockGetPosition.mockReturnValue(null);
    const client = makeClobClient();
    await expect(executeCopyOrder(client, makeTrade({ side: "SELL" }), 10)).rejects.toThrow(
      "No shares to sell",
    );
  });

  it("SELL: throws when position has 0 shares", async () => {
    mockGetPosition.mockReturnValue({ shares: 0, avgPrice: 0.5 });
    const client = makeClobClient();
    await expect(executeCopyOrder(client, makeTrade({ side: "SELL" }), 10)).rejects.toThrow(
      "No shares to sell",
    );
  });

  it("throws when no tokenId", async () => {
    const client = makeClobClient();
    await expect(executeCopyOrder(client, makeTrade({ tokenId: "" }), 10)).rejects.toThrow(
      "No tokenId",
    );
  });

  it("returns orderId from CLOB response object", async () => {
    const client = makeClobClient({
      postOrder: vi.fn().mockResolvedValue({ orderID: "abc-123" }),
    });
    const result = await executeCopyOrder(client, makeTrade(), 10);
    expect(result.orderId).toBe("abc-123");
  });

  it("handles string response from CLOB", async () => {
    const client = makeClobClient({
      postOrder: vi.fn().mockResolvedValue("str-order-id"),
    });
    const result = await executeCopyOrder(client, makeTrade(), 10);
    expect(result.orderId).toBe("str-order-id");
  });

  it("calls createOrder then postOrder in sequence", async () => {
    const createOrder = vi.fn().mockResolvedValue({ signed: true });
    const postOrder = vi.fn().mockResolvedValue({ orderID: "o1" });
    const client = makeClobClient({ createOrder, postOrder });
    await executeCopyOrder(client, makeTrade(), 10);
    expect(createOrder).toHaveBeenCalledTimes(1);
    expect(postOrder).toHaveBeenCalledWith({ signed: true });
  });

  // --- Adaptive pricing tests ---

  it("BUY with snapshot: uses bestAsk when within 2% cap", async () => {
    const client = makeClobClient();
    const snapshot: MarketSnapshot = {
      bestBid: 0.49, bestAsk: 0.51, midpoint: 0.50,
      spread: 0.02, spreadBps: 400, fetchedAt: Date.now(),
    };
    // traderPrice=0.50, bestAsk=0.51, cap=0.50*1.02=0.51 → use 0.51
    const result = await executeCopyOrder(client, makeTrade({ price: 0.50 }), 10, snapshot);
    expect(result.orderPrice).toBe(0.51);
  });

  it("BUY with snapshot: caps at trader+2% when bestAsk too high", async () => {
    const client = makeClobClient();
    const snapshot: MarketSnapshot = {
      bestBid: 0.55, bestAsk: 0.60, midpoint: 0.575,
      spread: 0.05, spreadBps: 870, fetchedAt: Date.now(),
    };
    // traderPrice=0.50, bestAsk=0.60, cap=0.50*1.02=0.51 → use 0.51
    const result = await executeCopyOrder(client, makeTrade({ price: 0.50 }), 10, snapshot);
    expect(result.orderPrice).toBe(0.51);
  });

  it("BUY without snapshot: uses fixed 2% buffer", async () => {
    const client = makeClobClient();
    const result = await executeCopyOrder(client, makeTrade({ price: 0.50 }), 10, null);
    expect(result.orderPrice).toBe(0.51);
  });

  it("SELL with snapshot: uses bestBid when within 2% floor", async () => {
    mockGetPosition.mockReturnValue({ shares: 100, avgPrice: 0.5 });
    const client = makeClobClient();
    const snapshot: MarketSnapshot = {
      bestBid: 0.49, bestAsk: 0.51, midpoint: 0.50,
      spread: 0.02, spreadBps: 400, fetchedAt: Date.now(),
    };
    // traderPrice=0.50, bestBid=0.49, floor=0.50*0.98=0.49 → use 0.49
    const result = await executeCopyOrder(client, makeTrade({ price: 0.50, side: "SELL" }), 10, snapshot);
    expect(result.orderPrice).toBe(0.49);
  });

  it("SELL without snapshot: uses fixed 2% buffer", async () => {
    mockGetPosition.mockReturnValue({ shares: 100, avgPrice: 0.5 });
    const client = makeClobClient();
    const result = await executeCopyOrder(client, makeTrade({ price: 0.50, side: "SELL" }), 10, null);
    expect(result.orderPrice).toBe(0.49);
  });
});
