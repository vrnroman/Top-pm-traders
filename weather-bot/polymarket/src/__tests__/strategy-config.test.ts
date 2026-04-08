import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

describe("strategy-config", () => {
  const originalEnv = { ...process.env };

  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    process.env = { ...originalEnv };
  });

  it("loads tier 1a wallets from env", async () => {
    process.env.STRATEGY_1A_WALLETS = "0x858d551d073e9c647c17079ad9021de830201047,0x4dfd481c16d9995b809780fd8a9808e8689f6e4a";
    process.env.STRATEGY_1A_ENABLED = "true";
    process.env.STRATEGY_1A_COPY_PERCENTAGE = "15";
    process.env.STRATEGY_1A_MAX_BET = "75";

    const mod = await import("../strategy-config");
    expect(mod.TIER_1A.wallets).toHaveLength(2);
    expect(mod.TIER_1A.copyPercentage).toBe(15);
    expect(mod.TIER_1A.maxBet).toBe(75);
    expect(mod.TIER_1A.enabled).toBe(true);
  });

  it("classifies wallets into correct tiers", async () => {
    process.env.STRATEGY_1A_WALLETS = "0x858d551d073e9c647c17079ad9021de830201047";
    process.env.STRATEGY_1B_WALLETS = "0x8c80d213c0cbad777d06ee3f58f6ca4bc03102c3";

    const mod = await import("../strategy-config");
    expect(mod.getWalletTier("0x858d551d073e9c647c17079ad9021de830201047")).toBe("1a");
    expect(mod.getWalletTier("0x8c80d213c0cbad777d06ee3f58f6ca4bc03102c3")).toBe("1b");
    expect(mod.getWalletTier("0x0000000000000000000000000000000000000000")).toBeNull();
  });

  it("is case-insensitive for wallet lookup", async () => {
    process.env.STRATEGY_1A_WALLETS = "0x858d551d073e9c647c17079ad9021de830201047";

    const mod = await import("../strategy-config");
    expect(mod.getWalletTier("0x858D551D073E9C647C17079AD9021DE830201047")).toBe("1a");
  });

  it("returns all tiered wallets for detection", async () => {
    process.env.STRATEGY_1A_WALLETS = "0x858d551d073e9c647c17079ad9021de830201047";
    process.env.STRATEGY_1B_WALLETS = "0x8c80d213c0cbad777d06ee3f58f6ca4bc03102c3";

    const mod = await import("../strategy-config");
    const all = mod.getAllTieredWallets();
    expect(all).toHaveLength(2);
    expect(all).toContain("0x858d551d073e9c647c17079ad9021de830201047");
    expect(all).toContain("0x8c80d213c0cbad777d06ee3f58f6ca4bc03102c3");
  });

  it("detects tiered mode when wallets are configured", async () => {
    process.env.STRATEGY_1A_WALLETS = "0x858d551d073e9c647c17079ad9021de830201047";

    const mod = await import("../strategy-config");
    expect(mod.TIERED_MODE).toBe(true);
  });

  it("returns TIERED_MODE=false when no tiered wallets", async () => {
    delete process.env.STRATEGY_1A_WALLETS;
    delete process.env.STRATEGY_1B_WALLETS;
    delete process.env.STRATEGY_1C_ENABLED;

    const mod = await import("../strategy-config");
    expect(mod.TIERED_MODE).toBe(false);
  });

  it("loads 1c config with defaults", async () => {
    process.env.STRATEGY_1C_ENABLED = "true";
    process.env.STRATEGY_1C_ALERT_ONLY = "true";

    const mod = await import("../strategy-config");
    expect(mod.TIER_1C.enabled).toBe(true);
    expect(mod.TIER_1C.alertOnly).toBe(true);
    expect(mod.TIER_1C.newAccountAgeDays).toBe(30);
    expect(mod.TIER_1C.dormantDays).toBe(60);
  });
});
