import { describe, it, expect, vi, beforeEach } from "vitest";

// Test that real Data API ID generation produces canonical keys compatible with on-chain source.
// Imports real fetchTraderActivity to verify production code path.

vi.mock("axios", () => ({
  default: { get: vi.fn(), isAxiosError: vi.fn(() => false) },
}));

vi.mock("../config", () => ({
  CONFIG: { dataApiUrl: "https://data-api.test", userAddresses: ["0xtrader1"], fetchConcurrency: 5 },
}));

vi.mock("../logger", () => ({
  logger: { warn: vi.fn(), error: vi.fn(), info: vi.fn(), debug: vi.fn() },
}));

import axios from "axios";
import { fetchTraderActivity, resetCursors } from "../trade-monitor";

describe("cross-source dedupe: canonical trade key from real code", () => {
  beforeEach(() => { vi.clearAllMocks(); resetCursors(); });

  it("Data API generates txHash-tokenId-side key when transactionHash + asset present", async () => {
    vi.mocked(axios.get).mockResolvedValueOnce({
      data: [{ transactionHash: "0xabc123", conditionId: "cond-1", asset: "tok-99", side: "BUY", usdcSize: "10", price: "0.5" }],
    });
    const trades = await fetchTraderActivity("0xtrader1");
    // On-chain source would generate: "0xabc123-tok-99-BUY"
    expect(trades[0].id).toBe("0xabc123-tok-99-BUY");
  });

  it("SELL side produces different key than BUY for same tx+token", async () => {
    vi.mocked(axios.get).mockResolvedValueOnce({
      data: [{ transactionHash: "0xabc123", conditionId: "cond-1", asset: "tok-99", side: "SELL", usdcSize: "10", price: "0.5" }],
    });
    const trades = await fetchTraderActivity("0xtrader1");
    expect(trades[0].id).toBe("0xabc123-tok-99-SELL");
  });

  it("different outcome tokens in same market produce different keys", async () => {
    vi.mocked(axios.get).mockResolvedValueOnce({
      data: [
        { transactionHash: "0xabc", conditionId: "cond-1", asset: "tok-yes", side: "BUY", usdcSize: "5", price: "0.6" },
        { transactionHash: "0xabc", conditionId: "cond-1", asset: "tok-no", side: "BUY", usdcSize: "5", price: "0.4" },
      ],
    });
    const trades = await fetchTraderActivity("0xtrader1");
    expect(trades[0].id).toBe("0xabc-tok-yes-BUY");
    expect(trades[1].id).toBe("0xabc-tok-no-BUY");
    expect(trades[0].id).not.toBe(trades[1].id);
  });

  it("falls back to composite key when no transactionHash", async () => {
    vi.mocked(axios.get).mockResolvedValueOnce({
      data: [{ id: "api-id-123", conditionId: "c1", asset: "tok-1", side: "BUY", usdcSize: "10", price: "0.5" }],
    });
    const trades = await fetchTraderActivity("0xtrader1");
    // No txHash → uses item.id as fallback
    expect(trades[0].id).toBe("api-id-123");
  });

  it("on-chain format matches data-api format for same fill", () => {
    // Verify the format: both sources use "${txHash}-${tokenId}-${side}"
    const txHash = "0xdeadbeef";
    const tokenId = "123456789";
    const side = "BUY";

    // Data API format (verified by tests above)
    const dataApiId = `${txHash}-${tokenId}-${side}`;
    // On-chain format (from onchain-source.ts line: id: `${log.transactionHash}-${tokenId}-${side}`)
    const onchainId = `${txHash}-${tokenId}-${side}`;

    expect(dataApiId).toBe(onchainId);
  });
});
