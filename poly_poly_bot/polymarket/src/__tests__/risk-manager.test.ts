import { describe, it, expect, vi } from "vitest";

// Mock config to avoid requiring env vars (risk-manager imports CONFIG at module level)
vi.mock("../config", () => ({
  CONFIG: {
    copyStrategy: "PERCENTAGE",
    copySize: 10,
    maxOrderSizeUsd: 100,
    minOrderSizeUsd: 1,
    maxPositionPerMarketUsd: 500,
    maxDailyVolumeUsd: 1000,
    maxTradeAgeHours: 1,
    previewMode: true,
    userAddresses: [],
    proxyWallet: "0x0000000000000000000000000000000000000000",
    signatureType: 0,
    telegramBotToken: "",
    telegramChatId: "",
    fetchInterval: 1000,
    fetchConcurrency: 5,
    maxPriceDriftBps: 300,
    maxSpreadBps: 500,
    maxCopiesPerMarketSide: 2,
    redeemIntervalHours: 0.5,
    tradeMonitorMode: "data-api",
    clobApiUrl: "",
    dataApiUrl: "",
    rpcUrl: "",
    chainId: 137,
  },
  getPrivateKey: () => "0".repeat(64),
}));

import { _evaluateTradeWithState, RiskState, RiskConfig, CopyDecision } from "../risk-manager";

function makeState(overrides: Partial<RiskState> = {}): RiskState {
  return {
    dailyVolumeUsd: 0,
    dailyVolumeDate: "2026-04-02",
    dailySpendByMarket: {},
    ...overrides,
  };
}

function makeConfig(overrides: Partial<RiskConfig> = {}): RiskConfig {
  return {
    copyStrategy: "PERCENTAGE",
    copySize: 10,
    maxOrderSizeUsd: 100,
    minOrderSizeUsd: 1,
    maxPositionPerMarketUsd: 500,
    maxDailyVolumeUsd: 1000,
    maxTradeAgeHours: 1,
    ...overrides,
  };
}

// Fixed "now" = 2026-04-02T12:00:00Z
const NOW = new Date("2026-04-02T12:00:00Z").getTime();
const FRESH_TS = new Date("2026-04-02T11:55:00Z").toISOString(); // 5 min ago
const OLD_TS = new Date("2026-04-02T10:00:00Z").toISOString(); // 2 hours ago

function evaluate(
  overrides: {
    state?: Partial<RiskState>;
    config?: Partial<RiskConfig>;
    traderOrderSize?: number;
    traderPrice?: number;
    tradeTimestamp?: string;
    marketKey?: string;
    usdcBalance?: number;
    side?: "BUY" | "SELL";
  } = {}
): CopyDecision {
  return _evaluateTradeWithState(
    makeState(overrides.state),
    makeConfig(overrides.config),
    overrides.traderOrderSize ?? 100,
    overrides.traderPrice ?? 0.5,
    overrides.tradeTimestamp ?? FRESH_TS,
    overrides.marketKey ?? "market-1",
    overrides.usdcBalance ?? 500,
    overrides.side ?? "BUY",
    NOW
  );
}

describe("risk-manager evaluateTrade", () => {
  it("accepts a valid trade", () => {
    const d = evaluate();
    expect(d.shouldCopy).toBe(true);
    expect(d.copySize).toBe(10); // 10% of 100
  });

  it("rejects NaN traderOrderSize", () => {
    const d = evaluate({ traderOrderSize: NaN });
    expect(d.shouldCopy).toBe(false);
    expect(d.reason).toContain("NaN");
  });

  it("rejects zero size", () => {
    const d = evaluate({ traderOrderSize: 0 });
    expect(d.shouldCopy).toBe(false);
  });

  it("rejects trade too old", () => {
    const d = evaluate({ tradeTimestamp: OLD_TS });
    expect(d.shouldCopy).toBe(false);
    expect(d.reason).toContain("too old");
  });

  it("accepts fresh trade", () => {
    const d = evaluate({ tradeTimestamp: FRESH_TS });
    expect(d.shouldCopy).toBe(true);
  });

  it("rejects when daily volume limit reached", () => {
    const d = evaluate({ state: { dailyVolumeUsd: 1000 } });
    expect(d.shouldCopy).toBe(false);
    expect(d.reason).toContain("Daily volume limit");
  });

  it("resets daily volume on new day", () => {
    const d = evaluate({ state: { dailyVolumeUsd: 1000, dailyVolumeDate: "2026-04-01" } });
    expect(d.shouldCopy).toBe(true);
  });

  it("calculates PERCENTAGE strategy correctly", () => {
    const d = evaluate({ traderOrderSize: 200, config: { copySize: 5 } });
    expect(d.copySize).toBe(10); // 5% of 200
  });

  it("calculates FIXED strategy correctly", () => {
    const d = evaluate({ config: { copyStrategy: "FIXED", copySize: 25 } });
    expect(d.copySize).toBe(25);
  });

  it("rejects below min order", () => {
    const d = evaluate({ traderOrderSize: 5, config: { copySize: 1, minOrderSizeUsd: 2 } });
    // 1% of 5 = 0.05
    expect(d.shouldCopy).toBe(false);
    expect(d.reason).toContain("below min");
  });

  it("caps above max order", () => {
    const d = evaluate({ traderOrderSize: 5000, config: { copySize: 50, maxOrderSizeUsd: 50 } });
    expect(d.shouldCopy).toBe(true);
    expect(d.copySize).toBeLessThanOrEqual(50);
  });

  it("rejects invalid price <= 0", () => {
    const d = evaluate({ traderPrice: 0 });
    expect(d.shouldCopy).toBe(false);
    expect(d.reason).toContain("Invalid price");
  });

  it("rejects invalid price >= 1", () => {
    const d = evaluate({ traderPrice: 1 });
    expect(d.shouldCopy).toBe(false);
    expect(d.reason).toContain("Invalid price");
  });

  it("rejects when per-market daily cap reached", () => {
    const d = evaluate({ state: { dailySpendByMarket: { "market-1": 500 } }, config: { maxPositionPerMarketUsd: 500 } });
    expect(d.shouldCopy).toBe(false);
    expect(d.reason).toContain("market cap");
  });

  it("reduces order to fit market cap room", () => {
    const d = evaluate({
      state: { dailySpendByMarket: { "market-1": 495 } },
      config: { maxPositionPerMarketUsd: 500, minOrderSizeUsd: 1 },
    });
    expect(d.shouldCopy).toBe(true);
    expect(d.copySize).toBeLessThanOrEqual(5);
  });

  it("reduces BUY to available balance", () => {
    const d = evaluate({ usdcBalance: 5, config: { copySize: 100, copyStrategy: "FIXED" } });
    expect(d.shouldCopy).toBe(true);
    expect(d.copySize).toBe(5);
  });

  it("does not check balance for SELL", () => {
    const d = evaluate({ usdcBalance: 0, side: "SELL", config: { copySize: 25, copyStrategy: "FIXED" } });
    expect(d.shouldCopy).toBe(true);
    expect(d.copySize).toBe(25);
  });

  it("rejects when balance below min order", () => {
    const d = evaluate({ usdcBalance: 0.5, config: { copySize: 50, copyStrategy: "FIXED", minOrderSizeUsd: 1 } });
    expect(d.shouldCopy).toBe(false);
    expect(d.reason).toContain("Insufficient USDC");
  });
});

