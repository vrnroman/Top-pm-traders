// Trader analysis logic — scoring and holding period calculation

export interface LeaderboardEntry {
  rank: string;
  proxyWallet: string;
  userName: string;
  vol: number;
  pnl: number;
}

export interface ClosedPosition {
  conditionId: string;
  avgPrice: number;
  totalBought: number;
  realizedPnl: number;
  curPrice: number;
  title: string;
  eventSlug: string;
  outcome: string;
  endDate: string;
  timestamp: number;
}

export interface ActivityTrade {
  conditionId: string;
  timestamp: number;
  side: string;
  size: number;
  price: number;
  title: string;
  type?: string;    // "TRADE" or "REDEEM"
  usdcSize?: number; // USD value of the trade
}

export interface TraderAnalysis {
  rank: string;
  address: string;
  userName: string;
  resolvedPositions: number;
  winRate: number;
  roi: number;
  worstLossPct: number;
  uniqueMarkets: number;
  profitConcentration: number;
  avgHoldingHours: number;
  tradesPerDay: number;
  maxDailyBurst: number;
  maxTradesPerMarketPerDay: number; // highest single-market trades in one day — detects DCA/scalping
  recentRoi: number;
  addressablePct: number;
  lastTradeDate: string;
  daysSinceLastTrade: number;
  peakHourUtc: number;          // most active hour (0-23 UTC)
  activeInWindowPct: number;    // % of trades within target timezone window
  score: number;
  passed: boolean;
  failReasons: string[];
  openConditionIds: Set<string>;
}

const FILTERS = {
  minResolvedPositions: 30,
  maxDaysInactive: 45,
  minRoi: -5,
  minAnnualizedRoi: 30,          // annualized ROI % — filters slow grinders
  minDaysActive: 90,             // 3 months min — find pros with sustained track record, not lucky runs
  minVolumeUsd: 500,             // min total buy volume — filters micro-traders
  minUniqueMarkets: 10,          // min market diversification
  maxRedeemBuyRatio: 5,          // max REDEEM/BUY count ratio — filters redemption collectors
  maxTradesPerMarketPerDay: 10,  // daily scalpers
  maxTradesPerMarket: 20,        // patient DCA accumulators
  maxTopTradeConcentration: 30,  // max % of total PnL from single best trade — filters lucky bets
};

/** Build map of conditionId → earliest BUY timestamp from activity. */
function buildOpenTimesMap(activity: ActivityTrade[]): Map<string, number> {
  const openTimes = new Map<string, number>();
  for (const t of activity) {
    if (t.side === "BUY" && t.conditionId) {
      const existing = openTimes.get(t.conditionId);
      if (!existing || t.timestamp < existing) {
        openTimes.set(t.conditionId, t.timestamp);
      }
    }
  }
  return openTimes;
}

function calculateHoldingPeriod(closed: ClosedPosition[], openTimes: Map<string, number>): number {
  const holdingHours: number[] = [];
  for (const pos of closed) {
    const openTime = openTimes.get(pos.conditionId);
    const closeTime = pos.timestamp;
    if (openTime && closeTime > openTime) {
      holdingHours.push((closeTime - openTime) / 3600);
    }
  }

  if (holdingHours.length === 0) return 0;
  return holdingHours.reduce((a, b) => a + b, 0) / holdingHours.length;
}

/** Calculate peak trading hour (UTC) and % of activity within a target window. */
function analyzeTimezone(activity: ActivityTrade[], windowStartUtc: number, windowEndUtc: number): { peakHourUtc: number; activeInWindowPct: number } {
  if (activity.length === 0) return { peakHourUtc: -1, activeInWindowPct: 0 };

  const hourCounts = new Array(24).fill(0);
  let inWindow = 0;

  for (const t of activity) {
    if (t.timestamp <= 0) continue;
    const hour = new Date(t.timestamp * 1000).getUTCHours();
    hourCounts[hour]++;
    // Handle windows that wrap around midnight (e.g., 22-06)
    if (windowStartUtc <= windowEndUtc) {
      if (hour >= windowStartUtc && hour < windowEndUtc) inWindow++;
    } else {
      if (hour >= windowStartUtc || hour < windowEndUtc) inWindow++;
    }
  }

  const peakHourUtc = hourCounts.indexOf(Math.max(...hourCounts));
  const activeInWindowPct = activity.length > 0 ? Math.round((inWindow / activity.length) * 100) : 0;
  return { peakHourUtc, activeInWindowPct };
}

