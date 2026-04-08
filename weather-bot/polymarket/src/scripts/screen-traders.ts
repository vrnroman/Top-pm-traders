/**
 * Trader Screening Script — v4 (soft scoring)
 *
 * Usage:
 *   npx tsx src/scripts/screen-traders.ts
 *   npx tsx src/scripts/screen-traders.ts --target 20
 *   npx tsx src/scripts/screen-traders.ts --category SPORTS --target 10
 *   npx tsx src/scripts/screen-traders.ts --top 15 --max-pages 50
 */

import axios from "axios";
import { sleep } from "../utils";
import { errorMessage } from "../types";
import { analyzeTrader, TraderAnalysis, LeaderboardEntry, ClosedPosition, ActivityTrade } from "./screen-traders-analysis";
import { printResults } from "./screen-traders-output";
import { CONFIG } from "../config";
import { ResearchRun, ResearchTraderResult, saveResearchRun } from "./research-types";
import { loadScanCache, saveScanCache, addToCache } from "./scan-cache";

const DATA_API = CONFIG.dataApiUrl;
const DELAY_MS = 1000;

/** Parse dollar string like "$3.5M", "$149.2K", "$26.7K" to number */
function parseDollar(s: string): number {
  const clean = s.replace(/[<>$,\s]/g, "");
  if (clean.endsWith("B")) return parseFloat(clean) * 1e9;
  if (clean.endsWith("M")) return parseFloat(clean) * 1e6;
  if (clean.endsWith("K")) return parseFloat(clean) * 1e3;
  return parseFloat(clean) || 0;
}

/** Scrape Biggest Win from Polymarket profile page. Returns 0 if unavailable. */
async function fetchBiggestWin(username: string): Promise<number> {
  if (!username || username.includes("...")) return 0;
  try {
    const res = await axios.get(`https://polymarket.com/@${username}`, {
      timeout: 15000, headers: { "User-Agent": "Mozilla/5.0" },
    });
    const chunks = (res.data as string).match(/text-lg font-medium[^>]*>([^<]+)</g) || [];
    const values = chunks.map((s: string) => s.replace(/.*>/, ""));
    // Order: [Positions Value, Biggest Win, Predictions]
    return values[1] ? parseDollar(values[1]) : 0;
  } catch { return 0; }
}

const FILTERS = {
  minResolvedPositions: 30,
  maxDaysInactive: 45,
  minRoi: -5,
  targetWinRate: 55,
  targetMaxDD: 20,
  targetMaxConcentration: 70,
  targetMinMarkets: 10,
  targetMaxTradesPerDay: 5,
  maxOverlapPercent: 20,
};

// ---- API CALLS ----

async function fetchLeaderboardPage(category: string, limit: number, offset: number): Promise<LeaderboardEntry[]> {
  try {
    const res = await axios.get(`${DATA_API}/v1/leaderboard`, {
      params: { category, timePeriod: "ALL", orderBy: "PNL", limit, offset },
      timeout: 15000,
    });
    return Array.isArray(res.data) ? res.data : [];
  } catch { return []; }
}


async function fetchClosedPositions(address: string, maxPages = 10): Promise<ClosedPosition[]> {
  // Use /positions?status=closed — returns ALL closed positions (wins AND losses).
  // /closed-positions only returns winning positions — produces inflated WR/ROI.
  const all: ClosedPosition[] = [];
  for (let page = 0; page < maxPages; page++) {
    try {
      const res = await axios.get(`${DATA_API}/positions`, {
        params: { user: address, status: "closed", limit: 50, offset: page * 50 },
        timeout: 15000,
      });
      const batch = Array.isArray(res.data) ? res.data : [];
      // Map /positions fields to ClosedPosition shape
      const mapped: ClosedPosition[] = batch.map((p: Record<string, unknown>) => ({
        conditionId: String(p.conditionId || ""),
        avgPrice: Number(p.avgPrice) || 0,
        totalBought: (Number(p.size) || 0) * (Number(p.avgPrice) || 0),
        realizedPnl: Number(p.cashPnl) || 0,
        curPrice: Number(p.curPrice) || 0,
        title: String(p.title || ""),
        eventSlug: String(p.eventSlug || p.conditionId || ""),
        outcome: String(p.outcome || ""),
        endDate: String(p.endDate || ""),
        timestamp: Number(p.timestamp) || (p.endDate ? new Date(String(p.endDate)).getTime() / 1000 : 0),
      }));
      all.push(...mapped);
      if (batch.length < 50) break;
      await sleep(300);
    } catch { break; }
  }
  return all;
}

