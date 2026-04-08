import { describe, it, expect, vi, beforeEach } from "vitest";

const mockReadContract = vi.fn();
const mockWriteContract = vi.fn();
const mockWaitForTransactionReceipt = vi.fn();
const mockEstimateFeesPerGas = vi.fn();

vi.mock("viem", async () => {
  const actual = await vi.importActual("viem");
  return {
    ...actual,
    createPublicClient: () => ({
      readContract: mockReadContract,
      estimateFeesPerGas: mockEstimateFeesPerGas,
      waitForTransactionReceipt: mockWaitForTransactionReceipt,
    }),
    createWalletClient: () => ({
      writeContract: mockWriteContract,
    }),
  };
});

vi.mock("viem/accounts", () => ({
  privateKeyToAccount: () => ({ address: "0xTestAccount" }),
}));

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

import { checkAndSetApprovals } from "../check-approvals";
import { logger } from "../logger";

describe("check-approvals", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockEstimateFeesPerGas.mockResolvedValue({ maxFeePerGas: 30000000000n });
    mockWaitForTransactionReceipt.mockResolvedValue({});
  });

  it("skips approve when allowance is sufficient", async () => {
    // USDC allowance > 1M (1_000_000 * 10^6 = 1_000_000_000_000n)
    mockReadContract
      .mockResolvedValueOnce(1_000_000_000_000n) // USDC allowance — sufficient
      .mockResolvedValueOnce(true)  // CTF isApprovedForAll — true
      .mockResolvedValueOnce(1_000_000_000_000n) // Neg Risk USDC allowance — sufficient
      .mockResolvedValueOnce(true); // Neg Risk CTF isApprovedForAll — true

    await checkAndSetApprovals("abc123");

    expect(mockWriteContract).not.toHaveBeenCalled();
    expect(vi.mocked(logger.info)).toHaveBeenCalledWith("USDC approval: OK");
    expect(vi.mocked(logger.info)).toHaveBeenCalledWith("Conditional Tokens approval: OK");
  });

  it("calls approve with maxUint256 and gas overrides when USDC allowance is low", async () => {
    mockReadContract
      .mockResolvedValueOnce(100n) // USDC allowance — too low
      .mockResolvedValueOnce(true)  // CTF — ok
      .mockResolvedValueOnce(1_000_000_000_000n) // Neg Risk USDC — ok
      .mockResolvedValueOnce(true); // Neg Risk CTF — ok
    mockWriteContract.mockResolvedValueOnce("0xtx1");

    await checkAndSetApprovals("abc123");

    expect(mockWriteContract).toHaveBeenCalledTimes(1);
    const call = mockWriteContract.mock.calls[0][0];
    expect(call.address).toBe("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174");
    expect(call.functionName).toBe("approve");
    expect(call.args[0]).toBe("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E");
    expect(call.args[1]).toBe(2n ** 256n - 1n);
  });

  it("calls setApprovalForAll with correct args when ERC1155 not approved", async () => {
    mockReadContract
      .mockResolvedValueOnce(1_000_000_000_000n) // USDC — ok
      .mockResolvedValueOnce(false) // CTF — not approved
      .mockResolvedValueOnce(1_000_000_000_000n) // Neg Risk USDC — ok
      .mockResolvedValueOnce(true); // Neg Risk CTF — ok
    mockWriteContract.mockResolvedValueOnce("0xtx2");

    await checkAndSetApprovals("abc123");

    expect(mockWriteContract).toHaveBeenCalledTimes(1);
    const call = mockWriteContract.mock.calls[0][0];
    expect(call.address).toBe("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045");
    expect(call.functionName).toBe("setApprovalForAll");
    expect(call.args[0]).toBe("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E");
    expect(call.args[1]).toBe(true);
  });

  it("approves all when all are insufficient", async () => {
    mockReadContract
      .mockResolvedValueOnce(0n)    // USDC — zero
      .mockResolvedValueOnce(false) // CTF — not approved
      .mockResolvedValueOnce(0n)    // Neg Risk USDC — zero
      .mockResolvedValueOnce(false); // Neg Risk CTF — not approved
    mockWriteContract
      .mockResolvedValueOnce("0xtx1")
      .mockResolvedValueOnce("0xtx2")
      .mockResolvedValueOnce("0xtx3")
      .mockResolvedValueOnce("0xtx4");

    await checkAndSetApprovals("abc123");

    expect(mockWriteContract).toHaveBeenCalledTimes(4);
  });
});
