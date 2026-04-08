/**
 * Market-Based Trader Discovery — finds profitable traders outside the leaderboard
 *
 * Approach: scan active markets via Data API, collect unique trader addresses,
 * filter by activity volume, then analyze with existing scoring pipeline.
 *
 * Usage:
 *   npx tsx src/scripts/discover-traders-market.ts
 *   npx tsx src/scripts/discover-traders-market.ts --markets 20 --min-trades 5 --top 10
 *   npx tsx src/scripts/discover-traders-market.ts --tz 6-15
 */

import axios from "axios";
import { sleep } from "../utils";
import { errorMessage } from "../types";
import { analyzeTrader, TraderAnalysis, ClosedPosition, ActivityTrade } from "./screen-traders-analysis";
import { printResults } from "./screen-traders-output";
import { CONFIG } from "../config";
import { ResearchRun, ResearchTraderResult, saveResearchRun } from "./research-types";
import { loadScanCache, saveScanCache } from "./scan-cache";

const DATA_API = CONFIG.dataApiUrl;
const DELAY_MS = 800;

// --- CLI args ---
const args = process.argv.slice(2);
let marketCount = 20;
let minTrades = 3;
let topN = 15;
let targetPass = 20;
let windowStartUtc = 0;
let windowEndUtc = 24;

for (let i = 0; i < args.length; i++) {
  if (args[i] === "--markets" && args[i + 1]) marketCount = parseInt(args[i + 1]);
  if (args[i] === "--min-trades" && args[i + 1]) minTrades = parseInt(args[i + 1]);
  if (args[i] === "--top" && args[i + 1]) topN = parseInt(args[i + 1]);
  if (args[i] === "--target" && args[i + 1]) targetPass = parseInt(args[i + 1]) || 20;
  if (args[i] === "--tz" && args[i + 1]) {
    const [s, e] = args[i + 1].split("-").map(Number);
    if (!isNaN(s) && !isNaN(e)) { windowStartUtc = s; windowEndUtc = e; }
  }
}

// --- Phase 1: Find active markets ---

interface MarketInfo {
  conditionId: string;
  questionId: string;
  title: string;
  tokens: { token_id: string; outcome: string }[];
}

async function fetchActiveMarkets(count: number): Promise<MarketInfo[]> {
  const markets: MarketInfo[] = [];
  // Use Data API sampling endpoint — returns recent active markets sorted by volume
  while (markets.length < count) {
    try {
      const res = await axios.get(`https://gamma-api.polymarket.com/markets`, {
        params: { limit: 50, active: true, closed: false, order: "volume24hr", ascending: false, offset: markets.length },
        timeout: 15000,
      });
      const batch = Array.isArray(res.data) ? res.data : [];
      if (batch.length === 0) break;
      for (const m of batch) {
        if (!m.conditionId) continue;
        let tokens: { token_id: string; outcome: string }[] = [];
        try {
          const ids = JSON.parse(m.clobTokenIds || "[]");
          const outcomes = JSON.parse(m.outcomes || '["Yes","No"]');
          tokens = ids.map((id: string, i: number) => ({ token_id: id, outcome: outcomes[i] || `Outcome ${i}` }));
        } catch { /* skip malformed */ }
        markets.push({
          conditionId: m.conditionId,
          questionId: m.questionId || "",
          title: m.question || m.title || "?",
          tokens,
        });
      }
      if (batch.length < 50) break;
      await sleep(DELAY_MS);
    } catch (err: unknown) {
      console.error(`Failed to fetch markets: ${errorMessage(err)}`);
      break;
    }
  }
  return markets.slice(0, count);
}

// --- Phase 2: Collect traders from market activity ---

interface TraderCandidate {
  address: string;
  tradeCount: number;
  totalVolume: number;
  markets: Set<string>;
}

async function collectTradersFromMarkets(markets: MarketInfo[]): Promise<Map<string, TraderCandidate>> {
  const traders = new Map<string, TraderCandidate>();

  for (let i = 0; i < markets.length; i++) {
    const m = markets[i];
    process.stdout.write(`[${String(i + 1).padStart(3)}/${markets.length}] Scanning "${m.title.slice(0, 40)}"...`);

    for (const token of m.tokens) {
      try {
        // Use /trades endpoint — returns recent trades per token with proxyWallet
        const res = await axios.get(`${DATA_API}/trades`, {
          params: { asset: token.token_id, limit: 100 },
          timeout: 15000,
        });

        if (!Array.isArray(res.data)) continue;

        for (const trade of res.data) {
          const addr = trade.proxyWallet;
          if (!addr) continue;
          const vol = parseFloat(trade.size || "0") * parseFloat(trade.price || "0");
          if (!traders.has(addr)) {
            traders.set(addr, { address: addr, tradeCount: 0, totalVolume: 0, markets: new Set() });
          }
          const t = traders.get(addr)!;
          t.tradeCount++;
          t.totalVolume += vol;
          t.markets.add(m.conditionId);
        }
        await sleep(300);
      } catch {
        // Skip failed token queries
      }
    }

    const unique = traders.size;
    console.log(` ${unique} traders found so far`);
    if (i < markets.length - 1) await sleep(DELAY_MS);
  }

  return traders;
}

// --- Phase 3: Filter and analyze ---

