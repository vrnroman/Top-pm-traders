// Output formatting and overlap detection for trader screening

import { TraderAnalysis } from "./screen-traders-analysis";

const MAX_OVERLAP_PERCENT = 20;

export function checkOverlap(traders: TraderAnalysis[]): Map<string, number> {
  const pairOverlaps = new Map<string, number>();

  for (let i = 0; i < traders.length; i++) {
    for (let j = i + 1; j < traders.length; j++) {
      const a = traders[i].openConditionIds;
      const b = traders[j].openConditionIds;
      if (a.size === 0 || b.size === 0) continue;

      let shared = 0;
      a.forEach((id) => { if (b.has(id)) shared++; });
      const union = new Set(Array.from(a).concat(Array.from(b))).size;
      const overlapPct = union > 0 ? (shared / union) * 100 : 0;

      if (overlapPct > 0) {
        const nameA = traders[i].userName.slice(0, 12);
        const nameB = traders[j].userName.slice(0, 12);
        pairOverlaps.set(`${nameA} ↔ ${nameB}`, Math.round(overlapPct * 10) / 10);
      }
    }
  }
  return pairOverlaps;
}

export function printResults(all: TraderAnalysis[], topN: number): void {
  const passed = all.filter((t) => t.passed).sort((a, b) => b.score - a.score);
  const failed = all.filter((t) => !t.passed);

  console.log(`\n${"=".repeat(60)}`);
  console.log(` TRADER SCREENING — ${passed.length} PASSED / ${failed.length} FAILED`);
  console.log(`${"=".repeat(60)}\n`);

  console.log(" Rk | Score | WR%    | ROI%   | DD%  | Conc | Hold  | T/day | Scalp | Mkts | Peak | TZ%  | Active     | Name");
  console.log("-".repeat(131));

  for (const t of passed) {
    const peakStr = t.peakHourUtc >= 0 ? `${String(t.peakHourUtc).padStart(2, "0")}h` : " --";
    console.log(
      `${t.rank.padStart(3)} | ` +
      `${String(t.score).padStart(5)} | ` +
      `${t.winRate.toFixed(1).padStart(5)}% | ` +
      `${(t.roi > 0 ? "+" : "") + t.roi.toFixed(1).padStart(5)}% | ` +
      `${t.worstLossPct.toFixed(1).padStart(3)}% | ` +
      `${String(t.profitConcentration).padStart(3)}% | ` +
      `${String(t.avgHoldingHours).padStart(4)}h | ` +
      `${String(t.tradesPerDay).padStart(5)} | ` +
      `${String(t.maxTradesPerMarketPerDay).padStart(5)} | ` +
      `${String(t.uniqueMarkets).padStart(4)} | ` +
      `${peakStr.padStart(3)} | ` +
      `${String(t.activeInWindowPct).padStart(3)}% | ` +
      `${t.lastTradeDate} | ` +
      t.userName.slice(0, 20)
    );
  }

  const top = passed.slice(0, topN);
  if (top.length >= 2) {
    const overlaps = checkOverlap(top);
    console.log(`\n--- OVERLAP (top ${top.length}) ---`);
    if (overlaps.size === 0) {
      console.log("  No overlap detected — fully diversified.");
    } else {
      overlaps.forEach((pct, pair) => {
        const warn = pct > MAX_OVERLAP_PERCENT ? " HIGH" : "";
        console.log(`  ${pair}: ${pct}%${warn}`);
      });
    }
  }

  if (top.length > 0) {
    console.log(`\n--- TOP ${top.length} RECOMMENDED ---\n`);
    for (const t of top) {
      const peakLabel = t.peakHourUtc >= 0 ? `peak=${String(t.peakHourUtc).padStart(2, "0")}h tz=${t.activeInWindowPct}%` : "";
      console.log(`  #${t.rank.padStart(3)} ${t.userName.slice(0, 16).padEnd(16)} score=${t.score} WR=${t.winRate}% ROI=${t.roi > 0 ? "+" : ""}${t.roi}% DD=${t.worstLossPct}% hold=${t.avgHoldingHours}h ${t.tradesPerDay}t/d mkts=${t.uniqueMarkets} ${peakLabel}`);
    }
    console.log(`\nUSER_ADDRESSES=${top.map((t) => t.address).join(",")}`);
  }

  if (failed.length > 0) {
    console.log(`\n--- FAILED (${failed.length}) — top reasons ---\n`);
    const reasonCounts = new Map<string, number>();
    for (const t of failed) {
      for (const r of t.failReasons) {
        const key = r.split(" ")[0];
        reasonCounts.set(key, (reasonCounts.get(key) || 0) + 1);
      }
    }
    Array.from(reasonCounts.entries()).sort((a, b) => b[1] - a[1]).slice(0, 5).forEach(([reason, count]) => {
      console.log(`  ${reason}: ${count} traders`);
    });
  }

  console.log();
}
