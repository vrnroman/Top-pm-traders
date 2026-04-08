// Output formatting for backtest results

import { TraderBacktest, BT_CONFIG } from "./backtest-traders-types";

export function printResults(results: TraderBacktest[], daysBack: number): void {
  console.log(`\n${"=".repeat(80)}`);
  console.log(` BACKTEST v2: Activity-Based Simulation (last ${daysBack} days)`);
  console.log(` Config: $${BT_CONFIG.COPY_SIZE}/trade, $${BT_CONFIG.MAX_MARKET_PER_DAY}/mkt/day, $${BT_CONFIG.MAX_DAILY_VOLUME}/day, ${BT_CONFIG.PRICE_BUFFER * 100}% slip, ${BT_CONFIG.FILL_RATE * 100}% fill rate`);
  console.log(`${"=".repeat(80)}\n`);

  console.log("Trader           | Trades | Dedup | Limit | Filled | WR%   | Our P&L | Trd P&L | Slip  | Skip reasons");
  console.log("-".repeat(110));

  let totalOurPnl = 0;
  let totalTraderPnl = 0;

  for (const r of results) {
    const skipStr = Object.entries(r.skippedReasons).map(([k, v]) => `${k}=${v}`).join(",") || "none";
    console.log(
      `${r.name.slice(0, 16).padEnd(16)} | ` +
      `${String(r.totalTrades).padStart(6)} | ` +
      `${String(r.withinWindow).padStart(5)} | ` +
      `${String(r.afterLimits).padStart(5)} | ` +
      `${String(r.filled).padStart(6)} | ` +
      `${r.winRate.toFixed(1).padStart(5)}% | ` +
      `${(r.totalPnl >= 0 ? "+" : "") + r.totalPnl.toFixed(2).padStart(6)}$ | ` +
      `${(r.traderTheoreticalPnl >= 0 ? "+" : "") + r.traderTheoreticalPnl.toFixed(2).padStart(6)}$ | ` +
      `${r.avgSlippageCents.toFixed(1).padStart(4)}c | ` +
      skipStr
    );
    totalOurPnl += r.totalPnl;
    totalTraderPnl += r.traderTheoreticalPnl;
  }

  console.log("-".repeat(110));
  const totalFilled = results.reduce((s, r) => s + r.filled, 0);
  console.log(
    `${"TOTAL".padEnd(16)} | ` +
    `${String(results.reduce((s, r) => s + r.totalTrades, 0)).padStart(6)} | ` +
    `${"".padStart(5)} | ` +
    `${"".padStart(5)} | ` +
    `${String(totalFilled).padStart(6)} | ` +
    `${"".padStart(6)} | ` +
    `${(totalOurPnl >= 0 ? "+" : "") + totalOurPnl.toFixed(2).padStart(6)}$ | ` +
    `${(totalTraderPnl >= 0 ? "+" : "") + totalTraderPnl.toFixed(2).padStart(6)}$ |`
  );

  // Daily P&L
  console.log(`\n--- DAILY P&L ---\n`);
  const allTrades = results.flatMap((r) => r.simTrades);
  const byDay = new Map<string, { pnl: number; trades: number; wins: number }>();
  for (const t of allTrades) {
    if (!byDay.has(t.day)) byDay.set(t.day, { pnl: 0, trades: 0, wins: 0 });
    const d = byDay.get(t.day)!;
    d.pnl += t.pnl;
    d.trades++;
    if (t.outcome === "WIN") d.wins++;
  }

  const days = Array.from(byDay.entries()).sort((a, b) => a[0].localeCompare(b[0]));
  let cumPnl = 0;
  for (const [day, d] of days) {
    cumPnl += d.pnl;
    console.log(
      `${day} | ${String(d.trades).padStart(2)} trades | ${d.wins}W/${d.trades - d.wins}L | ` +
      `day: ${d.pnl >= 0 ? "+" : ""}$${d.pnl.toFixed(2).padStart(5)} | ` +
      `cum: ${cumPnl >= 0 ? "+" : ""}$${cumPnl.toFixed(2)}`
    );
  }

  // Funnel
  const totalTrades = results.reduce((s, r) => s + r.totalTrades, 0);
  const totalDedup = results.reduce((s, r) => s + r.withinWindow, 0);
  const totalAfterLimits = results.reduce((s, r) => s + r.afterLimits, 0);
  console.log(`\n--- CONVERSION FUNNEL ---\n`);
  console.log(`Activity trades:  ${totalTrades}`);
  console.log(`After dedup:      ${totalDedup} (${Math.round(totalDedup / totalTrades * 100)}%)`);
  console.log(`After limits:     ${totalAfterLimits} (${Math.round(totalAfterLimits / totalTrades * 100)}%)`);
  console.log(`Filled:           ${totalFilled} (${Math.round(totalFilled / totalTrades * 100)}%)`);
  console.log(`ROI:              ${totalFilled > 0 ? ((totalOurPnl / totalFilled) * 100).toFixed(1) : "n/a"}%`);
}
