# Poly Poly Bot

Multi-strategy automated trading bot for [Polymarket](https://polymarket.com). Runs three independent strategies that can be toggled on/off via environment variables, controlled through Telegram, and deployed to GCP with a single command.

## Strategies

| # | Strategy | Language | Description | Toggle |
|---|----------|----------|-------------|--------|
| 1 | **Copy Traders** | TypeScript | Monitors top Polymarket traders and copies their trades in real-time | `STRATEGY1_ENABLED` |
| 2 | **Weather Betting** | Python | Predicts daily max temperature using a KDE model, bets when model probability exceeds market price by a configurable edge | `STRATEGY2_ENABLED` |
| 3 | **Tennis Odds Arbitrage** | Python | Compares Pinnacle sharp odds against Polymarket tennis match prices, bets on divergences above a threshold | `STRATEGY3_ENABLED` |

Each strategy runs independently. Enable any combination via `.env`.

## Prerequisites

- **Python 3.12+** (strategies #2 and #3, orchestrator, Telegram bot)
- **Node.js 22+** (strategy #1 only)
- **Docker** (for deployment)
- A **Telegram bot token** (from [@BotFather](https://t.me/BotFather))
- A **Polymarket wallet** (private key + proxy wallet) for live trading

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/vrnroman/Top-pm-traders.git
cd Top-pm-traders/poly_poly_bot

# 2. Create and fill in your environment file
cp .env.example .env
# Edit .env — at minimum set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. (Optional) Install Strategy #1 dependencies
cd polymarket && npm install && cd ..

# 5. Run in preview mode (no real trades)
python main.py

# 6. Or run a single prediction cycle
python main.py --once
```

By default `PREVIEW_MODE=true` — the bot calculates signals, logs everything, and sends Telegram notifications, but does **not** place real orders.

## Configuration

All configuration is via environment variables (`.env` file). See `.env.example` for the full list with descriptions.

### Key Parameters

| Variable | Default | Description |
|----------|---------|-------------|
| `PREVIEW_MODE` | `true` | `true` = signals only, `false` = live trading |
| `STRATEGY1_ENABLED` | `false` | Enable copy-trading strategy |
| `STRATEGY2_ENABLED` | `true` | Enable weather betting strategy |
| `STRATEGY3_ENABLED` | `false` | Enable tennis arbitrage strategy |
| `CITIES_TO_BET` | `nyc,chicago,denver,dallas` | Comma-separated city keys for weather strategy |
| `DAYS_IN_ADVANCE` | `4` | How far ahead to look for weather markets |
| `MIN_EDGE` | `0.10` | Minimum edge (10%) to place a weather bet |
| `BET_SIZE` | `10.0` | USD per bet (weather) |
| `SCHEDULE_HOUR_SGT` | `15` | Auto-run hour in Singapore Time |
| `TENNIS_MIN_DIVERGENCE` | `0.10` | Minimum edge for tennis bets |
| `TENNIS_SCAN_INTERVAL` | `300` | Seconds between tennis scans |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token |
| `TELEGRAM_CHAT_ID` | — | Telegram chat ID for notifications |

### Wallet Configuration (Live Trading Only)

| Variable | Description |
|----------|-------------|
| `PRIVATE_KEY` | Polymarket wallet private key |
| `PROXY_WALLET` | Polymarket proxy wallet address |

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/predict 11 Apr` | Run weather prediction for a specific date |
| `/predict` | Run prediction for default date (today + `DAYS_IN_ADVANCE`) |
| `/tennis` | Show current tennis divergences being monitored |
| `/tennis_pnl` | Tennis strategy P&L breakdown |
| `/status` | Bot status for all strategies |
| `/pnl` | Realized + unrealized P&L per strategy |
| `/takeprofit` | Close positions with >30% unrealized profit |
| `/help` | List commands |

## Deployment (GCP)

The bot deploys to a GCP Compute Engine VM via Docker.

```bash
cd poly_poly_bot

# Make sure .env is configured with your GCP_PROJECT_ID
./deploy.sh
```

This will:
1. Create a GCE VM (`e2-small` in `asia-northeast1-a`) if it doesn't exist
2. Archive and upload the code
3. Build a Docker image on the VM
4. Start the container with auto-restart

### Post-deployment commands

```bash
# Monitor logs
gcloud compute ssh poly-poly-bot --zone=asia-northeast1-a --command='docker logs -f poly-poly-bot'

# Stop the bot
gcloud compute ssh poly-poly-bot --zone=asia-northeast1-a --command='docker stop poly-poly-bot'

# View daily log file
gcloud compute ssh poly-poly-bot --zone=asia-northeast1-a --command='cat ~/app/logs/bot-$(date +%Y-%m-%d).log'
```

### Docker Compose (local)

```bash
cd poly_poly_bot
docker compose up --build
```

## Backtesting

Replay historical data through the same signal pipeline:

```bash
# Weather strategy backtest
python backtest.py

# Tennis strategy backtest
python backtest/tennis_backtest.py
```

## Project Structure

```
poly_poly_bot/
├── main.py                # Orchestrator (scheduler, telegram, all strategies)
├── bot.py                 # Strategy #2 standalone CLI
├── config.py              # Configuration from .env
├── telegram_bot.py        # Telegram commands and notifications
├── weather_predictor.py   # KDE temperature prediction model
├── polymarket_fetcher.py  # Polymarket API client (Gamma + CLOB)
├── weather_data.py        # Open-Meteo historical data fetcher
├── cities.py              # City definitions (20+ cities)
├── backtest.py            # Weather strategy backtest
├── generate_report.py     # HTML report with predictions vs market prices
├── deploy.sh              # GCP deployment script
├── Dockerfile             # Container build
├── docker-compose.yml     # Local Docker run
├── .env.example           # Template environment file
├── requirements.txt       # Python dependencies
├── src/
│   ├── odds/              # Odds data fetching module
│   │   ├── base.py        # Abstract OddsProvider interface
│   │   ├── oddspapi.py    # OddsPapi free tier (Pinnacle odds)
│   │   ├── scraper.py     # Web scraping fallback
│   │   └── models.py      # MatchOdds, OddsComparison pydantic models
│   └── strategies/
│       └── tennis_arb.py  # Strategy #3: Tennis Odds Arbitrage
├── tests/
│   ├── test_tennis_arb.py     # Strategy #3 tests
│   └── test_odds_provider.py  # Odds provider tests
├── backtest/
│   └── tennis_backtest.py # Tennis arb backtesting
└── polymarket/            # Strategy #1 — TypeScript copy-trader bot
```
