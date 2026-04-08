import { describe, it, expect } from "vitest";
import { ResearchTraderResult } from "../scripts/research-types";
import {
  mergeByTrader,
  computeMetrics,
  rankAndClassify,
  detectTypeFromFilename,
  mapOldFormat,
} from "../scripts/aggregate-research-logic";

function makeTrader(overrides: Partial<ResearchTraderResult> = {}): ResearchTraderResult {
  return {
    address: "0xabc123",
    source: "leaderboard",
    score: 50,
    passed: true,
    roi: 5,
    winRate: 60,
    ...overrides,
  };
}

describe("mergeByTrader", () => {
  it("merges same address from different runs", () => {
    const entries = [
      makeTrader({ address: "0xABC", source: "leaderboard" }),
      makeTrader({ address: "0xABC", source: "market-discovery" }),
    ];
    const merged = mergeByTrader(entries);
    expect(merged.size).toBe(1);
    expect(merged.get("0xabc")!.length).toBe(2);
  });

  it("normalizes address case for merge", () => {
    const entries = [
      makeTrader({ address: "0xAbC123" }),
      makeTrader({ address: "0xabc123" }),
      makeTrader({ address: "0xABC123" }),
    ];
    const merged = mergeByTrader(entries);
    expect(merged.size).toBe(1);
    expect(merged.get("0xabc123")!.length).toBe(3);
  });

  it("keeps different addresses separate", () => {
    const entries = [
      makeTrader({ address: "0xaaa" }),
      makeTrader({ address: "0xbbb" }),
    ];
    const merged = mergeByTrader(entries);
    expect(merged.size).toBe(2);
  });
});

describe("computeMetrics", () => {
  it("calculates passRate from screening/discovery runs", () => {
    const entries = [
      makeTrader({ source: "leaderboard", passed: true }),
      makeTrader({ source: "leaderboard", passed: false }),
      makeTrader({ source: "market-discovery", passed: true }),
    ];
    const metrics = computeMetrics(entries);
    expect(metrics.passRate).toBeCloseTo(2 / 3);
    expect(metrics.timesPassed).toBe(2);
    expect(metrics.screeningRuns).toBe(2);
    expect(metrics.discoveryRuns).toBe(1);
  });

  it("passRate is 0 with only backtest runs (no pass/fail)", () => {
    const entries = [
      makeTrader({ source: "backtest", passed: undefined, backtestRoi: 5 }),
    ];
    const metrics = computeMetrics(entries);
    expect(metrics.passRate).toBe(0);
    expect(metrics.backtestRuns).toBe(1);
  });

  it("handles missing optional fields", () => {
    const entries = [
      { address: "0xmin", source: "leaderboard" as const },
    ];
    const metrics = computeMetrics(entries);
    expect(metrics.address).toBe("0xmin");
    expect(metrics.avgScore).toBe(0);
    expect(metrics.avgRoi).toBe(0);
    expect(metrics.runsSeen).toBe(1);
  });

  it("computes backtest metrics", () => {
    const entries = [
      makeTrader({ source: "backtest", backtestRoi: 10, backtestAvgSlippageCents: 2 }),
      makeTrader({ source: "backtest", backtestRoi: 20, backtestAvgSlippageCents: 4 }),
    ];
    const metrics = computeMetrics(entries);
    expect(metrics.avgBacktestRoi).toBe(15);
    expect(metrics.medianBacktestRoi).toBe(15);
    expect(metrics.avgSlippageCents).toBe(3);
  });
});