async function fetchClosedPositions(address: string, maxPages = 5): Promise<ClosedPosition[]> {
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
      await sleep(DELAY_MS);
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

// --- Phase 4: Check if trader is already on leaderboard ---

async function fetchLeaderboardAddresses(): Promise<Set<string>> {
  const addresses = new Set<string>();
  try {
    for (let page = 0; page < 4; page++) {
      const res = await axios.get(`${DATA_API}/v1/leaderboard`, {
        params: { category: "OVERALL", timePeriod: "ALL", orderBy: "PNL", limit: 50, offset: page * 50 },
        timeout: 15000,
      });
      const batch = Array.isArray(res.data) ? res.data : [];
      for (const e of batch) {
        if (e.proxyWallet) addresses.add(e.proxyWallet.toLowerCase());
      }
      if (batch.length < 50) break;
      await sleep(DELAY_MS);
    }
  } catch { /* ignore */ }
  return addresses;
}

// --- Main ---

async function main(): Promise<void> {
  const tzLabel = windowStartUtc === 0 && windowEndUtc === 24
    ? "all hours"
    : `${String(windowStartUtc).padStart(2, "0")}:00–${String(windowEndUtc).padStart(2, "0")}:00 UTC`;

  console.log(`\n=== On-Chain Trader Discovery ===`);
  console.log(`Markets to scan: ${marketCount} | Min trades: ${minTrades} | Top: ${topN}`);
  console.log(`Timezone window: ${tzLabel}\n`);

  // Phase 1: Active markets
  console.log("Phase 1: Fetching active markets...");
  const markets = await fetchActiveMarkets(marketCount);
  console.log(`Found ${markets.length} active markets.\n`);

  // Phase 2: Collect traders
  console.log("Phase 2: Scanning market activity for traders...");
  const allTraders = await collectTradersFromMarkets(markets);
  console.log(`\nTotal unique traders: ${allTraders.size}`);

  // Filter by minimum trades
  const candidates = [...allTraders.values()]
    .filter(t => t.tradeCount >= minTrades && t.markets.size >= 2)
    .sort((a, b) => b.totalVolume - a.totalVolume);
  console.log(`Candidates (>=${minTrades} trades, >=2 markets): ${candidates.length}\n`);

  // Phase 3: Check leaderboard overlap
  console.log("Phase 3: Checking leaderboard overlap...");
  const leaderboardAddrs = await fetchLeaderboardAddresses();
  const newTraders = candidates.filter(c => !leaderboardAddrs.has(c.address.toLowerCase()));
  const overlapCount = candidates.length - newTraders.length;
  console.log(`Leaderboard traders: ${leaderboardAddrs.size} | Overlap: ${overlapCount} | New: ${newTraders.length}\n`);

  // Phase 4: Analyze candidates until we find targetPass that PASS (skip cached)
  const scanCache = loadScanCache();
  const toAnalyze = newTraders.filter(c => !scanCache.has(c.address.toLowerCase()));
  console.log(`Phase 4: Analyzing traders (target: ${targetPass} PASS, ${newTraders.length - toAnalyze.length} cache-skipped)...\n`);

  const results: TraderAnalysis[] = [];
  let passCount = 0;

  for (let i = 0; i < toAnalyze.length && passCount < targetPass; i++) {
    const c = toAnalyze[i];
    const name = `${c.address.slice(0, 10)}...`;
    process.stdout.write(`[${i + 1} scanned, ${passCount}/${targetPass} pass] ${name.padEnd(14)} vol=$${c.totalVolume.toFixed(0).padStart(6)}`);

    const [closed, activity, current] = await Promise.all([
      fetchClosedPositions(c.address, 5),
      fetchActivity(c.address),
      fetchCurrentPositions(c.address),
    ]);

    const entry = { rank: "-", proxyWallet: c.address, userName: name, vol: c.totalVolume, pnl: 0 };
    const analysis = analyzeTrader(entry, closed, activity, current, windowStartUtc, windowEndUtc);
    results.push(analysis);
    scanCache.set(c.address.toLowerCase(), { address: c.address, scannedAt: new Date().toISOString(), passed: analysis.passed });

    if (analysis.passed) passCount++;
    const status = analysis.passed ? `✓ score=${analysis.score}` : `✗ ${analysis.failReasons[0]?.slice(0, 25) || "FAIL"}`;
    console.log(` | ${String(closed.length).padStart(3)} closed | ${status}`);

    if (i < toAnalyze.length - 1) await sleep(DELAY_MS);
  }

  saveScanCache(scanCache);

  // Results
  console.log(`\n${"=".repeat(60)}`);
  console.log(` MARKET DISCOVERY RESULTS`);
  console.log(` Scanned ${markets.length} markets → ${allTraders.size} traders → ${newTraders.length} new → ${results.filter(r => r.passed).length} passed`);
  console.log(`${"=".repeat(60)}`);

  printResults(results, topN);

  // Compare summary
  const passed = results.filter(r => r.passed).sort((a, b) => b.score - a.score);
  if (passed.length > 0) {
    console.log(`\n--- NOT ON LEADERBOARD — discovered from markets ---\n`);
    for (const t of passed.slice(0, topN)) {
      console.log(`  ${t.address}`);
      console.log(`    score=${t.score} WR=${t.winRate}% ROI=${t.roi > 0 ? "+" : ""}${t.roi}% positions=${t.resolvedPositions} mkts=${t.uniqueMarkets} peak=${t.peakHourUtc}h tz=${t.activeInWindowPct}%`);
    }
  } else {
    console.log("\nNo new profitable traders found outside the leaderboard.");
  }

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
    source: "market-discovery" as const,
  }));
  const run: ResearchRun = {
    version: 1,
    type: "discovery",
    createdAt: new Date().toISOString(),
    config: { marketCount, minTrades, topN, windowStartUtc, windowEndUtc },
    traders: researchTraders,
  };
  saveResearchRun(run, "market-discovery");
}

main().catch((err: unknown) => {
  console.error("Fatal:", errorMessage(err));
  process.exit(1);
});
