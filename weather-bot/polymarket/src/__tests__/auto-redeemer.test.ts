import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock fs before any module imports (inventory depends on it)
vi.mock("fs", () => ({
  default: {
    existsSync: vi.fn(() => false),
    mkdirSync: vi.fn(),
    readFileSync: vi.fn(),
    writeFileSync: vi.fn(),
    renameSync: vi.fn(),
    unlinkSync: vi.fn(),
  },
}));

vi.mock("axios", () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    isAxiosError: vi.fn(() => false),
  },
}));

vi.mock("../config", () => ({
  CONFIG: {
    dataApiUrl: "https://data-api.test",
    proxyWallet: "0xTestWallet",
    rpcUrl: "https://rpc.test",
  },
}));

vi.mock("../logger", () => ({
  logger: {
    debug: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  },
}));

const mockWriteContract = vi.fn();
const mockWaitForTransactionReceipt = vi.fn();
const mockEstimateFeesPerGas = vi.fn();

vi.mock("viem", async () => {
  const actual = await vi.importActual("viem");
  return {
    ...actual,
    createPublicClient: () => ({
      estimateFeesPerGas: mockEstimateFeesPerGas.mockResolvedValue({ maxFeePerGas: 30000000000n }),
      waitForTransactionReceipt: mockWaitForTransactionReceipt.mockResolvedValue({}),
    }),
    createWalletClient: () => ({
      writeContract: mockWriteContract,
    }),
  };
});

vi.mock("viem/accounts", () => ({
  privateKeyToAccount: () => ({ address: "0xTestAccount" }),
}));

import axios from "axios";
import { recordSell, getPosition } from "../inventory";

vi.mock("../inventory", () => ({
  recordSell: vi.fn(),
  getPosition: vi.fn().mockReturnValue(null),
}));

// Import after all mocks are set up
import { checkAndRedeemPositions } from "../auto-redeemer";

describe("auto-redeemer", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockWriteContract.mockReset();
    mockWaitForTransactionReceipt.mockReset().mockResolvedValue({});
    mockEstimateFeesPerGas.mockReset().mockResolvedValue({ maxFeePerGas: 30000000000n });
  });

  it("returns count 0 when no redeemable positions", async () => {
    vi.mocked(axios.get).mockResolvedValueOnce({ data: [] });

    const result = await checkAndRedeemPositions("abc123");

    expect(result).toEqual({ count: 0, markets: [], totalShares: 0, details: [] });
    expect(mockWriteContract).not.toHaveBeenCalled();
    expect(recordSell).not.toHaveBeenCalled();
  });

  it("redeems single position and updates inventory", async () => {
    vi.mocked(axios.get).mockResolvedValueOnce({
      data: [{ conditionId: "cond-1", asset: "tok-1", title: "Will X happen?", size: 10, curPrice: 1.0 }],
    });
    mockWriteContract.mockResolvedValueOnce("0xabc");

    const result = await checkAndRedeemPositions("abc123");

    expect(result.count).toBe(1);
    expect(result.markets).toEqual(["Will X happen?"]);
    expect(result.totalShares).toBe(10);
    expect(result.details).toEqual([{ title: "Will X happen?", shares: 10, costBasis: 0, returned: 10 }]);
    expect(recordSell).toHaveBeenCalledWith("tok-1", 10);
  });

  it("continues on partial failure — redeems 2 of 3", async () => {
    vi.mocked(axios.get).mockResolvedValueOnce({
      data: [
        { conditionId: "c1", asset: "t1", title: "Market A", size: 5, curPrice: 1.0 },
        { conditionId: "c2", asset: "t2", title: "Market B", size: 8, curPrice: 1.0 },
        { conditionId: "c3", asset: "t3", title: "Market C", size: 3, curPrice: 1.0 },
      ],
    });
    mockWriteContract
      .mockResolvedValueOnce("0x1")
      .mockRejectedValueOnce(new Error("revert"))
      .mockResolvedValueOnce("0x3");

    const result = await checkAndRedeemPositions("abc123");

    expect(result.count).toBe(2);
    expect(result.markets).toEqual(["Market A", "Market C"]);
    expect(result.totalShares).toBe(8); // 5 + 3
    expect(recordSell).toHaveBeenCalledTimes(2);
  });

  it("throws when Data API fails", async () => {
    vi.mocked(axios.get).mockRejectedValueOnce(new Error("network error"));

    await expect(checkAndRedeemPositions("abc123")).rejects.toThrow("network error");
  });

  it("skips positions without conditionId", async () => {
    vi.mocked(axios.get).mockResolvedValueOnce({
      data: [
        { asset: "tok-1", title: "No condition", size: 10 },
        { conditionId: "c2", asset: "tok-2", title: "Valid", size: 5, curPrice: 1.0 },
      ],
    });
    mockWriteContract.mockResolvedValueOnce("0xok");

    const result = await checkAndRedeemPositions("abc123");

    expect(result.count).toBe(1);
    expect(result.markets).toEqual(["Valid"]);
    expect(mockWriteContract).toHaveBeenCalledTimes(1);
  });

  it("skips positions without asset/tokenId", async () => {
    vi.mocked(axios.get).mockResolvedValueOnce({
      data: [
        { conditionId: "c1", title: "No asset", size: 10 },
        { conditionId: "c2", asset: "tok-2", title: "Valid", size: 5, curPrice: 1.0 },
      ],
    });
    mockWriteContract.mockResolvedValueOnce("0xok");

    const result = await checkAndRedeemPositions("abc123");

    expect(result.count).toBe(1);
    expect(result.markets).toEqual(["Valid"]);
    expect(mockWriteContract).toHaveBeenCalledTimes(1);
  });
});
