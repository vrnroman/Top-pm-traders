import { describe, it, expect, vi, beforeEach } from "vitest";
import type { ClobClient } from "@polymarket/clob-client";

vi.mock("../constants", () => ({
  FILL_CHECK_DELAY_MS: 0, // no delay in tests
  FILL_CHECK_RETRIES: 2,
}));

vi.mock("../logger", () => ({
  logger: { warn: vi.fn(), info: vi.fn(), error: vi.fn(), debug: vi.fn() },
}));

import { verifyOrderFill } from "../order-verifier";

function makeClobClient(getOrderFn: ReturnType<typeof vi.fn>) {
  return { getOrder: getOrderFn } as unknown as ClobClient;
}

describe("order-verifier verifyOrderFill", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("returns FILLED when sizeMatched >= originalSize", async () => {
    const getOrder = vi.fn().mockResolvedValue({
      original_size: "20",
      size_matched: "20",
      price: "0.5",
    });
    const result = await verifyOrderFill(makeClobClient(getOrder), "order-1");
    expect(result.status).toBe("FILLED");
    expect(result.filledShares).toBe(20);
    expect(result.filledUsd).toBe(10); // 20 * 0.5
    expect(result.fillPrice).toBe(0.5);
  });

  it("returns PARTIAL when 0 < sizeMatched < originalSize", async () => {
    const getOrder = vi.fn().mockResolvedValue({
      original_size: "20",
      size_matched: "10",
      price: "0.5",
    });
    const result = await verifyOrderFill(makeClobClient(getOrder), "order-1");
    expect(result.status).toBe("PARTIAL");
    expect(result.filledShares).toBe(10);
  });

  it("returns UNFILLED after all retries when sizeMatched = 0", async () => {
    const getOrder = vi.fn().mockResolvedValue({
      original_size: "20",
      size_matched: "0",
      price: "0.5",
    });
    const result = await verifyOrderFill(makeClobClient(getOrder), "order-1");
    expect(result.status).toBe("UNFILLED");
    expect(result.filledShares).toBe(0);
    // attempt 0,1,2 = 3 calls total (FILL_CHECK_RETRIES=2 means 0..2)
    expect(getOrder).toHaveBeenCalledTimes(3);
  });

  it("returns UNKNOWN when API throws on all retries", async () => {
    const getOrder = vi.fn().mockRejectedValue(new Error("API down"));
    const result = await verifyOrderFill(makeClobClient(getOrder), "order-1");
    expect(result.status).toBe("UNKNOWN");
    expect(result.filledShares).toBe(0);
    expect(getOrder).toHaveBeenCalledTimes(3);
  });

  it("retries on error then returns FILLED when API recovers", async () => {
    const getOrder = vi
      .fn()
      .mockRejectedValueOnce(new Error("timeout"))
      .mockResolvedValueOnce({
        original_size: "10",
        size_matched: "10",
        price: "0.5",
      });
    const result = await verifyOrderFill(makeClobClient(getOrder), "order-1");
    expect(result.status).toBe("FILLED");
    expect(getOrder).toHaveBeenCalledTimes(2);
  });

  it("returns FILLED on first attempt without exhausting retries", async () => {
    const getOrder = vi.fn().mockResolvedValue({
      original_size: "10",
      size_matched: "10",
      price: "0.5",
    });
    const result = await verifyOrderFill(makeClobClient(getOrder), "order-1");
    expect(result.status).toBe("FILLED");
    expect(getOrder).toHaveBeenCalledTimes(1);
  });

  it("handles missing fields with defaults of 0", async () => {
    const getOrder = vi.fn().mockResolvedValue({});
    const result = await verifyOrderFill(makeClobClient(getOrder), "order-1");
    // original_size=0, sizeMatched=0, originalSize=0 → sizeMatched >= originalSize is 0>=0 true
    // but originalSize > 0 check fails → continues to next retry
    expect(result.status).toBe("UNFILLED");
  });
});
