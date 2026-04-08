# Polymarket Copy-Trading Bot — Code Standards

Codebase structure, patterns, and best practices for maintaining consistency across the project.

## Codebase Organization

```
src/
├── index.ts                    # Entry point, main polling loop, lock file
├── config.ts                   # Env var loading, validation, CONFIG object
├── config-validators.ts        # Pure validation functions
├── constants.ts                # Contract addresses, ABIs (viem parseAbi), trading constants
├── types.ts                    # Shared interfaces, error helper
├── utils.ts                    # Shared helpers (sleep, shortAddress, roundCents)
│
├── trade-monitor.ts            # Fetch trader activity from Data API
├── risk-manager.ts             # 8-check risk evaluation
├── trade-executor.ts           # Orchestrate order placement + verification
├── order-executor.ts           # Place orders on CLOB API
├── order-verifier.ts           # Poll CLOB for fill status
├── trade-store.ts              # Dedup trades, track retry counts
├── inventory.ts                # Position tracking, weighted avg prices
│
├── get-balance.ts              # USDC.e balance via viem publicClient
├── check-approvals.ts          # ERC20 + ERC1155 approvals via viem
├── create-clob-client.ts       # CLOB singleton with viem walletClient
├── auto-redeemer.ts            # Detect & redeem resolved positions via viem
│
├── logger.ts                   # Async logger with daily rotation
├── telegram-notifier.ts        # Telegram alerts
│
├── market-price.ts             # Live CLOB price fetcher, drift calculator
├── scripts/
│   ├── health-check.ts         # API connectivity verification
│   ├── research-types.ts       # Unified research envelope, persistence
│   ├── aggregate-research-results.ts  # CLI research aggregator
│   ├── aggregate-research-logic.ts    # Pure ranking/classification functions
│   ├── scan-cache.ts           # 7-day TTL scan cache (dedup)
│   ├── screen-traders.ts       # Leaderboard analysis with hard filters
│   ├── discover-traders-market.ts    # Profitable traders from markets
│   ├── backtest-traders.ts     # Historical simulation
│   ├── backtest-preview.ts     # Preview-mode trade analysis
│   ├── sell-all.ts             # Liquidate all positions
│   └── performance-report.ts   # Per-trader P&L report
│
└── *.test.ts                   # Vitest unit tests (223 tests across 20 files)
    ├── config-validation.test.ts
    ├── inventory.test.ts
    ├── risk-manager.test.ts
    ├── trade-store.test.ts
    ├── utils.test.ts
    ├── auto-redeemer.test.ts
    ├── get-balance.test.ts
    ├── check-approvals.test.ts
    ├── create-clob-client.test.ts
    ├── market-price.test.ts
    ├── aggregate-research.test.ts
    ├── screen-traders.test.ts
    ├── backtest-traders.test.ts
    ├── discover-traders-market.test.ts
    ├── data-source.test.ts
    ├── onchain-source.test.ts
    └── (11 other test files)
```

---

## TypeScript Configuration

**File:** `tsconfig.json`

Key settings:
- `"strict": true` — Enforces all strict type checks
- `"lib": ["ES2020", "DOM"]` — DOM added for viem's ox dependency type inference
- `"moduleResolution": "node"` — Node.js module resolution
- `"skipLibCheck": true` — Skip checking lib.d.ts for speed

**No `any` types allowed** — Use typed interfaces from `types.ts` instead.

---

## Blockchain Integration Patterns (Viem)

### 1. Creating a Wallet Client (Private Key)

**Pattern** (`create-clob-client.ts`):
```typescript
import { createWalletClient, http } from "viem";
import { polygon } from "viem/chains";
import { privateKeyToAccount } from "viem/accounts";

const account = privateKeyToAccount(`0x${getPrivateKey()}`);
const walletClient = createWalletClient({
  account,
  chain: polygon,
  transport: http(CONFIG.rpcUrl),
});
```

**Usage:** Signing transactions, deriving API credentials

---

