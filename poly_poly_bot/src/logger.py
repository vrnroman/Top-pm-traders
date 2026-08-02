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
import re
import sys
import time
from datetime import datetime, timedelta, timezone
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


# Credentials that must never reach a log file, scrubbed from every record
# before any handler writes it. The 2026-07-16→25 incident: urllib3 DEBUG was
# on for a week and every Telegram long-poll request line carried the live bot
# token (".../bot<digits>:<secret>/getUpdates") into bot-*.log — ~55k copies.
_TELEGRAM_TOKEN_RE = re.compile(r"bot\d{6,12}:[A-Za-z0-9_-]{30,}")


def scrub_secrets(text: str) -> str:
    """Replace credential-shaped substrings with a redaction marker."""
    return _TELEGRAM_TOKEN_RE.sub("bot***REDACTED", text)


class SecretScrubFilter(logging.Filter):
    """Rewrite credentials out of a record before handlers format it.

    Attach to HANDLERS, not loggers: logger filters never see propagated
    records, so a third-party library (urllib3) logging through the root
    handlers would bypass a logger-level scrub. Renders the message once and
    pins the scrubbed form (``args=()``) so downstream formatters can't
    re-interpolate the original arguments.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # a broken format must not silence the record
            return True
        scrubbed = scrub_secrets(msg)
        if scrubbed != msg:
            record.msg = scrubbed
            record.args = ()
        return True


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
        return scrub_secrets(
            f"{gray}{ts}{reset} {color}{level}{reset} {record.getMessage()}")


class PlainFormatter(logging.Formatter):
    """Plain text formatter for log files."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        level = record.levelname.ljust(5)
        return scrub_secrets(f"{ts} {level} {record.getMessage()}")


class JsonFormatter(logging.Formatter):
    """JSON structured formatter for ops monitoring."""

    def format(self, record: logging.LogRecord) -> str:
        import json
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "msg": record.getMessage(),
        }
        return scrub_secrets(json.dumps(entry))


class SecretScrubFormatter(logging.Formatter):
    """A stdlib Formatter that scrubs credentials from the COMPLETE rendered
    record — message AND traceback.

    ``record.exc_info`` is rendered by the Formatter *after* filters run, so a
    filter-level scrub can never see it: a ``logger.exception`` around a failed
    Telegram-API call renders the token URL in the traceback tail (the
    command-dispatch ``logger.exception`` wraps ``_send`` → ``requests.post(
    .../bot<TOKEN>/...)`` — manager DONE-gate r4). This class closes every
    traceback path in one point; the filter stays as the early first layer.
    """

    def format(self, record: logging.LogRecord) -> str:
        return scrub_secrets(super().format(record))


_BOT_LOG_RE = re.compile(r"bot-(\d{4}-\d{2}-\d{2})\.log$")


def _purge_old_bot_logs(logs_dir: Path, retention_days: int,
                        today: "datetime.date | None" = None) -> None:
    """Delete ``bot-<date>.log`` files whose embedded UTC date is older than
    ``retention_days``, keeping the ``retention_days`` most-recent days.

    Purges by the **filename date**, not mtime: a long-running process appends
    to today's file for hours, so its mtime is always fresh and an mtime cutoff
    would (a) never delete the still-open file yet (b) spare genuinely old files
    the moment the process touched them. Keying on the date in the name makes
    the active file (always today's) unpurgeable and prunes strictly by age.
    Only ``bot-*.log`` is touched; ``signals-*.log`` is kept forever.
    """
    today = today or datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=retention_days)
    mtime_cutoff = time.time() - retention_days * 86400
    for path in logs_dir.glob("bot-*.log"):
        m = _BOT_LOG_RE.search(path.name)
        try:
            if m:
                file_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
                stale = file_date <= cutoff
            else:
                # Odd names (bot-old.log, a truncated/legacy name) can't be dated
                # from the filename — fall back to mtime so they still get reclaimed
                # rather than accumulating forever (the pre-refactor behaviour).
                stale = path.stat().st_mtime < mtime_cutoff
        except (ValueError, OSError):
            continue
        if stale:
            try:
                path.unlink()
            except OSError:
                pass


