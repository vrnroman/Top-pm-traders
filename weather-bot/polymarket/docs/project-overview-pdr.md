# Polymarket Copy-Trading Bot — Project Overview & PDR

**Product Development Requirements (PDR)** — Specification, design decisions, and acceptance criteria for the copy-trading bot.

---

## Project Vision

Automated copy-trading bot for [Polymarket](https://polymarket.com) prediction markets on Polygon. Monitors target wallets and replicates their trades through the CLOB API with configurable risk limits, inventory tracking, and Telegram alerts.

**Target Use:** Retail traders copying expert traders on Polymarket with controlled risk exposure.

---

## Core Features (v1.0.0)

### 1. Multi-Wallet Copy-Trading
- **Requirement:** Monitor N traders simultaneously; replicate BUY/SELL trades on their markets
- **Configuration:** `USER_ADDRESSES` (comma-separated wallet list)
- **Strategies:** 
  - PERCENTAGE: Copy X% of trader's order size
  - FIXED: Fixed USD amount per trade
- **Status:** COMPLETE (production-ready)

### 2. Risk Management (8-Layer)
- **Requirement:** Protect capital via sequential validation gates
- **Checks:** NaN guard → daily volume → trade age → copy size → min/max bounds → price validation → per-market cap → balance check
- **Configurable Limits:**
  - `MAX_DAILY_VOLUME_USD` — Total daily trading volume
  - `MAX_ORDER_SIZE_USD` — Single order ceiling
  - `MIN_ORDER_SIZE_USD` — Single order floor
  - `MAX_POSITION_PER_MARKET_USD` — Daily exposure per market
  - `MAX_TRADE_AGE_HOURS` — Ignore old trades
- **Status:** COMPLETE (injectable state for testing)

### 3. Inventory Tracking
- **Requirement:** Track open positions with weighted average prices (WAP)
- **Syncing:** API sync every 5 minutes (catch late fills, external changes)
- **Persistence:** `data/inventory.json`
- **Features:** Deduplication, buy/sell recording, reconciliation
- **Status:** COMPLETE

### 4. Order Execution with Quality Guards
- **Requirement:** Place orders on CLOB API with execution quality checks
- **Quality checks:**
  - Live market price fetch (200ms timeout, 5s cache)
  - Price drift guard: skip if market moved ≥3% from trader's entry
  - Spread guard: skip if bid-ask spread ≥5%
  - Crossed book detection: reject if bid > ask
  - Adaptive pricing: cap prices at ±2% of best bid/ask (vs fixed 2% buffer)
- **Fallback:** Use fixed 2% buffer if market snapshot times out
- **Verification:** Poll fills via CLOB (2 retries, 3s delay)
- **Failure handling:** Per-trade isolation; one failure doesn't cascade
- **Status:** COMPLETE (added 2026-04-04)

### 5. Telegram Notifications
- **Requirement:** Real-time alerts for all trade events and bot lifecycle
- **Events:** orderPlaced, orderFilled, orderUnfilled, orderFailed, tradeError, dailySummary, positionsRedeemed, botStarted
- **Configuration:** Optional (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
- **Status:** COMPLETE

### 6. Auto-Redeem Resolved Positions
- **Requirement:** Automatically claim winnings when markets resolve
- **Schedule:** Every 30 minutes (live mode only; disabled in preview)
- **Scope:** Binary markets only (skips multi-outcome neg-risk markets)
- **Error handling:** Per-position isolation
- **Status:** COMPLETE (added 2026-04-02)

### 7. Trader Research & Analysis
- **Scripts:**
  - `screen-traders.ts` — Leaderboard analysis with hard filters (ROI, win rate, min markets, volume, trade frequency)
  - `discover-traders-market.ts` — Find traders from active markets
  - `backtest-traders.ts` — Activity-based historical simulation with fill modeling
  - `aggregate-research-results.ts` — Rank traders by consistency across multiple screening runs
  - `performance-report.ts` — Per-trader P&L and slippage analysis
- **Research Envelope:** Unified format (`research-types.ts`) for all outputs; persistent to `data/research/*.json`
- **Consistency Scoring:** Pass rate, score stability (std dev), median backtest ROI; tiers: production/watchlist/reject
- **Status:** COMPLETE

### 8. Logging & Monitoring
- **Features:** Colored console, daily log rotation, trade history export (JSONL)
- **Health check:** `health-check.ts` script for API connectivity verification
- **Status:** COMPLETE (no HTTP endpoint yet; CLI-only)

---

## Technical Architecture

### Blockchain Integration (Viem v2.47.6)
- **Client types:** `createWalletClient()` for signing, `createPublicClient()` for reads
- **Contract calls:** `readContract()` for balances/approvals, `writeContract()` for redemptions
- **ABI parsing:** viem's `parseAbi()` for compile-time validation (removed ethers v5 dependency)
- **Network:** Polygon PoS (configurable RPC: Alchemy, Infura, or public polygon-rpc.com)

### CLOB Integration
- **Client:** `@polymarket/clob-client` v5.8.1 (viem-native)
- **Auth:** API credentials derived from wallet signature (no manual key management)
- **Operations:** Order submission, fill polling, market metadata fetch

### Data Sources
- **Polymarket Data API** — Trader activity, market metadata, position data
- **Polymarket CLOB API** — Order placement and status
- **Polygon RPC** — On-chain state (balances, approvals, redemptions)

### Deployment
- **Docker Compose** — Containerized with auto-restart policy
- **Persistent volumes:** `bot-data` (seen trades, inventory, state) and `bot-logs`
- **Lock file:** Prevents double-execution
- **Node.js:** >=20.0.0

---

## Requirements Specification

### Functional Requirements (MUSTs)

| ID | Requirement | Acceptance Criteria | Status |
|----|-------------|-------------------|--------|
| FR-1 | Monitor multiple traders | Fetch Data API for N wallets in USER_ADDRESSES | ✓ DONE |
| FR-2 | Replicate trades | Place copy orders on CLOB within 5s of original | ✓ DONE |
| FR-3 | Risk management | All 8 checks pass before execution; configurable limits | ✓ DONE |
| FR-4 | Track positions | Maintain inventory with WAP; sync API every 5min | ✓ DONE |
| FR-5 | Verify fills | Poll CLOB up to 2 retries; update inventory | ✓ DONE |
| FR-6 | Telegram alerts | Send notifications for all trade events | ✓ DONE |
| FR-7 | Auto-redeem | Detect resolved markets; redeem positions every 30min | ✓ DONE |
| FR-8 | Backtest capability | Replay historical trades with fill modeling | ✓ DONE |
| FR-9 | Error isolation | One trade failure doesn't cascade | ✓ DONE |
| FR-10 | Private key safety | Clear from process.env after init | ✓ DONE |

### Non-Functional Requirements (SHOULDs)

| ID | Requirement | Target | Status |
|----|-------------|--------|--------|
| NFR-1 | Type safety | Zero `any` types in TypeScript | ✓ DONE |
| NFR-2 | Test coverage | >70% unit test coverage | ✓ DONE (45+ tests) |
| NFR-3 | Linting | Zero ESLint violations | ✓ DONE |
| NFR-4 | Code formatting | Consistent style (Prettier) | ✓ DONE |
| NFR-5 | Performance | Latency <100ms for risk evaluation | ✓ DONE |
| NFR-6 | Logging | Async non-blocking, daily rotation | ✓ DONE |
| NFR-7 | Security | No hardcoded credentials, CVE remediation | ✓ DONE (viem migration) |
| NFR-8 | Documentation | Architecture, code standards, setup guide | ✓ DONE |

---

## Design Decisions

### Decision: Viem over Ethers (2026-04-03)

**Problem:** ethers v5 inherited 15 CVEs via elliptic dependency chain.

**Options:**
1. Upgrade ethers to v6 (breaking API changes, migration effort)
2. Switch to viem (modern, smaller, viem-native @polymarket/clob-client v5 available)

**Decision:** Migrate to viem v2.47.6

**Rationale:**
- viem is modern, modular, TypeScript-first (better IDE support)
- @polymarket/clob-client v5.8.1 is viem-native (no compatibility layer needed)
- parseAbi() provides compile-time ABI validation (prevents typos in function names)
- Smaller bundle (~40KB saved)
- Eliminates 15 CVEs immediately

**Implementation:** Updated 4 files (create-clob-client.ts, get-balance.ts, check-approvals.ts, auto-redeemer.ts); added 3 test files.

---

### Decision: 5-Minute Inventory Reconciliation

**Problem:** Inventory is placement-based guess between syncs; doesn't catch external fills or late API updates.

**Options:**
1. Real-time polling of pending orders (high RPC load)
2. 5-minute periodic API sync (moderate load, acceptable latency)
3. No sync (potential inventory desync)

**Decision:** 5-minute periodic sync via Data API

**Rationale:**
- Catches late fills within 5 minutes (acceptable for copy-trading latency)
- Moderate RPC/API load (one call per 5 min vs. per trade)
- Simple to implement; decoupled from trade loop
- Trade-off: Real-time inventory accuracy vs. operational overhead

---

### Decision: 30-Minute Auto-Redeem Cycle

**Problem:** Resolved positions must be redeemed to claim winnings; requires on-chain transaction.

**Options:**
1. Redeem immediately on market resolution (RPC latency, gas cost)
2. 30-minute batch redeem (lower gas, acceptable delay)
3. Manual redeem (user-initiated)

**Decision:** 30-minute batch cycle (live mode only)

**Rationale:**
- 30 min is acceptable delay for claiming winnings
- Batch reduces gas cost vs. per-position redemptions
- Live mode only (skipped in preview) prevents accidental redemptions during testing
- Per-position error isolation prevents one failure from blocking others

---

### Decision: Stateless Risk Manager (Injected State)

**Problem:** Risk evaluation requires state (daily volume, per-market spend) but shouldn't do I/O (RPC, file reads).

**Options:**
1. Pure function with injected state (testable, fast)
2. Stateful evaluator with I/O (harder to test)

**Decision:** Pure function with injected state

**Rationale:**
- Risk evaluation runs for every potential trade (~every 5s); must be fast (<100ms)
- Pure function enables unit testing without mocks
- State passed by caller; evaluator is side-effect free
- Simplifies debugging and performance profiling

---

### Decision: Async Logger with Buffered Writes

**Problem:** Sync file writes block event loop; could cause missed trades during I/O stalls.

**Options:**
1. Sync writes (simple, blocks main loop)
2. Async stream with buffering (non-blocking, complex)

**Decision:** Async stream with daily rotation

**Rationale:**
- Prevents I/O blocking on high-traffic days
- Buffering reduces syscalls
- Daily rotation keeps file sizes manageable
- Acceptable lag (sub-second buffer flush) for logging accuracy

---

## Acceptance Criteria (Definition of Done)

### Feature Acceptance
- [x] Code compiles without TypeScript errors
- [x] All unit tests pass (`npm test`)
- [x] ESLint passes (`npm run lint`)
- [x] Code is formatted (`npm run format`)
- [x] Integration with main polling loop verified

### Security Acceptance
- [x] No hardcoded credentials
- [x] Private key cleared from process.env
- [x] All dependencies scanned for CVEs (0 vulnerabilities)
- [x] Input validation (no NaN, negative sizes, etc.)

### Documentation Acceptance
- [x] Code comments for complex logic
- [x] Architecture documentation (system-architecture.md)
- [x] Code standards (code-standards.md)
- [x] Setup guide (setup-guide.md, in Russian)
- [x] Changelog (project-changelog.md)

### Testing Acceptance
- [x] Unit tests cover happy path and error cases
- [x] Risk manager tests cover all 8 checks
- [x] Inventory tests cover buy/sell/redemption
- [x] Auto-redeemer tests cover edge cases

---

## Known Limitations & Constraints

### Operational
- **Inventory is placement-based** — Between 5-min syncs, doesn't reflect external fills (design choice for latency)
- **No true drawdown cooldown** — Removed as dead code; needs fill-based P&L to implement
- **No multi-wallet support** — Single proxy wallet only; no capital spreading

### Monitoring
- **No HTTP health endpoint** — health-check.ts is CLI-only; HTTP endpoint not implemented
- **No dashboard/UI** — Telegram alerts only; no web visualization

### Advanced Features
- **No WebSocket live pricing** — Uses trader's price + buffer; could improve fill rates at larger sizes
- **No on-chain trade monitoring** — Relies on Data API; fallback to eth_getLogs not implemented
- **No news-driven signals** — Pure copy-trading only

---

## Future Roadmap (Tier 2+)

### Tier 2: Important (significantly better)
- [ ] Health monitoring HTTP endpoint (replaces CLI script)
- [ ] Dashboard / web UI (positions, P&L, last trades, bot status)
- [ ] Dynamic trader re-screening (weekly cron, detect degradation)
- [ ] Performance report cron (daily Telegram summary)

### Tier 3: Competitive Edge
- [ ] WebSocket live pricing (real-time market prices)
- [ ] On-chain trade monitoring via eth_getLogs (fallback to Data API)
- [ ] Multi-wallet support (capital spreading, risk isolation)
- [ ] Backtest-as-cron (weekly automated simulation)

### Tier 4: Advanced / V2
- [ ] News-driven signals (sentiment weighting)
- [ ] Multi-strategy (copy trading + market making + arbitrage)
- [ ] AI trade selection (Bayesian probability updates)
- [ ] Conditional token redemption optimizer (batch gas efficiency)

---

## Success Metrics

### Quantitative
- **Uptime:** >99% (Docker auto-restart handles crashes)
- **Trade latency:** <5 seconds from Data API to CLOB submission
- **Fill rate:** >90% (depends on CLOB liquidity, not bot design)
- **Risk accuracy:** All 8 checks enforced; no oversized orders
- **Error isolation:** Max 1 failed trade per event, others continue

### Qualitative
- **Operational ease:** Single `docker compose up` to deploy
- **Maintainability:** Full TypeScript typing, 45+ unit tests, clear module boundaries
- **Security:** Zero CVEs, private key managed safely, approvals explicit
- **Observability:** Telegram alerts for all critical events, daily log files

---

## Dependencies & Constraints

### Runtime
- Node.js >=20.0.0 (ES2020, async/await)
- Polygon RPC (Alchemy, Infura, or public endpoint)
- Polymarket Data API (HTTPS, no auth)
- Polymarket CLOB API (HTTPS, wallet-signed requests)

### Libraries
- **viem ^2.47.6** — Blockchain client, EVM calls
- **@polymarket/clob-client ^5.8.1** — CLOB API wrapper
- **axios ^1.7.0** — HTTP requests
- **dotenv ^16.4.7** — Env var loading

### Deployment
- Docker & Docker Compose (optional; can run locally)
- Polygon wallet with USDC.e + POL (collateral + gas)
- Telegram bot token (optional; alerts disabled without it)

---

## Maintenance & Support

### Monthly Tasks
- Monitor error logs for patterns
- Review trader performance (weekly leaderboard check)
- Verify POL gas balance (~$1–$2 required)
- Check for new Polymarket API changes

### Quarterly Tasks
- Update dependencies (`npm update`)
- Scan for new CVEs (`npm audit`)
- Backtest strategy on latest historical data
- Review and adjust risk limits based on portfolio

### On-Demand
- Emergency position liquidation (`sell-all.ts`)
- Manual trader screening (`screen-traders.ts`)
- Performance analysis (`performance-report.ts`)

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.1.0 | 2026-04-04 | Execution quality guards (price drift, spread checks), research aggregation (223 tests) |
| 1.0.0 | 2026-04-03 | Viem migration, CVE fixes, 0 vulnerabilities, Telegram commands |
| 0.9.0 | 2026-04-02 | Auto-redeem feature, 45+ unit tests, full type coverage |
| 0.8.0 | 2026-04-01 | Hostile audit fixes (RiskState rename, retry LRU, validation) |
| 0.1.0 | 2026-03-30 | Initial deployment (copy-trading, risk mgmt, Telegram) |

---

## Stakeholders & Ownership

| Role | Name/Contact | Responsibility |
|------|--------------|-----------------|
| **Developer** | Pavel Volkov | Implementation, testing, deployment |
| **Collaborator** | grinev (grinevruslan@gmail.com) | Code review, monitoring |
| **Operator** | Pavel Volkov | Configuration, wallet management, on-call |

---

## Approval & Sign-Off

**Last Updated:** 2026-04-04

**Document Status:** ACTIVE (reflects v1.1.0 with execution quality guards)

**Approval:** Project specification complete and verified against codebase.
