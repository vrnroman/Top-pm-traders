"""Structured logging with file rotation and colored console output."""

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Custom log levels
TRADE = 25  # Between INFO and WARNING
SKIP = 23

logging.addLevelName(TRADE, "TRADE")
logging.addLevelName(SKIP, "SKIP")

COLORS = {
    "DEBUG": "\033[90m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "TRADE": "\033[36m",
    "SKIP": "\033[35m",
    "RESET": "\033[0m",
    "GRAY": "\033[90m",
}


class ColorFormatter(logging.Formatter):
    """Human-readable colored log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        level = record.levelname.ljust(5)
        color = COLORS.get(record.levelname, "")
        reset = COLORS["RESET"]
        gray = COLORS["GRAY"]
        return f"{gray}{ts}{reset} {color}{level}{reset} {record.getMessage()}"


class PlainFormatter(logging.Formatter):
    """Plain text formatter for log files."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        level = record.levelname.ljust(5)
        return f"{ts} {level} {record.getMessage()}"


class JsonFormatter(logging.Formatter):
    """JSON structured formatter for ops monitoring."""

    def format(self, record: logging.LogRecord) -> str:
        import json
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "msg": record.getMessage(),
        }
        return json.dumps(entry)


class BotLogger:
    """Application logger with trade/skip custom levels and file rotation."""

    def __init__(self) -> None:
        self._logger = logging.getLogger("poly_poly_bot")
        self._logger.setLevel(logging.DEBUG)
        self._logger.handlers.clear()

        # Silence chatty HTTP libraries — each poll cycle generates dozens of
        # DEBUG lines for TLS handshakes, header exchanges, and connection
        # lifecycle events. At 21 wallets polled every 3s this adds up to
        # multiple GB/day of log noise.
        for noisy in ("httpcore", "httpx", "httpcore.connection", "httpcore.http11"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        log_format = os.environ.get("LOG_FORMAT", "text")

        # Console handler
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.DEBUG)
        if log_format == "json":
            console.setFormatter(JsonFormatter())
        else:
            console.setFormatter(ColorFormatter())
        self._logger.addHandler(console)

        # File handler (daily rotation)
        logs_dir = Path(os.environ.get("LOGS_DIR", "logs"))
        logs_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_file = logs_dir / f"bot-{date_str}.log"
        file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        if log_format == "json":
            file_handler.setFormatter(JsonFormatter())
        else:
            file_handler.setFormatter(PlainFormatter())
        self._logger.addHandler(file_handler)
        self._file_handler = file_handler

    def debug(self, msg: str) -> None:
        self._logger.debug(msg)

    def info(self, msg: str) -> None:
        self._logger.info(msg)

    def warn(self, msg: str) -> None:
        self._logger.warning(msg)

    def error(self, msg: str) -> None:
        self._logger.error(msg)

    def trade(self, msg: str) -> None:
        self._logger.log(TRADE, msg)

    def skip(self, msg: str) -> None:
        self._logger.log(SKIP, msg)

    def flush(self) -> None:
        """Flush and close file handlers."""
        for handler in self._logger.handlers:
            handler.flush()


logger = BotLogger()
