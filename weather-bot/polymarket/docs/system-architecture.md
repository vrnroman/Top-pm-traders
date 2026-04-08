# Polymarket Copy-Trading Bot — System Architecture

High-level system design, component interactions, and data flow for the copy-trading bot.

## System Overview

```
┌─────────────────────────────────────────────────────────────┐
│                   Polymarket Data API                        │
│              (Markets, Orders, Trade History)               │
└──────────────────────┬──────────────────────────────────────┘
                       │ Fetch trader activity
                       ▼
            ┌──────────────────────┐
            │   Trade Monitor      │
            │ (monitors N wallets) │
            └──────────┬───────────┘
                       │ Valid trades
                       ▼
            ┌──────────────────────┐
            │  Risk Manager        │
            │  (8 risk checks)     │
            └──────────┬───────────┘
                       │ Pass all checks
                       ▼
            ┌──────────────────────┐     ┌────────────────────┐
            │  Trade Executor      │────▶│  Order Executor    │
            │ (orchestrator)       │     │ (CLOB API place)   │
            └──────────┬───────────┘     └────────────────────┘
                       │                         │
                       │ Track & verify         │ CLOB API call
                       ▼                         │
            ┌──────────────────────┐     ┌───────▼─────────────┐
            │  Order Verifier      │     │   CLOB API          │
            │ (poll CLOB for fill) │     │ (Polymarket CLOB)   │
            └──────────┬───────────┘     └────────────────────┘
                       │ Filled order
                       ▼
            ┌──────────────────────┐
            │  Inventory Tracker   │
            │ (positions, WAP)     │
            └──────────┬───────────┘
                       │ Position change
                       ▼
            ┌──────────────────────┐
            │  Telegram Notifier   │
            │ (trade alerts)       │
            └──────────────────────┘

Parallel Tasks:
┌─────────────────────────────────────┐
│  Telegram Bot API (3s interval)     │
│  /status command polling            │
└──────────┬────────────────────────────
           │ Command request
           ▼
    ┌──────────────────────┐
    │ Telegram Commands    │
    │ (/status handler)    │
    └──────────┬───────────┘
               │ Response: balance + positions
               ▼
    ┌──────────────────────┐
    │ Telegram Message API │
    │ (send status reply)  │
    └──────────────────────┘

Periodic Tasks (via main loop):
  ▪ 5-min: Inventory API reconciliation (catch late fills, external changes)
  ▪ 30-min: Auto-redeem (detect resolved markets, redeem winnings)
  ▪ Daily: Log rotation, risk state reset (midnight UTC)
  ▪ 3s: Telegram command polling (parallel, independent)
```

## Component Responsibilities

### Trade Monitoring (`trade-monitor.ts`)

**Input:** Polymarket Data API responses (trader activity)

**Function:**
- Polls Data API at `FETCH_INTERVAL` (default 5s) for each watched trader
- Validates trades: requires tokenId, size > 0, not NaN
- Deduplicates via persistent seen-set (trade-store.ts)
- Filters by trade age (`MAX_TRADE_AGE_HOURS`)

**Output:** Valid, unseen trades queued for risk evaluation

**Failure modes:**
- API returns 429 (rate limit) → exponential backoff in main loop
- Invalid trade data → logged and skipped
- Network timeout → retried next cycle

---

### Risk Manager (`risk-manager.ts`)

**Input:** Proposed trade (type, size, price, trader address)

**Function:** Applies 8 sequential checks (all must pass):

