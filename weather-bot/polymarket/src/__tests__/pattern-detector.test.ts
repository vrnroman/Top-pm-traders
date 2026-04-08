import { describe, it, expect, beforeEach, vi } from "vitest";

// Mock telegram and logger before imports
vi.mock("../telegram-notifier", () => ({
  telegram: { botError: vi.fn() },
}));
vi.mock("../logger", () => ({
  logger: { warn: vi.fn(), info: vi.fn(), debug: vi.fn() },
}));
// Mock strategy-config to enable 1c
vi.mock("../strategy-config", () => ({
  TIER_1C: {
    tier: "1c",
    enabled: true,
    alertOnly: true,
    newAccountAgeDays: 30,
    minFirstBet: 5000,
    dormantDays: 60,
    wallets: [],
    copyPercentage: 5,
    maxBet: 10,
    minBet: 5,
    maxTotalExposure: 100,
    maxPrice: 0.90,
    minPrice: 0.10,
    minTraderBet: 0,
    holdToSettlement: false,
    autoFollow: false,
  },
}));

import { analyzeTradeForPatterns, isGeopoliticalMarket, _resetPatternDetector } from "../pattern-detector";
import type { DetectedTrade } from "../trade-monitor";

function makeTrade(overrides: Partial<DetectedTrade> = {}): DetectedTrade {
  return {
    id: `test-${Math.random()}`,
    traderAddress: "0xabc0000000000000000000000000000000000001",
    timestamp: new Date().toISOString(),
    market: "Will there be an Iran strike?",
    conditionId: "cond-geo-1",
    tokenId: "tok-1",
    side: "BUY",
    size: 10000,
    price: 0.30,
    outcome: "Yes",
    ...overrides,
  };
}

describe("pattern-detector", () => {
  beforeEach(() => {
    _resetPatternDetector();
  });

  describe("isGeopoliticalMarket", () => {
    it("detects geopolitical keywords", () => {
      expect(isGeopoliticalMarket("Will Israel strike Iran?")).toBe(true);
      expect(isGeopoliticalMarket("Ukraine ceasefire before July?")).toBe(true);
      expect(isGeopoliticalMarket("Bitcoin above $100k?")).toBe(false);
      expect(isGeopoliticalMarket("Will it rain tomorrow?")).toBe(false);
    });
  });

  describe("new account + large geo bet", () => {
    it("alerts on new account with large geo bet", () => {
      const trade = makeTrade({ size: 10000 });
      // First call registers AND alerts (account age = 0 days < 30)
      const alerts = analyzeTradeForPatterns(trade);
      expect(alerts.some(a => a.type === "new_account_large_geo")).toBe(true);
    });

    it("does not alert on small bets", () => {
      const trade = makeTrade({ size: 100 }); // below $5000 threshold
      analyzeTradeForPatterns(trade);
      const trade2 = makeTrade({
        id: "test-small-2",
        traderAddress: trade.traderAddress,
        size: 100,
      });
      const alerts = analyzeTradeForPatterns(trade2);
      expect(alerts.some(a => a.type === "new_account_large_geo")).toBe(false);
    });

    it("does not alert on non-geo markets", () => {
      const trade = makeTrade({ market: "Bitcoin above $100k?", size: 10000 });
      analyzeTradeForPatterns(trade);
      const trade2 = makeTrade({
        id: "test-nongeo-2",
        traderAddress: trade.traderAddress,
        market: "Bitcoin above $100k?",
        size: 10000,
      });
      const alerts = analyzeTradeForPatterns(trade2);
      expect(alerts.some(a => a.type === "new_account_large_geo")).toBe(false);
    });
  });

  describe("cluster detection", () => {
    it("alerts when 3+ wallets bet same direction on same market", () => {
      let lastAlerts;
      for (let i = 0; i < 3; i++) {
        const trade = makeTrade({
          id: `cluster-${i}`,
          traderAddress: `0x${String(i).padStart(40, "0")}`,
          conditionId: "same-market",
          side: "BUY",
          size: 10000,
        });
        lastAlerts = analyzeTradeForPatterns(trade);
      }
      expect(lastAlerts!.some(a => a.type === "cluster_detection")).toBe(true);
    });

    it("does not alert with only 2 wallets", () => {
      let lastAlerts;
      for (let i = 0; i < 2; i++) {
        const trade = makeTrade({
          id: `cluster2-${i}`,
          traderAddress: `0x${String(i + 10).padStart(40, "0")}`,
          conditionId: "same-market-2",
          side: "BUY",
          size: 10000,
        });
        lastAlerts = analyzeTradeForPatterns(trade);
      }
      expect(lastAlerts!.some(a => a.type === "cluster_detection")).toBe(false);
    });
  });
});
