# Polymarket Copy-Trading Bot — Changelog

All significant changes, features, and fixes documented by date and version.

## [2026-04-04] — Execution Quality Guards & Research Aggregation

**Status:** COMPLETE

### Features
- **NEW: Execution Quality Guard** (`market-price.ts`, `order-executor.ts`, `trade-executor.ts`)
  - Live CLOB price fetch with 200ms timeout and 5s cache
  - Price drift check: skip trades where market moved ≥300bps from trader's entry
  - Spread check: skip trades with bid-ask spread ≥500bps
  - Crossed book detection: reject if bid > ask (no real liquidity)
  - Adaptive pricing: cap prices at ±2% of best bid/ask (replaces fixed 2% buffer)
  - Fallback: use fixed 2% buffer on market snapshot timeout
  - Result: fewer bad fills, better trade quality in volatile markets

- **NEW: Research Aggregator** (`aggregate-research-results.ts`, `aggregate-research-logic.ts`, `research-types.ts`)
  - Unified envelope format for screening/discovery/backtest outputs
  - Offline aggregator: merge multiple research runs by address
  - Compute stability metrics: pass rate, score consistency (std dev), median backtest ROI
  - Classification: production/watchlist/reject tiers based on consistency
  - CLI with flags: `--dir`, `--min-runs`, `--top`, `--require-backtest`, `--json`
  - Persistent research runs: `data/research/*.json` with version control

- **NEW: Screener Improvements** (`screen-traders.ts`)
  - PnL calculation: activity-based (BUY+SELL+REDEEM) replaces broken cashPnl field
  - Win rate: uses resolved markets only, capped at 100%
  - Hard filters added: scalper (>10 trades/day), DCA (>20 per market), annualized ROI (≥30%), min 90 days active, min $500 volume, min 10 markets, redeem ratio (≤5x), top trade concentration (≤20%)
  - Scan cache (7-day TTL) via `scan-cache.ts` prevents duplicate screening
  - Target-based scanning: `--target N` scans until N PASS found (faster feedback)
  - Renamed: `discover-traders-onchain` → `discover-traders-market` (market-based discovery)

- **NEW: Infrastructure Updates**
  - Docker: bind mount `./data` and `./logs` instead of named volumes (easier local access)
  - Screening results: persist to `data/research/*.json`
  - Scan cache: shared between screeners via `scan-cache.ts`

### Testing
- **NEW: market-price.test.ts** (12 tests) — Snapshot fetch, drift calc, cache TTL, timeout handling, crossed book detection
- **NEW: aggregate-research.test.ts** (21 tests) — Merge, metrics computation, ranking, classification, consistency scoring
- **Other new tests** (115+) — Screen traders, backtest, discovery, data source, onchain source modules
- **Total:** 223 tests across 20 test files (was 183 tests/18 files)

### Performance
- Price quality checks prevent copying into bad market conditions → fewer liquidations from slippage
- Scan cache (7-day TTL) reduces duplicate API work across screening runs
- Research aggregation enables offline trader ranking (no re-screening needed)

### Breaking Changes
- None (backward compatible; new features opt-in via CLI flags)

---

## [2026-04-03] — Telegram Commands & UX Improvements

**Status:** COMPLETE

### Features
- **NEW: Telegram `/status` command** (`telegram-commands.ts`)
  - Polls Telegram Bot API every 3 seconds for incoming messages
  - Responds only to configured CHAT_ID (security)
  - Returns: balance, daily volume summary, list of open positions with USD values
  - Started/stopped via `startTelegramCommands()` / `stopTelegramCommands()` in index.ts lifecycle

- **NEW: `getPositions()` export** in `inventory.ts`
  - Returns array of `{market, shares, avgPrice}` for external consumers
  - Enables position display in Telegram `/status` command and other integrations
  - Only includes positions with shares > 0

- **Telegram fill notification now includes USD amount**
  - Was: `✅ Filled 1.41 shares on "Market" @ 0.71`
  - Now: `✅ Filled 1.41 shares ($1.00) on "Market" @ 0.71`
  - Improves user visibility into position value

- **Double shutdown guard** (`index.ts`)
  - `shuttingDown` flag prevents double logging on SIGINT+SIGTERM
  - Ensures clean shutdown even if multiple signals received

- **Debug log for empty postOrder** (`order-executor.ts`)
  - Logs full `postOrder` response when orderId is empty
  - Aids troubleshooting of CLOB API integration issues

### Testing
- All new Telegram command functionality integrates with existing test suite
- No breaking changes to existing test fixtures

---

## [2026-04-03] — Viem Migration & Security Hardening

**Status:** COMPLETE

### Features & Changes
- **Migrate ethers v5 → viem v2.47.6** — Replaced legacy ethers library with modern viem for better type safety, smaller bundle, and improved Polygon support
  - `create-clob-client.ts`: ethers.Wallet → viem `createWalletClient()` + `privateKeyToAccount()`
  - `get-balance.ts`: ethers.providers.JsonRpcProvider + ethers.Contract → viem `createPublicClient()` + `readContract()`
  - `check-approvals.ts`: ethers wallet/contract operations → viem `readContract()` / `writeContract()` with parseAbi()
  - `auto-redeemer.ts`: ethers.Contract redemption calls → viem writeContract for CTF redeem

- **Upgrade @polymarket/clob-client** — v4.22.8 → v5.8.1 (viem-native support)

