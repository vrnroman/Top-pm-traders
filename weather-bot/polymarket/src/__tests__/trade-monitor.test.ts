import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("axios", () => ({
  default: {
    get: vi.fn(),
    isAxiosError: vi.fn(() => false),
  },
}));

vi.mock("../config", () => ({
  CONFIG: {
    dataApiUrl: "https://data-api.test",
    userAddresses: ["0xtrader1", "0xtrader2"],
    fetchConcurrency: 5,
  },
}));

vi.mock("../logger", () => ({
  logger: { warn: vi.fn(), error: vi.fn(), info: vi.fn(), debug: vi.fn() },
}));

import axios from "axios";
import { fetchTraderActivity, fetchAllTraderActivities, resetCursors } from "../trade-monitor";

describe("trade-monitor fetchTraderActivity", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetCursors();
  });

  it("maps API response to DetectedTrade[]", async () => {
    vi.mocked(axios.get).mockResolvedValueOnce({
      data: [
        {
          id: "tx-1",
          conditionId: "cond-1",
          asset: "tok-1",
          side: "BUY",
          usdcSize: "50",
          price: "0.5",
          title: "Will it rain?",
          outcome: "Yes",
          timestamp: 1711987200,
        },
      ],
    });
    const trades = await fetchTraderActivity("0xtrader1");
    expect(trades).toHaveLength(1);
    expect(trades[0]).toMatchObject({
      id: "tx-1",
      traderAddress: "0xtrader1",
      market: "Will it rain?",
      conditionId: "cond-1",
      tokenId: "tok-1",
      side: "BUY",
      size: 50,
      price: 0.5,
      outcome: "Yes",
    });
  });

  it("returns [] for empty response", async () => {
    vi.mocked(axios.get).mockResolvedValueOnce({ data: [] });
    expect(await fetchTraderActivity("0xaddr")).toEqual([]);
  });

  it("returns [] for non-array response", async () => {
    vi.mocked(axios.get).mockResolvedValueOnce({ data: "not array" });
    expect(await fetchTraderActivity("0xaddr")).toEqual([]);
  });

  it("returns [] on 429 rate limit", async () => {
    const err = Object.assign(new Error("rate limited"), { response: { status: 429 } });
    vi.mocked(axios.get).mockRejectedValueOnce(err);
    vi.mocked(axios.isAxiosError).mockReturnValueOnce(true);
    expect(await fetchTraderActivity("0xaddr")).toEqual([]);
  });

  it("returns [] on network error", async () => {
    vi.mocked(axios.get).mockRejectedValueOnce(new Error("ECONNREFUSED"));
    expect(await fetchTraderActivity("0xaddr")).toEqual([]);
  });

  it("generates composite ID when id field is missing", async () => {
    vi.mocked(axios.get).mockResolvedValueOnce({
      data: [{ transactionHash: "0xhash", conditionId: "c1", asset: "tok-1", side: "BUY", size: "10", price: "0.5" }],
    });
    const trades = await fetchTraderActivity("0xaddr");
    expect(trades[0].id).toBe("0xhash-tok-1-BUY"); // canonical key: txHash-tokenId-side (for hybrid dedupe)
  });

  it("converts numeric timestamp (seconds) to ISO string", async () => {
    vi.mocked(axios.get).mockResolvedValueOnce({
      data: [{ timestamp: 1711987200, side: "BUY", price: "0.5", conditionId: "c", asset: "tok-1", usdcSize: "10" }],
    });
    const trades = await fetchTraderActivity("0xaddr");
    expect(trades[0].timestamp).toContain("2024-04-01");
  });

  it("passes through string timestamp", async () => {
    vi.mocked(axios.get).mockResolvedValueOnce({
      data: [{ timestamp: "2026-04-02T10:00:00Z", side: "SELL", price: "0.3", conditionId: "c", asset: "tok-1", usdcSize: "10" }],
    });
    const trades = await fetchTraderActivity("0xaddr");
    expect(trades[0].timestamp).toBe("2026-04-02T10:00:00Z");
  });

  it("defaults missing fields to safe values", async () => {
    vi.mocked(axios.get).mockResolvedValueOnce({
      data: [{ conditionId: "c", asset: "tok-1", usdcSize: "10" }],
    });
    const trades = await fetchTraderActivity("0xaddr");
    expect(trades[0].market).toBe("unknown");
    expect(trades[0].side).toBe("BUY");
    expect(trades[0].price).toBe(0);
    expect(trades[0].tokenId).toBe("tok-1");
  });

  // -- API input validation (Phase 4) --

  it("filters out trades with no tokenId", async () => {
    vi.mocked(axios.get).mockResolvedValueOnce({
      data: [
        { id: "valid", conditionId: "c1", asset: "tok-1", side: "BUY", usdcSize: "10", price: "0.5" },
        { id: "no-token", conditionId: "c2", side: "BUY", usdcSize: "10", price: "0.5" },
      ],
    });
    const trades = await fetchTraderActivity("0xaddr");
    expect(trades).toHaveLength(1);
    expect(trades[0].id).toBe("valid");
  });

  it("filters out trades with size <= 0", async () => {
    vi.mocked(axios.get).mockResolvedValueOnce({
      data: [
        { id: "good", conditionId: "c1", asset: "tok-1", side: "BUY", usdcSize: "10", price: "0.5" },
        { id: "zero", conditionId: "c2", asset: "tok-2", side: "BUY", usdcSize: "0", price: "0.5" },
        { id: "neg", conditionId: "c3", asset: "tok-3", side: "BUY", usdcSize: "-5", price: "0.5" },
      ],
    });
    const trades = await fetchTraderActivity("0xaddr");
    expect(trades).toHaveLength(1);
    expect(trades[0].id).toBe("good");
  });

  it("filters out trades with NaN size", async () => {
    vi.mocked(axios.get).mockResolvedValueOnce({
      data: [{ id: "nan", conditionId: "c1", side: "BUY", price: "0.5" }],
    });
    const trades = await fetchTraderActivity("0xaddr");
    expect(trades).toHaveLength(0);
  });
});

describe("trade-monitor fetchAllTraderActivities", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetCursors();
  });

  it("fetches from all configured addresses with concurrency", async () => {
    vi.mocked(axios.get)
      .mockResolvedValueOnce({ data: [{ id: "t1", side: "BUY", price: "0.5", conditionId: "c", asset: "tok-1", usdcSize: "10" }] })
      .mockResolvedValueOnce({ data: [{ id: "t2", side: "SELL", price: "0.3", conditionId: "c", asset: "tok-2", usdcSize: "5" }] });
    const trades = await fetchAllTraderActivities();
    expect(trades).toHaveLength(2);
    expect(axios.get).toHaveBeenCalledTimes(2);
  });

  it("returns empty when all fetches return empty", async () => {
    vi.mocked(axios.get).mockResolvedValue({ data: [] });
    expect(await fetchAllTraderActivities()).toEqual([]);
  });
});