// --- adjustPlacement tests ---
// Uses _adjustPlacementWithState (injectable) — same code path as production adjustPlacement.
import { _adjustPlacementWithState } from "../risk-manager";

describe("adjustPlacement", () => {
  it("FILLED: reduces dailyVolumeUsd by delta (optimistic=10, actual=9.8 → result 9.8)", () => {
    const s = makeState({ dailyVolumeUsd: 10, dailySpendByMarket: { "m1": 10 } });
    _adjustPlacementWithState(s, "m1", 10, 9.8, "BUY");
    // delta = 10 - 9.8 = 0.2 → volume drops from 10 to 9.8
    expect(s.dailyVolumeUsd).toBeCloseTo(9.8);
    expect(s.dailySpendByMarket["m1"]).toBeCloseTo(9.8);
  });

  it("UNFILLED: reverses full optimistic amount (actual=0)", () => {
    const s = makeState({ dailyVolumeUsd: 10, dailySpendByMarket: { "m1": 10 } });
    _adjustPlacementWithState(s, "m1", 10, 0, "BUY");
    expect(s.dailyVolumeUsd).toBe(0);
    expect(s.dailySpendByMarket["m1"]).toBe(0);
  });

  it("no-op when actual >= optimistic (delta <= 0)", () => {
    const s = makeState({ dailyVolumeUsd: 10, dailySpendByMarket: { "m1": 10 } });
    _adjustPlacementWithState(s, "m1", 10, 11, "BUY");
    expect(s.dailyVolumeUsd).toBe(10); // unchanged
    expect(s.dailySpendByMarket["m1"]).toBe(10);
  });

  it("clamps to zero on over-reversal", () => {
    const s = makeState({ dailyVolumeUsd: 3, dailySpendByMarket: { "m1": 3 } });
    _adjustPlacementWithState(s, "m1", 10, 0, "BUY");
    expect(s.dailyVolumeUsd).toBe(0);
    expect(s.dailySpendByMarket["m1"]).toBe(0);
  });

  it("SELL: reduces dailyVolumeUsd but NOT dailySpendByMarket", () => {
    const s = makeState({ dailyVolumeUsd: 10, dailySpendByMarket: { "m1": 5 } });
    _adjustPlacementWithState(s, "m1", 10, 0, "SELL");
    expect(s.dailyVolumeUsd).toBe(0);
    expect(s.dailySpendByMarket["m1"]).toBe(5); // untouched
  });

  it("UNFILLED frees budget for new trades (end-to-end with evaluate)", () => {
    // Volume at 10/10 — exhausted. Must use today's date or evaluate resets counters.
    const today = new Date().toISOString().slice(0, 10);
    const s = makeState({ dailyVolumeUsd: 10, dailyVolumeDate: today, dailySpendByMarket: {} });
    const cfg = makeConfig({ maxDailyVolumeUsd: 10, copyStrategy: "FIXED", copySize: 1, minOrderSizeUsd: 1 });
    // Budget exhausted — daily volume hit
    const before = _evaluateTradeWithState(s, cfg, 100, 0.5, new Date().toISOString(), "m2", 500, "BUY", Date.now());
    expect(before.shouldCopy).toBe(false);
    expect(before.reason).toContain("Daily volume");
    // UNFILLED reversal frees 10 from daily volume
    _adjustPlacementWithState(s, "m2", 10, 0, "BUY");
    const after = _evaluateTradeWithState(s, cfg, 100, 0.5, new Date().toISOString(), "m2", 500, "BUY", Date.now());
    expect(after.shouldCopy).toBe(true);
  });
});