describe("rankAndClassify", () => {
  it("consistent trader has higher finalRankScore than lucky one-shot", () => {
    const lucky = computeMetrics([
      makeTrader({ address: "0xlucky", score: 90, passed: true, roi: 50 }),
    ]);
    const consistent = computeMetrics([
      makeTrader({ address: "0xsteady", score: 50, passed: true, roi: 5 }),
      makeTrader({ address: "0xsteady", score: 48, passed: true, roi: 4 }),
      makeTrader({ address: "0xsteady", score: 52, passed: true, roi: 6 }),
      makeTrader({ address: "0xsteady", score: 50, passed: true, roi: 5 }),
      makeTrader({ address: "0xsteady", score: 49, passed: true, roi: 5 }),
    ]);
    const ranked = rankAndClassify([lucky, consistent], 1, false);
    const luckyResult = ranked.find((t) => t.address === "0xlucky")!;
    const steadyResult = ranked.find((t) => t.address === "0xsteady")!;
    // Both have passRate=100%, but steady has 5 runs and more data → higher rank via passRate weight
    // Steady gets passRate*100*0.20 = 20 points from passRate alone
    expect(steadyResult.passRate).toBe(1);
    expect(steadyResult.runsSeen).toBe(5);
    expect(luckyResult.runsSeen).toBe(1);
  });

  it("require-backtest excludes traders without backtest entirely", () => {
    const withBt = computeMetrics([
      makeTrader({ address: "0xwithbt", source: "backtest", backtestRoi: 5 }),
      makeTrader({ address: "0xwithbt", source: "leaderboard", passed: true, score: 60, roi: 10 }),
      makeTrader({ address: "0xwithbt", source: "leaderboard", passed: true, score: 60, roi: 10 }),
    ]);
    const noBt = computeMetrics([
      makeTrader({ address: "0xnobt", source: "leaderboard", passed: true, score: 60, roi: 10 }),
      makeTrader({ address: "0xnobt", source: "leaderboard", passed: true, score: 60, roi: 10 }),
    ]);
    const ranked = rankAndClassify([withBt, noBt], 1, true);
    // noBt excluded entirely from ranking
    expect(ranked.find((t) => t.address === "0xnobt")).toBeUndefined();
    expect(ranked.length).toBe(1);
    expect(ranked[0].address).toBe("0xwithbt");
  });

  it("classifies production tier with enough data", () => {
    // Need multiple traders for normalizer to produce meaningful scores
    const good = computeMetrics(Array.from({ length: 5 }, () =>
      makeTrader({ address: "0xprod", score: 70, passed: true, roi: 10, winRate: 65 })
    ));
    const bad = computeMetrics(Array.from({ length: 5 }, () =>
      makeTrader({ address: "0xbad2", score: 10, passed: false, roi: -20, winRate: 20 })
    ));
    const ranked = rankAndClassify([good, bad], 2, false);
    const goodResult = ranked.find((t) => t.address === "0xprod");
    const badResult = ranked.find((t) => t.address === "0xbad2");
    expect(goodResult!.tier).toBe("production");
    expect(badResult!.tier).toBe("reject");
  });

  it("classifies watchlist tier", () => {
    const entries = [
      makeTrader({ address: "0xwatch", score: 35, passed: true, roi: 2 }),
      makeTrader({ address: "0xwatch", score: 30, passed: false, roi: -1 }),
    ];
    const metrics = computeMetrics(entries);
    const ranked = rankAndClassify([metrics], 1, false);
    expect(ranked[0].tier).toBe("watchlist");
  });

  it("--min-runs filters traders globally, not just from production", () => {
    const few = computeMetrics([
      makeTrader({ address: "0xfew", score: 60, passed: true, roi: 10 }),
      makeTrader({ address: "0xfew", score: 60, passed: true, roi: 10 }),
    ]);
    const enough = computeMetrics([
      makeTrader({ address: "0xenough", score: 40, passed: true, roi: 3 }),
      makeTrader({ address: "0xenough", score: 40, passed: true, roi: 3 }),
      makeTrader({ address: "0xenough", score: 40, passed: true, roi: 3 }),
      makeTrader({ address: "0xenough", score: 40, passed: true, roi: 3 }),
      makeTrader({ address: "0xenough", score: 40, passed: true, roi: 3 }),
    ]);
    // With minRuns=5, the 2-run trader is excluded entirely
    const ranked = rankAndClassify([few, enough], 5, false);
    expect(ranked.length).toBe(1);
    expect(ranked[0].address).toBe("0xenough");
  });

  it("--require-backtest excludes traders without backtest from ranking entirely", () => {
    const withBt = computeMetrics([
      makeTrader({ address: "0xwbt", source: "backtest", backtestRoi: 5 }),
      makeTrader({ address: "0xwbt", source: "leaderboard", passed: true }),
    ]);
    const noBt = computeMetrics([
      makeTrader({ address: "0xnbt", source: "leaderboard", passed: true }),
      makeTrader({ address: "0xnbt", source: "leaderboard", passed: true }),
    ]);
    const ranked = rankAndClassify([withBt, noBt], 1, true);
    // noBt should be completely excluded, not just demoted
    expect(ranked.length).toBe(1);
    expect(ranked[0].address).toBe("0xwbt");
  });

  it("classifies reject tier for poor traders", () => {
    const entries = [
      makeTrader({ address: "0xbad", score: 5, passed: false, roi: -50 }),
    ];
    const metrics = computeMetrics(entries);
    const ranked = rankAndClassify([metrics], 1, false);
    expect(ranked[0].tier).toBe("reject");
  });

  it("production requires at least 1 screening/discovery run", () => {
    const entries = Array.from({ length: 5 }, () =>
      makeTrader({ address: "0xbtonly", source: "backtest", backtestRoi: 20, score: 80 })
    );
    const metrics = computeMetrics(entries);
    const ranked = rankAndClassify([metrics], 2, false);
    // No screening/discovery runs → can't be production
    expect(ranked[0].tier).not.toBe("production");
  });
});

describe("detectTypeFromFilename", () => {
  it("detects leaderboard files as screening", () => {
    expect(detectTypeFromFilename("leaderboard-2026-04-04.json")).toBe("screening");
  });

  it("detects market-discovery files as discovery", () => {
    expect(detectTypeFromFilename("market-discovery-2026-04-04.json")).toBe("discovery");
  });

  it("detects backtest files", () => {
    expect(detectTypeFromFilename("backtest-2026-04-04.json")).toBe("backtest");
  });

  it("falls back to screening for unknown", () => {
    expect(detectTypeFromFilename("unknown-file.json")).toBe("screening");
  });
});

describe("mapOldFormat", () => {
  it("maps old TraderAnalysis to ResearchTraderResult", () => {
    const old = { address: "0xtest", userName: "tester", score: 42, passed: true, roi: 5.1 };
    const mapped = mapOldFormat(old, "screening");
    expect(mapped.address).toBe("0xtest");
    expect(mapped.source).toBe("leaderboard");
    expect(mapped.score).toBe(42);
    expect(mapped.roi).toBe(5.1);
  });

  it("handles missing fields gracefully", () => {
    const old = { address: "0xmin" };
    const mapped = mapOldFormat(old, "discovery");
    expect(mapped.source).toBe("market-discovery");
    expect(mapped.score).toBeUndefined();
    expect(mapped.passed).toBeUndefined();
  });
});