class _DailyRotatingFileHandler(logging.FileHandler):
    """A ``FileHandler`` that writes to ``<prefix>-<UTC-date>.log`` and rolls to
    a new dated file when the UTC date changes.

    The original handler baked the date into the filename **once** at process
    start, so a container running across a UTC midnight kept appending to the
    start-day's file (a week of logs landing in ``bot-2026-06-29.log``). This
    checks the date on every emit and re-points at the new day's file when it
    turns over — preserving the ``bot-{date}.log`` / ``signals-{date}.log``
    naming that the log-analyzer tooling globs on. ``on_rollover`` fires after a
    successful roll (used to purge stale bot logs). ``clock`` is injectable for
    tests; it defaults to wall-clock UTC.
    """

    def __init__(self, logs_dir: Path, prefix: str, *, clock=None,
                 on_rollover=None, **kwargs) -> None:
        self._logs_dir = Path(logs_dir)
        self._prefix = prefix
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._on_rollover = on_rollover
        self._cur_date = self._clock().strftime("%Y-%m-%d")
        super().__init__(self._path(self._cur_date), mode="a",
                         encoding="utf-8", **kwargs)

    def _path(self, date_str: str) -> Path:
        return self._logs_dir / f"{self._prefix}-{date_str}.log"

    def _maybe_roll(self) -> None:
        today = self._clock().strftime("%Y-%m-%d")
        if today == self._cur_date:
            return
        self.acquire()
        try:
            self._cur_date = today
            if self.stream:
                self.stream.close()
                self.stream = None
            self.baseFilename = str(self._path(today).absolute())
            self.stream = self._open()
        finally:
            self.release()
        if self._on_rollover is not None:
            try:
                self._on_rollover(self._logs_dir)
            except Exception:  # a purge failure must never break logging
                pass

    def emit(self, record: logging.LogRecord) -> None:
        self._maybe_roll()
        super().emit(record)


class BotLogger:
    """Application logger with trade/skip custom levels and split log files."""

    def __init__(self) -> None:
        self._logger = logging.getLogger("poly_poly_bot")
        self._logger.setLevel(logging.DEBUG)
        self._logger.handlers.clear()

        # Third-party chatter suppression: the named loggers are pinned to
        # WARNING so their DEBUG/INFO records never reach our handlers. urllib3
        # is the big one — the Telegram long-poll logs every connection at DEBUG,
        # which was 93.5% of total log volume (68,889 of 73,666 lines on
        # 2026-07-24) and drowned real signal (~4,500 lines/day).
        for noisy in ("httpcore", "httpx", "httpcore.connection", "httpcore.http11",
                      "urllib3", "urllib3.connectionpool", "web3", "asyncio", "hpack"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        log_format = os.environ.get("LOG_FORMAT", "text")
        logs_dir = Path(os.environ.get("LOGS_DIR", "logs"))
        logs_dir.mkdir(parents=True, exist_ok=True)
        retention = int(os.environ.get("BOT_LOG_RETENTION_DAYS", "2"))

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
        console.addFilter(SecretScrubFilter())
        self._logger.addHandler(console)

        # -- Signals file: deal/alert events only, kept forever --
        # Rolls at UTC midnight so a long-running process doesn't append a week
        # of events into the start-day's file. Never purged.
        sig_handler = _DailyRotatingFileHandler(logs_dir, "signals")
        sig_handler.setLevel(logging.DEBUG)
        sig_handler.setFormatter(formatter)
        sig_handler.addFilter(_SignalFilter())
        sig_handler.addFilter(SecretScrubFilter())
        self._logger.addHandler(sig_handler)

        # -- Operational file: everything else, auto-purged --
        # Rolls at UTC midnight and purges stale bot logs on each rollover (not
        # just at startup), so a container that runs for days still prunes.
        ops_handler = _DailyRotatingFileHandler(
            logs_dir, "bot",
            on_rollover=lambda d: _purge_old_bot_logs(d, retention),
        )
        ops_handler.setLevel(logging.INFO)
        ops_handler.setFormatter(formatter)
        ops_handler.addFilter(_OperationalFilter())
        ops_handler.addFilter(SecretScrubFilter())
        self._logger.addHandler(ops_handler)

        # Purge old operational logs on startup, then on every midnight rollover.
        _purge_old_bot_logs(logs_dir, retention)

    def debug(self, msg: str) -> None:
        self._logger.debug(msg)

    def info(self, msg: str) -> None:
        self._logger.info(msg)

    def warn(self, msg: str) -> None:
        self._logger.warning(msg)

    # Stdlib logging.Logger spelling. Alias kept so call sites that reach
    # for the standard name don't blow up with AttributeError (the exact
    # bug class that took the bot down on 2026-05-10).
    def warning(self, msg: str) -> None:
        self._logger.warning(msg)

    def error(self, msg: str) -> None:
        self._logger.error(msg)

    def critical(self, msg: str) -> None:
        self._logger.critical(msg)

    def exception(self, msg: str) -> None:
        """Log an error with the current exception's traceback appended.

        Mirrors ``logging.Logger.exception`` so call sites inside ``except``
        blocks (``logger.exception("X failed")``) get the traceback for
        free, the same way a stdlib logger would.
        """
        self._logger.exception(msg)

    def log(self, level: int, msg: str) -> None:
        self._logger.log(level, msg)

    def trade(self, msg: str) -> None:
        self._logger.log(TRADE, msg)

    def skip(self, msg: str) -> None:
        self._logger.log(SKIP, msg)

    def flush(self) -> None:
        """Flush and close file handlers."""
        for handler in self._logger.handlers:
            handler.flush()


logger = BotLogger()