### 2. Creating a Public Client (Read-Only)

**Pattern** (`get-balance.ts`):
```typescript
import { createPublicClient, http } from "viem";
import { polygon } from "viem/chains";

const publicClient = createPublicClient({
  chain: polygon,
  transport: http(rpcUrl),
});
```

**Usage:** Reading contract state (balances, approvals), no signing

---

### 3. Reading Contract State

**Pattern** (`get-balance.ts`):
```typescript
import { readContract } from "viem";
import { ERC20_BALANCE_ABI, USDC_ADDRESS } from "./constants";

const balance = await publicClient.readContract({
  address: USDC_ADDRESS,
  abi: ERC20_BALANCE_ABI,
  functionName: "balanceOf",
  args: [walletAddress],
});
```

**Best practices:**
- Use typed ABI from `constants.ts` (parsed via `parseAbi()`)
- Await result; viem returns unwrapped value (not wrapped in object)
- RPC errors throw; use try-catch for recovery

---

### 4. Writing to Contract (Transaction)

**Pattern** (`check-approvals.ts` or `auto-redeemer.ts`):
```typescript
import { writeContract } from "viem";
import { ERC20_APPROVE_ABI, CTF_EXCHANGE, USDC_ADDRESS } from "./constants";

const hash = await walletClient.writeContract({
  address: USDC_ADDRESS,
  abi: ERC20_APPROVE_ABI,
  functionName: "approve",
  args: [CTF_EXCHANGE, MAX_UINT256],
});

// Optionally wait for confirmation
const receipt = await publicClient.waitForTransactionReceipt({ hash });
```

**Best practices:**
- Write via walletClient (requires account)
- Returns tx hash immediately (non-blocking)
- Use `waitForTransactionReceipt()` for confirmation
- Error handling for failed transactions (low gas, nonce issues, etc.)

---

### 5. Type-Safe ABI Parsing

**Pattern** (`constants.ts`):
```typescript
import { parseAbi } from "viem";

export const ERC20_APPROVE_ABI = parseAbi([
  "function approve(address spender, uint256 amount) returns (bool)",
  "function allowance(address owner, address spender) view returns (uint256)",
]);

export const CTF_REDEEM_ABI = parseAbi([
  "function redeemPositions(address collateralToken, bytes32 parentCollectionId, bytes32 conditionId, uint256[] indexSets)",
]);
```

**Benefits:**
- Compile-time validation of ABI fragments (catches typos in function signatures)
- Type inference for function arguments and return values
- IDE autocomplete for function names and parameter types

---

## Execution Quality Guard Pattern (market-price.ts)

Live price validation before order placement to prevent bad fills.

**Pattern** (`market-price.ts`):
```typescript
// Fetch live market snapshot with timeout
const snapshot = await fetchMarketSnapshot(clobClient, tokenId);
if (!snapshot) {
  logger.warn(`Market snapshot timeout, falling back to fixed pricing`);
  // Use fallback pricing
}

// Check price drift (market moved away from trader entry)
const driftBps = computeDriftBps(traderPrice, snapshot, side);
if (driftBps >= MAX_PRICE_DRIFT_BPS) {
  logger.info(`Skipping trade: market moved ${driftBps}bps from entry`);
  return;
}

// Check spread (bid-ask too wide)
if (snapshot.spreadBps >= MAX_SPREAD_BPS) {
  logger.info(`Skipping trade: spread ${snapshot.spreadBps}bps exceeds limit`);
  return;
}

// Use adaptive pricing (capped at ±2% of best prices)
const boundAsk = Math.min(snapshot.bestAsk * 1.02, snapshot.bestAsk + 0.01);
```

**Components:**
- `MarketSnapshot` — Best bid/ask, spread, spread in bps
- `fetchMarketSnapshot()` — Fetch with 200ms timeout and 5s cache
- `computeDriftBps()` — Basis points movement from trader's entry price
- `_clearSnapshotCache()` — Testing helper

