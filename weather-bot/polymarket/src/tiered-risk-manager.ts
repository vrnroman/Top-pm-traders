/**
 * Tiered bet sizing and risk evaluation for Strategy 1a/1b/1c.
 *
 * Implements the sizing algorithm from the issue:
 *   1. raw_size = trader_bet × COPY_PERCENTAGE / 100
 *   2. size = max(raw_size, MIN_BET)
 *   3. size = min(size, MAX_BET)
 *   4. remaining = MAX_TOTAL_EXPOSURE - current_open_total
 *   5. if size > remaining: size = remaining
 *   6. if size < MIN_BET: SKIP
 *
 * Each tier has its own exposure tracking, independent of the global risk-manager.
 */

import fs from "fs";
import path from "path";
import { TierConfig, StrategyTier } from "./strategy-config";
import { todayUtc } from "./utils";

// --- Per-tier exposure state ---

export interface TierExposure {
  openTotal: number;     // sum of open (unfilled/pending) bet sizes for this tier
  dailyDate: string;     // YYYY-MM-DD for daily reset
  dailyVolume: number;   // total placed today
}

export interface TieredRiskState {
  tiers: Record<string, TierExposure>;
}

const DATA_DIR = path.resolve(process.cwd(), "data");
const STATE_FILE = path.join(DATA_DIR, "tiered-risk-state.json");

function defaultExposure(): TierExposure {
  return { openTotal: 0, dailyDate: todayUtc(), dailyVolume: 0 };
}

function loadTieredState(): TieredRiskState {
  try {
    if (fs.existsSync(STATE_FILE)) {
      const data = JSON.parse(fs.readFileSync(STATE_FILE, "utf8")) as TieredRiskState;
      // Reset daily counters if date changed
      const today = todayUtc();
      for (const key of Object.keys(data.tiers)) {
        if (data.tiers[key].dailyDate !== today) {
          data.tiers[key].dailyVolume = 0;
          data.tiers[key].dailyDate = today;
        }
      }
      return data;
    }
  } catch { /* corrupted — start fresh */ }
  return { tiers: {} };
}

function saveTieredState(): void {
  if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
  const data = JSON.stringify(tieredState, null, 2);
  const tmp = STATE_FILE + ".tmp";
  fs.writeFileSync(tmp, data);
  try {
    fs.renameSync(tmp, STATE_FILE);
  } catch {
    fs.writeFileSync(STATE_FILE, data);
    try { fs.unlinkSync(tmp); } catch { /* ignore */ }
  }
}

const tieredState = loadTieredState();

function getExposure(tier: string): TierExposure {
  if (!tieredState.tiers[tier]) {
    tieredState.tiers[tier] = defaultExposure();
  }
  const exp = tieredState.tiers[tier];
  const today = todayUtc();
  if (exp.dailyDate !== today) {
    exp.dailyVolume = 0;
    exp.dailyDate = today;
  }
  return exp;
}

// --- Public API ---

export interface TieredCopyDecision {
  shouldCopy: boolean;
  copySize: number;
  reason?: string;
  tier: StrategyTier;
  alertOnly?: boolean;
}

/**
 * Evaluate a trade through tiered sizing logic.
 * Exported with injectable state for testing.
 */