export function analyzeTrader(
  entry: LeaderboardEntry,
  closed: ClosedPosition[],
  activity: ActivityTrade[],
  currentPositions: { conditionId: string }[],
  windowStartUtc = 0,
  windowEndUtc = 24,
): TraderAnalysis {
  const failReasons: string[] = [];

  // If activity is truncated (2500 = max from paginated fetch), history is longer than visible
  const activityTruncated = activity.length >= 2500;

  // PnL: prefer leaderboard data (all-time, not truncated) over activity-based calc
  let buyUsd = 0, sellUsd = 0, redeemUsd = 0;
  let buyCount = 0, redeemCount = 0;
  for (const t of activity) {
    const usd = t.usdcSize ?? (t.size * t.price);
    if (t.type === "REDEEM") { redeemUsd += usd; redeemCount++; }
    else if (t.side === "BUY") { buyUsd += usd; buyCount++; }
    else if (t.side === "SELL") { sellUsd += usd; }
  }
  // Use leaderboard PnL/vol when available (accurate all-time data, not truncated by activity window)
  const hasLeaderboardData = entry.pnl !== 0 || entry.vol > 0;
  const activityPnl = (sellUsd + redeemUsd) - buyUsd;
  const totalPnl = hasLeaderboardData ? entry.pnl : activityPnl;
  const totalVol = hasLeaderboardData ? entry.vol : buyUsd;
  const roi = totalVol > 0 ? (totalPnl / totalVol) * 100 : 0;
  // Flag: ROI from truncated activity is unreliable — could show profit while full history is loss
  const roiUnreliable = !hasLeaderboardData && activityTruncated;

  // Win rate: redeemed / resolved. Resolved = markets that appear in BOTH activity (buy) AND closed positions.
  // This excludes unresolved markets from denominator and prevents WR > 100%.
  const redeemedMarkets = new Set(activity.filter(t => t.type === "REDEEM" && t.conditionId).map(t => t.conditionId));
  const tradedMarkets = new Set(activity.filter(t => t.side === "BUY" && t.conditionId).map(t => t.conditionId));
  const closedMarkets = new Set(closed.map(p => p.conditionId).filter(Boolean));
  // Resolved = traded markets that also appear in closed positions (market ended or position exited)
  const resolvedMarkets = new Set([...tradedMarkets].filter(m => closedMarkets.has(m) || redeemedMarkets.has(m)));
  const winRate = resolvedMarkets.size > 0 ? Math.min((redeemedMarkets.size / resolvedMarkets.size) * 100, 100) : 0;

  // Worst loss: cashPnl from /positions is unreliable (ignores sells before resolution).
  // Approximate from activity: largest single-market loss = buyUsd on that market - any sells/redeems.
  const worstLoss = 0; // disabled — Data API cashPnl is broken, no reliable alternative

  const uniqueMarkets = new Set(
    activity.filter(t => t.conditionId).map(t => t.conditionId)
  ).size || new Set(closed.map((p) => p.eventSlug)).size;

  // Profit concentration from redemptions (largest wins vs total)
  const redeemAmounts = activity
    .filter(t => t.type === "REDEEM" && (t.usdcSize ?? 0) > 0)
    .map(t => t.usdcSize ?? 0)
    .sort((a, b) => b - a);
  const totalProfit = redeemAmounts.reduce((s, v) => s + v, 0);
  const top3 = redeemAmounts.slice(0, 3).reduce((s, v) => s + v, 0);
  const concentration = totalProfit > 0 ? (top3 / totalProfit) * 100 : 100;
  // Single best trade as % of total profit — detects lucky-bet traders
  const topTradeConcentration = totalProfit > 0 && redeemAmounts.length > 0
    ? (redeemAmounts[0] / totalProfit) * 100 : 0;
  const winTrades = redeemAmounts; // for addressablePct compatibility

  const openTimes = buildOpenTimesMap(activity);
  const avgHoldHours = calculateHoldingPeriod(closed, openTimes);

  const timestamps = closed.map((p) => p.timestamp).filter((t) => t > 0).sort();
  let tradesPerDay = 0;
  let maxDailyBurst = 0;
  if (timestamps.length >= 2) {
    const dailyCounts = new Map<string, number>();
    for (const ts of timestamps) {
      const day = new Date(ts * 1000).toISOString().slice(0, 10);
      dailyCounts.set(day, (dailyCounts.get(day) || 0) + 1);
    }
    const counts = Array.from(dailyCounts.values()).sort((a, b) => a - b);
    tradesPerDay = counts[Math.floor(counts.length / 2)];
    maxDailyBurst = counts[counts.length - 1];
  } else {
    tradesPerDay = closed.length;
    maxDailyBurst = closed.length;
  }

  // Sell ratio: SELLs as % of BUYs — distinguishes accumulators (buy-hold-redeem) from scalpers (buy-sell-buy-sell)
  const sellRatio = buyCount > 0 ? (activity.filter(t => t.side === "SELL").length / buyCount) * 100 : 0;
  const isAccumulator = sellRatio < 10; // <10% sells = position accumulator, not scalper

  // Per-market-per-day frequency — detects daily scalpers (only if NOT accumulator)
  let maxTradesPerMarketPerDay = 0;
  let maxTradesPerMarket = 0;
  {
    const marketDayCounts = new Map<string, number>();
    const marketTotalCounts = new Map<string, number>();
    for (const t of activity) {
      if (!t.conditionId || t.timestamp <= 0 || t.type === "REDEEM") continue;
      const day = new Date(t.timestamp * 1000).toISOString().slice(0, 10);
      const key = `${t.conditionId}|${day}`;
      marketDayCounts.set(key, (marketDayCounts.get(key) || 0) + 1);
      marketTotalCounts.set(t.conditionId, (marketTotalCounts.get(t.conditionId) || 0) + 1);
    }
    for (const count of marketDayCounts.values()) {
      if (count > maxTradesPerMarketPerDay) maxTradesPerMarketPerDay = count;
    }
    for (const count of marketTotalCounts.values()) {
      if (count > maxTradesPerMarket) maxTradesPerMarket = count;
    }
  }

  // Recent ROI from activity (last 30 days)
  const thirtyDaysAgo = Date.now() / 1000 - 30 * 86400;
  const recentActivity = activity.filter((t) => t.timestamp > thirtyDaysAgo);
  let recentBuy = 0, recentIn = 0;
  for (const t of recentActivity) {
    const usd = t.usdcSize ?? (t.size * t.price);
    if (t.type === "REDEEM") recentIn += usd;
    else if (t.side === "BUY") recentBuy += usd;
    else if (t.side === "SELL") recentIn += usd;
  }
  const recentRoi = recentBuy > 0 ? ((recentIn - recentBuy) / recentBuy) * 100 : 0;

  // Addressable %: redemptions with >2h holding period
  const profitableWithHold = activity.filter((t) => {
    if (t.type !== "REDEEM") return false;
    const openTime = openTimes.get(t.conditionId);
    if (!openTime) return false;
    return (t.timestamp - openTime) > 7200;
  });
  const addressablePct = winTrades.length > 0
    ? Math.round((profitableWithHold.length / winTrades.length) * 100)
    : 0;

  const lastTs = closed.length > 0 ? Math.max(...closed.map((p) => p.timestamp)) : 0;
  const lastTradeDate = lastTs > 0 ? new Date(lastTs * 1000).toISOString().slice(0, 10) : "unknown";
  const daysSince = lastTs > 0 ? Math.max(0, Math.floor((Date.now() / 1000 - lastTs) / 86400)) : 999;

  // Timezone activity analysis
  const tz = analyzeTimezone(activity, windowStartUtc, windowEndUtc);

  // Compute daysActive from activity timestamps (first to last)
  const activityTimestamps = activity.filter(t => t.timestamp > 0).map(t => t.timestamp);
  const daysActive = activityTimestamps.length >= 2
    ? Math.max(1, Math.ceil((Math.max(...activityTimestamps) - Math.min(...activityTimestamps)) / 86400))
    : 1;
  const annualizedRoi = totalVol > 0 && daysActive >= 1
    ? (totalPnl / totalVol) * (365 / daysActive) * 100
    : 0;
  const redeemBuyRatio = buyCount > 0 ? redeemCount / buyCount : 0;

  // Hard filters
  // Use tradedMarkets (unique BUY markets) for position count — resolvedMarkets undercounts due to truncated closed positions API
  if (tradedMarkets.size < FILTERS.minResolvedPositions) failReasons.push(`Positions ${tradedMarkets.size} < ${FILTERS.minResolvedPositions}`);
  if (daysSince > FILTERS.maxDaysInactive) failReasons.push(`Inactive ${daysSince}d > ${FILTERS.maxDaysInactive}d`);
  if (roi <= FILTERS.minRoi) failReasons.push(`ROI ${roi.toFixed(1)}% <= ${FILTERS.minRoi}%`);
  if (roiUnreliable && roi > 0) failReasons.push(`ROI unreliable (truncated activity, no leaderboard data)`);
  // Skip annualized ROI filter when activity is truncated — daysActive is underestimated, annualizedRoi overestimated
  if (!activityTruncated && annualizedRoi < FILTERS.minAnnualizedRoi && roi > 0) failReasons.push(`AnnROI ${annualizedRoi.toFixed(0)}% < ${FILTERS.minAnnualizedRoi}%`);
  // If activity truncated (≥2500 entries), trader has more history than visible — skip daysActive filter
  // beachboy4 example: "Joined Nov 2025" (5 months) but activity window shows only 74 days due to high volume
  if (!activityTruncated && daysActive < FILTERS.minDaysActive) failReasons.push(`History ${daysActive}d < ${FILTERS.minDaysActive}d`);
  if (totalVol < FILTERS.minVolumeUsd) failReasons.push(`Volume $${totalVol.toFixed(0)} < $${FILTERS.minVolumeUsd}`);
  if (uniqueMarkets < FILTERS.minUniqueMarkets) failReasons.push(`Markets ${uniqueMarkets} < ${FILTERS.minUniqueMarkets}`);
  if (redeemBuyRatio > FILTERS.maxRedeemBuyRatio) failReasons.push(`RedeemRatio ${redeemBuyRatio.toFixed(1)}x > ${FILTERS.maxRedeemBuyRatio}x`);
  // Accumulators (sell ratio < 10%) are exempt from scalper/DCA filters — they buy-hold-redeem
  if (!isAccumulator && maxTradesPerMarketPerDay > FILTERS.maxTradesPerMarketPerDay) failReasons.push(`Scalper ${maxTradesPerMarketPerDay}t/mkt/d > ${FILTERS.maxTradesPerMarketPerDay}`);
  if (!isAccumulator && maxTradesPerMarket > FILTERS.maxTradesPerMarket) failReasons.push(`DCA ${maxTradesPerMarket}t/mkt > ${FILTERS.maxTradesPerMarket}`);
  if (topTradeConcentration > FILTERS.maxTopTradeConcentration) failReasons.push(`LuckyBet top1=${topTradeConcentration.toFixed(0)}% > ${FILTERS.maxTopTradeConcentration}%`);

  let score = 0;
  score += winRate >= 65 ? 20 : winRate >= 55 ? 15 : winRate >= 50 ? 5 : -10;
  score += Math.min(Math.max(roi, 0) * 0.5, 25);
  score += Math.min(closed.length * 0.03, 10);
  score += Math.min(uniqueMarkets * 0.1, 8);
  score += daysSince < 3 ? 10 : daysSince < 7 ? 7 : daysSince < 30 ? 3 : 0;
  score += avgHoldHours >= 2 && avgHoldHours <= 12 ? 10 : avgHoldHours <= 24 ? 5 : avgHoldHours <= 72 ? 0 : -5;
  score += tradesPerDay <= 2 ? 12 : tradesPerDay <= 5 ? 8 : tradesPerDay <= 10 ? 0 : -10;
  score -= maxDailyBurst > 50 ? 15 : maxDailyBurst > 20 ? 5 : 0;
  // DCA/scalper penalty: many trades on same market in one day = bad copy target
  score -= maxTradesPerMarketPerDay > 15 ? 15 : maxTradesPerMarketPerDay > 8 ? 8 : maxTradesPerMarketPerDay > 4 ? 3 : 0;
  score -= worstLoss > 30 ? 15 : worstLoss > 20 ? 8 : worstLoss > 10 ? 3 : 0;
  score -= concentration > 80 ? 15 : concentration > 70 ? 8 : concentration > 50 ? 3 : 0;
  score += recentRoi > 10 ? 5 : recentRoi > 0 ? 2 : recentRoi > -10 ? 0 : -5;
  score += addressablePct > 80 ? 5 : addressablePct > 50 ? 2 : 0;
  // Timezone bonus: reward traders active in target window
  if (windowStartUtc !== 0 || windowEndUtc !== 24) {
    score += tz.activeInWindowPct >= 60 ? 10 : tz.activeInWindowPct >= 30 ? 5 : 0;
  }
  score = Math.round(Math.max(0, Math.min(100, score)));

  const openConditionIds = new Set(currentPositions.map((p) => p.conditionId));

  return {
    rank: entry.rank,
    address: entry.proxyWallet,
    userName: entry.userName || `${entry.proxyWallet.slice(0, 10)}...`,
    resolvedPositions: tradedMarkets.size,
    winRate: Math.round(winRate * 10) / 10,
    roi: Math.round(roi * 10) / 10,
    worstLossPct: Math.round(worstLoss * 10) / 10,
    uniqueMarkets,
    profitConcentration: Math.round(concentration),
    avgHoldingHours: Math.round(avgHoldHours),
    tradesPerDay,
    maxDailyBurst,
    maxTradesPerMarketPerDay,
    recentRoi: Math.round(recentRoi * 10) / 10,
    addressablePct,
    peakHourUtc: tz.peakHourUtc,
    activeInWindowPct: tz.activeInWindowPct,
    lastTradeDate,
    daysSinceLastTrade: daysSince,
    score,
    passed: failReasons.length === 0,
    failReasons,
    openConditionIds,
  };
}