**Benefits:**
- Prevents copying into bad market conditions (wide spreads)
- Detects market movement away from trader's original entry
- Crossed-book detection (bid > ask = no real liquidity)
- Fallback to fixed buffer if API timeout

---

## Research Aggregation Pattern (aggregate-research-*.ts)

Unified format for screening/discovery/backtest outputs; ranking by consistency across runs.

**Pattern** (`research-types.ts`):
```typescript
interface ResearchRun {
  version: 1;
  type: "screening" | "discovery" | "backtest";
  createdAt: string;
  config: Record<string, unknown>;
  traders: ResearchTraderResult[];
}

interface ResearchTraderResult {
  address: string;
  roi?: number;
  winRate?: number;
  backtestRoi?: number;
  source: "leaderboard" | "market-discovery" | "backtest";
}

saveResearchRun(run, prefix); // Persist to data/research/*.json
```

**Aggregation logic** (`aggregate-research-logic.ts`):
```typescript
// Merge multiple runs by address
const byTrader = mergeByTrader(allResults); // Map<string, ResearchTraderResult[]>

// Compute stability metrics per trader
const metrics = computeMetrics(entries); // Pass rate, score stability, avg ROI

// Rank and classify
const ranked = rankAndClassify(metrics, minRuns, requireBacktest);
// Output: production | watchlist | reject tiers
```

**Consistency scoring:**
- Pass rate (% of screening/discovery runs passed)
- Score stability (relative std dev; lower = more consistent)
- Backtest ROI median (if available)
- Adaptive ranking via min-max normalization

**CLI** (`aggregate-research-results.ts`):
```bash
npx tsx src/scripts/aggregate-research-results.ts \
  --dir data/research \
  --min-runs 3 \
  --require-backtest \
  --top 15 \
  --json
```

---

## Config & Environment

**File:** `config.ts`

Pattern:
```typescript
import { config } from "dotenv";

config(); // Load .env

interface Config {
  userAddresses: string[];
  proxyWallet: string;
  privateKey: string;
  signatureType: number;
  // ... all env vars typed
}

export const CONFIG: Config = {
  userAddresses: (process.env.USER_ADDRESSES || "").split(",").map(a => a.trim()),
  proxyWallet: requireEnv("PROXY_WALLET"),
  privateKey: requireEnv("PRIVATE_KEY"),
  // ...
};

function requireEnv(key: string): string {
  const val = process.env[key];
  if (!val) throw new Error(`Missing required env var: ${key}`);
  return val;
}
```

**Best practices:**
- Load all config at startup via `requireEnv()`
- Fail fast if required vars are missing
- Validate with `config-validators.ts` pure functions (no side effects)
- Export singleton `CONFIG` object; never re-read `process.env`
- Clear private key from process.env after use: `delete process.env.PRIVATE_KEY`

---

## Shared Types

**File:** `types.ts`

All API response shapes and internal domain types:
```typescript
export interface Trade {
  type: "BUY" | "SELL";
  size: number;
  price: number;
  trader: string;
  conditionId: string;
  tokenId: string;
  createdAt: number;
}

export interface Position {
  conditionId: string;
  amount: number;
  wap: number; // Weighted average price
  timestamp: number;
}

export interface ClobApiKeyResponse {
  apiKey?: string;
  key?: string;
  secret: string;
  passphrase: string;
}
```

**Best practices:**
- Use `interface` for object types (structural typing)
- Use `type` for unions, primitives, and type aliases
- Export from `types.ts`; never import from other modules
- Include JSDoc for complex fields

---

## Risk Manager Pattern

**File:** `risk-manager.ts`

Injectable state for testability:
```typescript
export interface RiskState {
  dailyVolume: number;
  dailySpendByMarket: Record<string, number>; // key: conditionId
  retryCount: Map<string, number>;
}

export function evaluateRisk(
  trade: Trade,
  riskState: RiskState,
  balance: bigint,
  currentTime: number
): { approved: boolean; finalSize: number; reason?: string } {
  // 8 checks: NaN, daily volume, trade age, copy size, min/max, price, per-market, balance
  // All checks return early with reason if failed
  // Pure function: no side effects
}
```

