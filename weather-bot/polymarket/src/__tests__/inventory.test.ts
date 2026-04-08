import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("fs", () => ({
  default: {
    existsSync: vi.fn(() => false),
    mkdirSync: vi.fn(),
    readFileSync: vi.fn(),
    writeFileSync: vi.fn(),
    renameSync: vi.fn(),
    unlinkSync: vi.fn(),
  },
}));

vi.mock("axios", () => ({
  default: {
    get: vi.fn(),
    isAxiosError: vi.fn(() => false),
  },
}));

vi.mock("../config", () => ({
  CONFIG: {
    dataApiUrl: "https://data-api.test",
    proxyWallet: "0xtest",
  },
}));

vi.mock("../logger", () => ({
  logger: {
    debug: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  },
}));

import fs from "fs";
import axios from "axios";
import {
  weightedAvgPrice,
  recordBuy,
  recordSell,
  hasPosition,
  getPosition,
  getInventorySummary,
  syncInventoryFromApi,
} from "../inventory";

// Module-level inventory persists — use unique tokenIds per test

describe("inventory (real imports, mocked fs)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // -- weightedAvgPrice (pure) --

  it("weightedAvgPrice: equal weights → midpoint", () => {
    expect(weightedAvgPrice(10, 0.4, 10, 0.6)).toBeCloseTo(0.5);
  });

  it("weightedAvgPrice: 0 existing → new price", () => {
    expect(weightedAvgPrice(0, 0, 10, 0.5)).toBeCloseTo(0.5);
  });

  it("weightedAvgPrice: 0 new → existing price", () => {
    expect(weightedAvgPrice(10, 0.5, 0, 0)).toBeCloseTo(0.5);
  });

  it("weightedAvgPrice: uneven weights", () => {
    expect(weightedAvgPrice(3, 0.2, 7, 0.8)).toBeCloseTo(0.62);
  });

  it("weightedAvgPrice: both zero → returns 0", () => {
    expect(weightedAvgPrice(0, 0, 0, 0)).toBe(0);
  });

  // -- recordBuy / hasPosition / getPosition --

  it("recordBuy creates new position", () => {
    recordBuy("inv-new-1", 10, 0.5, "cond-1", "Market A");
    expect(hasPosition("inv-new-1")).toBe(true);
    expect(getPosition("inv-new-1")).toEqual({
      shares: 10,
      avgPrice: 0.5,
      marketKey: "cond-1",
      market: "Market A",
      tokenId: "inv-new-1",
    });
  });

  it("recordBuy averages price on existing position", () => {
    recordBuy("inv-avg-1", 10, 0.4, "c", "M");
    recordBuy("inv-avg-1", 10, 0.6, "c", "M");
    const pos = getPosition("inv-avg-1")!;
    expect(pos.shares).toBe(20);
    expect(pos.avgPrice).toBeCloseTo(0.5);
  });

  it("recordBuy persists to disk via fs.writeFileSync", () => {
    recordBuy("inv-disk-1", 5, 0.5, "c", "M");
    expect(fs.writeFileSync).toHaveBeenCalled();
  });

  it("hasPosition returns false for unknown token", () => {
    expect(hasPosition("inv-unknown-1")).toBe(false);
  });

  it("getPosition returns null for unknown token", () => {
    expect(getPosition("inv-unknown-2")).toBeNull();
  });

  // -- recordSell --

  it("recordSell reduces shares partially", () => {
    recordBuy("inv-sell-1", 10, 0.5, "c", "M");
    recordSell("inv-sell-1", 3);
    expect(getPosition("inv-sell-1")!.shares).toBe(7);
  });

  it("recordSell full amount removes position", () => {
    recordBuy("inv-sell-2", 10, 0.5, "c", "M");
    recordSell("inv-sell-2", 10);
    expect(hasPosition("inv-sell-2")).toBe(false);
    expect(getPosition("inv-sell-2")).toBeNull();
  });

  it("recordSell more than held clamps and removes", () => {
    recordBuy("inv-sell-3", 5, 0.5, "c", "M");
    recordSell("inv-sell-3", 100);
    expect(hasPosition("inv-sell-3")).toBe(false);
  });

  it("recordSell on unknown token is no-op", () => {
    expect(() => recordSell("inv-sell-never", 10)).not.toThrow();
  });

  // -- getInventorySummary --

  it("getInventorySummary includes position info", () => {
    recordBuy("inv-sum-1", 10, 0.5, "c", "SummaryMarket");
    const summary = getInventorySummary();
    expect(summary).toContain("SummaryMarket");
    expect(summary).toContain("10.00");
  });

  it("getInventorySummary returns 'No open positions' when empty", async () => {
    vi.resetModules();
    const fresh = await import("../inventory");
    expect(fresh.getInventorySummary()).toBe("No open positions");
  });

  // -- syncInventoryFromApi (isolated via resetModules to avoid shared state) --

  describe("syncInventoryFromApi", () => {
    let fresh: typeof import("../inventory");

    beforeEach(async () => {
      vi.resetModules();
      vi.clearAllMocks();
      fresh = await import("../inventory");
    });

    it("replaces local inventory with API positions", async () => {
      fresh.recordBuy("inv-sync-old", 5, 0.3, "c", "Old");
      vi.mocked(axios.get).mockResolvedValueOnce({
        data: [{ asset: "api-tok-1", size: 20, avgPrice: 0.6, conditionId: "c1", title: "API Market" }],
      });
      await fresh.syncInventoryFromApi();
      expect(fresh.hasPosition("inv-sync-old")).toBe(false);
      expect(fresh.hasPosition("api-tok-1")).toBe(true);
      expect(fresh.getPosition("api-tok-1")).toEqual({
        shares: 20,
        avgPrice: 0.6,
        marketKey: "c1",
        market: "API Market",
        tokenId: "api-tok-1",
      });
    });

    it("skips non-array response", async () => {
      fresh.recordBuy("inv-sync-safe", 5, 0.3, "c", "Safe");
      vi.mocked(axios.get).mockResolvedValueOnce({ data: "not array" });
      await fresh.syncInventoryFromApi();
      expect(fresh.hasPosition("inv-sync-safe")).toBe(true);
    });

    it("keeps local state on API error", async () => {
      fresh.recordBuy("inv-sync-err", 5, 0.3, "c", "Kept");
      vi.mocked(axios.get).mockRejectedValueOnce(new Error("network"));
      await fresh.syncInventoryFromApi();
      expect(fresh.hasPosition("inv-sync-err")).toBe(true);
    });

    it("filters out zero-size positions", async () => {
      vi.mocked(axios.get).mockResolvedValueOnce({
        data: [{ asset: "zero-tok", size: 0, avgPrice: 0.5, conditionId: "c", title: "Zero" }],
      });
      await fresh.syncInventoryFromApi();
      expect(fresh.hasPosition("zero-tok")).toBe(false);
    });

    it("parses string size and handles null avgPrice from API", async () => {
      vi.mocked(axios.get).mockResolvedValueOnce({
        data: [
          { asset: "str-tok", size: "15.5", avgPrice: null, conditionId: "c1", title: "Parsed" },
          { asset: "nan-tok", size: "not-a-number", avgPrice: 0.5, conditionId: "c2", title: "Bad" },
        ],
      });
      await fresh.syncInventoryFromApi();
      expect(fresh.hasPosition("str-tok")).toBe(true);
      expect(fresh.getPosition("str-tok")).toEqual({
        shares: 15.5,
        avgPrice: 0,
        marketKey: "c1",
        market: "Parsed",
        tokenId: "str-tok",
      });
      expect(fresh.hasPosition("nan-tok")).toBe(false); // NaN size filtered
    });

    it("rejects Infinity size and avgPrice from API", async () => {
      vi.mocked(axios.get).mockResolvedValueOnce({
        data: [
          { asset: "inf-tok", size: "Infinity", avgPrice: "Infinity", conditionId: "c", title: "Inf" },
        ],
      });
      await fresh.syncInventoryFromApi();
      expect(fresh.hasPosition("inf-tok")).toBe(false);
    });

    it("handles missing asset field gracefully", async () => {
      vi.mocked(axios.get).mockResolvedValueOnce({
        data: [{ size: 10, avgPrice: 0.5, conditionId: "c" }],
      });
      await fresh.syncInventoryFromApi();
      // No position created — no asset key
      expect(fresh.getInventorySummary()).toBe("No open positions");
    });
  });
});
