# Polymarket Copy-Trading Bot — Codebase Summary

Quick reference for the entire codebase structure, modules, and entry points.

---

## At a Glance

- **Language:** TypeScript (v5.7.0)
- **Runtime:** Node.js >=20.0.0
- **Framework:** Viem v2.47.6 (blockchain client)
- **Testing:** Vitest (223 unit tests across 20 test files)
- **Lines of Code:** ~3,800 production + ~3,200 test
- **Modules:** 42 production files + 20 test files
- **Vulnerabilities:** 0 CVEs (viem migration eliminated 15 elliptic CVEs)

---

## Core Files (Entry Point)

### `src/index.ts` (206 lines)
**Main entry point** — Polling loop, orchestration, periodic tasks.

**Responsibilities:**
- Load config, initialize clients
- Main loop (5s interval): fetch trades → risk evaluation → order placement
- Periodic tasks: 5-min inventory sync, 30-min auto-redeem, daily state reset
- Lock file management (prevent double-execution)
- Graceful shutdown on SIGINT/SIGTERM with double-shutdown guard
- Telegram command polling (listens for `/status`)

**Key functions:**
- `main()` — Entry point, startup checks
- Main polling loop — Continuous trade monitoring
- Periodic timers — inventory reconciliation, auto-redeem, state reset
- `shutdown()` — Graceful cleanup with `shuttingDown` flag to prevent double SIGINT+SIGTERM

---

## Configuration Layer

### `src/config.ts` (44 lines)
Loads and validates environment variables; exports typed `CONFIG` object.

**Exports:**
- `CONFIG: Config` — Typed configuration object
- `getPrivateKey(): string` — Loads private key safely

**Env vars validated:**
- User/proxy wallet addresses
- Private key (64 hex chars)
- Risk limits (daily volume, per-market cap, order size bounds)
- Polling intervals, trading constants
- API endpoints, RPC URL
- Telegram credentials (optional)

### `src/config-validators.ts`
Pure validation functions (no side effects).

**Functions:**
- `isValidAddress(addr: string): boolean`
- `isValidPrivateKey(pk: string): boolean`
- `parseAddresses(csv: string): string[]`

### `src/types.ts` (80+ lines)
Shared TypeScript interfaces and helpers.

**Types:**
- `Trade` — Polymarket trade shape (BUY/SELL, size, price, trader, conditionId)
- `Position` — Inventory position (conditionId, amount, weighted avg price)
- `RiskState` — Risk manager state (daily volume, per-market spend, retry counts)
- `ClobApiKeyResponse` — API credential response shape
- `OrderResult` — Order execution outcome (FILLED, PARTIAL, PENDING, CANCELED)

**Helpers:**
- `errorMessage(err: unknown): string` — Normalize error to string

### `src/constants.ts` (27 lines)
Contract addresses, ABIs, trading constants.

**Addresses (Polygon):**
- `USDC_ADDRESS` — 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
- `CTF_EXCHANGE` — 0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E
- `CTF_CONTRACT` — 0x4D97DCd97eC945f40cF65F87097ACe5EA0476045

**ABIs (viem parseAbi):**
- `ERC20_BALANCE_ABI` — balanceOf()
- `ERC20_APPROVE_ABI` — approve(), allowance()
- `ERC1155_APPROVAL_ABI` — setApprovalForAll(), isApprovedForAll()
- `CTF_REDEEM_ABI` — redeemPositions()

**Constants:**
- `FILL_CHECK_DELAY_MS` = 3000
- `FILL_CHECK_RETRIES` = 2

### `src/utils.ts` (12 lines)
Shared utility functions.

**Functions:**
- `sleep(ms: number): Promise<void>` — Async delay
- `shortAddress(addr: string): string` — Format address for logging
- `roundCents(num: number): number` — Round to 2 decimal places

---

## Trade Monitoring & Execution

### `src/trade-monitor.ts` (80+ lines)
Fetches trader activity from Polymarket Data API.

**Responsibilities:**
- Poll Data API for each trader in USER_ADDRESSES
- Validate trades (tokenId present, size > 0, not NaN)
- Apply trade age filter (MAX_TRADE_AGE_HOURS)
- Pass validated trades to trade-store for deduplication

**Key function:**
- `fetchUserTrades(userAddress: string): Promise<Trade[]>`

**Error handling:** Log and continue on API errors (429, 500, timeout)

