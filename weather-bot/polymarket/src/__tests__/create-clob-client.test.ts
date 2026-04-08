import { describe, it, expect, vi, beforeEach } from "vitest";

const mockCreateOrDeriveApiKey = vi.fn();
const constructorCalls: unknown[][] = [];

vi.mock("@polymarket/clob-client", () => ({
  ClobClient: class {
    createOrDeriveApiKey = mockCreateOrDeriveApiKey;
    constructor(...args: unknown[]) { constructorCalls.push(args); }
  },
}));

vi.mock("viem", async () => {
  const actual = await vi.importActual("viem");
  return {
    ...actual,
    createWalletClient: () => ({ account: { address: "0xTest" } }),
  };
});

vi.mock("viem/accounts", () => ({
  privateKeyToAccount: () => ({ address: "0xTestAccount" }),
}));

vi.mock("../config", () => ({
  CONFIG: {
    clobApiUrl: "https://clob.test",
    chainId: 137,
    signatureType: 0,
    proxyWallet: "0xProxy",
    rpcUrl: "https://rpc.test",
  },
  getPrivateKey: () => "abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234",
}));

vi.mock("../logger", () => ({
  logger: {
    debug: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  },
}));

describe("create-clob-client", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    mockCreateOrDeriveApiKey.mockReset();
    constructorCalls.length = 0;
  });

  it("passes credentials, signatureType, and proxyWallet to authenticated ClobClient", async () => {
    mockCreateOrDeriveApiKey.mockResolvedValueOnce({
      apiKey: "key-1",
      secret: "secret-1",
      passphrase: "pass-1",
    });

    const { createClobClient } = await import("../create-clob-client");
    await createClobClient();

    expect(mockCreateOrDeriveApiKey).toHaveBeenCalledTimes(1);
    // 2 constructor calls: L1 (unauthenticated) + L2 (authenticated with creds)
    expect(constructorCalls).toHaveLength(2);
    // L1: (host, chainId, walletClient)
    expect(constructorCalls[0][0]).toBe("https://clob.test");
    expect(constructorCalls[0][1]).toBe(137);
    // L2: (host, chainId, walletClient, creds, signatureType, proxyWallet)
    expect(constructorCalls[1][0]).toBe("https://clob.test");
    expect(constructorCalls[1][1]).toBe(137);
    expect(constructorCalls[1][3]).toEqual({ key: "key-1", secret: "secret-1", passphrase: "pass-1" });
    expect(constructorCalls[1][4]).toBe(0); // signatureType
    expect(constructorCalls[1][5]).toBe("0xProxy"); // proxyWallet/funderAddress
  });

  it("returns same promise on second call (singleton)", async () => {
    mockCreateOrDeriveApiKey.mockResolvedValueOnce({
      apiKey: "key-1",
      secret: "secret-1",
      passphrase: "pass-1",
    });

    const { createClobClient } = await import("../create-clob-client");
    const promise1 = createClobClient();
    const promise2 = createClobClient();

    expect(promise1).toBe(promise2);
    await promise1;
    expect(mockCreateOrDeriveApiKey).toHaveBeenCalledTimes(1);
  });

  it("handles key field fallback (raw.key when apiKey absent)", async () => {
    mockCreateOrDeriveApiKey.mockResolvedValueOnce({
      key: "fallback-key",
      secret: "secret-2",
      passphrase: "pass-2",
    });

    const { createClobClient } = await import("../create-clob-client");
    const client = await createClobClient();

    expect(client).toBeDefined();
  });

  it("suppresses CLOB noise logs during createOrDeriveApiKey", async () => {
    const capturedLogs: string[] = [];
    const capturedErrors: string[] = [];

    mockCreateOrDeriveApiKey.mockImplementationOnce(async () => {
      // Simulate CLOB library noise during key derivation — should be suppressed
      console.log("[CLOB Client] request error: 400");
      console.error("[CLOB Client] request error: failed to create");
      // Simulate legitimate log — should pass through
      console.log("Legitimate log");
      return { apiKey: "k", secret: "s", passphrase: "p" };
    });

    // Spy on console to capture what gets through the filter
    const logSpy = vi.spyOn(console, "log").mockImplementation((...args: unknown[]) => {
      capturedLogs.push(String(args[0]));
    });
    const errorSpy = vi.spyOn(console, "error").mockImplementation((...args: unknown[]) => {
      capturedErrors.push(String(args[0]));
    });

    const { createClobClient } = await import("../create-clob-client");
    await createClobClient();

    logSpy.mockRestore();
    errorSpy.mockRestore();

    // CLOB noise should have been filtered out
    expect(capturedLogs).not.toContain("[CLOB Client] request error: 400");
    expect(capturedErrors).not.toContain("[CLOB Client] request error: failed to create");
    // Legitimate log should pass through
    expect(capturedLogs).toContain("Legitimate log");
  });
});
