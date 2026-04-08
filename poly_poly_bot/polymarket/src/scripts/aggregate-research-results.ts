/**
 * Research Aggregator — merges multiple research runs into stability-ranked trader shortlist.
 *
 * Usage:
 *   npx tsx src/scripts/aggregate-research-results.ts
 *   npx tsx src/scripts/aggregate-research-results.ts --dir data/research --min-runs 2 --top 20 --require-backtest --json
 */

import fs from "fs";
import path from "path";
import { ResearchRun, ResearchTraderResult } from "./research-types";
import {
  AggregatedTrader,
  AggregatedOutput,
  mergeByTrader,
  computeMetrics,
  rankAndClassify,
  detectTypeFromFilename,
  mapOldFormat,
} from "./aggregate-research-logic";

/** Load and parse all research run JSONs from given directories. */
export function loadAllRuns(dirs: string[]): ResearchRun[] {
  const runs: ResearchRun[] = [];
  for (const dir of dirs) {
    const abs = path.resolve(process.cwd(), dir);
    if (!fs.existsSync(abs)) continue;
    const files = fs.readdirSync(abs).filter((f) => f.endsWith(".json"));
    for (const file of files) {
      try {
        const filePath = path.join(abs, file);
        const raw = JSON.parse(fs.readFileSync(filePath, "utf-8"));
        if (Array.isArray(raw)) {
          // Old format — array of TraderAnalysis
          const type = detectTypeFromFilename(file);
          const stat = fs.statSync(filePath);
          const traders: ResearchTraderResult[] = raw.map((t) => mapOldFormat(t, type)).filter(t => t.address);
          runs.push({ version: 1, type, createdAt: stat.mtime.toISOString(), config: {}, traders });
        } else if (raw && typeof raw === "object" && raw.version === 1) {
          // New envelope format
          runs.push(raw as ResearchRun);
        } else {
          console.warn(`Skipping unrecognized format: ${file}`);
        }
      } catch (err) {
        console.warn(`Failed to parse ${file}: ${err instanceof Error ? err.message : err}`);
      }
    }
  }
  return runs;
}


function shortAddr(addr: string): string {
  return addr.length > 12 ? `${addr.slice(0, 6)}...${addr.slice(-4)}` : addr;
}

function fmtPct(v: number): string {
  return `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`;
}

function printResults(
  runs: ResearchRun[],
  ranked: AggregatedTrader[],
  minRuns: number,
  topN: number,
): void {
  const sc = runs.filter((r) => r.type === "screening").length;
  const dc = runs.filter((r) => r.type === "discovery").length;
  const bt = runs.filter((r) => r.type === "backtest").length;
  const allTraders = new Set(runs.flatMap((r) => r.traders.map((t) => t.address.toLowerCase())));
  const filtered = ranked.filter((t) => t.runsSeen >= minRuns);

  console.log(`\n=== Research Aggregation ===`);
  console.log(`Runs loaded: ${runs.length} (${sc} screening, ${dc} discovery, ${bt} backtest)`);
  console.log(`Unique traders: ${allTraders.size}`);
  console.log(`With >=${minRuns} runs: ${filtered.length}`);

  const prod = ranked.filter((t) => t.tier === "production");
  const watch = ranked.filter((t) => t.tier === "watchlist");

  if (prod.length > 0) {
    console.log(`\n=== Production Candidates (${prod.length}) ===`);
    for (let i = 0; i < prod.length; i++) {
      const t = prod[i];
      const bt = t.avgBacktestRoi ? `bt=${fmtPct(t.avgBacktestRoi)}` : "";
      console.log(
        `  #${i + 1}  ${shortAddr(t.address)}  rank=${t.finalRankScore.toFixed(0)}  ROI=${fmtPct(t.avgRoi)}  WR=${t.avgWinRate.toFixed(1)}%  pass=${(t.passRate * 100).toFixed(0)}%  runs=${t.runsSeen}  ${bt}`,
      );
    }
  }

  if (watch.length > 0) {
    console.log(`\n=== Watchlist (${watch.length}) ===`);
    for (let i = 0; i < Math.min(watch.length, topN); i++) {
      const t = watch[i];
      console.log(
        `  #${i + 1}  ${shortAddr(t.address)}  rank=${t.finalRankScore.toFixed(0)}  ROI=${fmtPct(t.avgRoi)}  pass=${(t.passRate * 100).toFixed(0)}%  runs=${t.runsSeen}`,
      );
    }
  }

  const top = filtered.slice(0, topN);
  if (top.length > 0) {
    console.log(`\n=== Top ${topN} by Stability ===`);
    console.log(`  #  | Address         | FinalRank | AvgScore | PassRate | ROI     | WR    | Runs | BT ROI | Tier`);
    for (let i = 0; i < top.length; i++) {
      const t = top[i];
      console.log(
        `  ${String(i + 1).padStart(2)} | ${shortAddr(t.address).padEnd(15)} | ${t.finalRankScore.toFixed(1).padStart(9)} | ${t.avgScore.toFixed(1).padStart(8)} | ${(t.passRate * 100).toFixed(0).padStart(7)}% | ${fmtPct(t.avgRoi).padStart(7)} | ${t.avgWinRate.toFixed(1).padStart(5)} | ${String(t.runsSeen).padStart(4)} | ${fmtPct(t.medianBacktestRoi).padStart(6)} | ${t.tier}`,
      );
    }
  }
}