### `src/risk-manager.ts` (100+ lines)
8-layer risk evaluation (pure function, testable, no I/O).

**8 checks (sequential, all must pass):**
1. NaN guard — Rejects malformed data
2. Daily volume — Caps total USD placed per day
3. Trade age — Ignores trades older than MAX_TRADE_AGE_HOURS
4. Copy size — Calculates order from strategy (PERCENTAGE or FIXED)
5. Min/max bounds — Enforces MIN_ORDER_SIZE_USD and MAX_ORDER_SIZE_USD
6. Price validation — Rejects prices at 0 or 1
7. Per-market cap — Limits daily spend per condition ID
8. Balance check — For BUY orders, caps to available USDC

**Key function:**
- `evaluateRisk(trade: Trade, riskState: RiskState, balance: bigint, currentTime: number): RiskDecision`

**Returns:** `{ approved: boolean; finalSize: number; reason?: string }`

### `src/trade-store.ts` (60+ lines)
Deduplicates trades and tracks retry counts.

**Responsibilities:**
- Maintain persistent seen-set (JSON file)
- Detect duplicate trades (prevent re-execution)
- Track per-trade retry attempts (LRU capped @ 1000 entries)
- Load/save state from disk

**Key functions:**
- `isSeen(tradeId: string): boolean` — Check if processed
- `mark(tradeId: string): void` — Record as seen
- `incrementRetry(tradeId: string): number` — Increment retry count

### `src/trade-executor.ts` (80+ lines)
Orchestrates order placement with execution quality guards.

**Flow:**
1. Fetch live market snapshot (200ms timeout) → skip if fails
2. Evaluate price drift (≥3%) and spread (≥5%) → skip if unsafe
3. Call order-executor to place order on CLOB
4. Call order-verifier to poll for fill confirmation
5. Update inventory on fill
6. Record to trade history (JSONL)
7. Send Telegram notification

**Key function:**
- `executeTrade(trade: Trade, ...): Promise<OrderResult>`

**Isolation:** One failed trade doesn't cascade; price quality checks prevent bad fills

### `src/market-price.ts` (100 lines)
Fetches live CLOB prices and computes price drift.

**Responsibilities:**
- Fetch live best bid/ask from CLOB with 200ms timeout
- Cache results 5s to avoid redundant API calls
- Detect crossed books (bid > ask)
- Compute price drift in basis points

**Key functions:**
- `fetchMarketSnapshot(clobClient, tokenId): Promise<MarketSnapshot | null>` — Live prices with timeout
- `computeDriftBps(traderPrice, snapshot, side): number` — Basis points movement from trader entry

### `src/order-executor.ts` (60+ lines)
Places copy orders on CLOB API with adaptive spread-aware pricing.

**Responsibilities:**
- Fetch live market snapshot (best bid/ask)
- Check price drift: skip if market moved ≥3% from trader entry
- Check spread: skip if bid-ask spread ≥5%
- Cap prices at ±2% (adaptive buffer, replaces fixed buffer)
- Call CLOB API `/submit_order`
- Fallback to fixed pricing on market snapshot timeout
- Log full `postOrder` response when orderId is empty (debug aid)

**Key function:**
- `executeCopyOrder(clobClient, trade, copySize): Promise<OrderResult>` — Places order with live price checks

### `src/order-verifier.ts` (50+ lines)
Polls CLOB for fill status with retries.

**Responsibilities:**
- Poll `/get_order/{orderId}` (up to 2 retries, 3s delay)
- Return final status: FILLED, PARTIAL, PENDING, CANCELED

**Key function:**
- `verifyFill(orderId: string): Promise<OrderStatus>`

---

## Inventory & Storage

### `src/inventory.ts` (80+ lines)
Tracks open positions with weighted average prices.

**Responsibilities:**
- Record buys (add position, update WAP)
- Record sells (remove/reduce position)
- API sync (every 5 min in main loop)
- Persistence (`data/inventory.json`)
- Deduplication (prevent double-recording)
- Expose position array for external consumers (e.g., Telegram commands)

**Key functions:**
- `recordBuy(tokenId, shares, price, marketKey, market): void`
- `recordSell(tokenId, shares): void` — For redemptions
- `syncInventoryFromApi(): Promise<void>` — Reconcile with Data API
- `getPosition(tokenId): Position | null`
- `getPositions(): Array<{market, shares, avgPrice}>` — For external consumers
- `getInventorySummary(): string` — Human-readable position summary
- `weightedAvgPrice(...)` — Calculate WAP for position merges

