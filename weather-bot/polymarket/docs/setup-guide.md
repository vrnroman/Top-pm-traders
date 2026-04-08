# Настройка Polymarket Copy-Trading Bot

## Что нужно перед запуском

### 1. MetaMask кошелёк на Polygon

Бот торгует на Polymarket через сеть Polygon. Нужен **отдельный кошелёк** (не основной!) с:

- **USDC.e** — коллатерал для ставок (бриджнутый USDC на Polygon, адрес контракта: `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`)
- **MATIC (POL)** — для оплаты газа

**Рекомендации по балансу:**
- USDC.e: $50–$500 в зависимости от стратегии
- POL (ex-MATIC): на **$1–$2** достаточно (POL ~$0.09). On-chain операции:
  - Approvals при первом запуске: ~0.01–0.03 MATIC
  - Redeem позиций (auto-redeemer каждые 30 мин): **~0.01–0.05 MATIC за позицию** (бот ставит 2x baseFee + 50 Gwei priority для надёжности)
  - При активной торговле 20–50 redemptions/мес → ~0.5–2.5 MATIC/мес

### 2. Приватный ключ кошелька

Из MetaMask: Settings → Security → Export Private Key. Нужен **64-символьный hex без 0x префикса**.

> **ВАЖНО:** Используй отдельный кошелёк с небольшим балансом. Никогда не храни крупные суммы на кошельке, приватный ключ которого лежит в .env файле.

### 3. Адреса трейдеров для копирования

Нужны Polymarket wallet-адреса трейдеров которых хочешь копировать. Как найти:
- Вручную на [polymarket.com/leaderboard](https://polymarket.com/leaderboard)
- Или через встроенный скрипт скрининга (см. ниже)

---

## Настройка .env

```bash
cp .env.example .env
```

### Обязательные переменные

```env
# Адреса кошельков трейдеров (через запятую)
USER_ADDRESSES=0xtrader1,0xtrader2,0xtrader3

# Твой кошелёк на Polygon
PROXY_WALLET=0xyourwallet

# Приватный ключ (64 hex символа, БЕЗ 0x)
PRIVATE_KEY=abc123...def456

# Тип подписи (только 0 — обычный EOA/MetaMask кошелёк)
SIGNATURE_TYPE=0
```

### Стратегия копирования

```env
# PERCENTAGE — копировать X% от размера ставки трейдера
# FIXED — фиксированная сумма за каждую сделку
COPY_STRATEGY=FIXED

# Для PERCENTAGE: 10.0 = 10% от ставки трейдера
# Для FIXED: 25.0 = $25 за каждую сделку
COPY_SIZE=1.0
```

### Лимиты риска (сбрасываются в полночь UTC)

```env
MAX_ORDER_SIZE_USD=100.0      # Макс. одна ставка
MIN_ORDER_SIZE_USD=1.0        # Мин. одна ставка
MAX_POSITION_PER_MARKET_USD=500.0  # Макс. на один рынок в день
MAX_DAILY_VOLUME_USD=1000.0   # Макс. общий объём за день
```

### RPC провайдер

По умолчанию используется публичный `https://polygon-rpc.com`. Рекомендуется заменить на Alchemy или Infura для надёжности:

```env
RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/YOUR_API_KEY
```

Бесплатный Alchemy план (30M compute units/мес) более чем достаточен для бота.

### Режим работы

```env
FETCH_INTERVAL=5          # Интервал опроса API (секунды)
MAX_TRADE_AGE_HOURS=1     # Игнорировать сделки старше X часов
PREVIEW_MODE=true         # true = логирует сделки, НЕ исполняет
```

> **Совет:** Начинай с `PREVIEW_MODE=true` и наблюдай за логами 1–2 дня.

### Telegram уведомления (опционально)

```env
TELEGRAM_BOT_TOKEN=123456:ABCxyz    # Получить у @BotFather
TELEGRAM_CHAT_ID=987654321          # Получить через @userinfobot
```

---

## Отбор трейдеров

### Автоматический скрининг

```bash
npx tsx src/scripts/screen-traders.ts --pages 4 --top 15
```

Скрипт анализирует лидерборд Polymarket и фильтрует по: ROI, win rate, активность, drawdown, кол-во resolved позиций.

### Бэктест

Проверить, как бот работал бы с выбранными трейдерами на исторических данных:

```bash
npx tsx src/scripts/backtest-traders.ts --days 30 --addresses 0xabc,0xdef
```

### Отчёт по результатам

```bash
npx tsx src/scripts/performance-report.ts
```

---

## Запуск

### Через Docker (рекомендуется)

```bash
docker compose up -d --build
docker compose logs -f bot          # Следить за логами
docker compose down                 # Остановить
```

### Локально

```bash
npm install
npm run health-check                # Проверить подключение к API
npm run dev                         # Запуск с auto-reload (для разработки)
npm run build && npm start          # Production запуск
```

---

## Данные и логи

Бот сохраняет данные в `data/`:

| Файл | Что хранит |
|---|---|
| `seen-trades.json` | ID уже обработанных сделок (дедупликация) |
| `trade-history.jsonl` | История всех сделок (JSONL) |
| `inventory.json` | Текущие открытые позиции |
| `risk-state.json` | Дневной объём и лимиты по рынкам |
| `bot.lock` | PID-файл (защита от двойного запуска) |

Логи в `logs/bot-YYYY-MM-DD.log` — ротация по дням.

---

## Экстренные действия

**Продать все позиции:**
```bash
npx tsx src/scripts/sell-all.ts
```

**Автоматический выкуп (auto-redeemer):**
Бот каждые 30 минут проверяет закрытые рынки и автоматически выкупает выигравшие позиции.

---

## Безопасность

- Приватный ключ очищается из `process.env` после старта (остаётся только в памяти процесса)
- Бот не запускает web-сервер, все соединения исходящие
- При старте автоматически проверяет/устанавливает approvals для USDC.e и Conditional Tokens
- API ключи CLOB деривируются автоматически из кошелька, ручная настройка не нужна

---

## Нерешённые вопросы

- Достаточно ли $2 MATIC для газа на длительный период? (зависит от частоты сделок, при активной торговле может понадобиться больше)
- Минимальный депозит USDC.e для стратегии FIXED $1 = от $50 рекомендуется
