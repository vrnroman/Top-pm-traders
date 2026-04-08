# Polymarket Trading Bot

Two-strategy automated trading bot for [Polymarket](https://polymarket.com).

## Strategies

| # | Strategy | Description | Status |
|---|----------|-------------|--------|
| 1 | **Copy Traders** | Monitors top traders and copies their trades | Toggle via `STRATEGY1_ENABLED` |
| 2 | **Weather Betting** | Predicts daily max temperature using KDE model, bets when model probability exceeds market price | Toggle via `STRATEGY2_ENABLED` |
| 3 | **Tennis Odds Arbitrage** | Compares Pinnacle sharp odds against Polymarket tennis match prices, bets on divergences >10% | Toggle via `STRATEGY3_ENABLED` |

## Quick Start

```bash
cd weather-bot
cp .env.example .env   # Fill in TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
python main.py          # Daemon mode (scheduler + telegram)
python main.py --once   # Single prediction run
```

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/predict 11 Apr` | Run prediction for a specific date |
| `/predict` | Run prediction for default date (today + DAYS_IN_ADVANCE) |
| `/tennis` | Show current tennis divergences being monitored |
| `/tennis_pnl` | Tennis strategy P&L breakdown |
| `/status` | Bot status for all strategies |
| `/pnl` | Realized + unrealized P&L per strategy |
| `/takeprofit` | Close positions with >30% unrealized profit |
| `/help` | List commands |

## Configuration

Key parameters in `.env`:

```
STRATEGY1_ENABLED=false          # Copy traders (TS bot)
STRATEGY2_ENABLED=true           # Weather betting (Python)
STRATEGY3_ENABLED=false          # Tennis odds arbitrage
CITIES_TO_BET=nyc,chicago,denver,dallas
DAYS_IN_ADVANCE=4
MIN_EDGE=0.10                    # 10% minimum edge to bet
BET_SIZE=10.0                    # USD per bet
SCHEDULE_HOUR_SGT=15             # Auto-run at 3pm SGT
PREVIEW_MODE=true                # true = signals only, false = live trading
TENNIS_ODDS_PROVIDER=oddspapi    # oddspapi | scraper
TENNIS_MIN_DIVERGENCE=0.10       # 10% minimum edge for tennis
TENNIS_SCAN_INTERVAL=300         # Scan every 5 minutes
```

## Deploy

```bash
cd weather-bot
./deploy.sh    # Deploys to GCP (asia-northeast1 / Japan)
```

## Structure

```
weather-bot/
├── main.py                # Orchestrator (scheduler, telegram, all strategies)
├── bot.py                 # Strategy #2 standalone CLI
├── backtest.py            # Backtest against historical Polymarket data
├── generate_report.py     # HTML report with predictions vs market prices
├── telegram_bot.py        # Telegram commands and notifications
├── weather_predictor.py   # KDE temperature prediction model
├── polymarket_fetcher.py  # Polymarket API client (Gamma + CLOB)
├── weather_data.py        # Open-Meteo historical data fetcher
├── cities.py              # City definitions (20+ cities)
├── config.py              # Configuration from .env
├── deploy.sh              # GCP deployment script
├── Dockerfile             # Container build
├── docker-compose.yml     # Local Docker run
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
