import fs from "fs";
import path from "path";
import { CONFIG } from "./config";
import { todayUtc } from "./utils";

// --- Runtime state (persisted to disk) ---

export interface RiskState {
  dailyVolumeUsd: number;
  dailyVolumeDate: string; // YYYY-MM-DD
  dailySpendByMarket: Record<string, number>; // daily USD spend per market, resets at midnight UTC
}

const DATA_DIR = path.resolve(process.cwd(), "data");
if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
const STATE_FILE = path.join(DATA_DIR, "risk-state.json");



function loadState(): RiskState {
  try {
    if (fs.existsSync(STATE_FILE)) {
      const data = JSON.parse(fs.readFileSync(STATE_FILE, "utf8"));
      if (data.dailyVolumeDate !== todayUtc()) {
        data.dailyVolumeUsd = 0;
        data.dailySpendByMarket = {};
        data.dailyVolumeDate = todayUtc();
      }
      // Migrate old field name from positionsByMarket → dailySpendByMarket
      if (data.positionsByMarket && !data.dailySpendByMarket) {
        data.dailySpendByMarket = data.positionsByMarket;
        delete data.positionsByMarket;
      }
      return data;
    }
  } catch {
    // Corrupted — start fresh
  }
  return { dailyVolumeUsd: 0, dailyVolumeDate: todayUtc(), dailySpendByMarket: {} };
}

function saveState(): void {
  const tmp = STATE_FILE + ".tmp";
  const data = JSON.stringify(state, null, 2);
  fs.writeFileSync(tmp, data);
  try {
    fs.renameSync(tmp, STATE_FILE);
  } catch {
    fs.writeFileSync(STATE_FILE, data);
    try { fs.unlinkSync(tmp); } catch { /* ignore */ }
  }
}

const state = loadState();

// --- Public API ---

export interface CopyDecision {
  shouldCopy: boolean;
  copySize: number;
  reason?: string;
}

export interface RiskConfig {
  copyStrategy: "PERCENTAGE" | "FIXED";
  copySize: number;
  maxOrderSizeUsd: number;
  minOrderSizeUsd: number;
  maxPositionPerMarketUsd: number;
  maxDailyVolumeUsd: number;
  maxTradeAgeHours: number;
}

/**
 * Evaluation logic with injectable state and clock for testing.
 * Mutates riskState (resets daily counters on new day).
 * Applies all risk checks in sequence: NaN guard, daily volume, trade age,
 * copy size calculation, min/max bounds, price validation, market cap, balance.
 */