**Data format:**
```json
{
  "0xtokenId": { "shares": 10, "avgPrice": 0.45, "marketKey": "0xconditionId", "market": "Market Name" }
}
```

---

## Blockchain Integration (Viem)

### `src/create-clob-client.ts` (69 lines)
Creates authenticated CLOB client singleton via viem wallet.

**Responsibilities:**
- Create viem `walletClient` from private key
- Derive CLOB API credentials via `createOrDeriveApiKey()`
- Cache client promise (singleton pattern)
- Suppress noisy log spam during key derivation

**Key function:**
- `createClobClient(): Promise<ClobClient>`

**Returns:** Authenticated CLOB client ready for order operations

### `src/get-balance.ts` (30+ lines)
Reads USDC.e balance from Polygon RPC via viem.

**Responsibilities:**
- Create viem `publicClient`
- Call `readContract()` for balanceOf()
- Handle RPC errors (timeout, 500)
- Cache result briefly

**Key function:**
- `getBalance(walletAddress: string): Promise<bigint>`

### `src/check-approvals.ts` (60+ lines)
Verifies and sets token approvals via viem.

**Approvals checked:**
- ERC20 USDC.e allowance for CTF Exchange
- ERC1155 CTF approval for CTF Exchange

**Responsibilities:**
- Read current allowance/approval
- If missing, submit transaction to set to MAX_UINT256
- Wait for confirmation
- Log results

**Key function:**
- `ensureApprovals(walletAddress: string): Promise<void>`

### `src/auto-redeemer.ts` (103 lines)
Detects resolved markets and redeems winning positions.

**Responsibilities:**
- Poll Data API for resolved binary markets
- Match with inventory positions
- Call CTF contract `redeemPositions()` via viem `writeContract()`
- Per-position error isolation
- Update inventory on success
- Send Telegram notification

**Key function:**
- `redeemResolvedPositions(): Promise<void>`

**Schedule:** Every 30 minutes (live mode only; skipped in preview)

---

## Logging & Notifications

### `src/logger.ts` (80+ lines)
Async logger with daily rotation.

**Features:**
- Colored console output
- Daily log file (`logs/bot-YYYY-MM-DD.log`)
- Buffered async writes (non-blocking)
- Structured timestamps

**Key functions:**
- `logger.info(msg: string)`
- `logger.warn(msg: string)`
- `logger.error(msg: string)`

### `src/telegram-notifier.ts` (55 lines)
Sends alerts to Telegram.

**Events notified:**
- orderPlaced, orderFilled (includes USD amount), orderUnfilled, orderFailed
- tradeError, dailySummary
- positionsRedeemed, botStarted

**Key function:**
- `telegram.{event}(details: object): Promise<void>`

**Graceful degradation:** If TELEGRAM_BOT_TOKEN not set, alerts are logged only

**Format example (filled trade):**
```
✅ Filled
10 shares ($1.00) on "Market Name" @ 0.10
```

### `src/telegram-commands.ts` (101 lines)
Polls Telegram Bot API for interactive bot commands.

**Responsibilities:**
- Continuously poll Telegram `/getUpdates` API (3s interval)
- Parse incoming messages (only responds to configured CHAT_ID)
- Handle `/status` command

**Status response includes:**
- Current USDC.e balance
- Daily volume and per-market spend summary (from risk-manager)
- List of all open positions with per-line USD value

**Key functions:**
- `startTelegramCommands(): void` — Called at bot startup
- `stopTelegramCommands(): void` — Called at shutdown
- `handleStatus(chatId)` — Process `/status` command
- `pollUpdates()` — Main polling loop

**Status format example:**
```
📊 Bot Status

💰 Balance: $500.00
📈 Daily: $150/$1000 | Market #1: $100/$500 | Market #2: $50/$500

📦 Positions (2):
  • Market Name 1: 10.00 sh @ 0.45 ($4.50)
  • Market Name 2: 5.00 sh @ 0.80 ($4.00)
```

---

## Scripts

### `src/scripts/research-types.ts`
Unified envelope format for screening/discovery/backtest outputs.

**Types:**
- `ResearchRun` — Metadata + trader results with version control
- `ResearchTraderResult` — Address, score, ROI, win rate, backtest metrics
- `saveResearchRun()` — Persist results to `data/research/*.json`

