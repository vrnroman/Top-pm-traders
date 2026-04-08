import { ClobClient } from "@polymarket/clob-client";
import { createWalletClient, http } from "viem";
import { polygon } from "viem/chains";
import { privateKeyToAccount } from "viem/accounts";
import { CONFIG, getPrivateKey } from "./config";
import { logger } from "./logger";
import { ClobApiKeyResponse } from "./types";

let clientPromise: Promise<ClobClient> | null = null;

/** Singleton CLOB client — derives API credentials from the wallet on first call. */
export function createClobClient(): Promise<ClobClient> {
  if (!clientPromise) {
    clientPromise = initClient();
  }
  return clientPromise;
}

async function initClient(): Promise<ClobClient> {
  const account = privateKeyToAccount(`0x${getPrivateKey()}`);
  const walletClient = createWalletClient({
    account,
    chain: polygon,
    transport: http(CONFIG.rpcUrl),
  });

  logger.info("Deriving API credentials from wallet...");

  const l1Client = new ClobClient(
    CONFIG.clobApiUrl,
    CONFIG.chainId,
    walletClient
  );

  // Suppress noisy "[CLOB Client] request error" from the library — it tries to create
  // a new API key first (returns 400 if key exists), then falls back to deriving. Expected behavior.
  const origLog = console.log;
  const origError = console.error;
  const isClobNoise = (args: unknown[]) => {
    const first = typeof args[0] === "string" ? args[0] : "";
    return first.includes("[CLOB Client]") && first.includes("request error");
  };
  console.log = (...args: unknown[]) => { if (!isClobNoise(args)) origLog(...args); };
  console.error = (...args: unknown[]) => { if (!isClobNoise(args)) origError(...args); };
  let raw: ClobApiKeyResponse;
  try {
    raw = await l1Client.createOrDeriveApiKey() as ClobApiKeyResponse;
  } finally {
    console.log = origLog;
    console.error = origError;
  }

  const client = new ClobClient(
    CONFIG.clobApiUrl,
    CONFIG.chainId,
    walletClient,
    {
      key: raw.apiKey ?? raw.key ?? "",
      secret: raw.secret,
      passphrase: raw.passphrase,
    },
    CONFIG.signatureType,
    CONFIG.proxyWallet
  );

  logger.info("CLOB client authenticated successfully");
  return client;
}
