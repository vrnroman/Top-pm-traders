# Polymarket Copy Trading Bot

Automated copy-trading bot for [Polymarket](https://polymarket.com) prediction markets on Polygon. Monitors target wallets and replicates their trades through the CLOB API with configurable risk limits.

## Architecture

```
Polymarket Data API ──→ Trade Monitor ──→ Risk Manager ──→ Trade Executor ──→ CLOB API
                                                ↕                                ↕
                                          Trade Store                      Inventory
                                                ↕
                                       Telegram Notifier
```

## Features

- **Copy-trading** — mirrors BUY/SELL trades from multiple wallets
- **Risk management** — daily volume caps, per-market limits, min/max order sizes, balance checks, trade age filter
- **Preview mode** — logs would-be trades without executing
- **Inventory tracking** — weighted average price, API sync on startup
- **Telegram alerts** — order placed, filled (with USD value), unfilled, failed, daily summary
- **Telegram `/status` command** — Check balance, daily volume, and open positions on-demand
- **Trader screening** — soft-scoring leaderboard analysis (win rate, ROI, holding period, burst detection)
- **Backtesting** — activity-based simulation with fill rate modeling and slippage analysis

## Quick Start

```bash
git clone <repo-url> && cd polymarket-copy-trading-bot
npm install
cp .env.example .env   # configure your wallets & keys
npm run dev             # starts in preview mode by default
```

## Configuration

All settings via `.env` (see `.env.example` for defaults):

| Variable | Type | Default | Description |
|---|---|---|---|
| `USER_ADDRESSES` | string | — | Comma-separated trader wallet addresses to copy |
| `PROXY_WALLET` | string | — | Your Polygon wallet address |
| `PRIVATE_KEY` | string | — | Wallet private key (64 hex chars, no 0x) |
| `SIGNATURE_TYPE` | number | `0` | 0 = EOA, 1 = POLY_PROXY, 2 = GNOSIS_SAFE |
| `COPY_STRATEGY` | string | `PERCENTAGE` | `PERCENTAGE` or `FIXED` |
| `COPY_SIZE` | number | `10.0` | % of trader's order (PERCENTAGE) or USD amount (FIXED) |
| `MAX_ORDER_SIZE_USD` | number | `100.0` | Max single order in USD |
| `MIN_ORDER_SIZE_USD` | number | `1.0` | Min single order in USD |
| `MAX_POSITION_PER_MARKET_USD` | number | `500.0` | Max USD placed per market per day |
| `MAX_DAILY_VOLUME_USD` | number | `1000.0` | Max total daily trading volume |
| `MAX_PRICE_DRIFT_BPS` | number | `300` | Max price drift from trader's entry (basis points, 300=3%) |
| `MAX_SPREAD_BPS` | number | `500` | Max acceptable bid-ask spread (basis points, 500=5%) |
| `FETCH_INTERVAL` | number | `1` | Polling interval in seconds |
| `FETCH_CONCURRENCY` | number | `5` | Max concurrent trader fetches per batch |
| `MAX_TRADE_AGE_HOURS` | number | `1` | Ignore trades older than this |
| `TRADE_MONITOR_MODE` | string | `data-api` | `data-api`, `hybrid`, or `onchain` |
| `PREVIEW_MODE` | boolean | `true` | Log trades without executing |
| `CLOB_API_URL` | string | `https://clob.polymarket.com` | CLOB API endpoint |
| `DATA_API_URL` | string | `https://data-api.polymarket.com` | Data API endpoint |
| `RPC_URL` | string | `https://polygon-rpc.com` | Polygon RPC endpoint |
| `TELEGRAM_BOT_TOKEN` | string | — | Telegram bot token (optional) |
| `TELEGRAM_CHAT_ID` | string | — | Telegram chat ID (optional) |

## Scripts

| Script | Command | Description |
|---|---|---|
| Dev | `npm run dev` | Run bot with tsx (auto-reload) |
| Build | `npm run build` | Compile TypeScript to dist/ |
| Start | `npm start` | Run compiled bot |
| Test | `npm test` | Run unit tests (vitest) |
| Type Check | `npm run typecheck` | Run TypeScript type checker |
| Lint | `npm run lint` | Run ESLint |
| Format | `npm run format` | Format code with Prettier |
| Health Check | `npm run health-check` | Test API connectivity |
| Screen Traders | `npx tsx src/scripts/screen-traders.ts` | Analyze leaderboard for copy targets (hard filters: activity, ROI, volume) |
| Discover Traders | `npx tsx src/scripts/discover-traders-market.ts` | Find profitable traders from active markets |
| Backtest | `npx tsx src/scripts/backtest-traders.ts` | Simulate copy-trading on historical data (fill rate, slippage) |
| Aggregate Research | `npx tsx src/scripts/aggregate-research-results.ts` | Merge screening runs, rank by consistency (pass rate, score stability) |
| Sell All | `npx tsx src/scripts/sell-all.ts` | Liquidate all open positions |
| Performance | `npx tsx src/scripts/performance-report.ts` | Per-trader P&L and slippage report |
| Backtest Preview | `npx tsx src/scripts/backtest-preview.ts` | Evaluate preview-mode trades |

## Module Overview

| Module | Responsibility |
|---|---|
| `config.ts` | Loads and validates all env vars, exposes typed `CONFIG` object |
| `config-validators.ts` | Pure validation functions (address, private key, address parsing) |
| `types.ts` | Shared interfaces for Polymarket API responses + `errorMessage()` helper |
| `constants.ts` | Contract addresses, ABIs, trading constants |
| `utils.ts` | Shared utilities: `sleep()`, `shortAddress()`, `roundCents()` |
| `index.ts` | Entry point — lock file, startup, main polling loop |
| `trade-monitor.ts` | Fetches trader activity from Polymarket Data API |
| `risk-manager.ts` | 8-check risk evaluation with injectable state for testing |
| `trade-executor.ts` | Orchestrates order placement, fill verification, inventory updates |
| `market-price.ts` | Fetches live best bid/ask from CLOB with 200ms timeout for quality checks |
| `order-executor.ts` | Places copy orders on CLOB with adaptive spread-aware pricing |
| `order-verifier.ts` | Polls CLOB for fill status with retries |
| `trade-store.ts` | Deduplicates trades via persistent seen-set, tracks retry counts |
| `inventory.ts` | Tracks positions with weighted average prices, syncs from API |
| `get-balance.ts` | Reads USDC.e balance from Polygon RPC with singleton provider |
| `check-approvals.ts` | Ensures ERC20 + ERC1155 approvals for Polymarket Exchange |
| `create-clob-client.ts` | Promise-cached singleton CLOB client with API key derivation |
| `logger.ts` | Colored console + async daily log file rotation |
| `market-price.ts` | Fetches live best bid/ask from CLOB with 200ms timeout, computes price drift, detects crossed books |
| `telegram-notifier.ts` | HTML-escaped trade events and bot lifecycle alerts to Telegram |
| `telegram-commands.ts` | Polls Telegram Bot API for `/status` command, returns balance + positions |

## Risk Management & Execution Quality

The bot applies two layers of checks:

### Risk Evaluation (8 checks, all must pass):
1. **NaN guard** — rejects malformed API data
2. **Daily volume** — caps total USD placed per day (resets midnight UTC)
3. **Trade age** — ignores trades older than `MAX_TRADE_AGE_HOURS`
4. **Copy size** — calculates order from strategy (percentage or fixed)
5. **Min/max order** — enforces bounds, caps oversized orders
6. **Price validation** — rejects prices at 0 or 1 (no edge)
7. **Per-market cap** — limits daily exposure per condition ID
8. **Balance check** — reduces order to available USDC (BUY only)

### Execution Quality Guards (before placement):
- **Live market snapshot** — Fetch best bid/ask (200ms timeout, 5s cache)
- **Price drift** — Skip if market moved ≥300bps from trader's entry
- **Spread check** — Skip if bid-ask spread ≥500bps
- **Crossed book detection** — Skip if bid > ask (no real liquidity)
- **Adaptive pricing** — Cap prices at ±2% of best bid/ask (vs fixed buffer)
- **Fallback** — Use fixed 2% buffer on API timeout

## Docker

```bash
# Build and run
docker compose up -d

# View logs
docker compose logs -f bot

# Stop
docker compose down
```

Data and logs are persisted in bind mounts (`./data`, `./logs`) for easier local access.

## Security

- Private key is cleared from `process.env` after CLOB client initialization (remains in module memory for the process lifetime)
- **Use a dedicated low-balance wallet** — never use your main wallet
- Token approvals are set to max uint256 for gas efficiency; revoke manually if needed
- No web server, no inbound connections — outbound API calls only

## License

MIT
