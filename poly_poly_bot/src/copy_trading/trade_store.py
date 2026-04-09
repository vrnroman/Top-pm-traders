"""Seen-trade tracking, retry counts, per-trader copy limits, and trade history.

Persists:
  - data/seen-trades.json    (set of trade IDs, max 10K)
  - data/trader-counts.json  (per-trader copy counts, max 20K with eviction)
  - data/trade-history.jsonl (append-only audit trail)
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Optional

from src.config import CONFIG
from src.logger import logger
from src.models import TradeRecord


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_SEEN_TRADES = 10_000
_MAX_RETRIES = 3
_MAX_RETRY_MAP = 1_000
_MAX_TRADER_COUNTS = 20_000
_LATENCY_WINDOW = 50  # rolling window for avg reaction latency


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _atomic_write_json(path: str, data: object) -> None:
    """Write JSON atomically: write to tmp file then rename."""
    dir_path = os.path.dirname(path)
    os.makedirs(dir_path, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Seen trades (max 10K)
# ---------------------------------------------------------------------------

_SEEN_FILE = os.path.join(CONFIG.data_dir, "seen-trades.json")
_seen_trades: set[str] = set()


def _load_seen_trades() -> None:
    global _seen_trades
    try:
        with open(_SEEN_FILE, "r") as f:
            raw = json.load(f)
        if isinstance(raw, list):
            _seen_trades = set(raw[-_MAX_SEEN_TRADES:])
        else:
            _seen_trades = set()
    except (FileNotFoundError, json.JSONDecodeError):
        _seen_trades = set()


def _save_seen_trades() -> None:
    # Evict oldest entries by converting to list and trimming
    items = list(_seen_trades)
    if len(items) > _MAX_SEEN_TRADES:
        items = items[-_MAX_SEEN_TRADES:]
        _seen_trades.clear()
        _seen_trades.update(items)
    _atomic_write_json(_SEEN_FILE, items)


_load_seen_trades()


def is_seen_trade(trade_id: str) -> bool:
    """Check if a trade has already been processed."""
    return trade_id in _seen_trades


def mark_trade_as_seen(trade_id: str) -> None:
    """Mark a trade as processed."""
    _seen_trades.add(trade_id)
    # Evict if over limit
    if len(_seen_trades) > _MAX_SEEN_TRADES:
        excess = len(_seen_trades) - _MAX_SEEN_TRADES
        items = list(_seen_trades)
        for i in range(excess):
            _seen_trades.discard(items[i])
    _save_seen_trades()


# ---------------------------------------------------------------------------
# Retry counts (in-memory, max 3 retries, 1K cap with eviction)
# ---------------------------------------------------------------------------

_retry_counts: OrderedDict[str, int] = OrderedDict()


def increment_retry(trade_id: str) -> int:
    """Increment retry count for a trade. Returns new count."""
    count = _retry_counts.get(trade_id, 0) + 1
    _retry_counts[trade_id] = count
    # Move to end (most recent)
    _retry_counts.move_to_end(trade_id)
    # Evict oldest if over cap
    while len(_retry_counts) > _MAX_RETRY_MAP:
        _retry_counts.popitem(last=False)
    return count


def is_max_retries(trade_id: str) -> bool:
    """Check if a trade has exceeded max retry attempts."""
    return _retry_counts.get(trade_id, 0) >= _MAX_RETRIES


# ---------------------------------------------------------------------------
# Per-trader copy counts (data/trader-counts.json, max 20K with eviction)
# ---------------------------------------------------------------------------

_TRADER_COUNTS_FILE = os.path.join(CONFIG.data_dir, "trader-counts.json")
_trader_counts: OrderedDict[str, int] = OrderedDict()


def _load_trader_counts() -> None:
    global _trader_counts
    try:
        with open(_TRADER_COUNTS_FILE, "r") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            _trader_counts = OrderedDict(raw)
        else:
            _trader_counts = OrderedDict()
    except (FileNotFoundError, json.JSONDecodeError):
        _trader_counts = OrderedDict()


def _save_trader_counts() -> None:
    # Evict oldest if over limit
    while len(_trader_counts) > _MAX_TRADER_COUNTS:
        _trader_counts.popitem(last=False)
    _atomic_write_json(_TRADER_COUNTS_FILE, dict(_trader_counts))


_load_trader_counts()


def get_copy_count(trader_address: str) -> int:
    """Get the number of times we have copied this trader."""
    return _trader_counts.get(trader_address.lower(), 0)


def increment_copy_count(trader_address: str) -> int:
    """Increment copy count for a trader. Returns new count."""
    key = trader_address.lower()
    count = _trader_counts.get(key, 0) + 1
    _trader_counts[key] = count
    _trader_counts.move_to_end(key)
    _save_trader_counts()
    return count


# ---------------------------------------------------------------------------
# Trade history (append-only JSONL)
# ---------------------------------------------------------------------------

_HISTORY_FILE = os.path.join(CONFIG.data_dir, "trade-history.jsonl")


def append_trade_history(record: TradeRecord) -> None:
    """Append a trade record to the JSONL history file."""
    os.makedirs(os.path.dirname(_HISTORY_FILE), exist_ok=True)
    line = record.model_dump_json() + "\n"
    try:
        with open(_HISTORY_FILE, "a") as f:
            f.write(line)
    except Exception as e:
        logger.error(f"[trade-store] Failed to append trade history: {e}")


# ---------------------------------------------------------------------------
# Reaction latency tracking (rolling window, max 50 samples)
# ---------------------------------------------------------------------------

_latency_samples: list[float] = []


def record_reaction_latency(latency_ms: float) -> None:
    """Record a reaction latency sample (ms from detection to order submission)."""
    _latency_samples.append(latency_ms)
    if len(_latency_samples) > _LATENCY_WINDOW:
        _latency_samples.pop(0)


def get_avg_reaction_latency() -> Optional[float]:
    """Get average reaction latency in ms over the rolling window.

    Returns None if no samples recorded.
    """
    if not _latency_samples:
        return None
    return sum(_latency_samples) / len(_latency_samples)
