import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// Mock fs to prevent disk I/O during tests
vi.mock("fs", () => ({
  default: {
    existsSync: vi.fn(() => false),
    mkdirSync: vi.fn(),
    readFileSync: vi.fn(() => "[]"),
    writeFileSync: vi.fn(),
    appendFileSync: vi.fn(),
    renameSync: vi.fn(),
    unlinkSync: vi.fn(),
  },
}));

import fs from "fs";
import {
  isSeenTrade,
  markTradeAsSeen,
  incrementRetry,
  isMaxRetries,
  appendTradeHistory,
} from "../trade-store";

// Module-level state persists between tests — use unique IDs per test to avoid pollution

describe("trade-store (real imports, mocked fs)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("isSeenTrade returns false for unknown trade", () => {
    expect(isSeenTrade("ts-unseen-1")).toBe(false);
  });

  it("markTradeAsSeen makes isSeenTrade return true", () => {
    markTradeAsSeen("ts-mark-1");
    expect(isSeenTrade("ts-mark-1")).toBe(true);
  });

  it("markTradeAsSeen persists via fs.writeFileSync", () => {
    markTradeAsSeen("ts-persist-1");
    expect(fs.writeFileSync).toHaveBeenCalled();
  });

  it("markTradeAsSeen deletes retry count for that trade", () => {
    incrementRetry("ts-clean-1");
    incrementRetry("ts-clean-1");
    incrementRetry("ts-clean-1");
    expect(isMaxRetries("ts-clean-1")).toBe(true);
    markTradeAsSeen("ts-clean-1");
    expect(isMaxRetries("ts-clean-1")).toBe(false);
  });

  it("incrementRetry returns ascending count starting at 1", () => {
    expect(incrementRetry("ts-inc-1")).toBe(1);
    expect(incrementRetry("ts-inc-1")).toBe(2);
    expect(incrementRetry("ts-inc-1")).toBe(3);
  });

  it("isMaxRetries: false below 3, true at 3+", () => {
    expect(isMaxRetries("ts-max-1")).toBe(false);
    incrementRetry("ts-max-1");
    incrementRetry("ts-max-1");
    expect(isMaxRetries("ts-max-1")).toBe(false);
    incrementRetry("ts-max-1");
    expect(isMaxRetries("ts-max-1")).toBe(true);
  });

  it("retryCount evicts oldest entries when cap exceeded (preserves recent)", () => {
    // Fill past the 1000-entry cap
    for (let i = 0; i < 1002; i++) {
      incrementRetry(`overflow-${i}`);
    }
    // Recent entries should survive eviction
    expect(isMaxRetries("overflow-1001")).toBe(false); // count=1, not wiped
    incrementRetry("overflow-1001");
    incrementRetry("overflow-1001");
    expect(isMaxRetries("overflow-1001")).toBe(true); // count=3
  });

  it("appendTradeHistory writes JSONL to history file", () => {
    appendTradeHistory({
      timestamp: "2026-04-02T12:00:00Z",
      traderAddress: "0xabc",
      market: "Test Market",
      side: "BUY",
      traderSize: 100,
      copySize: 10,
      price: 0.5,
      status: "filled",
    });
    expect(fs.appendFileSync).toHaveBeenCalledWith(
      expect.stringContaining("trade-history.jsonl"),
      expect.stringContaining('"status":"filled"'),
    );
  });

  it("appendTradeHistory swallows IO errors without crashing", () => {
    vi.mocked(fs.appendFileSync).mockImplementationOnce(() => {
      throw new Error("disk full");
    });
    expect(() =>
      appendTradeHistory({
        timestamp: "",
        traderAddress: "",
        market: "",
        side: "BUY",
        traderSize: 0,
        copySize: 0,
        price: 0,
        status: "failed",
      }),
    ).not.toThrow();
  });

  it("atomicWrite falls back to direct write on rename failure", () => {
    vi.mocked(fs.renameSync).mockImplementationOnce(() => {
      throw new Error("EPERM");
    });
    markTradeAsSeen("ts-atomic-1");
    // writeFileSync called: (1) tmp write, (2) fallback direct write
    expect(vi.mocked(fs.writeFileSync).mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  describe("loadSeen on module init", () => {
    afterEach(() => {
      vi.mocked(fs.existsSync).mockReturnValue(false);
      vi.mocked(fs.readFileSync).mockReturnValue("[]");
    });

    it("restores persisted trades from JSON", async () => {
      vi.resetModules();
      vi.mocked(fs.existsSync).mockReturnValue(true);
      vi.mocked(fs.readFileSync).mockReturnValue(JSON.stringify(["restored-1", "restored-2"]));
      const fresh = await import("../trade-store");
      expect(fresh.isSeenTrade("restored-1")).toBe(true);
      expect(fresh.isSeenTrade("restored-2")).toBe(true);
      expect(fresh.isSeenTrade("not-there")).toBe(false);
    });

    it("starts fresh on corrupted JSON file", async () => {
      vi.resetModules();
      vi.mocked(fs.existsSync).mockReturnValue(true);
      vi.mocked(fs.readFileSync).mockReturnValue("{broken");
      const fresh = await import("../trade-store");
      expect(fresh.isSeenTrade("anything")).toBe(false);
    });
  });
});