async function fetchCurrentPositions(address: string): Promise<{ conditionId: string }[]> {
  try {
    const res = await axios.get(`${DATA_API}/positions`, {
      params: { user: address },
      timeout: 15000,
    });
    return Array.isArray(res.data) ? res.data : [];
  } catch { return []; }
}

async function fetchActivity(address: string, maxPages = 5): Promise<ActivityTrade[]> {
  // Fetch ALL activity types (TRADE + REDEEM) with pagination — REDEEM entries needed for accurate PnL
  const all: ActivityTrade[] = [];
  for (let page = 0; page < maxPages; page++) {
    try {
      const res = await axios.get(`${DATA_API}/activity`, {
        params: { user: address, limit: 500, offset: page * 500 },
        timeout: 15000,
      });
      const batch = Array.isArray(res.data) ? res.data : [];
      for (const t of batch) {
        all.push({
          conditionId: String(t.conditionId || ""),
          timestamp: typeof t.timestamp === "number" ? t.timestamp : 0,
          side: String(t.side || ""),
          size: parseFloat(String(t.size || "0")),
          price: parseFloat(String(t.price || "0")),
          title: String(t.title || ""),
          type: String(t.type || "TRADE"),
          usdcSize: parseFloat(String(t.usdcSize || "0")) || undefined,
        });
      }
      if (batch.length < 500) break;
      await sleep(300);
    } catch { break; }
  }
  return all;
}

