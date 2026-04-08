# Polymarket Copy-Trading Bot — Backlog

## Recent Completions (Viem Migration & CVE Fix — 3 Apr 2026)

- [x] **Migrate from ethers v5 to viem** — replaced ethers.Wallet/JsonRpcProvider/Contract with viem createWalletClient/createPublicClient/readContract/writeContract; updated create-clob-client.ts, get-balance.ts, check-approvals.ts, auto-redeemer.ts
- [x] **Update @polymarket/clob-client** — upgraded from 4.22.8 to 5.8.1 (viem-compatible)
- [x] **Eliminate 15 CVEs** — elliptic vulnerabilities removed; 0 vulnerabilities remaining in dependencies
- [x] **Parse ABIs for viem type safety** — constants.ts uses `parseAbi()` for ERC20, ERC1155, CTF redeem ABIs
- [x] **Update TypeScript config** — added "DOM" to lib (required by viem's ox dependency)
- [x] **Add test files** — get-balance.test.ts, check-approvals.test.ts, create-clob-client.test.ts; updated auto-redeemer.test.ts mocks

## Recent Completions (Repo & Docs Setup — 2 Apr 2026)

- [x] **GitHub private repo** — created `ak40u/polymarket`, clean init commit without AI tooling files (.claude, .agents, .opencode)
- [x] **Collaborator invite** — `grinev` (grinevruslan@gmail.com) invited as contributor with push access
- [x] **Setup guide** — `docs/setup-guide.md` — полная инструкция по настройке: MetaMask/Polygon, .env, скрипты скрининга, Docker, Alchemy RPC, газ/POL оценки
- [x] **.gitignore cleanup** — excluded `.claude/`, `.agents/`, `.opencode/`, `CLAUDE.md`, `AGENTS.md` from repo
- [x] **Docker rebuild** — container rebuilt and running in LIVE mode (FIXED $1, 4 traders)

## Recent Completions (Hostile Audit Fixes — Apr 2026)

**Risk & Memory Management:**
- [x] **RiskState field rename** — `positionsByMarket` → `dailySpendByMarket` for semantic clarity; migration handles old JSON state files
- [x] **Retry map LRU eviction** — `retryCount` map capped at 1000 entries with half-size eviction to prevent unbounded memory growth
- [x] **Trade validation filter** — `isValidTrade()` now requires `tokenId` and valid `size > 0` (rejects NaN/missing data)
- [x] **Periodic inventory reconciliation** — syncs positions every 5 minutes in main loop to catch late fills and external changes

## Recent Completions (Auto-Redeem Feature — Apr 2026)

**Resolved Market Handling:**
- [x] **Auto-redeem module** — `auto-redeemer.ts` periodically detects and redeems resolved CTF positions
- [x] **Binary market filtering** — skips neg-risk (multi-outcome) markets, only redeems binary markets
- [x] **Per-position error isolation** — one redemption failure doesn't block others
- [x] **Telegram notifications** — sends alerts when positions redeemed via `telegram.positionsRedeemed()`
- [x] **Unit tests** — 6 Vitest tests covering: empty state, single/multiple redemptions, partial failures, API errors, missing fields
- [x] **Integration** — runs every 30 minutes in live mode; skipped in preview mode; integrated into main polling loop

## Recent Completions (Portfolio Refactor — Apr 2026)

**Code Quality & Tooling:**
- [x] **Full type coverage** — eliminated all `any` types, replaced with typed interfaces in new `types.ts`
- [x] **ESLint configuration** — strict TypeScript linting with recommended rules
- [x] **Prettier formatter** — automated code formatting configuration
- [x] **Vitest unit tests** — 45+ tests covering: config validation, inventory, risk manager, trade store, utils, auto-redeemer
- [x] **Node version lock** — `.nvmrc` pins to Node.js 20+

**Module Refactoring:**
- [x] **Extracted constants** — centralized in `constants.ts` (contract addresses, ABIs, trading constants)
- [x] **Extracted utilities** — `utils.ts` eliminates duplication (sleep, shortAddress, roundCents)
- [x] **Split trade-executor** — now `order-executor.ts` + `order-verifier.ts` (single responsibility)
- [x] **Split screen-traders** — now `screen-traders.ts` + `screen-traders-analysis.ts` + `screen-traders-output.ts`

**Performance & Architecture:**
- [x] **Async logger** — switched from `appendFileSync` to `createWriteStream` with buffering and daily rotation
- [x] **New npm scripts** — `lint`, `lint:fix`, `format`, `typecheck`, `test`, `test:watch`

**Module Inventory (31 production files + 6 test files):**

| Module | File | Lines | Responsibility |
|--------|------|-------|---|
| Core | `index.ts` | 206 | Entry point, polling loop, lock file, 5-min periodic inventory reconciliation, 30-min auto-redeem |
| Config | `config.ts` | 44 | Env validation, typed CONFIG object |
| Types | `types.ts` | 80+ | All shared interfaces (API responses, order shapes) |
| Constants | `constants.ts` | 25 | Contract addresses, ABIs (includes CTF_REDEEM_ABI), trading limits |
| Utils | `utils.ts` | 12 | Shared helpers (sleep, formatting, rounding) |
| Monitoring | `trade-monitor.ts` | 80+ | Fetch trader activity from Data API; validates tokenId & size |
| Risk | `risk-manager.ts` | 100+ | 7-layer risk evaluation; dailySpendByMarket (formerly positionsByMarket) |
| Orders | `order-executor.ts` | 60+ | Place copy orders with price buffer |
| Orders | `order-verifier.ts` | 50+ | Poll CLOB for fill status, update inventory |
| Storage | `trade-store.ts` | 60+ | Dedup trades, track retry counts (LRU capped @ 1000 entries, persistent seen-set) |
| Inventory | `inventory.ts` | 80+ | Position tracking, weighted avg prices, API sync, recordSell() for redemptions |
| Blockchain | `get-balance.ts` | 30+ | USDC.e balance from Polygon RPC via viem publicClient |
| Blockchain | `check-approvals.ts` | 60+ | ERC20 + ERC1155 approval checks/setup via viem readContract/writeContract |
| Blockchain | `create-clob-client.ts` | 30+ | CLOB singleton with viem walletClient + API key derivation |
| Blockchain | `auto-redeemer.ts` | 103 | Detect resolved positions from Data API, redeem on CTF contract via viem, per-position error isolation |
| Logging | `logger.ts` | 80+ | Colored console + async daily log rotation |
| Notifications | `telegram-notifier.ts` | 55 | Order/error/summary/redemption alerts to Telegram |
| Scripts | `screen-traders.ts` | Entry point for leaderboard analysis |
| Scripts | `screen-traders-analysis.ts` | Core analysis logic for trader screening |
| Scripts | `screen-traders-output.ts` | Formatting + output for screening results |
| Scripts | `health-check.ts` | API connectivity verification |
| Scripts | `backtest-traders.ts` | Activity-based historical simulation |
| Scripts | `backtest-preview.ts` | Evaluate preview-mode trades |
| Scripts | `sell-all.ts` | Liquidate all open positions |
| Scripts | `performance-report.ts` | Per-trader P&L and slippage report |

**Tests (Vitest):**
- `config-validation.test.ts` — ENV var loading and type coercion
- `inventory.test.ts` — position tracking, weighted pricing, deduplication
- `risk-manager.test.ts` — all 7 risk layers (NaN, daily vol, age, size, min/max, price, balance)
- `trade-store.test.ts` — seen-set persistence, retry counting
- `utils.test.ts` — helper functions (sleep, formatting, rounding)
- `auto-redeemer.test.ts` — empty state, single/multiple redemptions, partial failures, API errors, field validation

## Tier 1: Must Have (production blockers)

- [x] **Telegram alerts** — placed/filled/unfilled/failed/error/dailySummary/positionsRedeemed/botStarted — all implemented in `telegram-notifier.ts`
- [x] **Auto-restart (Docker)** — `docker compose` with `restart: unless-stopped` handles crash recovery; PM2 not needed with Docker
- [x] **Periodic inventory sync** — sync with API positions every 5 min in main polling loop
- [~] **Deploy to VPS** — running locally via Docker; VPS deployment (US-based, geo-unblocked) not yet done

## Tier 2: Important (significantly better)

- [x] **Auto-redeem resolved positions** — claim winnings automatically when markets resolve
- [ ] **Health monitoring endpoint** — HTTP endpoint returning bot status, balance, positions, uptime (health-check.ts exists as CLI script only, no HTTP endpoint)
- [ ] **Dynamic trader re-screening** — weekly cron to re-run screen-traders.ts, alert if trader degrades
- [ ] **Dashboard / web UI** — simple HTML page: positions, P&L, last trades, bot status
- [ ] **Performance report cron** — daily/weekly performance-report.ts run with Telegram summary

## Tier 3: Competitive Edge

- [ ] **WebSocket live pricing** — subscribe to CLOB WS (`wss://ws-subscriptions-clob.polymarket.com/ws/market`) for real-time prices on markets where tracked traders have positions; use live price instead of trader's price + buffer for better entry; subscribe on-demand after first trade detected on a market; useful for SELL monitoring and slippage protection at higher order sizes ($50+); marginal benefit at $1 trades
- [ ] **On-chain trade monitoring** — monitor CTF Exchange `OrderFilled` events via `eth_getLogs` (Alchemy RPC); more reliable than Data API but ~10-15s slower due to confirmation buffer; plan exists at `plans/260402-2102-onchain-trade-monitor/`; consider as fallback if Data API becomes persistently unreliable
- [~] **WebSocket trade detection** — investigated: CLOB WS only streams orderbook, not per-wallet trades; Polygon RPC `eth_subscribe` needs Alchemy Growth ($49/mo); NOT viable on free tier
- [~] **Real fill-based inventory** — `order-verifier.ts` uses `getOrder()` with retries at placement time; periodic polling of pending orders not implemented
- [ ] **Multi-wallet support** — spread capital across wallets for risk isolation
- [ ] **Backtest as cron** — weekly automated backtest with Telegram report
- [ ] **SELL mirroring with real inventory** — use on-chain position data for accurate SELL sizing

## Tier 4: Advanced / V2

- [ ] **News-driven signals** — scrape news feeds, weight copy signals by news sentiment
- [ ] **Multi-strategy** — copy trading + market making + arbitrage in one bot
- [ ] **AI trade selection** — Bayesian model for probability updates, filter low-confidence copies
- [ ] **Conditional token redemption optimizer** — batch redeem across markets for gas efficiency
- [~] **Rate limit adaptive throttling** — 429 detection + backoff in `trade-monitor.ts`, exponential backoff in main loop (`index.ts`); poll interval not dynamically adjusted yet

## Known Technical Debt

- [ ] Inventory is placement-based guess between syncs (design choice for latency; use `getOrder()` polling for real-time)
- [ ] No true drawdown/cooldown (removed as dead code — needs fill-based P&L to implement)
- [ ] Private key exposed in chat history — rotate before production
- [ ] USDC.e balance sometimes returns $0 from RPC (cached workaround in place)
- [x] ~~No test suite~~ — 45 Vitest unit tests added (config, inventory, risk-manager, trade-store, utils)
- [x] ~~No linting/formatting~~ — ESLint + Prettier configured and integrated into npm scripts
- [x] ~~Type safety incomplete~~ — all `any` types eliminated, full TypeScript coverage with shared interfaces
