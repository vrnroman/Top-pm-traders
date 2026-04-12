"""Data API polling source -- wraps fetchAllTraderActivities with circuit breaker."""

import asyncio
from src.copy_trading.trade_monitor import fetch_all_trader_activities
from src.copy_trading.trade_store import is_seen_trade, is_max_retries
from src.copy_trading.trade_queue import enqueue_trade
from src.config import CONFIG
from src.logger import logger
from src.models import QueuedTrade
from src.utils import error_message


class DataApiSource:
    """Polls the Data API for trader activity with exponential backoff and circuit breaker."""

    name = "data-api"

    def __init__(self) -> None:
        self._running = False

    async def start(self) -> None:
        """Start polling loop with circuit breaker protection."""
        self._running = True
        circuit_breaker_threshold = 10
        consecutive_failures = 0
        circuit_breaker_alerted = False

        while self._running:
            try:
                trades = await fetch_all_trader_activities()
                new_trades = [
                    t for t in trades
                    if not is_seen_trade(t.id) and not is_max_retries(t.id)
                ]
                for trade in new_trades:
                    from datetime import datetime
                    ts_ms = (
                        datetime.fromisoformat(
                            trade.timestamp.replace("Z", "+00:00")
                        ).timestamp()
                        * 1000
                    )
                    enqueue_trade(QueuedTrade(
                        trade=trade,
                        enqueued_at=ts_ms,
                        source_detected_at=ts_ms,
                        source="data-api",
                    ))
                consecutive_failures = 0
                circuit_breaker_alerted = False
            except Exception as err:
                consecutive_failures += 1
                logger.error(
                    f"Data API error ({consecutive_failures}x): {error_message(err)}"
                )
                if (
                    consecutive_failures >= circuit_breaker_threshold
                    and not circuit_breaker_alerted
                ):
                    circuit_breaker_alerted = True
                    from src.copy_trading.telegram_notifier import telegram
                    await telegram.bot_error(
                        f"Data API circuit breaker: {consecutive_failures} failures. "
                        f"Last: {error_message(err)}"
                    )

            backoff = (
                min(
                    CONFIG.fetch_interval * (2 ** min(consecutive_failures, 6)),
                    300,
                )
                if consecutive_failures > 0
                else CONFIG.fetch_interval
            )
            await asyncio.sleep(backoff)

    def stop(self) -> None:
        """Signal the polling loop to stop."""
        self._running = False
