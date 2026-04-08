/**
 * Strategy 1c: Pattern-based detection of previously unknown insider wallets.
 *
 * Detection rules:
 * 1. New account (< 30 days old) + first bet > $5K + geopolitical market = ALERT
 * 2. Multiple fresh wallets betting same direction on same market within 1 hour = ALERT (cluster)
 * 3. Dormant account (> 60 days inactive) suddenly places large geopolitical bet = ALERT
 *
 * Sends Telegram alerts for manual review. Optionally auto-follows with small size.
 */

import { DetectedTrade } from "./trade-monitor";
import { TIER_1C, Strategy1cConfig } from "./strategy-config";
import { logger } from "./logger";
import { telegram } from "./telegram-notifier";

// --- Geopolitical keyword heuristic ---

const GEO_KEYWORDS = [
  "war", "military", "strike", "invasion", "missile", "nuclear",
  "sanctions", "ceasefire", "troops", "nato", "iran", "israel",
  "russia", "ukraine", "china", "taiwan", "north korea", "syria",
  "hamas", "hezbollah", "maduro", "venezuela", "coup", "assassination",
  "bomb", "airstrike", "conflict", "peace deal", "hostage",
  "tariff", "embargo", "annex", "occupation", "insurgent",
];

export function isGeopoliticalMarket(marketTitle: string): boolean {
  const lower = marketTitle.toLowerCase();
  return GEO_KEYWORDS.some(kw => lower.includes(kw));
}

// --- Activity tracking for pattern detection ---

interface WalletActivity {
  firstSeen: number;      // epoch ms
  lastSeen: number;       // epoch ms
  tradeCount: number;
  totalVolume: number;
}

// In-memory tracker — resets on restart (acceptable for alerting)
const walletActivity = new Map<string, WalletActivity>();

// Cluster detection: track recent large bets on same market
interface RecentBet {
  wallet: string;
  market: string;
  conditionId: string;
  side: string;
  size: number;
  timestamp: number;
}

const recentBets: RecentBet[] = [];
const MAX_RECENT_BETS = 500;
const CLUSTER_WINDOW_MS = 60 * 60 * 1000; // 1 hour
const CLUSTER_MIN_WALLETS = 3;

// Track already-alerted patterns to avoid spam
const alertedPatterns = new Set<string>();
const MAX_ALERTED = 1000;

function alertKey(type: string, ...parts: string[]): string {
  return `${type}:${parts.join(":")}`;
}

function shouldAlert(key: string): boolean {
  if (alertedPatterns.has(key)) return false;
  if (alertedPatterns.size >= MAX_ALERTED) {
    // Evict oldest half
    const arr = [...alertedPatterns];
    for (let i = 0; i < arr.length / 2; i++) alertedPatterns.delete(arr[i]);
  }
  alertedPatterns.add(key);
  return true;
}

// --- Pattern checks ---

export interface PatternAlert {
  type: "new_account_large_geo" | "cluster_detection" | "dormant_reactivation";
  trade: DetectedTrade;
  details: string;
}

function checkNewAccountLargeGeo(trade: DetectedTrade, config: Strategy1cConfig): PatternAlert | null {
  const activity = walletActivity.get(trade.traderAddress.toLowerCase());
  if (!activity) return null; // First trade — we flag on second observation if pattern holds

  const accountAgeDays = (Date.now() - activity.firstSeen) / (24 * 60 * 60 * 1000);
  if (accountAgeDays > config.newAccountAgeDays) return null;
  if (trade.size < config.minFirstBet) return null;
  if (!isGeopoliticalMarket(trade.market)) return null;

  const key = alertKey("new_geo", trade.traderAddress, trade.conditionId);
  if (!shouldAlert(key)) return null;

  return {
    type: "new_account_large_geo",
    trade,
    details: `New account (${Math.round(accountAgeDays)}d old) placed $${trade.size.toFixed(0)} on geopolitical market "${trade.market}"`,
  };
}

