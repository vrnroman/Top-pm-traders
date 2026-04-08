/**
 * Backtest Copy-Trading Strategy v2 — Activity-Based
 *
 * Usage: npx tsx src/scripts/backtest-traders.ts
 *        npx tsx src/scripts/backtest-traders.ts --days 30
 *        npx tsx src/scripts/backtest-traders.ts --addresses 0xabc,0xdef
 */

import axios from "axios";
import { sleep } from "../utils";
import { errorMessage } from "../types";
import { CONFIG } from "../config";
import { ActivityTrade, MarketResolution, TraderBacktest, BT_CONFIG } from "./backtest-traders-types";
import { simulateCopyTrading } from "./backtest-traders-simulation";
import { printResults } from "./backtest-traders-output";
import { ResearchRun, ResearchTraderResult, saveResearchRun } from "./research-types";

const DATA_API = CONFIG.dataApiUrl;
const DELAY_MS = 500;

async function fetchActivity(address: string): Promise<ActivityTrade[]> {
  const all: ActivityTrade[] = [];
  for (let offset = 0; offset < 500; offset += 100) {
    try {
      const res = await axios.get(`${DATA_API}/activity`, {
        params: { user: address, type: "TRADE", limit: 100, offset },
        timeout: 15000,
      });
      if (!Array.isArray(res.data) || res.data.length === 0) break;
      for (const t of res.data) {
        all.push({
          conditionId: t.conditionId || "",
          timestamp: typeof t.timestamp === "number" ? t.timestamp : 0,
          side: t.side || "BUY",
          size: parseFloat(t.size || "0"),
          usdcSize: parseFloat(t.usdcSize || t.size || "0"),
          price: parseFloat(t.price || "0"),
          title: t.title || "",
          asset: t.asset || "",
          outcome: t.outcome || "",
        });
      }
      if (res.data.length < 100) break;
      await sleep(DELAY_MS);
    } catch { break; }
  }
  return all;
}

async function fetchClosedPositions(address: string): Promise<Map<string, MarketResolution>> {
  const resolutions = new Map<string, MarketResolution>();
  for (let page = 0; page < 10; page++) {
    try {
      const res = await axios.get(`${DATA_API}/closed-positions`, {
        params: { user: address, limit: 50, offset: page * 50 },
        timeout: 15000,
      });
      if (!Array.isArray(res.data) || res.data.length === 0) break;
      for (const p of res.data) {
        resolutions.set(p.conditionId, { curPrice: p.curPrice, avgPrice: p.avgPrice });
      }
      if (res.data.length < 50) break;
      await sleep(DELAY_MS);
    } catch { break; }
  }
  return resolutions;
}

async function fetchTraderName(address: string): Promise<string> {
  try {
    const res = await axios.get(`${DATA_API}/v1/leaderboard`, {
      params: { user: address, timePeriod: "ALL", limit: 1 },
      timeout: 10000,
    });
    if (Array.isArray(res.data) && res.data.length > 0) return res.data[0].userName || address.slice(0, 10);
  } catch { /* ignore */ }
  return address.slice(0, 10);
}

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  let daysBack = 30;
  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--days" && args[i + 1]) daysBack = parseInt(args[i + 1]);
  }

  let traders: string[] = [];
  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--addresses" && args[i + 1]) {
      traders = args[i + 1].split(",").map((a) => a.trim()).filter(Boolean);
    }
  }
  if (traders.length === 0) {
    traders = CONFIG.userAddresses;
  }

  console.log(`Backtesting ${traders.length} traders over last ${daysBack} days (activity-based)...\n`);

  const results: TraderBacktest[] = [];
  const unrealizedByTrader: Map<string, number> = new Map();

  for (const addr of traders) {
    const name = await fetchTraderName(addr);
    process.stdout.write(`  ${name.padEnd(16)} `);

    process.stdout.write("activity...");
    const activity = await fetchActivity(addr);
    process.stdout.write(` ${activity.length} trades, `);

    process.stdout.write("resolutions...");
    const resolutions = await fetchClosedPositions(addr);
    process.stdout.write(` ${resolutions.size} markets, `);

    // Fetch unrealized PnL from open positions
    process.stdout.write("positions...");
    let unrealized = 0;
    try {
      const posRes = await axios.get(`${DATA_API}/positions`, { params: { user: addr }, timeout: 15000 });
      const positions = Array.isArray(posRes.data) ? posRes.data.filter((p: Record<string, unknown>) => (Number(p.size) || 0) > 0) : [];
      for (const p of positions) {
        unrealized += ((Number(p.curPrice) || 0) - (Number(p.avgPrice) || 0)) * (Number(p.size) || 0);
      }
      process.stdout.write(` ${positions.length} open\n`);
    } catch { process.stdout.write(" err\n"); }
    unrealizedByTrader.set(addr, Math.round(unrealized));

    const bt = simulateCopyTrading(activity, resolutions, daysBack);
    bt.address = addr;
    bt.name = name;
    results.push(bt);

    console.log(`    → ${bt.filled} filled, P&L: $${bt.totalPnl.toFixed(2)}, unrealized: $${unrealized.toFixed(0)}`);
    await sleep(DELAY_MS);
  }

  printResults(results, daysBack);

  // Unrealized PnL summary
  console.log("\n--- UNREALIZED P&L (open positions) ---\n");
  let totalUnrealized = 0;
  for (const r of results) {
    const unr = unrealizedByTrader.get(r.address) || 0;
    totalUnrealized += unr;
    const icon = unr >= 0 ? "✅" : (Math.abs(unr) > 100000 ? "❌" : "⚠️");
    console.log(`${icon} ${r.name.padEnd(16)} unrealized: $${unr.toLocaleString().padStart(10)} | realized: $${r.totalPnl.toFixed(2).padStart(8)} | net: $${(r.totalPnl + unr).toFixed(0)}`);
  }
  const totalRealized = results.reduce((s, r) => s + r.totalPnl, 0);
  console.log(`\nTotal realized: $${totalRealized.toFixed(2)} | Total unrealized: $${totalUnrealized.toLocaleString()} | Net: $${(totalRealized + totalUnrealized).toFixed(0)}`);

  // Persist results in unified research envelope
  const researchTraders: ResearchTraderResult[] = results.map(r => ({
    address: r.address,
    userName: r.name,
    backtestRoi: r.filled > 0 ? (r.totalPnl / (r.filled * BT_CONFIG.COPY_SIZE)) * 100 : 0,
    backtestFilled: r.filled,
    backtestTotalTrades: r.totalTrades,
    backtestAvgSlippageCents: r.avgSlippageCents,
    backtestWinRate: r.winRate,
    source: "backtest" as const,
  }));
  const run: ResearchRun = {
    version: 1,
    type: "backtest",
    createdAt: new Date().toISOString(),
    config: { daysBack, traders: traders.map(a => a.slice(0, 10)) },
    traders: researchTraders,
  };
  saveResearchRun(run, "backtest");
}

main().catch((err: unknown) => {
  console.error("Fatal:", errorMessage(err));
  process.exit(1);
});
