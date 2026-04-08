import { describe, it, expect, vi, beforeEach } from "vitest";
import type { ClobClient } from "@polymarket/clob-client";

vi.mock("../logger", () => ({
  logger: { trade: vi.fn(), debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

import { fetchMarketSnapshot, computeDriftBps, MarketSnapshot, _clearSnapshotCache } from "../market-price";

function makeClobClient(overrides: Record<string, unknown> = {}) {
  return {
    getPrice: vi.fn().mockResolvedValue("0.50"),
    ...overrides,
  } as unknown as ClobClient;
}

function makeSnapshot(overrides: Partial<MarketSnapshot> = {}): MarketSnapshot {
  return {
    bestBid: 0.49,
    bestAsk: 0.51,
    midpoint: 0.50,
    spread: 0.02,
    spreadBps: 400,
    fetchedAt: Date.now(),
    ...overrides,
  };
}

describe("fetchMarketSnapshot", () => {
  beforeEach(() => { vi.clearAllMocks(); _clearSnapshotCache(); });

  it("returns valid snapshot from CLOB prices", async () => {
    const client = makeClobClient({
      getPrice: vi.fn()
        .mockResolvedValueOnce("0.49") // BUY side = best bid
        .mockResolvedValueOnce("0.51"), // SELL side = best ask
    });

    const snap = await fetchMarketSnapshot(client, "tok-1");

    expect(snap).not.toBeNull();
    expect(snap!.bestBid).toBe(0.49);
    expect(snap!.bestAsk).toBe(0.51);
    expect(snap!.midpoint).toBe(0.50);
    expect(snap!.spread).toBeCloseTo(0.02);
    expect(snap!.spreadBps).toBe(400);
    expect(snap!.fetchedAt).toBeGreaterThan(0);
  });

  it("returns null on timeout (>200ms)", async () => {
    const client = makeClobClient({
      getPrice: vi.fn().mockImplementation(
        () => new Promise((resolve) => setTimeout(() => resolve("0.50"), 300))
      ),
    });

    const snap = await fetchMarketSnapshot(client, "tok-1");
    expect(snap).toBeNull();
  });

  it("returns null on API error", async () => {
    const client = makeClobClient({
      getPrice: vi.fn().mockRejectedValue(new Error("CLOB 503")),
    });

    const snap = await fetchMarketSnapshot(client, "tok-1");
    expect(snap).toBeNull();
  });

  it("returns null on invalid data (NaN)", async () => {
    const client = makeClobClient({
      getPrice: vi.fn().mockResolvedValue("not-a-number"),
    });

    const snap = await fetchMarketSnapshot(client, "tok-1");
    expect(snap).toBeNull();
  });

  it("returns null when price is zero", async () => {
    const client = makeClobClient({
      getPrice: vi.fn().mockResolvedValue("0"),
    });

    const snap = await fetchMarketSnapshot(client, "tok-1");
    expect(snap).toBeNull();
  });

  it("handles object response { price: string } from SDK", async () => {
    const client = makeClobClient({
      getPrice: vi.fn()
        .mockResolvedValueOnce({ price: "0.49" })
        .mockResolvedValueOnce({ price: "0.51" }),
    });

    const snap = await fetchMarketSnapshot(client, "tok-obj");
    expect(snap).not.toBeNull();
    expect(snap!.bestBid).toBe(0.49);
    expect(snap!.bestAsk).toBe(0.51);
  });

  it("caches snapshot for same tokenId within TTL", async () => {
    const getPrice = vi.fn()
      .mockResolvedValueOnce("0.49")
      .mockResolvedValueOnce("0.51")
      .mockResolvedValueOnce("0.48") // would return different values if called again
      .mockResolvedValueOnce("0.52");
    const client = makeClobClient({ getPrice });

    const snap1 = await fetchMarketSnapshot(client, "tok-cache");
    const snap2 = await fetchMarketSnapshot(client, "tok-cache");

    // Second call should return cached result, not call API again
    expect(snap1).toEqual(snap2);
    expect(getPrice).toHaveBeenCalledTimes(2); // only 2 calls (BUY + SELL), not 4
  });

  it("returns null on crossed book (bid > ask)", async () => {
    const client = makeClobClient({
      getPrice: vi.fn()
        .mockResolvedValueOnce("0.55") // BUY = bid
        .mockResolvedValueOnce("0.50"), // SELL = ask (lower = crossed)
    });

    const snap = await fetchMarketSnapshot(client, "tok-1");
    expect(snap).toBeNull();
  });
});

describe("computeDriftBps", () => {
  it("BUY: positive drift when market moved up", () => {
    // traderPrice=0.50, ask moved to 0.52 → (0.52-0.50)/0.50 = 4% = 400bps
    const drift = computeDriftBps(0.50, makeSnapshot({ bestAsk: 0.52 }), "BUY");
    expect(drift).toBe(400);
  });

  it("BUY: negative drift when market moved down (favorable)", () => {
    // traderPrice=0.50, ask dropped to 0.48 → (0.48-0.50)/0.50 = -4% = -400bps
    const drift = computeDriftBps(0.50, makeSnapshot({ bestAsk: 0.48 }), "BUY");
    expect(drift).toBe(-400);
  });

  it("SELL: positive drift when market moved down", () => {
    // traderPrice=0.50, bid dropped to 0.48 → (0.50-0.48)/0.50 = 4% = 400bps
    const drift = computeDriftBps(0.50, makeSnapshot({ bestBid: 0.48 }), "SELL");
    expect(drift).toBe(400);
  });

  it("SELL: negative drift when market moved up (favorable)", () => {
    // traderPrice=0.50, bid rose to 0.52 → (0.50-0.52)/0.50 = -4% = -400bps
    const drift = computeDriftBps(0.50, makeSnapshot({ bestBid: 0.52 }), "SELL");
    expect(drift).toBe(-400);
  });

  it("returns 0 when traderPrice is 0", () => {
    expect(computeDriftBps(0, makeSnapshot(), "BUY")).toBe(0);
  });

  it("zero drift when prices match", () => {
    const drift = computeDriftBps(0.51, makeSnapshot({ bestAsk: 0.51 }), "BUY");
    expect(drift).toBe(0);
  });
});