### `src/scripts/aggregate-research-results.ts`
CLI aggregator: merges multiple research runs, computes stability metrics, ranks by consistency.

**Features:**
- Merge by address, deduplicate
- Compute consistency score (pass rate, score stability)
- Classify: production/watchlist/reject tiers
- CLI flags: `--dir`, `--min-runs`, `--top`, `--require-backtest`, `--json`

**Usage:** `npx tsx src/scripts/aggregate-research-results.ts --dir data/research --top 15`

### `src/scripts/aggregate-research-logic.ts`
Pure ranking functions: merit scoring, tier classification, normalization.

**Functions:**
- `mergeByTrader()` — Group results by address
- `computeMetrics()` — Per-trader aggregation (score, pass rate, backtest ROI)
- `rankAndClassify()` — Rank and assign tiers based on consistency

### `src/scripts/screen-traders.ts`
Analyzes Polymarket leaderboard for copy candidates.

**Filters:** ROI, win rate, activity, min markets, min volume, redeem ratio, top trade concentration

**Hard filters:** Scalper (>10 trades/day), DCA (>20 per market), annualized ROI (≥30%), min 90 days active, ≥$500 volume, ≥10 markets, redeem ratio ≤5x, top trade ≤20%

**Usage:** `npx tsx src/scripts/screen-traders.ts --pages 4 --top 15 --target 5`

### `src/scripts/discover-traders-market.ts`
Find profitable traders from active markets (renamed from discover-traders-onchain).

**Usage:** `npx tsx src/scripts/discover-traders-market.ts --markets 10`

### `src/scripts/scan-cache.ts`
Shared scan cache (7-day TTL) — prevents re-scanning same traders.

### `src/scripts/backtest-traders.ts`
Activity-based historical simulation with fill modeling and slippage.

**Outputs:** Fill rate, slippage, estimated P&L, backtest ROI

**Usage:** `npx tsx src/scripts/backtest-traders.ts --days 30 --addresses 0xabc,0xdef`

### `src/scripts/performance-report.ts`
Per-trader P&L and slippage analysis from trade history.

### `src/scripts/sell-all.ts`
Emergency script to liquidate all positions.

### `src/scripts/backtest-preview.ts`
Analyze trades placed in preview mode (non-executed).

---

## Testing (Vitest)

### Test Files (223 tests across 20 files)

| File | Tests | Coverage |
|------|-------|----------|
| `config-validation.test.ts` | 5 | ENV var loading, type coercion |
| `inventory.test.ts` | 8 | Buy, sell, WAP, API sync, dedup |
| `risk-manager.test.ts` | 8 | All 8 checks (happy path + edge cases) |
| `trade-store.test.ts` | 6 | Seen-set, retry counting, LRU |
| `utils.test.ts` | 4 | sleep, shortAddress, roundCents |
| `auto-redeemer.test.ts` | 6 | Empty state, single/multiple, failures |
| `get-balance.test.ts` | 3 | Read balance, RPC errors, caching |
| `check-approvals.test.ts` | 3 | Read approval, set approval, error handling |
| `create-clob-client.test.ts` | 2 | Client initialization, singleton caching |
| `market-price.test.ts` | 12 | Snapshot fetch, drift calc, cache, timeout, crossed book |
| `aggregate-research.test.ts` | 21 | Merge, metrics, ranking, classification, consistency |
| (Other tests) | 115+ | Screen, backtest, discovery, data source, onchain source |

**Run tests:** `npm test`

**Coverage:** `npm run test:coverage`

---

## Dependencies

### Production (`package.json`)

| Package | Version | Purpose |
|---------|---------|---------|
| `viem` | ^2.47.6 | Blockchain client (wallet, reads, writes) |
| `@polymarket/clob-client` | ^5.8.1 | CLOB API wrapper (viem-native) |
| `axios` | ^1.7.0 | HTTP requests (Data API, CLOB fallback) |
| `dotenv` | ^16.4.7 | Env var loading |

### Dev Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `typescript` | ^5.7.0 | TypeScript compiler |
| `vitest` | ^4.1.2 | Unit test framework |
| `@types/node` | ^22.10.0 | Node.js types |
| `eslint` | ^9.39.4 | Linting |
| `prettier` | ^3.8.1 | Code formatting |
| `tsx` | ^4.19.0 | TypeScript execution (dev scripts) |