// ---- MAIN ----

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  let category = "OVERALL";
  let targetPass = 20;   // scan until we find this many PASS traders
  let maxPages = 100;    // safety cap — don't scan forever
  let topN = 10;
  let windowStartUtc = 0;
  let windowEndUtc = 24;

  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--category" && args[i + 1]) category = args[i + 1].toUpperCase();
    if (args[i] === "--target" && args[i + 1]) targetPass = parseInt(args[i + 1]) || 20;
    if (args[i] === "--max-pages" && args[i + 1]) maxPages = parseInt(args[i + 1]) || 100;
    if (args[i] === "--top" && args[i + 1]) topN = parseInt(args[i + 1]) || 10;
    if (args[i] === "--tz" && args[i + 1]) {
      const [s, e] = args[i + 1].split("-").map(Number);
      if (!isNaN(s) && !isNaN(e)) { windowStartUtc = s; windowEndUtc = e; }
    }
  }

  const scanCache = loadScanCache();
  const cachedSkips = scanCache.size;

  const tzLabel = windowStartUtc === 0 && windowEndUtc === 24
    ? "all hours"
    : `${String(windowStartUtc).padStart(2, "0")}:00–${String(windowEndUtc).padStart(2, "0")}:00 UTC`;

  console.log(`Screening: ${category}, target ${targetPass} PASS traders (max ${maxPages} pages)`);
  console.log(`Scan cache: ${cachedSkips} addresses cached (7d TTL)`);
  console.log(`Timezone: ${tzLabel}\n`);

  const results: TraderAnalysis[] = [];
  let passCount = 0;
  let scanned = 0;
  let skipped = 0;

  for (let page = 0; page < maxPages && passCount < targetPass; page++) {
    const batch = await fetchLeaderboardPage(category, 50, page * 50);
    if (batch.length === 0) break;

    for (const e of batch) {
      if (passCount >= targetPass) break;

      // Skip recently scanned
      const cached = scanCache.get(e.proxyWallet.toLowerCase());
      if (cached) { skipped++; continue; }

      const name = (e.userName || `${e.proxyWallet.slice(0, 10)}...`).slice(0, 20);
      scanned++;
      process.stdout.write(`[${scanned} scanned, ${passCount}/${targetPass} pass] ${name.padEnd(20)}`);

      const [closed, activity, current] = await Promise.all([
        fetchClosedPositions(e.proxyWallet, 10),
        fetchActivity(e.proxyWallet),
        fetchCurrentPositions(e.proxyWallet),
      ]);

      const analysis = analyzeTrader(e, closed, activity, current, windowStartUtc, windowEndUtc);
      results.push(analysis);
      addToCache(scanCache, e.proxyWallet, analysis.passed);

      // Post-pass checks (expensive — only for candidates that passed all other filters)
      if (analysis.passed) {
        // Check 1: Biggest win concentration from profile
        if (e.pnl > 0) {
          const bigWin = await fetchBiggestWin(e.userName || "");
          if (bigWin > 0) {
            const conc = (bigWin / e.pnl) * 100;
            if (conc > 30) {
              analysis.passed = false;
              analysis.failReasons.push(`BigWin ${conc.toFixed(0)}% of PnL ($${(bigWin/1000).toFixed(0)}K / $${(e.pnl/1000).toFixed(0)}K)`);
            }
          }
        }
        // Check 2: Unrealized PnL — open positions losing more than realized profit
        if (analysis.passed) {
          let unrealized = 0;
          for (const p of current as Record<string, unknown>[]) {
            const size = Number(p.size) || 0;
            const avg = Number(p.avgPrice) || 0;
            const cur = Number(p.curPrice) || 0;
            if (size > 0) unrealized += (cur - avg) * size;
          }
          if (unrealized < 0 && e.pnl > 0 && Math.abs(unrealized) > e.pnl * 0.5) {
            analysis.passed = false;
            analysis.failReasons.push(`Unrealized -$${(Math.abs(unrealized)/1000).toFixed(0)}K > 50% of PnL $${(e.pnl/1000).toFixed(0)}K`);
          }
        }
      }
      if (analysis.passed) passCount++;
      const status = analysis.passed ? "✓ PASS" : `✗ ${analysis.failReasons[0]?.slice(0, 25) || "FAIL"}`;
      const tzInfo = analysis.peakHourUtc >= 0 ? ` | peak=${String(analysis.peakHourUtc).padStart(2, "0")}h tz=${analysis.activeInWindowPct}%` : "";
      console.log(` ${String(closed.length).padStart(3)} closed | score=${String(analysis.score).padStart(2)}${tzInfo} | ${status}`);

      await sleep(DELAY_MS);
    }

    if (batch.length < 50) break; // no more pages
  }

  saveScanCache(scanCache);
  console.log(`\nDone: ${scanned} scanned, ${skipped} cache-skipped, ${passCount} passed\n`);

  printResults(results, topN);

  // Persist results in unified research envelope
  const researchTraders: ResearchTraderResult[] = results.map(r => ({
    address: r.address,
    userName: r.userName,
    score: r.score,
    passed: r.passed,
    failReasons: r.failReasons,
    roi: r.roi,
    winRate: r.winRate,
    resolvedPositions: r.resolvedPositions,
    uniqueMarkets: r.uniqueMarkets,
    avgHoldingHours: r.avgHoldingHours,
    tradesPerDay: r.tradesPerDay,
    maxTradesPerMarketPerDay: r.maxTradesPerMarketPerDay,
    recentRoi: r.recentRoi,
    source: "leaderboard" as const,
  }));
  const run: ResearchRun = {
    version: 1,
    type: "screening",
    createdAt: new Date().toISOString(),
    config: { category, targetPass, maxPages, topN, windowStartUtc, windowEndUtc },
    traders: researchTraders,
  };
  saveResearchRun(run, "leaderboard");
}

main().catch((err: unknown) => {
  console.error("Fatal:", errorMessage(err));
  process.exit(1);
});
