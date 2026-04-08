// Output formatting for per-trader performance report

import { shortAddress } from "../utils";

interface TraderStats {
  address: string;
  filled: number;
  partial: number;
  unfilled: number;
  failed: number;
  skipped: number;
  preview: number;
  totalInvested: number;
  avgBuySlippageCents: number;
  avgSellSlippageCents: number;
  buySlippageCount: number;
  sellSlippageCount: number;
  skipReasons: Map<string, number>;
  markets: Map<string, { pnl: number; trades: number; outcome: string }>;
}

export function printTraderSummary(traders: Map<string, TraderStats>): void {
  console.log("=== PER-TRADER SUMMARY ===\n");
  console.log("Trader       | Filled | Unfill | Failed | Skip | Invested | BuySlip  | SellSlip | Markets");
  console.log("-".repeat(100));

  traders.forEach((s) => {
    const addr = shortAddress(s.address);
    const filledCount = s.filled + s.partial;
    const buySlip = s.buySlippageCount > 0 ? `${s.avgBuySlippageCents > 0 ? "+" : ""}${s.avgBuySlippageCents.toFixed(1)}c` : "n/a";
    const sellSlip = s.sellSlippageCount > 0 ? `${s.avgSellSlippageCents > 0 ? "+" : ""}${s.avgSellSlippageCents.toFixed(1)}c` : "n/a";
    console.log(
      `${addr} | ` +
      `${String(filledCount).padStart(6)} | ` +
      `${String(s.unfilled).padStart(6)} | ` +
      `${String(s.failed).padStart(6)} | ` +
      `${String(s.skipped).padStart(4)} | ` +
      `$${s.totalInvested.toFixed(2).padStart(7)} | ` +
      `${buySlip.padStart(8)} | ` +
      `${sellSlip.padStart(8)} | ` +
      `${s.markets.size}`
    );
  });
}

export function printSkipReasons(traders: Map<string, TraderStats>): void {
  console.log("\n=== SKIP REASONS PER TRADER ===\n");

  traders.forEach((s) => {
    if (s.skipReasons.size === 0) return;
    const addr = shortAddress(s.address);
    const reasons: string[] = [];
    s.skipReasons.forEach((count, reason) => {
      reasons.push(`${reason}=${count}`);
    });
    console.log(`${addr}: ${reasons.join(", ")}`);
  });
}

export function printMarketDetail(traders: Map<string, TraderStats>): void {
  console.log("\n=== PER-TRADER MARKET DETAIL ===\n");

  traders.forEach((s) => {
    if (s.markets.size === 0) return;
    const addr = shortAddress(s.address);
    console.log(`--- ${addr} ---`);
    s.markets.forEach((m, market) => {
      console.log(`  ${market.slice(0, 40).padEnd(40)} | ${m.trades} trades | ${m.outcome}`);
    });
    console.log();
  });

  console.log("Note: Realized P&L requires market resolution data.");
  console.log("Re-run after markets resolve to see actual wins/losses.\n");
}
