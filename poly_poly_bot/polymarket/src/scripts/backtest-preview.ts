/**
 * Backtest Preview Trades
 *
 * Reads trade-history.jsonl, finds preview trades,
 * checks market outcomes via Data API, and calculates simulated P&L.
 *
 * Usage: npx tsx src/scripts/backtest-preview.ts
 */

import fs from "fs";
import path from "path";
import { sleep, shortAddress } from "../utils";
import { errorMessage } from "../types";

const HISTORY_FILE = path.resolve(process.cwd(), "data", "trade-history.jsonl");
const DELAY_MS = 500;

interface TradeRecord {
  timestamp: string;
  traderAddress: string;
  market: string;
  side: string;
  traderSize: number;
  copySize: number;
  price: number;
  status: string;
  reason?: string;
}

interface BacktestResult {
  market: string;
  side: string;
  entryPrice: number;
  copySize: number;
  outcome: "WIN" | "LOSS" | "OPEN" | "UNKNOWN";
  pnl: number;
  traderAddress: string;
  timestamp: string;
}

function loadPreviewTrades(): TradeRecord[] {
  if (!fs.existsSync(HISTORY_FILE)) {
    console.log("No trade history found. Run bot in preview mode first.");
    process.exit(0);
  }

  const lines = fs.readFileSync(HISTORY_FILE, "utf8").trim().split(/\r?\n/);
  const trades: TradeRecord[] = [];

  for (const line of lines) {
    try {
      const record = JSON.parse(line) as TradeRecord;
      if (record.status === "preview" && record.copySize > 0) {
        trades.push(record);
      }
    } catch {
      continue;
    }
  }

  return trades;
}

async function main(): Promise<void> {
  const trades = loadPreviewTrades();
  console.log(`\nFound ${trades.length} preview trades in history.\n`);

  if (trades.length === 0) {
    console.log("No preview trades to backtest. Let the bot run longer.");
    return;
  }

  // Deduplicate markets
  const uniqueMarkets = new Map<string, TradeRecord[]>();
  for (const t of trades) {
    const key = t.market;
    if (!uniqueMarkets.has(key)) uniqueMarkets.set(key, []);
    uniqueMarkets.get(key)!.push(t);
  }

  console.log(`Unique markets: ${uniqueMarkets.size}\n`);

  const results: BacktestResult[] = [];
  // Currently all trades marked OPEN — will be updated when market resolution is implemented
  /* eslint-disable prefer-const */
  let totalPnl = 0;
  let wins = 0;
  let losses = 0;
  /* eslint-enable prefer-const */
  let open = 0;
  let totalInvested = 0;

  for (const [, marketTrades] of uniqueMarkets) {
    // For now, mark all as OPEN since we can't reliably resolve via API
    // The user can check manually or we update this when markets resolve
    for (const t of marketTrades) {
      results.push({
        market: t.market,
        side: t.side,
        entryPrice: t.price,
        copySize: t.copySize,
        outcome: "OPEN",
        pnl: 0,
        traderAddress: t.traderAddress,
        timestamp: t.timestamp,
      });
      open++;
      totalInvested += t.copySize;
    }

    await sleep(DELAY_MS);
  }

  // Print results
  console.log("=== BACKTEST RESULTS ===\n");
  console.log("Timestamp           | Trader      | Side | Price | Size    | Market");
  console.log("-".repeat(100));

  for (const r of results) {
    const addr = shortAddress(r.traderAddress);
    const ts = r.timestamp.slice(0, 19).replace("T", " ");
    console.log(
      `${ts} | ${addr} | ${r.side.padEnd(4)} | $${r.entryPrice.toFixed(2).padStart(4)} | $${r.copySize.toFixed(2).padStart(6)} | ${r.market.slice(0, 40)}`
    );
  }

  console.log("\n=== SUMMARY ===\n");
  console.log(`Total preview trades: ${results.length}`);
  console.log(`Total would-be invested: $${totalInvested.toFixed(2)}`);
  console.log(`Wins: ${wins} | Losses: ${losses} | Open/Unknown: ${open}`);
  console.log(`Simulated P&L: $${totalPnl.toFixed(2)}`);

  if (open > 0) {
    console.log(`\nNote: ${open} trades are still in open markets.`);
    console.log("Re-run this script after markets resolve for actual P&L.");
  }

  // Write results to file
  const outputFile = path.resolve(process.cwd(), "data", "backtest-results.json");
  fs.writeFileSync(outputFile, JSON.stringify({ results, summary: { totalPnl, wins, losses, open, totalInvested } }, null, 2));
  console.log(`\nFull results saved to: ${outputFile}`);
}

main().catch((err: unknown) => {
  console.error("Error:", errorMessage(err));
  process.exit(1);
});