**Vulnerability status:** 0 CVEs (viem migration eliminated elliptic chain)

---

## Data Files

### Persistent State (`data/` directory)

```
data/
├── seen-trades.json       # Set of trade IDs (deduplication)
├── trade-history.jsonl    # All executed trades (one JSON per line)
├── inventory.json         # Current positions with WAP
├── risk-state.json        # Daily volume, per-market spend tracking
└── bot.lock               # PID file (prevent double-execution)
```

### Logs (`logs/` directory)

```
logs/
└── bot-YYYY-MM-DD.log     # Daily rotated log file
```

---

## Build & Deployment

### Scripts (npm)

| Command | Purpose |
|---------|---------|
| `npm run build` | Compile TypeScript → `dist/` |
| `npm run dev` | Run with tsx (auto-reload, dev mode) |
| `npm start` | Run compiled bot |
| `npm test` | Run Vitest |
| `npm run test:coverage` | Coverage report |
| `npm run lint` | Run ESLint |
| `npm run lint:fix` | Auto-fix ESLint |
| `npm run format` | Format with Prettier |
| `npm run typecheck` | Run TypeScript type checker |

### Docker

```bash
docker compose up -d --build    # Build & run
docker compose logs -f bot      # View logs
docker compose down             # Stop
```

**Volumes:**
- `bot-data` — Persistent state (inventory, seen trades, risk state)
- `bot-logs` — Daily log files

**Auto-restart:** `restart: unless-stopped` (handle crashes)

---

## Key Metrics & Decisions

| Aspect | Value | Rationale |
|--------|-------|-----------|
| **Trade monitoring interval** | 5 seconds | Balance responsiveness vs. API load |
| **Inventory sync** | 5 minutes | Catch late fills; reduce RPC load |
| **Auto-redeem cycle** | 30 minutes | Batch gas efficiency; acceptable delay |
| **Fill verification retries** | 2 × 3 seconds | 9s total latency for fill confirmation |
| **Retry count cap** | 1000 entries | Prevent unbounded memory growth |
| **Risk check overhead** | <100 ms | Pure function; no I/O blocking |
| **Blockchain library** | viem v2 | Modern, modular, 0 CVEs (vs ethers v5 with 15 CVEs) |

---

## Architecture Layers

```
┌─────────────────────────────────┐
│   User Interface (CLI)          │  (health-check.ts, screen-traders.ts, etc.)
├─────────────────────────────────┤
│   Core Bot Logic (index.ts)     │  (polling loop, orchestration)
├─────────────────────────────────┤
│   Trade Execution Layer         │  (monitor, risk mgr, executor, verifier)
├─────────────────────────────────┤
│   Inventory & Storage           │  (inventory.ts, trade-store.ts)
├─────────────────────────────────┤
│   Blockchain Integration (viem) │  (get-balance, check-approvals, redeem)
├─────────────────────────────────┤
│   External APIs                 │  (CLOB, Data API, Polymarket RPC)
└─────────────────────────────────┘
```

---

## Related Documentation

- **`project-overview-pdr.md`** — Full spec, design decisions, acceptance criteria
- **`code-standards.md`** — Patterns, conventions, linting, testing
- **`system-architecture.md`** — Data flow, component responsibilities, design principles
- **`project-changelog.md`** — Version history, viem migration details
- **`setup-guide.md`** — Configuration and deployment instructions (Russian)
- **`backlog.md`** — Feature status, known issues, roadmap

---

## Quick Start (Development)

```bash
# Setup
npm install
cp .env.example .env
# Configure .env with wallets, API keys

# Run
npm run dev                    # Auto-reload development mode
npm run health-check          # Verify API connectivity
npm test                      # Run unit tests

# Lint & Format
npm run lint:fix
npm run format

# Deploy
npm run build
npm start                      # Production mode
# OR
docker compose up -d          # Docker deployment
```

---

## Support & Maintenance

**Issues?** Check:
1. `logs/bot-YYYY-MM-DD.log` for error messages
2. `docs/setup-guide.md` for configuration
3. `docs/system-architecture.md` for module responsibilities
4. `docs/backlog.md` for known issues

**Performance tuning?** See `FETCH_INTERVAL`, `MAX_ORDER_SIZE_USD`, `MAX_DAILY_VOLUME_USD` in `.env.example`.

**Private key leaked?** Rotate immediately; create new wallet, update PRIVATE_KEY and PROXY_WALLET in `.env`.
