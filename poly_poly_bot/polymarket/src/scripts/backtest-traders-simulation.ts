// Copy-trading simulation engine for backtesting

import { ActivityTrade, MarketResolution, SimTrade, TraderBacktest, BT_CONFIG } from "./backtest-traders-types";

export function simulateCopyTrading(
  trades: ActivityTrade[],
  resolutions: Map<string, MarketResolution>,
  daysBack: number
): TraderBacktest {
  const cutoff = Date.now() / 1000 - daysBack * 86400;

  const recent = trades
    .filter((t) => t.timestamp > cutoff && t.side === "BUY" && t.price > 0 && t.price < 1)
    .sort((a, b) => a.timestamp - b.timestamp);

  const simTrades: SimTrade[] = [];
  const skippedReasons: Record<string, number> = {};
  const dailyVolume: Record<string, number> = {};
  const dailyMarketVolume: Record<string, Record<string, number>> = {};

  const lastSeenByMarket = new Map<string, number>();
  let withinWindow = 0;
  let afterLimits = 0;

  for (const trade of recent) {
    const day = new Date(trade.timestamp * 1000).toISOString().slice(0, 10);

    const lastSeen = lastSeenByMarket.get(trade.conditionId) || 0;
    const gap = trade.timestamp - lastSeen;
    if (lastSeen > 0 && gap < 10) {
      skippedReasons["DEDUP"] = (skippedReasons["DEDUP"] || 0) + 1;
      lastSeenByMarket.set(trade.conditionId, trade.timestamp);
      continue;
    }
    lastSeenByMarket.set(trade.conditionId, trade.timestamp);

    withinWindow++;

    if (trade.price <= 0.01 || trade.price >= 0.99) {
      skippedReasons["BAD_PRICE"] = (skippedReasons["BAD_PRICE"] || 0) + 1;
      continue;
    }

    if (!dailyVolume[day]) dailyVolume[day] = 0;
    if (dailyVolume[day] >= BT_CONFIG.MAX_DAILY_VOLUME) {
      skippedReasons["DAILY_LIMIT"] = (skippedReasons["DAILY_LIMIT"] || 0) + 1;
      continue;
    }

    if (!dailyMarketVolume[day]) dailyMarketVolume[day] = {};
    if (!dailyMarketVolume[day][trade.conditionId]) dailyMarketVolume[day][trade.conditionId] = 0;
    if (dailyMarketVolume[day][trade.conditionId] >= BT_CONFIG.MAX_MARKET_PER_DAY) {
      skippedReasons["MARKET_CAP"] = (skippedReasons["MARKET_CAP"] || 0) + 1;
      continue;
    }

    afterLimits++;

    // Deterministic fill simulation: skip every Nth trade based on fill rate
    // e.g. FILL_RATE=0.75 → skip every 4th trade (1 / (1 - 0.75) = 4)
    const skipInterval = Math.round(1 / (1 - BT_CONFIG.FILL_RATE));
    if (afterLimits % skipInterval === 0) {
      skippedReasons["NO_FILL"] = (skippedReasons["NO_FILL"] || 0) + 1;
      continue;
    }

    const traderPrice = trade.price;
    const ourPrice = Math.min(0.99, traderPrice * (1 + BT_CONFIG.PRICE_BUFFER));
    const copySize = Math.min(BT_CONFIG.COPY_SIZE, BT_CONFIG.MAX_ORDER_SIZE);
    const shares = copySize / ourPrice;

    const resolution = resolutions.get(trade.conditionId);
    let outcome: "WIN" | "LOSS" | "UNRESOLVED" = "UNRESOLVED";
    let ourPnl = 0;
    let traderPnl = 0;

    if (resolution) {
      if (resolution.curPrice === 1) {
        outcome = "WIN";
        ourPnl = shares * 1.0 - copySize;
        traderPnl = (1.0 / traderPrice) * 1.0 - 1.0;
      } else if (resolution.curPrice === 0) {
        outcome = "LOSS";
        ourPnl = -copySize;
        traderPnl = -1.0;
      }
    }

    if (outcome === "UNRESOLVED") {
      skippedReasons["UNRESOLVED"] = (skippedReasons["UNRESOLVED"] || 0) + 1;
      continue;
    }

    simTrades.push({
      day,
      market: trade.title,
      entryPrice: Math.round(ourPrice * 1000) / 1000,
      traderPrice: Math.round(traderPrice * 1000) / 1000,
      outcome,
      pnl: Math.round(ourPnl * 100) / 100,
      traderPnl: Math.round(traderPnl * 100) / 100,
      slippage: Math.round((ourPrice - traderPrice) * 10000) / 100,
      filled: true,
    });

    dailyVolume[day] += copySize;
    dailyMarketVolume[day][trade.conditionId] += copySize;
  }

  const wins = simTrades.filter((t) => t.outcome === "WIN").length;
  const totalPnl = simTrades.reduce((s, t) => s + t.pnl, 0);
  const traderTotalPnl = simTrades.reduce((s, t) => s + t.traderPnl, 0);
  const avgSlippage = simTrades.length > 0
    ? simTrades.reduce((s, t) => s + t.slippage, 0) / simTrades.length
    : 0;
  const daysActive = new Set(simTrades.map((t) => t.day)).size;

  return {
    address: "",
    name: "",
    totalTrades: recent.length,
    withinWindow,
    afterLimits,
    simulated: afterLimits,
    filled: simTrades.length,
    simTrades,
    totalPnl: Math.round(totalPnl * 100) / 100,
    traderTheoreticalPnl: Math.round(traderTotalPnl * 100) / 100,
    winRate: simTrades.length > 0 ? Math.round((wins / simTrades.length) * 1000) / 10 : 0,
    avgSlippageCents: Math.round(avgSlippage * 100) / 100,
    daysActive,
    skippedReasons,
  };
}
