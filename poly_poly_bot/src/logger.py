"""Structured logging with two on-disk files and colored console output.

Log files
---------
  signals-{date}.log   Trade decisions, pattern alerts, watchlist alerts,
                        execution events, warnings, errors. Low volume —
                        kept forever.

  bot-{date}.log        Everything else (polling, queue depths, geo-scan,
                        scheduler heartbeats). Higher volume — auto-deleted
                        after BOT_LOG_RETENTION_DAYS (default 2).
"""

import glob
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

# Message prefixes that belong in the signals log. Everything matching
# these goes to signals-{date}.log; the rest goes to bot-{date}.log.
_SIGNAL_PREFIXES = (
    "[pattern]",
    "[watchlist]",
    "[exec]",
    "[verify]",
    "[recovery]",
)


class _SignalFilter(logging.Filter):
    """Pass only signal/deal-related records to the signals file."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        if record.levelno in (TRADE, SKIP):
            return True
        msg = record.getMessage()
        return any(msg.startswith(p) for p in _SIGNAL_PREFIXES)


class _OperationalFilter(logging.Filter):
    """Pass only non-signal records to the operational (debug) file."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno in (TRADE, SKIP):
            return False
        if record.levelno >= logging.WARNING:
            return False
        msg = record.getMessage()
        return not any(msg.startswith(p) for p in _SIGNAL_PREFIXES)


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


def _purge_old_bot_logs(logs_dir: Path, retention_days: int) -> None:
    """Delete bot-*.log files older than `retention_days`."""
    import time
    cutoff = time.time() - retention_days * 86400
    for path in logs_dir.glob("bot-*.log"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


class BotLogger:
    """Application logger with trade/skip custom levels and split log files."""

    def __init__(self) -> None:
        self._logger = logging.getLogger("poly_poly_bot")
        self._logger.setLevel(logging.DEBUG)
        self._logger.handlers.clear()

        for noisy in ("httpcore", "httpx", "httpcore.connection", "httpcore.http11"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        log_format = os.environ.get("LOG_FORMAT", "text")
        logs_dir = Path(os.environ.get("LOGS_DIR", "logs"))
        logs_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        formatter: logging.Formatter
        if log_format == "json":
            formatter = JsonFormatter()
        else:
            formatter = PlainFormatter()

        # -- Console: everything (DEBUG+) --
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.DEBUG)
        if log_format == "json":
            console.setFormatter(JsonFormatter())
        else:
            console.setFormatter(ColorFormatter())
        self._logger.addHandler(console)

        # -- Signals file: deal/alert events only, kept forever --
        signals_file = logs_dir / f"signals-{date_str}.log"
        sig_handler = logging.FileHandler(signals_file, mode="a", encoding="utf-8")
        sig_handler.setLevel(logging.DEBUG)
        sig_handler.setFormatter(formatter)
        sig_handler.addFilter(_SignalFilter())
        self._logger.addHandler(sig_handler)

        # -- Operational file: everything else, auto-purged --
        bot_file = logs_dir / f"bot-{date_str}.log"
        ops_handler = logging.FileHandler(bot_file, mode="a", encoding="utf-8")
        ops_handler.setLevel(logging.INFO)
        ops_handler.setFormatter(formatter)
        ops_handler.addFilter(_OperationalFilter())
        self._logger.addHandler(ops_handler)

        # Purge old operational logs on startup
        retention = int(os.environ.get("BOT_LOG_RETENTION_DAYS", "2"))
        _purge_old_bot_logs(logs_dir, retention)

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
