import { describe, it, expect, beforeEach } from "vitest";
import {
  _evaluateTieredTradeWithState,
  TierExposure,
  TieredRiskState,
} from "../tiered-risk-manager";
import { TierConfig } from "../strategy-config";

function makeConfig(overrides: Partial<TierConfig> = {}): TierConfig {
  return {
    tier: "1a",
    enabled: true,
    wallets: [],
    copyPercentage: 10,
    maxBet: 50,
    minBet: 5,
    maxTotalExposure: 500,
    maxPrice: 0.85,
    minPrice: 0,
    minTraderBet: 0,
    holdToSettlement: true,
    alertOnly: false,
    ...overrides,
  };
}

function makeState(): TieredRiskState {
  return { tiers: {} };
}

const NOW = Date.now();
const RECENT_TS = new Date(NOW - 5 * 60 * 1000).toISOString(); // 5 min ago

describe("tiered bet sizing", () => {
  let state: TieredRiskState;
  let config: TierConfig;

  beforeEach(() => {
    state = makeState();
    config = makeConfig();
  });

  it("calculates raw_size = trader_bet * COPY_PERCENTAGE / 100", () => {
    // Trader bets $200, 10% → $20
    const d = _evaluateTieredTradeWithState(state, config, 200, 0.50, RECENT_TS, NOW);
    expect(d.shouldCopy).toBe(true);
    expect(d.copySize).toBe(20);
  });

  it("floors to MIN_BET when raw_size is below minimum", () => {
    // Trader bets $30, 10% → $3 → floored to $5
    const d = _evaluateTieredTradeWithState(state, config, 30, 0.50, RECENT_TS, NOW);
    expect(d.shouldCopy).toBe(true);
    expect(d.copySize).toBe(5);
  });

  it("caps to MAX_BET when raw_size exceeds maximum", () => {
    // Trader bets $1000, 10% → $100 → capped to $50
    const d = _evaluateTieredTradeWithState(state, config, 1000, 0.50, RECENT_TS, NOW);
    expect(d.shouldCopy).toBe(true);
    expect(d.copySize).toBe(50);
  });

  it("respects MAX_TOTAL_EXPOSURE cap", () => {
    // Pre-fill exposure to $480
    state.tiers["1a"] = { openTotal: 480, dailyDate: new Date(NOW).toISOString().slice(0, 10), dailyVolume: 480 };
    // Only $20 remaining. Trader bets $500 → raw=$50 → capped to $20
    const d = _evaluateTieredTradeWithState(state, config, 500, 0.50, RECENT_TS, NOW);
    expect(d.shouldCopy).toBe(true);
    expect(d.copySize).toBe(20);
  });

  it("SKIPs when remaining exposure < MIN_BET", () => {
    state.tiers["1a"] = { openTotal: 497, dailyDate: new Date(NOW).toISOString().slice(0, 10), dailyVolume: 497 };
    // Only $3 remaining, min is $5 → SKIP
    const d = _evaluateTieredTradeWithState(state, config, 500, 0.50, RECENT_TS, NOW);
    expect(d.shouldCopy).toBe(false);
    expect(d.reason).toContain("exposure limit");
  });

  it("rejects trades with price above tier maxPrice", () => {
    const d = _evaluateTieredTradeWithState(state, config, 200, 0.90, RECENT_TS, NOW);
    expect(d.shouldCopy).toBe(false);
    expect(d.reason).toContain("above tier max");
  });

  it("rejects trades with price below tier minPrice", () => {
    config = makeConfig({ minPrice: 0.10, tier: "1b" });
    const d = _evaluateTieredTradeWithState(state, config, 200, 0.05, RECENT_TS, NOW);
    expect(d.shouldCopy).toBe(false);
    expect(d.reason).toContain("below tier min");
  });

  it("rejects trades below minTraderBet threshold", () => {
    config = makeConfig({ minTraderBet: 10000, tier: "1b" });
    const d = _evaluateTieredTradeWithState(state, config, 5000, 0.50, RECENT_TS, NOW);
    expect(d.shouldCopy).toBe(false);
    expect(d.reason).toContain("below tier min");
  });

  it("rejects old trades (> 1 hour)", () => {
    const oldTs = new Date(NOW - 2 * 60 * 60 * 1000).toISOString();
    const d = _evaluateTieredTradeWithState(state, config, 200, 0.50, oldTs, NOW);
    expect(d.shouldCopy).toBe(false);
    expect(d.reason).toContain("too old");
  });

  it("returns alertOnly for alert-only config", () => {
    config = makeConfig({ alertOnly: true, tier: "1c" });
    const d = _evaluateTieredTradeWithState(state, config, 200, 0.50, RECENT_TS, NOW);
    expect(d.shouldCopy).toBe(false);
    expect(d.alertOnly).toBe(true);
    expect(d.copySize).toBeGreaterThan(0);
  });

  it("rejects NaN/zero trade data", () => {
    const d = _evaluateTieredTradeWithState(state, config, 0, 0.50, RECENT_TS, NOW);
    expect(d.shouldCopy).toBe(false);
    expect(d.reason).toContain("Invalid");
  });

  describe("issue examples: MAX_BET=50, MIN_BET=5, COPY_PERCENTAGE=10%", () => {
    it("$200 trade → bet $20", () => {
      const d = _evaluateTieredTradeWithState(state, config, 200, 0.50, RECENT_TS, NOW);
      expect(d.copySize).toBe(20);
    });
    it("$1000 trade → capped at $50", () => {
      const d = _evaluateTieredTradeWithState(state, config, 1000, 0.50, RECENT_TS, NOW);
      expect(d.copySize).toBe(50);
    });
    it("$30 trade → floored to $5", () => {
      const d = _evaluateTieredTradeWithState(state, config, 30, 0.50, RECENT_TS, NOW);
      expect(d.copySize).toBe(5);
    });
    it("$10 trade → floored to $5", () => {
      const d = _evaluateTieredTradeWithState(state, config, 10, 0.50, RECENT_TS, NOW);
      expect(d.copySize).toBe(5);
    });
  });
});