1. **NaN Guard** — Rejects malformed API data
2. **Daily Volume** — Caps total USD placed per day (resets midnight UTC)
3. **Trade Age** — Ignores trades older than `MAX_TRADE_AGE_HOURS`
4. **Copy Size** — Calculates order from strategy (PERCENTAGE of trader's size or FIXED amount)
5. **Min/Max Bounds** — Enforces `MIN_ORDER_SIZE_USD` and `MAX_ORDER_SIZE_USD`
6. **Price Validation** — Rejects prices at 0 or 1 (no edge trading)
7. **Per-Market Cap** — Limits daily exposure per condition ID (`MAX_POSITION_PER_MARKET_USD`)
8. **Balance Check** — For BUY orders, caps to available USDC balance

**Output:** Approved trade with final size or rejection reason

**Isolation:** Injected state allows testing without I/O (file reads, RPC calls)

---

### Trade Executor (`trade-executor.ts`)

**Input:** Approved trade, CLOB client, inventory tracker

**Function:** Orchestrates placement + verification:
- Calls order-executor to place order on CLOB
- Polls order-verifier for fill confirmation (2 retries, 3s delay)
- Updates inventory on fill
- Records to trade history (JSONL)

**Output:** Trade result (filled, partial, unfilled, error)

**Failure isolation:** One failed trade doesn't block others

---

### Order Executor (`order-executor.ts`)

**Input:** Trade details, wallet address

**Function:**
- Fetch live market snapshot (200ms timeout, 5s cache)
- Check price drift: skip if market moved ≥300bps from trader entry
- Check spread: skip if bid-ask spread ≥500bps
- Detect crossed books (bid > ask)
- Construct order with adaptive pricing: cap prices at ±2% of best bid/ask (replaces fixed buffer)
- Fallback to fixed 2% buffer if market snapshot times out
- Call CLOB API `/submit_order`
- Handle 400/500 errors gracefully

**Output:** CLOB order ID or error (or skipped due to price quality)

**Example skip:** Market now bid=0.50 ask=0.55, trader entered at 0.40 → drift=25% > 3% limit → skip trade

---

### Order Verifier (`order-verifier.ts`)

**Input:** Order ID, expected condition ID

**Function:**
- Polls CLOB `/get_order/{orderId}` for fill status
- Retries up to 2 times with 3s delay
- Returns final status: FILLED, PARTIAL, PENDING, CANCELED

**Output:** Order status + filled amount

---

### Inventory (`inventory.ts`)

**Input:** Trade fills, API position sync, redemption events

**Function:**
- Tracks open positions per condition ID (market + outcome)
- Calculates weighted average price (WAP) on buys
- Syncs with Data API every 5 minutes to catch late fills
- Updates on redemption events (`recordSell()` for successful redeems)

**Data persistence:** `data/inventory.json` (updated in-memory, synced to disk at shutdown)

**Output:** Current positions for balance checks, P&L reporting

---

### Auto-Redeem (`auto-redeemer.ts`)

**Input:** Resolved market list from Data API

**Function:**
- Polls Data API for markets with `resolutionSource === "API"` (resolved binary markets)
- Checks inventory for matching positions
- Calls CTF contract `redeemPositions()` via viem `writeContract()`
- Per-position error isolation (one failure doesn't block others)
- Runs every 30 minutes (live mode only, disabled in preview)

**Output:** Redemption receipts, Telegram notifications

---

### Blockchain Integration (viem-based)

#### `get-balance.ts`
- Reads USDC.e balance via viem `publicClient.readContract()`
- Uses singleton client pattern for connection reuse
- Caches result briefly to avoid RPC spam

#### `check-approvals.ts`
- Verifies ERC20 approval for USDC.e on CTF Exchange
- Verifies ERC1155 approval for Conditional Tokens (CTF) on CTF Exchange
- Sets approvals to `uint256.MAX` if missing
- Uses viem `readContract()` / `writeContract()`

#### `create-clob-client.ts`
- Creates viem `walletClient` from private key
- Derives CLOB API credentials via `createOrDeriveApiKey()`
- Returns authenticated CLOB client singleton
- Suppresses noisy "[CLOB Client] request error" log spam during key derivation

#### `constants.ts`
- Centralized Polygon contract addresses (USDC.e, CTF Exchange, CTF)
- Type-safe ABI fragments via viem's `parseAbi()`:
  - ERC20_BALANCE_ABI, ERC20_APPROVE_ABI
  - ERC1155_APPROVAL_ABI
  - CTF_REDEEM_ABI

---

### Notifications (`telegram-notifier.ts`)

**Input:** Trade events, error events, bot lifecycle events

**Function:**
- Sends HTML-escaped alerts to Telegram
- Events: orderPlaced, orderFilled (with USD amount), orderUnfilled, orderFailed, tradeError, dailySummary, positionsRedeemed, botStarted

**Output:** Telegram messages (optional if TELEGRAM_BOT_TOKEN not set)

---

### Telegram Commands (`telegram-commands.ts`)

**Input:** Telegram Bot API `/getUpdates` polling (3s interval)

**Function:**
- Polls Telegram API for incoming messages
- Filters by configured CHAT_ID (security: only respond to owner)
- Handles `/status` command
- Returns: balance, daily volume summary, open positions with USD values

**Output:** Telegram message with bot status

**Startup/Shutdown:** Started in main loop, stopped on graceful shutdown

---

### Logging (`logger.ts`)

**Input:** Log messages at different levels (info, warn, error)

**Function:**
- Colored console output with level filtering
- Async write stream to daily log file with buffering
- Daily rotation: `logs/bot-YYYY-MM-DD.log`
- Buffer auto-flush at shutdown or interval

**Output:** Console + daily log files

---

## Data Storage

```
data/
├── seen-trades.json       # Set of trade IDs already processed (prevents duplicates)
├── trade-history.jsonl    # All executed trades (one JSON object per line)
├── inventory.json         # Current open positions, weighted average prices
├── risk-state.json        # Daily volume tracking, per-market spending by market
└── bot.lock               # PID file (prevents double-execution)

logs/
└── bot-YYYY-MM-DD.log     # Daily rotated log file
```

---

## Data Flow Examples

### Buy Order Flow

```
1. Data API returns: { type: "BUY", trader: 0xabc..., size: 100, price: 0.45, conditionId: "0x123..." }

2. Trade Monitor: validates, deduplicates, checks age → passes

3. Risk Manager checks all 8 gates:
   - Daily volume: $50 placed today, $1000 limit → OK
   - Trade age: 2 minutes old, 1 hour limit → OK
   - Copy size: PERCENTAGE 10% → 100 * 0.1 = 10 USD
   - Min/max: $1–$100 → 10 USD is OK
   - Price: 0.45 is OK (not 0 or 1)
   - Per-market: $0 on this market, $500 limit → OK
   - Balance: USDC balance $500, order $10 → OK
   ✓ Approved: Buy 10 USD at price 0.45 for conditionId 0x123

4. Trade Executor: quality checks before placing order
   - Fetch live market snapshot: { bid: 0.44, ask: 0.46 } → OK
   - Price drift: (0.46 - 0.45) / 0.45 = 0.22% << 3% limit → OK
   - Spread: (0.46 - 0.44) / 0.45 = 4.4% << 5% limit → OK
   - Crossed book: 0.44 < 0.46 → OK (not crossed)
   ✓ Approved for execution

5. Order Executor: places order with adaptive pricing
   - Cap ask at min(0.46 * 1.02, 0.46 + 0.01) = 0.47
   - Place BUY order at 0.47 (more aggressive than trader's 0.45)
   - CLOB API returns orderId "12345"

6. Order Verifier: polls CLOB for status (3s, 3s, 3s) → FILLED at 0.46

7. Inventory: records position { conditionId: "0x123", amount: 10 USD, wap: 0.46 }

8. Telegram: sends "✓ Filled: Buy 10 USD ($4.60) at 0.46 on Market XYZ (from 0xabc...)"
```

### Sell Order Flow

```
1. Data API returns: { type: "SELL", trader: 0xabc..., size: 100, price: 0.55, conditionId: "0x123..." }

2. Trade Monitor validates → passes (no balance check for SELL)

3. Risk Manager: same 8 checks (no per-market cap enforcement for SELLs in current impl)

4. Trade Executor places order → Verifier confirms fill

5. Inventory: updates position record to reflect sold amount

6. Telegram alert sent
```

### Auto-Redeem Flow (every 30 minutes)

```
1. Auto-Redeem polls Data API: resolved markets { conditionId: "0x123", outcome: 1 }

2. Checks inventory: { conditionId: "0x123", amount: 10 USD, wap: 0.45 } exists

3. Outcome matches position → eligible for redemption

4. Calls CTF contract redeemPositions() via viem writeContract()

5. On success: inventory.recordSell() removes position

6. Telegram: sends "✓ Redeemed: 10 USD from Market XYZ (profit: +$2.55)"

7. On failure (e.g., gas): logs error, marks position for retry, continues (per-position isolation)
```

---

## Periodic Tasks (Main Loop)

```
main loop (FETCH_INTERVAL = 5s):
  ├─ Fetch trader activity (Data API)
  ├─ Risk evaluation & order placement
  ├─ Track retry counts (LRU capped @ 1000)
  │
  ├─ Every 5 minutes:
  │  └─ Inventory reconciliation API sync
  │
  ├─ Every 30 minutes:
  │  └─ Auto-redeem resolved positions
  │
  ├─ At midnight UTC:
  │  └─ Risk state reset (daily volume, per-market spending)
  │
  └─ Parallel polling (3s interval, independent of main loop):
     └─ Telegram command polling (/status) — started at bot startup
```

---

## Technology Stack

| Layer | Technology | Role |
|-------|-----------|------|
| **Blockchain RPC** | Polygon (Alchemy or polygon-rpc.com) | Read USDC.e balance, call on-chain functions |
| **Client Library** | viem ^2.47.6 | createWalletClient, createPublicClient, readContract, writeContract |
| **Exchange API** | Polymarket CLOB API | Order placement, status polling |
| **Data API** | Polymarket Data API | Trader activity, market metadata, position data |
| **Smart Contracts** | CTF (Conditional Token) | ERC1155 token redemption |
| **Runtime** | Node.js >=20.0.0 | Entry point, polling loop, async I/O |
| **Testing** | Vitest | Unit tests, coverage reporting |
| **Linting** | ESLint | TypeScript strict mode, no `any` types |
| **Formatting** | Prettier | Consistent code style |

---

## Design Principles

1. **Fail-safe isolation** — One trade failure doesn't cascade; per-position error handling in auto-redeem
2. **Stateless checks** — Risk manager accepts injected state; no side effects during evaluation
3. **Periodic reconciliation** — 5-min inventory sync catches late fills and external changes
4. **Singleton clients** — CLOB client promise-cached, RPC connections reused (viem pooling)
5. **Async streaming** — Logger uses buffered write stream, not sync file ops
6. **Type safety** — Full TypeScript coverage, no `any` types; viem's parseAbi for compile-time ABI validation
7. **Privacy by design** — Private key cleared from process.env after init; no hardcoded credentials