function saveJson(ranked: AggregatedTrader[], runs: ResearchRun[], minRuns: number): string {
  const sc = runs.filter((r) => r.type === "screening").length;
  const dc = runs.filter((r) => r.type === "discovery").length;
  const bt = runs.filter((r) => r.type === "backtest").length;

  const output: AggregatedOutput = {
    createdAt: new Date().toISOString(),
    totalRuns: runs.length,
    runBreakdown: { screening: sc, discovery: dc, backtest: bt },
    uniqueTraders: new Set(runs.flatMap((r) => r.traders.map((t) => t.address.toLowerCase()))).size,
    minRunsFilter: minRuns,
    productionCandidates: ranked.filter((t) => t.tier === "production"),
    watchlist: ranked.filter((t) => t.tier === "watchlist"),
    allRanked: ranked,
  };

  const outDir = path.resolve(process.cwd(), "data", "research");
  if (!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true });
  const outFile = path.join(outDir, "aggregated-results.json");
  fs.writeFileSync(outFile, JSON.stringify(output, null, 2));
  return outFile;
}

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  let dirs = ["data/research", "data/screening"];
  let minRuns = 2;
  let topN = 20;
  let jsonOutput = false;
  let requireBacktest = false;

  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--dir" && args[i + 1]) dirs = [args[++i]];
    if (args[i] === "--min-runs" && args[i + 1]) minRuns = parseInt(args[++i]) || 2;
    if (args[i] === "--top" && args[i + 1]) topN = parseInt(args[++i]) || 20;
    if (args[i] === "--json") jsonOutput = true;
    if (args[i] === "--require-backtest") requireBacktest = true;
  }

  const runs = loadAllRuns(dirs);
  if (runs.length === 0) {
    console.log("No research runs found. Run screening/discovery scripts first.");
    return;
  }

  const allEntries = runs.flatMap((r) => r.traders);
  const grouped = mergeByTrader(allEntries);
  const metrics = Array.from(grouped.values()).map(computeMetrics);
  const ranked = rankAndClassify(metrics, minRuns, requireBacktest);

  printResults(runs, ranked, minRuns, topN);

  if (jsonOutput) {
    const outFile = saveJson(ranked, runs, minRuns);
    console.log(`\nJSON saved: ${outFile}`);
  }
}

main().catch((err: unknown) => {
  console.error("Fatal:", err instanceof Error ? err.message : err);
  process.exit(1);
});