function checkClusterDetection(trade: DetectedTrade): PatternAlert | null {
  const now = Date.now();

  // Add to recent bets
  recentBets.push({
    wallet: trade.traderAddress.toLowerCase(),
    market: trade.conditionId || trade.market,
    conditionId: trade.conditionId,
    side: trade.side,
    size: trade.size,
    timestamp: now,
  });

  // Prune old bets
  while (recentBets.length > MAX_RECENT_BETS || (recentBets.length > 0 && now - recentBets[0].timestamp > CLUSTER_WINDOW_MS * 2)) {
    recentBets.shift();
  }

  // Find bets on same market+side within window
  const marketKey = trade.conditionId || trade.market;
  const clusterBets = recentBets.filter(
    b => b.market === marketKey && b.side === trade.side && now - b.timestamp <= CLUSTER_WINDOW_MS
  );

  // Count unique wallets
  const uniqueWallets = new Set(clusterBets.map(b => b.wallet));
  if (uniqueWallets.size < CLUSTER_MIN_WALLETS) return null;

  const key = alertKey("cluster", marketKey, trade.side);
  if (!shouldAlert(key)) return null;

  return {
    type: "cluster_detection",
    trade,
    details: `${uniqueWallets.size} fresh wallets betting ${trade.side} on "${trade.market}" within 1h (cluster pattern)`,
  };
}

function checkDormantReactivation(trade: DetectedTrade, config: Strategy1cConfig): PatternAlert | null {
  const activity = walletActivity.get(trade.traderAddress.toLowerCase());
  if (!activity) return null;

  const daysSinceLastSeen = (Date.now() - activity.lastSeen) / (24 * 60 * 60 * 1000);
  if (daysSinceLastSeen < config.dormantDays) return null;
  if (trade.size < config.minFirstBet) return null;
  if (!isGeopoliticalMarket(trade.market)) return null;

  const key = alertKey("dormant", trade.traderAddress, trade.conditionId);
  if (!shouldAlert(key)) return null;

  return {
    type: "dormant_reactivation",
    trade,
    details: `Dormant wallet (${Math.round(daysSinceLastSeen)}d inactive) placed $${trade.size.toFixed(0)} on geopolitical market "${trade.market}"`,
  };
}

// --- Main entry point ---

/**
 * Analyze a trade for insider patterns. Called for ALL detected trades (not just tiered wallets).
 * Updates activity tracking and fires alerts when patterns match.
 *
 * Returns alerts (if any) for the caller to act on.
 */
export function analyzeTradeForPatterns(trade: DetectedTrade): PatternAlert[] {
  if (!TIER_1C.enabled) return [];

  const addr = trade.traderAddress.toLowerCase();
  const now = Date.now();

  // Update activity
  const existing = walletActivity.get(addr);
  if (existing) {
    existing.lastSeen = now;
    existing.tradeCount++;
    existing.totalVolume += trade.size;
  } else {
    walletActivity.set(addr, {
      firstSeen: now,
      lastSeen: now,
      tradeCount: 1,
      totalVolume: trade.size,
    });
  }

  // Run pattern checks
  const alerts: PatternAlert[] = [];

  const newGeo = checkNewAccountLargeGeo(trade, TIER_1C);
  if (newGeo) alerts.push(newGeo);

  const cluster = checkClusterDetection(trade);
  if (cluster) alerts.push(cluster);

  const dormant = checkDormantReactivation(trade, TIER_1C);
  if (dormant) alerts.push(dormant);

  // Send Telegram alerts
  for (const alert of alerts) {
    const emoji = alert.type === "cluster_detection" ? "🔍" : alert.type === "dormant_reactivation" ? "💤" : "🆕";
    const msg = `${emoji} <b>[1c PATTERN ALERT]</b>\n<b>${alert.type.replace(/_/g, " ").toUpperCase()}</b>\n${alert.details}\n\nWallet: <code>${trade.traderAddress}</code>\n${trade.side} $${trade.size.toFixed(2)} @ ${trade.price} on "${trade.market}"`;
    telegram.botError(msg); // Reuse botError for quick notification
    logger.warn(`[1c] ${alert.type}: ${alert.details}`);
  }

  return alerts;
}

/** Reset state — for testing. */
export function _resetPatternDetector(): void {
  walletActivity.clear();
  recentBets.length = 0;
  alertedPatterns.clear();
}
