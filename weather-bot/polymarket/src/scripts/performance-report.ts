/**
 * Per-Trader Performance Report
 *
 * Reads trade-history.jsonl, calculates per-trader stats:
 * slippage, fill rates, skip reasons, per-market breakdown.
 *
 * Usage: npx tsx src/scripts/performance-report.ts
 */

import fs from "fs";
import path from "path";
import { printTraderSummary, printSkipReasons, printMarketDetail } from "./performance-report-output";
import type { TradeRecord } from "../trade-store";

const HISTORY_FILE = path.resolve(process.cwd(), "data", "trade-history.jsonl");

interface TraderStats {
  address: string;
  filled: number;
  partial: number;
  unfilled: number;
  failed: number;
  skipped: number;
  preview: number;
  totalInvested: number;
  realizedPnl: number;
  theoreticalPnl: number;
  avgBuySlippageCents: number;
  avgSellSlippageCents: number;
  buySlippageCount: number;
  sellSlippageCount: number;
  winRate: number;
  wins: number;
  losses: number;
  pending: number;
  missedProfit: number;
  skipReasons: Map<string, number>;
  markets: Map<string, { pnl: number; trades: number; outcome: string }>;
}

function loadTrades(): TradeRecord[] {
  if (!fs.existsSync(HISTORY_FILE)) {
    console.log("No trade history found.");
    process.exit(0);
  }
  return fs.readFileSync(HISTORY_FILE, "utf8")
    .trim()
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line) => { try { return JSON.parse(line); } catch { return null; } })
    .filter(Boolean) as TradeRecord[];
}

function categorizeSkipReason(reason: string): string {
  if (reason.includes("too old")) return "TOO_OLD";
  if (reason.includes("below min")) return "MIN_SIZE";
  if (reason.includes("market cap") || reason.includes("volume limit")) return "RISK_LIMIT";
  if (reason.includes("balance")) return "NO_BALANCE";
  if (reason.includes("drifted")) return "PRICE_DRIFT";
  if (reason.includes("Spread")) return "WIDE_SPREAD";
  if (reason.includes("price")) return "BAD_PRICE";
  if (reason.includes("BUY-only") || reason.includes("No position")) return "SELL_SKIP";
  if (reason.includes("Conflicting")) return "CONFLICT";
  if (reason.includes("NaN") || reason.includes("Invalid")) return "BAD_DATA";
  return "OTHER";
}

function main(): void {
  const trades = loadTrades();
  console.log(`\nLoaded ${trades.length} trades from history.\n`);

  const traders = new Map<string, TraderStats>();

  for (const t of trades) {
    if (!traders.has(t.traderAddress)) {
      traders.set(t.traderAddress, {
        address: t.traderAddress,
        filled: 0, partial: 0, unfilled: 0, failed: 0, skipped: 0, preview: 0,
        totalInvested: 0, realizedPnl: 0, theoreticalPnl: 0,
        avgBuySlippageCents: 0, avgSellSlippageCents: 0,
        buySlippageCount: 0, sellSlippageCount: 0,
        winRate: 0, wins: 0, losses: 0, pending: 0,
        missedProfit: 0,
        skipReasons: new Map(),
        markets: new Map(),
      });
    }
    const s = traders.get(t.traderAddress)!;

    if (t.status === "filled") s.filled++;
    else if (t.status === "partial") s.partial++;
    else if (t.status === "unfilled") s.unfilled++;
    else if (t.status === "failed") s.failed++;
    else if (t.status === "skipped") {
      s.skipped++;
      const category = categorizeSkipReason(t.reason || "unknown");
      s.skipReasons.set(category, (s.skipReasons.get(category) || 0) + 1);
    }
    else if (t.status === "preview") s.preview++;

    if ((t.status === "filled" || t.status === "partial") && t.fillShares && t.fillPrice) {
      s.totalInvested += t.fillShares * t.fillPrice;

      if (t.traderPrice && t.traderPrice > 0) {
        const slippage = (t.fillPrice - t.traderPrice) * 100;
        if (t.side === "BUY") {
          s.avgBuySlippageCents += slippage;
          s.buySlippageCount++;
        } else {
          s.avgSellSlippageCents += slippage;
          s.sellSlippageCount++;
        }
      }

      if (!s.markets.has(t.market)) {
        s.markets.set(t.market, { pnl: 0, trades: 0, outcome: "pending" });
      }
      s.markets.get(t.market)!.trades++;
    }
  }

  traders.forEach((s) => {
    if (s.buySlippageCount > 0) s.avgBuySlippageCents = Math.round((s.avgBuySlippageCents / s.buySlippageCount) * 100) / 100;
    if (s.sellSlippageCount > 0) s.avgSellSlippageCents = Math.round((s.avgSellSlippageCents / s.sellSlippageCount) * 100) / 100;
  });

  printTraderSummary(traders);
  printSkipReasons(traders);
  printMarketDetail(traders);
  printDriftReport(trades);
  printLatencyReport(trades);
}