export function _evaluateTradeWithState(
  riskState: RiskState,
  config: RiskConfig,
  traderOrderSize: number,
  traderPrice: number,
  tradeTimestamp: string,
  marketKey: string,
  usdcBalance: number,
  side: "BUY" | "SELL",
  now: number
): CopyDecision {
  if (isNaN(traderOrderSize) || isNaN(traderPrice) || traderOrderSize <= 0) {
    return { shouldCopy: false, copySize: 0, reason: "Invalid trade data (NaN or zero)" };
  }

  const today = new Date(now).toISOString().slice(0, 10);
  if (riskState.dailyVolumeDate !== today) {
    riskState.dailyVolumeUsd = 0;
    riskState.dailySpendByMarket = {};
    riskState.dailyVolumeDate = today;
  }
  if (riskState.dailyVolumeUsd >= config.maxDailyVolumeUsd) {
    return {
      shouldCopy: false,
      copySize: 0,
      reason: `Daily volume limit reached: $${riskState.dailyVolumeUsd.toFixed(2)} / $${config.maxDailyVolumeUsd}`,
    };
  }

  const tradeTime = new Date(tradeTimestamp).getTime();
  if (isNaN(tradeTime)) {
    return { shouldCopy: false, copySize: 0, reason: "Invalid timestamp" };
  }
  const ageMs = now - tradeTime;
  const maxAgeMs = config.maxTradeAgeHours * 60 * 60 * 1000;
  if (ageMs > maxAgeMs) {
    return { shouldCopy: false, copySize: 0, reason: `Trade too old (${Math.round(ageMs / 60000)}min)` };
  }

  let copySize: number;
  if (config.copyStrategy === "PERCENTAGE") {
    copySize = traderOrderSize * (config.copySize / 100);
  } else {
    copySize = config.copySize;
  }
  copySize = Math.round(copySize * 100) / 100;

  if (copySize < config.minOrderSizeUsd) {
    return { shouldCopy: false, copySize, reason: `Copy size $${copySize} below min $${config.minOrderSizeUsd}` };
  }
  if (copySize > config.maxOrderSizeUsd) {
    copySize = config.maxOrderSizeUsd;
  }

  // Enforce hard daily volume cap — reduce order to fit remaining headroom
  // 2% tolerance absorbs ceilCents rounding drift (actual fill slightly above approved copySize)
  const ROUNDING_TOLERANCE = 0.98;
  const dailyRoom = config.maxDailyVolumeUsd - riskState.dailyVolumeUsd;
  if (copySize > dailyRoom) {
    if (dailyRoom < config.minOrderSizeUsd * ROUNDING_TOLERANCE) {
      return { shouldCopy: false, copySize: 0, reason: `Daily volume limit: $${riskState.dailyVolumeUsd.toFixed(2)} / $${config.maxDailyVolumeUsd}` };
    }
    copySize = Math.round(dailyRoom * 100) / 100;
  }

  if (traderPrice <= 0 || traderPrice >= 1) {
    return { shouldCopy: false, copySize: 0, reason: `Invalid price: ${traderPrice}` };
  }
  // Skip extreme prices — no liquidity below 0.10 or above 0.95, CLOB rejects small notionals
  if (traderPrice < 0.10 || traderPrice > 0.95) {
    return { shouldCopy: false, copySize: 0, reason: `Price too extreme: ${traderPrice} (need 0.10–0.95)` };
  }

  // Per-market cap only applies to BUY (new exposure). SELL reduces exposure — never block exits.
  if (side === "BUY") {
    const placedToday = riskState.dailySpendByMarket[marketKey] || 0;
    if (placedToday + copySize > config.maxPositionPerMarketUsd) {
      const room = config.maxPositionPerMarketUsd - placedToday;
      if (room < config.minOrderSizeUsd * ROUNDING_TOLERANCE) {
        return {
          shouldCopy: false,
          copySize: 0,
          reason: `Daily market cap: $${placedToday.toFixed(2)} / $${config.maxPositionPerMarketUsd} placed today`,
        };
      }
      copySize = Math.round(room * 100) / 100;
    }
  }

  if (side === "BUY" && usdcBalance >= 0 && copySize > usdcBalance) {
    if (usdcBalance < config.minOrderSizeUsd) {
      return { shouldCopy: false, copySize: 0, reason: `Insufficient USDC balance: $${usdcBalance.toFixed(2)}` };
    }
    copySize = Math.round(usdcBalance * 100) / 100;
  }

  // Final min check after all reductions (market cap, daily cap, balance may reduce below minimum)
  if (copySize < config.minOrderSizeUsd) {
    return { shouldCopy: false, copySize: 0, reason: `Copy size $${copySize.toFixed(2)} below min after caps` };
  }

  return { shouldCopy: true, copySize };
}

/** Evaluate whether a detected trade should be copied, applying all risk checks. */
export function evaluateTrade(
  traderOrderSize: number,
  traderPrice: number,
  tradeTimestamp: string,
  marketKey: string,
  usdcBalance: number,
  side: "BUY" | "SELL" = "BUY"
): CopyDecision {
  return _evaluateTradeWithState(state, CONFIG, traderOrderSize, traderPrice, tradeTimestamp, marketKey, usdcBalance, side, Date.now());
}

/** Record a verified fill — updates daily volume (BUY+SELL) and per-market spend (BUY only). */
export function recordPlacement(marketKey: string, amountUsd: number, side: "BUY" | "SELL"): void {
  state.dailyVolumeUsd += amountUsd;
  // Only BUY adds to per-market spend (new exposure). SELL is revenue, not new exposure.
  if (side === "BUY") {
    state.dailySpendByMarket[marketKey] = (state.dailySpendByMarket[marketKey] || 0) + amountUsd;
  }
  saveState();
}

/** Injectable version of adjustPlacement for testing. */
export function _adjustPlacementWithState(
  riskState: RiskState, marketKey: string, optimisticUsd: number, actualUsd: number, side: "BUY" | "SELL"
): void {
  const delta = optimisticUsd - actualUsd;
  if (delta <= 0) return;
  riskState.dailyVolumeUsd = Math.max(0, riskState.dailyVolumeUsd - delta);
  if (side === "BUY") {
    const current = riskState.dailySpendByMarket[marketKey] || 0;
    riskState.dailySpendByMarket[marketKey] = Math.max(0, current - delta);
  }
}

/** Adjust optimistic risk accounting after fill verification.
 *  Called with (optimistic=copySize, actual=filledUsd) to correct delta.
 *  FILLED: small delta. PARTIAL: reverse unfilled portion. UNFILLED/UNKNOWN: reverse all (actual=0). */
export function adjustPlacement(marketKey: string, optimisticUsd: number, actualUsd: number, side: "BUY" | "SELL"): void {
  _adjustPlacementWithState(state, marketKey, optimisticUsd, actualUsd, side);
  saveState();
}

export function getRiskStatus(): string {
  return [
    `Daily volume: $${state.dailyVolumeUsd.toFixed(2)} / $${CONFIG.maxDailyVolumeUsd}`,
    `Markets tracked: ${Object.keys(state.dailySpendByMarket).length}`,
  ].join(" | ");
}
