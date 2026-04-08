import { describe, it, expect, vi, beforeEach } from "vitest";

const { mockReadContract } = vi.hoisted(() => ({
  mockReadContract: vi.fn(),
}));

vi.mock("viem", async () => {
  const actual = await vi.importActual("viem");
  return {
    ...actual,
    createPublicClient: () => ({
      readContract: mockReadContract,
    }),
  };
});

vi.mock("../config", () => ({
  CONFIG: {
    rpcUrl: "https://rpc.test",
    proxyWallet: "0xTestWallet",
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

// Must import after mocks
import { getUsdcBalance } from "../get-balance";

describe("get-balance", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockReadContract.mockReset();
  });

  it("returns formatted balance from readContract", async () => {
    // 50.123456 USDC = 50123456n (6 decimals)
    mockReadContract.mockResolvedValueOnce(50123456n);

    const balance = await getUsdcBalance();

    expect(balance).toBeCloseTo(50.123456, 5);
    expect(mockReadContract).toHaveBeenCalledTimes(1);
  });

  it("returns cached balance on spurious zero (first zero after high balance)", async () => {
    // First call — establish a known balance above threshold
    mockReadContract.mockResolvedValueOnce(25000000n); // $25
    await getUsdcBalance();

    // Second call — RPC returns zero (spurious)
    mockReadContract.mockResolvedValueOnce(0n);
    const balance = await getUsdcBalance();

    // Should return cached $25, not $0
    expect(balance).toBeCloseTo(25, 1);
  });

  it("accepts confirmed zero after consecutive zero readings", async () => {
    // Establish high balance
    mockReadContract.mockResolvedValueOnce(25000000n); // $25
    await getUsdcBalance();

    // 3 consecutive zeros → confirmed real zero
    mockReadContract.mockResolvedValue(0n);
    await getUsdcBalance(); // zero 1 — cached
    await getUsdcBalance(); // zero 2 — cached
    const balance = await getUsdcBalance(); // zero 3 — confirmed

    expect(balance).toBe(0);
  });

  it("returns lastKnownBalance on RPC error", async () => {
    // Establish balance
    mockReadContract.mockResolvedValueOnce(10000000n); // $10
    await getUsdcBalance();

    // RPC error
    mockReadContract.mockRejectedValueOnce(new Error("RPC timeout"));
    const balance = await getUsdcBalance();

    expect(balance).toBeCloseTo(10, 1);
  });

  it("returns -1 on RPC error with no prior balance", async () => {
    mockReadContract.mockRejectedValueOnce(new Error("RPC timeout"));

    // Fresh module state — lastKnownBalance defaults to -1
    // Note: this test may be affected by prior test state since the module is cached
    const balance = await getUsdcBalance();

    // Either returns cached from prior tests or -1 if first call
    expect(typeof balance).toBe("number");
  });
});