export function _evaluateTieredTradeWithState(
  state: TieredRiskState,
  config: TierConfig,
  traderBetSize: number,
  traderPrice: number,
  tradeTimestamp: string,
  now: number
): TieredCopyDecision {
  const tier = config.tier;

  if (isNaN(traderBetSize) || traderBetSize <= 0 || isNaN(traderPrice) || traderPrice <= 0 || traderPrice >= 1) {
    return { shouldCopy: false, copySize: 0, reason: "Invalid trade data", tier };
  }

  // Trade age check (1 hour default)
  const tradeTime = new Date(tradeTimestamp).getTime();
  if (isNaN(tradeTime)) {
    return { shouldCopy: false, copySize: 0, reason: "Invalid timestamp", tier };
  }
  const ageMs = now - tradeTime;
  if (ageMs > 60 * 60 * 1000) {
    return { shouldCopy: false, copySize: 0, reason: `Trade too old (${Math.round(ageMs / 60000)}min)`, tier };
  }

  // Min trader bet filter (1b requires > $10K)
  if (config.minTraderBet > 0 && traderBetSize < config.minTraderBet) {
    return {
      shouldCopy: false, copySize: 0,
      reason: `Trader bet $${traderBetSize} below tier min $${config.minTraderBet}`,
      tier,
    };
  }

  // Price bounds
  if (config.minPrice > 0 && traderPrice < config.minPrice) {
    return { shouldCopy: false, copySize: 0, reason: `Price ${traderPrice} below tier min ${config.minPrice}`, tier };
  }
  if (config.maxPrice > 0 && traderPrice > config.maxPrice) {
    return { shouldCopy: false, copySize: 0, reason: `Price ${traderPrice} above tier max ${config.maxPrice}`, tier };
  }

  // --- Sizing algorithm ---
  // 1. raw_size = trader_bet × COPY_PERCENTAGE / 100
  let size = traderBetSize * (config.copyPercentage / 100);

  // 2. Floor to MIN_BET
  size = Math.max(size, config.minBet);

  // 3. Cap to MAX_BET
  size = Math.min(size, config.maxBet);

  // Round to cents
  size = Math.round(size * 100) / 100;

  // 4. Exposure check
  if (!state.tiers[tier]) {
    state.tiers[tier] = defaultExposure();
  }
  const exposure = state.tiers[tier];
  const today = new Date(now).toISOString().slice(0, 10);
  if (exposure.dailyDate !== today) {
    exposure.dailyVolume = 0;
    exposure.dailyDate = today;
  }

  const remainingExposure = config.maxTotalExposure - exposure.openTotal;

  // 5. If size > remaining: size = remaining
  if (size > remainingExposure) {
    size = Math.round(remainingExposure * 100) / 100;
  }

  // 6. If size < MIN_BET: SKIP
  if (size < config.minBet) {
    return {
      shouldCopy: false, copySize: 0,
      reason: `Tier ${tier} exposure limit: $${exposure.openTotal.toFixed(2)} / $${config.maxTotalExposure} (need $${config.minBet})`,
      tier,
    };
  }

  // Alert-only mode (1c)
  if (config.alertOnly) {
    return { shouldCopy: false, copySize: size, reason: "Alert only — manual review required", tier, alertOnly: true };
  }

  return { shouldCopy: true, copySize: size, tier };
}

/** Evaluate a tiered trade using the global state. */
export function evaluateTieredTrade(
  config: TierConfig,
  traderBetSize: number,
  traderPrice: number,
  tradeTimestamp: string,
): TieredCopyDecision {
  return _evaluateTieredTradeWithState(tieredState, config, traderBetSize, traderPrice, tradeTimestamp, Date.now());
}

/** Record a placement against tier exposure. */
export function recordTieredPlacement(tier: StrategyTier, amountUsd: number): void {
  const exp = getExposure(tier);
  exp.openTotal += amountUsd;
  exp.dailyVolume += amountUsd;
  saveTieredState();
}

/** Release exposure after fill/cancel verification. */
export function releaseTieredExposure(tier: StrategyTier, amountUsd: number): void {
  const exp = getExposure(tier);
  exp.openTotal = Math.max(0, exp.openTotal - amountUsd);
  saveTieredState();
}

/** Get tier exposure status for heartbeat/logging. */
export function getTieredRiskStatus(): string {
  const lines: string[] = [];
  for (const [tier, exp] of Object.entries(tieredState.tiers)) {
    lines.push(`Tier ${tier}: $${exp.openTotal.toFixed(2)} open, $${exp.dailyVolume.toFixed(2)} today`);
  }
  return lines.length > 0 ? lines.join(" | ") : "No tiered activity";
}
