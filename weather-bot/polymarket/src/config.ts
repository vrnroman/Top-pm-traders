import dotenv from "dotenv";
import path from "path";
import { parseAddresses, validatePrivateKey, validateAddress } from "./config-validators";

dotenv.config({ path: path.resolve(process.cwd(), ".env") });

function required(name: string): string {
  const val = process.env[name]?.trim();
  if (!val) throw new Error(`Missing required env var: ${name}`);
  return val;
}

function optional(name: string, fallback: string): string {
  return process.env[name]?.trim() || fallback;
}

const userAddresses = parseAddresses(required("USER_ADDRESSES"));
for (const addr of userAddresses) {
  validateAddress(addr, "USER_ADDRESSES entry");
}

// Validate private key at startup, but don't store in CONFIG.
// Only create-clob-client.ts should access it via getPrivateKey().
// Key remains in module memory for the process lifetime — use a dedicated low-balance wallet.
const _validatedKey = validatePrivateKey(required("PRIVATE_KEY"));
delete process.env.PRIVATE_KEY; // Remove from env immediately — module const is the single source

export function getPrivateKey(): string {
  return _validatedKey;
}

export const CONFIG = {
  // Trader wallets to copy
  userAddresses: userAddresses,

  // Your wallet
  proxyWallet: validateAddress(required("PROXY_WALLET"), "PROXY_WALLET"),
  signatureType: (() => {
    const t = parseInt(optional("SIGNATURE_TYPE", "0"), 10);
    if (t !== 0) throw new Error(`SIGNATURE_TYPE=${t} not supported. Only EOA (0) is implemented.`);
    return t as 0;
  })(),

  // Copy strategy
  copyStrategy: (() => {
    const v = optional("COPY_STRATEGY", "PERCENTAGE");
    if (v !== "PERCENTAGE" && v !== "FIXED") {
      throw new Error(`COPY_STRATEGY must be "PERCENTAGE" or "FIXED", got "${v}"`);
    }
    return v;
  })(),

  // Telegram (optional)
  telegramBotToken: optional("TELEGRAM_BOT_TOKEN", ""),
  telegramChatId: optional("TELEGRAM_CHAT_ID", ""),
  copySize: parseFloat(optional("COPY_SIZE", "10.0")),

  // Risk limits
  maxOrderSizeUsd: parseFloat(optional("MAX_ORDER_SIZE_USD", "100.0")),
  minOrderSizeUsd: parseFloat(optional("MIN_ORDER_SIZE_USD", "1.0")),
  maxPositionPerMarketUsd: parseFloat(optional("MAX_POSITION_PER_MARKET_USD", "500.0")),
  maxDailyVolumeUsd: parseFloat(optional("MAX_DAILY_VOLUME_USD", "1000.0")),

  // Bot settings
  fetchInterval: parseInt(optional("FETCH_INTERVAL", "1"), 10) * 1000,
  fetchConcurrency: Math.max(1, parseInt(optional("FETCH_CONCURRENCY", "5"), 10)),
  maxTradeAgeHours: parseFloat(optional("MAX_TRADE_AGE_HOURS", "1")),
  maxPriceDriftBps: parseInt(optional("MAX_PRICE_DRIFT_BPS", "300"), 10),   // 300 = 3%
  maxSpreadBps: parseInt(optional("MAX_SPREAD_BPS", "500"), 10),             // 500 = 5%
  maxCopiesPerMarketSide: parseInt(optional("MAX_COPIES_PER_MARKET_SIDE", "2"), 10),
  previewMode: optional("PREVIEW_MODE", "true").toLowerCase() === "true",
  redeemIntervalHours: parseFloat(optional("REDEEM_INTERVAL_HOURS", "0.5")),

  // Trade monitoring mode: data-api (default), hybrid (both), onchain (RPC only)
  tradeMonitorMode: (() => {
    const v = optional("TRADE_MONITOR_MODE", "data-api");
    if (!["data-api", "hybrid", "onchain"].includes(v)) {
      throw new Error(`TRADE_MONITOR_MODE must be data-api|hybrid|onchain, got "${v}"`);
    }
    return v;
  })(),

  // API endpoints
  clobApiUrl: optional("CLOB_API_URL", "https://clob.polymarket.com"),
  dataApiUrl: optional("DATA_API_URL", "https://data-api.polymarket.com"),
  rpcUrl: optional("RPC_URL", "https://polygon-rpc.com"),

  // Polygon
  chainId: 137,
} as const;
