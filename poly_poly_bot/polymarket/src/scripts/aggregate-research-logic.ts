// Pure logic for research aggregation — scoring, ranking, classification

import { ResearchRun, ResearchTraderResult } from "./research-types";

export interface AggregatedTrader {
  address: string;
  userName?: string;
  runsSeen: number;
  screeningRuns: number;
  discoveryRuns: number;
  backtestRuns: number;
  timesPassed: number;
  passRate: number;
  avgScore: number;
  medianScore: number;
  bestScore: number;
  worstScore: number;
  scoreStdDev: number;
  avgRoi: number;
  avgWinRate: number;
  avgRecentRoi: number;
  avgBacktestRoi: number;
  medianBacktestRoi: number;
  avgSlippageCents: number;
  consistencyScore: number;
  finalRankScore: number;
  tier: "production" | "watchlist" | "reject";
}

export interface AggregatedOutput {
  createdAt: string;
  totalRuns: number;
  runBreakdown: { screening: number; discovery: number; backtest: number };
  uniqueTraders: number;
  minRunsFilter: number;
  productionCandidates: AggregatedTrader[];
  watchlist: AggregatedTrader[];
  allRanked: AggregatedTrader[];
}

function median(arr: number[]): number {
  if (arr.length === 0) return 0;
  const sorted = [...arr].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid];
}

function stdDev(arr: number[], mean: number): number {
  if (arr.length < 2) return 0;
  const variance = arr.reduce((sum, v) => sum + (v - mean) ** 2, 0) / arr.length;
  return Math.sqrt(variance);
}

function avg(arr: number[]): number {
  if (arr.length === 0) return 0;
  return arr.reduce((a, b) => a + b, 0) / arr.length;
}

/** Min-max normalize values to 0..100. If all equal → 50. Iterative min/max to avoid stack overflow. */
function buildNormalizer(values: number[]): (v: number) => number {
  let min = Infinity, max = -Infinity;
  for (const v of values) { if (v < min) min = v; if (v > max) max = v; }
  if (max === min) return () => 50;
  return (v: number) => ((v - min) / (max - min)) * 100;
}

/** Group all trader results by address (lowercased). */
export function mergeByTrader(entries: ResearchTraderResult[]): Map<string, ResearchTraderResult[]> {
  const map = new Map<string, ResearchTraderResult[]>();
  for (const e of entries) {
    const key = e.address.toLowerCase();
    const list = map.get(key);
    if (list) list.push(e);
    else map.set(key, [e]);
  }
  return map;
}

/** Compute metrics for a single trader from their result entries. */
export function computeMetrics(entries: ResearchTraderResult[]): Omit<AggregatedTrader, "tier"> {
  const address = entries[0].address.toLowerCase();
  const userName = entries.find((e) => e.userName)?.userName;

  const screeningRuns = entries.filter((e) => e.source === "leaderboard").length;
  const discoveryRuns = entries.filter((e) => e.source === "market-discovery").length;
  const backtestRuns = entries.filter((e) => e.source === "backtest").length;

  const scores = entries.filter((e) => e.score != null).map((e) => e.score!);
  const avgScore = avg(scores);

  const rois = entries.filter((e) => e.roi != null).map((e) => e.roi!);
  const winRates = entries.filter((e) => e.winRate != null).map((e) => e.winRate!);
  const recentRois = entries.filter((e) => e.recentRoi != null).map((e) => e.recentRoi!);
  const btRois = entries.filter((e) => e.backtestRoi != null).map((e) => e.backtestRoi!);
  const slippages = entries
    .filter((e) => e.backtestAvgSlippageCents != null)
    .map((e) => e.backtestAvgSlippageCents!);

  // Only count passes from screening/discovery — backtest has no pass/fail concept
  const timesPassed = entries.filter((e) => e.passed === true && e.source !== "backtest").length;
  const screenDiscovery = screeningRuns + discoveryRuns;

  return {
    address,
    userName,
    runsSeen: entries.length,
    screeningRuns,
    discoveryRuns,
    backtestRuns,
    timesPassed,
    passRate: screenDiscovery > 0 ? timesPassed / screenDiscovery : 0,
    avgScore,
    medianScore: median(scores),
    bestScore: scores.length > 0 ? Math.max(...scores) : 0,
    worstScore: scores.length > 0 ? Math.min(...scores) : 0,
    scoreStdDev: stdDev(scores, avgScore),
    avgRoi: avg(rois),
    avgWinRate: avg(winRates),
    avgRecentRoi: avg(recentRois),
    avgBacktestRoi: avg(btRois),
    medianBacktestRoi: median(btRois),
    avgSlippageCents: avg(slippages),
    consistencyScore: 0, // computed in rankAndClassify
    finalRankScore: 0,
  };
}

