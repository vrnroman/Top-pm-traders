# Poly Poly Bot v2.0

Unified Polymarket trading bot with three independent strategies, running as a single Python process.

## Overview

| Strategy | Description | Signal Source |
|----------|-------------|---------------|
| **1 - Copy Trading** | Copy insider/whale trades with tiered risk (1a geopolitical insiders, 1b leaderboard whales, 1c auto-detect new insiders) | Polymarket Data API / on-chain |
| **2 - Weather Betting** | Bet on temperature markets using NOAA ensemble forecasts | NOAA GFS/GEFS API |
| **3 - Tennis Arbitrage** | Exploit divergence between sharp bookmaker odds and Polymarket prices | The Odds API / scrapers |

## Architecture

```
poly_poly_bot/
  main.py              # Entry point — starts enabled strategies
  src/
    config.py          # Unified .env configuration
    config_validators.py
    models.py          # Pydantic data models
    utils.py           # Shared utilities
    logger.py          # Structured logging
    constants.py       # Contract addresses, ABIs
    copy_trading/      # Strategy 1: risk manager, order executor, trade monitor
    weather/           # Strategy 2: forecast, market matching, betting
    strategies/        # Strategy 3: tennis arb logic
    odds/              # Odds providers (oddspapi, scraper)
  tests/               # pytest test suite
  data/                # Runtime state (risk state, inventory, trade history)
  cache/               # API response caches
  results/             # Backtest results
  logs/                # Application logs
```

All three strategies share the same configuration system, Telegram notifier, and CLOB client. Each strategy can be independently enabled/disabled via environment variables.

## Quick Start

### 1. Install dependencies

```bash
make setup
# or: pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your keys and strategy settings
```

### 3. Run in preview mode (recommended first)

```bash
make run-preview
# or: PREVIEW_MODE=true python main.py
```

Preview mode calculates everything, logs decisions, and sends Telegram notifications but does **not** place real orders. Simulated P&L is tracked.

### 4. Run live

```bash
make run-live
# or: PREVIEW_MODE=false python main.py
```

## Configuration

All configuration lives in `.env`. See `.env.example` for the full reference.

### Global Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `PREVIEW_MODE` | `true` | `true` = dry-run, `false` = live trading |
| `PRIVATE_KEY` | | Polygon wallet private key (64 hex chars) |
| `PROXY_WALLET` | | Polymarket proxy wallet address |
| `TELEGRAM_BOT_TOKEN` | | Telegram bot token for notifications |
| `TELEGRAM_CHAT_ID` | | Telegram chat ID for alerts |

### Strategy 1: Copy Trading (Tiered)

- **Tier 1a** (`STRATEGY_1A_*`): Geopolitical insiders - high conviction, larger bets
- **Tier 1b** (`STRATEGY_1B_*`): Leaderboard whales - medium conviction
- **Tier 1c** (`STRATEGY_1C_*`): Auto-detected new insiders - alert-only by default

Each tier has independent: `WALLETS`, `COPY_PERCENTAGE`, `MAX_BET`, `MIN_BET`, `MAX_TOTAL_EXPOSURE`, `MAX_PRICE`, `MIN_TRADER_BET`.

### Strategy 2: Weather Betting

| Variable | Default | Description |
|----------|---------|-------------|
| `STRATEGY2_ENABLED` | `false` | Enable weather strategy |
| `CITIES_TO_BET` | `nyc,chicago,denver,dallas` | Cities to monitor |
| `MIN_EDGE` | `0.10` | Minimum edge (10%) to place bet |
| `BET_SIZE` | `10.0` | USD per bet |

### Strategy 3: Tennis Arbitrage

| Variable | Default | Description |
|----------|---------|-------------|
| `STRATEGY3_ENABLED` | `false` | Enable tennis arb strategy |
| `ODDSPAPI_API_KEY` | | The Odds API key |
| `TENNIS_MIN_DIVERGENCE` | `0.10` | Minimum edge (10%) |
| `TENNIS_KELLY_FRACTION` | `0.25` | Quarter-Kelly sizing |

## Testing

```bash
make test
# or: python -m pytest tests/ -v --tb=short

# With coverage
python -m pytest tests/ --cov=src --cov-report=term-missing

# Lint
make lint
```

The test suite covers: config validators, utilities, risk managers (legacy and tiered), order verification, inventory tracking, trade store, trade queues, pattern detection, market price snapshots, strategy configuration, tennis arbitrage, and odds providers.

## Deployment

### Docker (local)

```bash
docker compose up -d
docker compose logs -f
```

### GCP Compute Engine

```bash
make deploy
# or: bash deploy.sh
```

This creates (or reuses) an `e2-small` VM in `asia-northeast1-a`, uploads the code, builds the Docker image on the VM, and starts the container with persistent volumes for data, cache, results, and logs.

## Preview Mode

Every trading function checks `PREVIEW_MODE`. When enabled:

- All signals are calculated normally
- All risk checks are evaluated
- Trade decisions are logged with full context
- Telegram notifications are sent (tagged as PREVIEW)
- Orders are **not** submitted to the CLOB
- Simulated P&L is tracked in `data/`

Always run in preview mode first to validate your configuration and observe signal quality before going live.