**Benefits:**
- No I/O in pure function (no RPC calls, API calls)
- Easy to test with mock state
- Deterministic behavior (same input → same output)

---

## Async Patterns

### Sleep Helper
```typescript
// utils.ts
export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Usage
await sleep(3000); // Wait 3 seconds
```

### Retry Pattern
```typescript
// order-verifier.ts
for (let attempt = 0; attempt < FILL_CHECK_RETRIES; attempt++) {
  await sleep(FILL_CHECK_DELAY_MS);
  const order = await clobClient.getOrder(orderId);
  if (order.status === "FILLED") return order;
}
```

### Promise Caching (Singleton)
```typescript
// create-clob-client.ts
let clientPromise: Promise<ClobClient> | null = null;

export function createClobClient(): Promise<ClobClient> {
  if (!clientPromise) {
    clientPromise = initClient();
  }
  return clientPromise;
}
```

---

## Error Handling

### Config Errors (Fail-Fast)
```typescript
if (!CONFIG.userAddresses.length) {
  throw new Error("USER_ADDRESSES env var is required");
}
```

### API Errors (Log & Continue)
```typescript
try {
  const trades = await tradeMonitor.fetch();
} catch (err) {
  logger.error(`Trade monitor error: ${errorMessage(err)}`);
  // Continue to next cycle
}
```

### Per-Trade Errors (Isolate)
```typescript
// auto-redeemer.ts
for (const position of positions) {
  try {
    await redeem(position);
  } catch (err) {
    logger.error(`Redeem failed for ${position.conditionId}: ${errorMessage(err)}`);
    // Continue to next position
  }
}
```

### Error Helper
```typescript
// types.ts
export function errorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  if (typeof err === "string") return err;
  return String(err);
}
```

---

## Testing with Vitest

**File:** Any `*.test.ts`

Pattern:
```typescript
import { describe, it, expect, vi } from "vitest";

describe("RiskManager", () => {
  it("rejects trades exceeding daily volume", () => {
    const state: RiskState = { dailyVolume: 900, dailySpendByMarket: {}, retryCount: new Map() };
    const trade = { size: 200, price: 0.5, ... };
    
    const result = evaluateRisk(trade, state, 1000n, Date.now());
    
    expect(result.approved).toBe(false);
    expect(result.reason).toContain("daily volume");
  });

  it("calls API with correct params", async () => {
    const mockClient = { fetch: vi.fn().mockResolvedValue([...]) };
    
    await tradeMonitor.fetch(); // Mocked
    
    expect(mockClient.fetch).toHaveBeenCalledWith(...);
  });
});
```

**Best practices:**
- Test pure functions without mocks (risk-manager, inventory calculations)
- Mock external I/O: RPC, CLOB API, Data API
- Test error paths: API 500, invalid data, network timeouts
- Use descriptive test names (behavior, not implementation)
- Aim for coverage: `npm run test:coverage`

---

## Logging

**File:** `logger.ts`

Pattern:
```typescript
import { logger } from "./logger";

logger.info("Bot started");
logger.warn("Trade age exceeds limit");
logger.error(`Order failed: ${errorMessage(err)}`);
```

**Features:**
- Colored console output
- Daily log file rotation
- Buffered async writes (not blocking)
- Structured logging with timestamps

---

## Naming Conventions

| Type | Convention | Example |
|------|-----------|---------|
| Files | kebab-case | `trade-monitor.ts`, `risk-manager.ts` |
| Functions | camelCase | `evaluateRisk()`, `createWalletClient()` |
| Classes | PascalCase | `ClobClient`, `TelegramNotifier` |
| Constants | UPPER_SNAKE_CASE | `MAX_UINT256`, `FILL_CHECK_RETRIES` |
| Interfaces | PascalCase | `Trade`, `RiskState`, `Position` |
| Env vars | UPPER_SNAKE_CASE | `USER_ADDRESSES`, `MAX_DAILY_VOLUME_USD` |