- **Add type-safe ABI parsing** — constants.ts uses viem's `parseAbi()` for compile-time ABI verification:
  - ERC20_BALANCE_ABI, ERC20_APPROVE_ABI
  - ERC1155_APPROVAL_ABI
  - CTF_REDEEM_ABI

- **TypeScript config update** — Added "DOM" to lib (required by viem's ox dependency for proper type inference)

### Security
- **Eliminate 15 CVEs** — Removed elliptic vulnerability chain inherited from ethers; 0 vulnerabilities remaining in production dependencies
- **Private key handling unchanged** — Still cleared from process.env after init; remains in module memory per process lifetime

### Testing
- **Add 3 new test files** with viem mocks:
  - `get-balance.test.ts` — viem publicClient mock, readContract behavior verification
  - `check-approvals.test.ts` — viem readContract/writeContract mocks, approval logic validation
  - `create-clob-client.test.ts` — viem walletClient mock, API key derivation flow
- **Update auto-redeemer.test.ts** — Mocks converted from ethers.Contract to viem writeContract

### Dependencies
```json
{
  "dependencies": {
    "@polymarket/clob-client": "^5.8.1",  // was 4.22.8
    "viem": "^2.47.6"                      // new
    // ethers removed
  }
}
```

**Breaking Change:** Code using ethers patterns will not compile. See migration examples in constants.ts and create-clob-client.ts.

**Impact:** Reduced bundle size ~40KB, improved IDE autocomplete with viem's Wagmi ecosystem tooling.

---

## [2026-04-02] — Auto-Redeem & Portfolio Refactor (Summary)

**Status:** COMPLETE

### Features
- **Auto-redeem module** (`auto-redeemer.ts`) — Periodically detects resolved markets and redeems winning positions
  - Runs every 30 minutes in live mode (skipped in preview)
  - Per-position error isolation — one failure doesn't block others
  - Telegram notifications on redemption events
  - Binary market filtering — skips neg-risk multi-outcome markets

### Code Quality
- **Full type coverage** — Eliminated all `any` types; added shared `types.ts`
- **Linting & formatting** — ESLint + Prettier integrated
- **Test suite** — 45+ Vitest unit tests (config, inventory, risk-manager, trade-store, utils, auto-redeemer)
- **Module refactoring**:
  - Extracted constants into `constants.ts`
  - Extracted utilities into `utils.ts`
  - Split trade-executor into `order-executor.ts` + `order-verifier.ts`
  - Split screen-traders into 3 focused modules

### Performance
- **Async logger** — Switched from sync file writes to buffered stream with daily rotation
- **Inventory reconciliation** — Periodic 5-min API sync to catch late fills and external changes

---

## [2026-04-01] — Hostile Audit Fixes

**Status:** COMPLETE

### Fixes
- **RiskState field rename** — `positionsByMarket` → `dailySpendByMarket` for semantic clarity
  - Migration handles old JSON state files automatically
- **Retry map LRU eviction** — Capped `retryCount` map at 1000 entries with half-size eviction
- **Trade validation filter** — `isValidTrade()` now requires `tokenId` and valid `size > 0`
- **Periodic inventory reconciliation** — Syncs positions every 5 minutes in main loop

---

## [2026-03-30] — Initial Deployment

**Status:** COMPLETE

### Core Features
- Copy-trading engine with PERCENTAGE and FIXED sizing strategies
- 8-layer risk management (NaN guard, daily volume, trade age, size validation, price validation, per-market cap, balance check)
- Inventory tracking with weighted average prices and API sync
- Telegram alerts (order placed/filled/unfilled/failed/error/summary)
- Backtesting engine with fill rate modeling
- Auto-restart via Docker with persistent volumes
- Health check script for API connectivity verification

### Infrastructure
- Docker Compose setup with restart policy
- Persistent data volumes (bot-data, bot-logs)
- Daily log file rotation
- Bot lock file to prevent double-execution

### Monitoring & Observability
- Colored console logging with log level filtering
- Daily trade history export (JSONL)
- Per-trader performance reporting
- Leaderboard-based trader screening

---

## Versioning

- **v1.1.0** — Current (execution quality guards, research aggregation, 223 tests)
- **v1.0.0** — Viem migration, CVE fixes, Telegram commands
- **v0.9.0** — Auto-redeem feature, full type coverage
- @polymarket/clob-client v5.8.1 compatible

---

## Known Issues & Technical Debt

### Resolved
- [x] ~~Type safety incomplete~~ → Full TypeScript coverage (v1.0.0)
- [x] ~~No test suite~~ → 45+ Vitest tests (v1.0.0)
- [x] ~~No linting~~ → ESLint + Prettier configured (v1.0.0)
- [x] ~~Elliptic CVEs~~ → Eliminated via viem migration (v1.0.0)

### Outstanding
- [ ] No HTTP health monitoring endpoint (health-check.ts is CLI-only)
- [ ] Inventory is placement-based guess between 5-min syncs (design choice for latency)
- [ ] No true drawdown/cooldown mechanism (requires fill-based P&L)
- [ ] Private key exposed in chat history pre-rotation — rotate before production use

### Backlog (Tier 2+)
- Dynamic trader re-screening (weekly cron)
- Dashboard / web UI
- WebSocket live pricing for better fill rates
- On-chain trade monitoring via eth_getLogs as fallback
- Performance report cron
