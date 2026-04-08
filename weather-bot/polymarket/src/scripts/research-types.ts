// Unified envelope format for all research script outputs

import fs from "fs";
import path from "path";

export interface ResearchRun {
  version: 1;
  type: "screening" | "discovery" | "backtest";
  createdAt: string; // ISO
  config: Record<string, unknown>;
  traders: ResearchTraderResult[];
}

export interface ResearchTraderResult {
  address: string;
  userName?: string;
  score?: number;
  passed?: boolean;
  failReasons?: string[];
  roi?: number;
  winRate?: number;
  resolvedPositions?: number;
  uniqueMarkets?: number;
  avgHoldingHours?: number;
  tradesPerDay?: number;
  maxTradesPerMarketPerDay?: number;
  recentRoi?: number;
  // backtest-specific
  backtestRoi?: number;
  backtestFilled?: number;
  backtestTotalTrades?: number;
  backtestAvgSlippageCents?: number;
  backtestWinRate?: number;
  source: "leaderboard" | "market-discovery" | "backtest";
}

/** Save a research run to data/research/{prefix}-{timestamp}.json */
export function saveResearchRun(run: ResearchRun, prefix: string): string {
  const outDir = path.resolve(process.cwd(), "data", "research");
  if (!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true });
  const timestamp = new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-");
  const outFile = path.join(outDir, `${prefix}-${timestamp}.json`);
  fs.writeFileSync(outFile, JSON.stringify(run, null, 2));
  console.log(`\nResearch saved: ${outFile}`);
  return outFile;
}