---

## Linting & Formatting

Run before commit:
```bash
npm run lint:fix     # Auto-fix ESLint violations
npm run format       # Format code with Prettier
npm run typecheck    # Run TypeScript type checker
npm test             # Run unit tests
```

**ESLint rules:**
- `@typescript-eslint/no-explicit-any` — Enforce typed code (no `any`)
- `@typescript-eslint/explicit-function-return-types` — Function return types required
- No unused variables or imports

**Prettier settings:**
- 2-space indentation
- Single quotes for strings
- Trailing commas (ES5 compatible)

---

## Performance Considerations

1. **RPC Connection Pooling** — viem reuses connections; avoid creating new clients in loops
2. **Singleton Clients** — CLOB, public RPC clients cached at module level
3. **Async Logging** — Non-blocking file writes via stream buffering
4. **Retry Limits** — Cap retry counts (LRU map @ 1000 entries) to prevent memory leaks
5. **Polling Intervals** — Default 5s trade monitoring; configurable via `FETCH_INTERVAL`
6. **Inventory Sync** — Every 5 min (not on every trade) to balance latency + API load

---

## Security Best Practices

1. **Private Key Handling**
   - Load from `.env` only; never commit
   - Clear from `process.env` after CLOB init: `delete process.env.PRIVATE_KEY`
   - Remains in module memory for process lifetime (acceptable design choice)

2. **Token Approvals**
   - Set to `MAX_UINT256` for gas efficiency
   - Checked at startup via `check-approvals.ts`
   - Revoke manually if needed (not auto-revoked)

3. **RPC Privacy**
   - Use private Alchemy key or alternative to avoid IP tracking
   - Never hardcode API keys; load from `.env`

4. **Wallet Security**
   - Use dedicated low-balance wallet (never main wallet)
   - Keep POL for gas only (~$1–$2 recommended)

---

## Module Dependencies

```
index.ts
├── config.ts (CONFIG object)
├── trade-monitor.ts
│  ├── types.ts (Trade)
│  └── config.ts (API_URL, etc.)
├── risk-manager.ts
│  ├── types.ts (Trade, RiskState)
│  ├── config.ts (risk limits)
│  └── get-balance.ts (USDC balance)
├── trade-executor.ts
│  ├── order-executor.ts
│  ├── order-verifier.ts
│  └── inventory.ts
├── inventory.ts
│  └── types.ts (Position)
├── auto-redeemer.ts
│  ├── create-clob-client.ts
│  └── types.ts
├── logger.ts
├── telegram-notifier.ts
│  └── types.ts
└── check-approvals.ts (startup)

Blockchain layer (viem):
├── create-clob-client.ts
├── get-balance.ts
├── check-approvals.ts
├── auto-redeemer.ts
└── constants.ts (ABIs, addresses)
```

**Rule:** No circular dependencies; DAG (directed acyclic graph) only.

---

## Migrations & Breaking Changes

### Ethers v5 → Viem v2.47.6 (2026-04-03)

**Pattern change:**
```typescript
// OLD (ethers v5)
import { ethers } from "ethers";
const provider = new ethers.providers.JsonRpcProvider(rpcUrl);
const balance = await provider.getBalance(address);
const contract = new ethers.Contract(address, ABI, signer);

// NEW (viem v2)
import { createPublicClient, createWalletClient, http, readContract } from "viem";
const publicClient = createPublicClient({ chain: polygon, transport: http(rpcUrl) });
const balance = await publicClient.getBalance({ address });
const result = await publicClient.readContract({ address, abi: ABI, functionName: "..." });
```

**Removed dependency:** `ethers` and `@polymarket/order-utils`

**New dependency:** `viem` ^2.47.6 and `@polymarket/clob-client` ^5.8.1

**Benefit:** 15 CVEs eliminated (elliptic vulnerability chain), improved type safety via parseAbi()
