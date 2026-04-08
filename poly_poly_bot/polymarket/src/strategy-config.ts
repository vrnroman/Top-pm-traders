/**
 * Tiered insider strategy configuration.
 *
 * Loads STRATEGY_1A_*, STRATEGY_1B_*, STRATEGY_1C_* env vars.
 * Falls back to legacy USER_ADDRESSES + COPY_STRATEGY config when tiered vars are absent.
 */

import { parseAddresses, validateAddress } from "./config-validators";

// --- Types ---

export type StrategyTier = "1a" | "1b" | "1c" | "legacy";

export interface TierConfig {
  tier: StrategyTier;
  enabled: boolean;
  wallets: string[];
  copyPercentage: number;
  maxBet: number;
  minBet: number;
  maxTotalExposure: number;
  maxPrice: number;
  minPrice: number;
  minTraderBet: number;
  holdToSettlement: boolean;
  alertOnly: boolean; // 1c: alert without auto-following
}

export interface Strategy1cConfig extends TierConfig {
  autoFollow: boolean;
  newAccountAgeDays: number;
  minFirstBet: number;
  dormantDays: number;
}

// --- Env helpers (duplicated to avoid circular dep on config.ts) ---

function opt(name: string, fallback: string): string {
  return process.env[name]?.trim() || fallback;
}

function optFloat(name: string, fallback: number): number {
  const v = process.env[name]?.trim();
  return v ? parseFloat(v) : fallback;
}

function optBool(name: string, fallback: boolean): boolean {
  const v = process.env[name]?.trim();
  if (!v) return fallback;
  return v.toLowerCase() === "true";
}

function loadWallets(envKey: string): string[] {
  const raw = opt(envKey, "");
  if (!raw) return [];
  const addrs = parseAddresses(raw);
  for (const a of addrs) validateAddress(a, envKey);
  return addrs;
}

// --- Tier loaders ---

function loadTier1a(): TierConfig {
  const wallets = loadWallets("STRATEGY_1A_WALLETS");
  return {
    tier: "1a",
    enabled: optBool("STRATEGY_1A_ENABLED", wallets.length > 0),
    wallets,
    copyPercentage: optFloat("STRATEGY_1A_COPY_PERCENTAGE", 10),
    maxBet: optFloat("STRATEGY_1A_MAX_BET", 50),
    minBet: optFloat("STRATEGY_1A_MIN_BET", 5),
    maxTotalExposure: optFloat("STRATEGY_1A_MAX_TOTAL_EXPOSURE", 500),
    maxPrice: optFloat("STRATEGY_1A_MAX_PRICE", 0.85),
    minPrice: optFloat("STRATEGY_1A_MIN_PRICE", 0),
    minTraderBet: optFloat("STRATEGY_1A_MIN_TRADER_BET", 0),
    holdToSettlement: optBool("STRATEGY_1A_HOLD_TO_SETTLEMENT", true),
    alertOnly: false,
  };
}

function loadTier1b(): TierConfig {
  const wallets = loadWallets("STRATEGY_1B_WALLETS");
  return {
    tier: "1b",
    enabled: optBool("STRATEGY_1B_ENABLED", wallets.length > 0),
    wallets,
    copyPercentage: optFloat("STRATEGY_1B_COPY_PERCENTAGE", 5),
    maxBet: optFloat("STRATEGY_1B_MAX_BET", 25),
    minBet: optFloat("STRATEGY_1B_MIN_BET", 5),
    maxTotalExposure: optFloat("STRATEGY_1B_MAX_TOTAL_EXPOSURE", 200),
    maxPrice: optFloat("STRATEGY_1B_MAX_PRICE", 0.90),
    minPrice: optFloat("STRATEGY_1B_MIN_PRICE", 0.10),
    minTraderBet: optFloat("STRATEGY_1B_MIN_TRADER_BET", 10000),
    holdToSettlement: optBool("STRATEGY_1B_HOLD_TO_SETTLEMENT", false),
    alertOnly: false,
  };
}

function loadTier1c(): Strategy1cConfig {
  return {
    tier: "1c",
    enabled: optBool("STRATEGY_1C_ENABLED", false),
    wallets: [], // 1c discovers wallets dynamically
    copyPercentage: optFloat("STRATEGY_1C_COPY_PERCENTAGE", 5),
    maxBet: optFloat("STRATEGY_1C_MAX_BET", 10),
    minBet: optFloat("STRATEGY_1C_MIN_BET", 5),
    maxTotalExposure: optFloat("STRATEGY_1C_MAX_TOTAL_EXPOSURE", 100),
    maxPrice: optFloat("STRATEGY_1C_MAX_PRICE", 0.90),
    minPrice: optFloat("STRATEGY_1C_MIN_PRICE", 0.10),
    minTraderBet: optFloat("STRATEGY_1C_MIN_TRADER_BET", 0),
    holdToSettlement: false,
    alertOnly: optBool("STRATEGY_1C_ALERT_ONLY", true),
    autoFollow: optBool("STRATEGY_1C_AUTO_FOLLOW", false),
    newAccountAgeDays: optFloat("STRATEGY_1C_NEW_ACCOUNT_AGE_DAYS", 30),
    minFirstBet: optFloat("STRATEGY_1C_MIN_FIRST_BET", 5000),
    dormantDays: optFloat("STRATEGY_1C_DORMANT_DAYS", 60),
  };
}

// --- Exported config ---

export const TIER_1A = loadTier1a();
export const TIER_1B = loadTier1b();
export const TIER_1C = loadTier1c();

/** Whether any tiered strategy is configured. */
export const TIERED_MODE = TIER_1A.wallets.length > 0 || TIER_1B.wallets.length > 0 || TIER_1C.enabled;

/** All tracked wallets across tiers (for detection). Does NOT include 1c (dynamic). */
export function getAllTieredWallets(): string[] {
  const set = new Set<string>();
  if (TIER_1A.enabled) TIER_1A.wallets.forEach(w => set.add(w.toLowerCase()));
  if (TIER_1B.enabled) TIER_1B.wallets.forEach(w => set.add(w.toLowerCase()));
  return [...set];
}

/** Lookup wallet → tier mapping. Returns null if wallet is not in any tier. */
const walletTierMap = new Map<string, StrategyTier>();
function buildWalletTierMap(): void {
  if (TIER_1A.enabled) {
    for (const w of TIER_1A.wallets) walletTierMap.set(w.toLowerCase(), "1a");
  }
  if (TIER_1B.enabled) {
    for (const w of TIER_1B.wallets) walletTierMap.set(w.toLowerCase(), "1b");
  }
}
buildWalletTierMap();

export function getWalletTier(address: string): StrategyTier | null {
  return walletTierMap.get(address.toLowerCase()) ?? null;
}

export function getTierConfig(tier: StrategyTier): TierConfig {
  switch (tier) {
    case "1a": return TIER_1A;
    case "1b": return TIER_1B;
    case "1c": return TIER_1C;
    default: throw new Error(`Unknown tier: ${tier}`);
  }
}
