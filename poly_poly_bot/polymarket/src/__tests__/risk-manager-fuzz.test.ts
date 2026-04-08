import { describe, it, expect, vi } from "vitest";

// Mock config to avoid requiring env vars
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

import { _evaluateTradeWithState, RiskState, RiskConfig } from "../risk-manager";

// Property-based / fuzz tests: verify invariants hold for ANY combination of inputs.
// No combination should crash, return NaN, or violate bounds.

function makeState(): RiskState {
  return { dailyVolumeUsd: 0, dailyVolumeDate: "2026-04-02", dailySpendByMarket: {} };
}

function makeConfig(): RiskConfig {
  return {
    copyStrategy: "PERCENTAGE",
    copySize: 10,
    maxOrderSizeUsd: 100,
    minOrderSizeUsd: 1,
    maxPositionPerMarketUsd: 500,
    maxDailyVolumeUsd: 1000,
    maxTradeAgeHours: 1,
  };
}

const NOW = new Date("2026-04-02T12:00:00Z").getTime();
const FRESH = new Date("2026-04-02T11:55:00Z").toISOString();

// Seeded pseudo-random for determinism
function seededRandom(seed: number): () => number {
  return () => {
    seed = (seed * 16807) % 2147483647;
    return (seed - 1) / 2147483646;
  };
}

describe("risk-manager property-based tests", () => {
  const rand = seededRandom(42);

  it("never returns NaN copySize for any input combination", () => {
    for (let i = 0; i < 500; i++) {
      const traderOrderSize = rand() * 10000 - 1000; // includes negatives
      const traderPrice = rand() * 2 - 0.5; // includes <0 and >1
      const usdcBalance = rand() * 1000 - 100; // includes negatives
      const side = rand() > 0.5 ? "BUY" : "SELL" as const;

      const d = _evaluateTradeWithState(
        makeState(), makeConfig(),
        traderOrderSize, traderPrice, FRESH, "mkt", usdcBalance, side, NOW
      );

      expect(Number.isNaN(d.copySize)).toBe(false);
      expect(Number.isFinite(d.copySize)).toBe(true);
    }
  });

  it("never crashes for any input combination", () => {
    const edgeCases = [
      { size: 0, price: 0, balance: 0 },
      { size: -1, price: -1, balance: -1 },
      { size: Infinity, price: Infinity, balance: Infinity },
      { size: -Infinity, price: -Infinity, balance: -Infinity },
      { size: NaN, price: NaN, balance: NaN },
      { size: Number.MAX_SAFE_INTEGER, price: 0.999999, balance: Number.MAX_SAFE_INTEGER },
      { size: Number.MIN_VALUE, price: Number.MIN_VALUE, balance: Number.MIN_VALUE },
    ];

    for (const { size, price, balance } of edgeCases) {
      expect(() => {
        _evaluateTradeWithState(makeState(), makeConfig(), size, price, FRESH, "mkt", balance, "BUY", NOW);
      }).not.toThrow();
    }
  });

  it("copySize never exceeds maxOrderSizeUsd when shouldCopy is true", () => {
    const config = makeConfig();
    for (let i = 0; i < 500; i++) {
      const traderOrderSize = rand() * 5000;
      const traderPrice = 0.01 + rand() * 0.98; // valid range
      const usdcBalance = rand() * 2000;

      const d = _evaluateTradeWithState(
        makeState(), config,
        traderOrderSize, traderPrice, FRESH, `mkt-${i}`, usdcBalance, "BUY", NOW
      );

      if (d.shouldCopy) {
        expect(d.copySize).toBeLessThanOrEqual(config.maxOrderSizeUsd);
        expect(d.copySize).toBeGreaterThanOrEqual(config.minOrderSizeUsd);
      }
    }
  });

  it("copySize never exceeds available balance for BUY when shouldCopy is true", () => {
    for (let i = 0; i < 500; i++) {
      const traderOrderSize = 50 + rand() * 500;
      const traderPrice = 0.1 + rand() * 0.8;
      const usdcBalance = 1 + rand() * 50; // small balances

      const d = _evaluateTradeWithState(
        makeState(), makeConfig(),
        traderOrderSize, traderPrice, FRESH, `mkt-${i}`, usdcBalance, "BUY", NOW
      );

      if (d.shouldCopy) {
        expect(d.copySize).toBeLessThanOrEqual(usdcBalance + 0.01); // +0.01 for rounding
      }
    }
  });

  it("SELL trades ignore balance check", () => {
    const config = { ...makeConfig(), copyStrategy: "FIXED" as const, copySize: 25 };

    const d = _evaluateTradeWithState(
      makeState(), config,
      100, 0.5, FRESH, "mkt", 0, "SELL", NOW
    );

    expect(d.shouldCopy).toBe(true);
    expect(d.copySize).toBe(25);
  });

  it("rejects all trades with invalid prices (<=0 or >=1)", () => {
    const invalidPrices = [0, -0.5, 1, 1.5, 2, -100, 100];
    for (const price of invalidPrices) {
      const d = _evaluateTradeWithState(
        makeState(), makeConfig(),
        100, price, FRESH, "mkt", 500, "BUY", NOW
      );
      expect(d.shouldCopy).toBe(false);
    }
  });

  it("daily volume never exceeded across sequential evaluations", () => {
    const state = makeState();
    const config = { ...makeConfig(), maxDailyVolumeUsd: 50, copyStrategy: "FIXED" as const, copySize: 10 };
    let totalApproved = 0;

    for (let i = 0; i < 100; i++) {
      const d = _evaluateTradeWithState(
        state, config,
        100, 0.5, FRESH, `mkt-${i}`, 500, "BUY", NOW
      );

      if (d.shouldCopy) {
        totalApproved += d.copySize;
        // Simulate recordPlacement
        state.dailyVolumeUsd += d.copySize;
        state.dailySpendByMarket[`mkt-${i}`] = (state.dailySpendByMarket[`mkt-${i}`] || 0) + d.copySize;
      }
    }

    expect(totalApproved).toBeLessThanOrEqual(config.maxDailyVolumeUsd);
  });

  it("per-market cap never exceeded across sequential evaluations", () => {
    const state = makeState();
    const config = { ...makeConfig(), maxPositionPerMarketUsd: 30, copyStrategy: "FIXED" as const, copySize: 10 };
    let totalForMarket = 0;

    for (let i = 0; i < 50; i++) {
      const d = _evaluateTradeWithState(
        state, config,
        100, 0.5, FRESH, "same-mkt", 500, "BUY", NOW
      );

      if (d.shouldCopy) {
        totalForMarket += d.copySize;
        state.dailyVolumeUsd += d.copySize;
        state.dailySpendByMarket["same-mkt"] = (state.dailySpendByMarket["same-mkt"] || 0) + d.copySize;
      }
    }

    expect(totalForMarket).toBeLessThanOrEqual(config.maxPositionPerMarketUsd);
  });
});