function printDriftReport(records: TradeRecord[]): void {
  const withDrift = records.filter(r => r.driftBps !== undefined);
  if (withDrift.length === 0) {
    console.log("\n=== Execution Quality ===");
    console.log("  No drift data yet (trades placed before quality guard)");
    return;
  }

  const drifts = withDrift.map(r => r.driftBps!).sort((a, b) => a - b);
  const spreads = withDrift.filter(r => r.spreadBps !== undefined).map(r => r.spreadBps!).sort((a, b) => a - b);
  const skippedDrift = withDrift.filter(r => r.status === "skipped" && r.reason?.includes("drifted")).length;
  const skippedSpread = withDrift.filter(r => r.status === "skipped" && r.reason?.includes("Spread")).length;

  console.log("\n=== Execution Quality (drift/spread) ===");
  console.log(`  Samples: ${withDrift.length}`);
  console.log(`  Drift — avg: ${Math.round(drifts.reduce((a, b) => a + b, 0) / drifts.length)}bps, max: ${drifts[drifts.length - 1]}bps`);
  if (spreads.length > 0) {
    console.log(`  Spread — avg: ${Math.round(spreads.reduce((a, b) => a + b, 0) / spreads.length)}bps, max: ${spreads[spreads.length - 1]}bps`);
  }
  console.log(`  Skipped (drift): ${skippedDrift}, Skipped (spread): ${skippedSpread}`);
}

function percentile(sorted: number[], p: number): number {
  if (sorted.length === 0) return 0;
  const idx = Math.ceil(sorted.length * p / 100) - 1;
  return sorted[Math.max(0, idx)];
}

function printLatencyReport(records: TradeRecord[]): void {
  // Only terminal statuses — "placed" is intermediate, would double-count reaction latency
  const terminalStatuses = ["filled", "partial", "unfilled", "unknown"];
  const timed = records.filter(r => terminalStatuses.includes(r.status) && r.sourceDetectedAt && r.orderSubmittedAt);
  if (timed.length === 0) {
    console.log("\n=== Latency Report ===");
    console.log("  No latency data yet (trades placed before instrumentation)");
    return;
  }

  const reactionMs = timed
    .map(r => r.orderSubmittedAt! - r.sourceDetectedAt!)
    .sort((a, b) => a - b);

  console.log("\n=== Reaction Latency (source detected → order submitted) ===");
  console.log(`  Samples: ${reactionMs.length}`);
  console.log(`  p50: ${percentile(reactionMs, 50)}ms`);
  console.log(`  p95: ${percentile(reactionMs, 95)}ms`);
  console.log(`  max: ${reactionMs[reactionMs.length - 1]}ms`);

  // Breakdown by source
  for (const src of ["data-api", "onchain"]) {
    const subset = timed.filter(r => r.source === src);
    if (subset.length === 0) continue;
    const ms = subset.map(r => r.orderSubmittedAt! - r.sourceDetectedAt!).sort((a, b) => a - b);
    console.log(`  ${src}: p50=${percentile(ms, 50)}ms p95=${percentile(ms, 95)}ms (n=${ms.length})`);
  }

  // Fill latency
  const fillTimed = records.filter(r => r.orderSubmittedAt && r.firstFillSeenAt);
  if (fillTimed.length > 0) {
    const fillMs = fillTimed
      .map(r => r.firstFillSeenAt! - r.orderSubmittedAt!)
      .sort((a, b) => a - b);
    console.log("\n=== Fill Latency (order submitted → fill seen) ===");
    console.log(`  Samples: ${fillMs.length}`);
    console.log(`  p50: ${percentile(fillMs, 50)}ms`);
    console.log(`  p95: ${percentile(fillMs, 95)}ms`);
    console.log(`  max: ${fillMs[fillMs.length - 1]}ms`);
  }
}

main();