/** Rank and classify traders. Filters by minRuns and requireBacktest before ranking. */
export function rankAndClassify(
  traders: Omit<AggregatedTrader, "tier">[],
  minRuns: number,
  requireBacktest: boolean,
): AggregatedTrader[] {
  // Global filters — applied before ranking, not just classification
  const filtered = traders.filter((t) => {
    if (t.runsSeen < minRuns) return false;
    if (requireBacktest && t.backtestRuns === 0) return false;
    return true;
  });
  if (filtered.length === 0) return [];

  // Build normalizers from dataset
  const passRates = filtered.map((t) => t.passRate * 100);
  const relStdDevs = filtered.map((t) => {
    const rsd = t.avgScore !== 0 ? (t.scoreStdDev / t.avgScore) * 100 : t.scoreStdDev;
    return 100 - rsd;
  });
  const medScores = filtered.map((t) => t.medianScore);
  const avgScores = filtered.map((t) => t.avgScore);
  const medBtRois = filtered.filter((t) => t.backtestRuns > 0 && t.medianBacktestRoi > 0)
    .map((t) => t.medianBacktestRoi);

  const normPassRate = buildNormalizer(passRates);
  const normRelStdDev = buildNormalizer(relStdDevs);
  const normMedScore = buildNormalizer(medScores);
  const normAvgScore = buildNormalizer(avgScores);
  const normBtRoi = medBtRois.length > 0 ? buildNormalizer(medBtRois) : () => 0;

  const result: AggregatedTrader[] = filtered.map((t) => {
    const rsd = t.avgScore !== 0 ? (t.scoreStdDev / t.avgScore) * 100 : t.scoreStdDev;

    const consistency =
      normPassRate(t.passRate * 100) * 0.4 +
      normRelStdDev(100 - rsd) * 0.3 +
      normMedScore(t.medianScore) * 0.3;

    const backtestBonus =
      t.backtestRuns > 0 && t.medianBacktestRoi > 0 ? normBtRoi(t.medianBacktestRoi) : 0;

    const finalRank =
      consistency * 0.4 +
      normAvgScore(t.avgScore) * 0.3 +
      t.passRate * 100 * 0.2 +
      backtestBonus * 0.1;

    return { ...t, consistencyScore: round2(consistency), finalRankScore: round2(finalRank), tier: "reject" as const };
  });

  // Classify
  for (const t of result) {
    const hasPositiveReturn = t.avgRoi > 0 || (t.screeningRuns === 0 && t.avgBacktestRoi > 0);
    const hasScreenOrDisc = t.screeningRuns + t.discoveryRuns >= 1;
    const meetsBacktest = !requireBacktest || t.backtestRuns > 0;

    if (
      t.finalRankScore >= 60 &&
      t.passRate >= 0.7 &&
      t.runsSeen >= minRuns &&
      hasPositiveReturn &&
      hasScreenOrDisc &&
      meetsBacktest
    ) {
      t.tier = "production";
    } else if (t.finalRankScore >= 30 && t.passRate >= 0.4 && t.runsSeen >= Math.max(2, minRuns)) {
      t.tier = "watchlist";
    }
  }

  result.sort((a, b) => b.finalRankScore - a.finalRankScore);
  return result;
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

/** Detect research run type from legacy filename. */
export function detectTypeFromFilename(filename: string): ResearchRun["type"] {
  if (filename.startsWith("leaderboard-")) return "screening";
  if (filename.startsWith("market-discovery-")) return "discovery";
  if (filename.startsWith("backtest-")) return "backtest";
  return "screening";
}

/** Map old TraderAnalysis JSON to ResearchTraderResult. */
export function mapOldFormat(
  t: Record<string, unknown>,
  type: ResearchRun["type"],
): ResearchTraderResult {
  const sourceMap: Record<string, ResearchTraderResult["source"]> = {
    screening: "leaderboard",
    discovery: "market-discovery",
    backtest: "backtest",
  };
  return {
    address: String(t.address || ""),
    userName: t.userName ? String(t.userName) : undefined,
    score: typeof t.score === "number" ? t.score : undefined,
    passed: typeof t.passed === "boolean" ? t.passed : undefined,
    failReasons: Array.isArray(t.failReasons) ? t.failReasons.map(String) : undefined,
    roi: typeof t.roi === "number" ? t.roi : undefined,
    winRate: typeof t.winRate === "number" ? t.winRate : undefined,
    resolvedPositions: typeof t.resolvedPositions === "number" ? t.resolvedPositions : undefined,
    uniqueMarkets: typeof t.uniqueMarkets === "number" ? t.uniqueMarkets : undefined,
    avgHoldingHours: typeof t.avgHoldingHours === "number" ? t.avgHoldingHours : undefined,
    tradesPerDay: typeof t.tradesPerDay === "number" ? t.tradesPerDay : undefined,
    maxTradesPerMarketPerDay: typeof t.maxTradesPerMarketPerDay === "number" ? t.maxTradesPerMarketPerDay : undefined,
    recentRoi: typeof t.recentRoi === "number" ? t.recentRoi : undefined,
    backtestRoi: typeof t.backtestRoi === "number" ? t.backtestRoi : undefined,
    backtestFilled: typeof t.backtestFilled === "number" ? t.backtestFilled : undefined,
    backtestTotalTrades: typeof t.backtestTotalTrades === "number" ? t.backtestTotalTrades : undefined,
    backtestAvgSlippageCents: typeof t.backtestAvgSlippageCents === "number" ? t.backtestAvgSlippageCents : undefined,
    backtestWinRate: typeof t.backtestWinRate === "number" ? t.backtestWinRate : undefined,
    source: sourceMap[type],
  };
}
