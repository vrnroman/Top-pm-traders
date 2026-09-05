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

_MAX_SEEN_TRADES = 50_000
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
# OrderedDict so eviction is true LRU. A plain set's `list(...)` order is hash
# order, which means eviction was effectively random — old IDs (including for
# wallets where the cursor is stuck on a single old trade) could be evicted
# while newer ones survived, causing the same old trade to be re-emitted on
# every fetch and re-alerted by downstream consumers.
_seen_trades: "OrderedDict[str, None]" = OrderedDict()


def _load_seen_trades() -> None:
    global _seen_trades
    try:
        with open(_SEEN_FILE, "r") as f:
            raw = json.load(f)
        if isinstance(raw, list):
            _seen_trades = OrderedDict((tid, None) for tid in raw[-_MAX_SEEN_TRADES:])
        else:
            _seen_trades = OrderedDict()
    except (FileNotFoundError, json.JSONDecodeError):
        _seen_trades = OrderedDict()


def _save_seen_trades() -> None:
    while len(_seen_trades) > _MAX_SEEN_TRADES:
        _seen_trades.popitem(last=False)
    _atomic_write_json(_SEEN_FILE, list(_seen_trades.keys()))


_load_seen_trades()


def is_seen_trade(trade_id: str) -> bool:
    """Check if a trade has already been processed."""
    return trade_id in _seen_trades


def mark_trade_as_seen(trade_id: str) -> None:
    """Mark a trade as processed."""
    if trade_id in _seen_trades:
        _seen_trades.move_to_end(trade_id)
    else:
        _seen_trades[trade_id] = None
    while len(_seen_trades) > _MAX_SEEN_TRADES:
        _seen_trades.popitem(last=False)
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


# Alias kept for callers that import the older name. Same function.
record_trade_history = append_trade_history


def get_duplicate_count(market_key: str, side: str) -> int:
    """Count how many trades we've already recorded for ``market_key`` on ``side``.

    Used by the executor to enforce ``max_copies_per_market_side``: if we've
    already copied this market+side N times, the next attempt is skipped.
    Reads the trade-history JSONL on demand; cheap enough for the modest
    history sizes the bot accumulates between restarts.
    """
    if not market_key or not os.path.exists(_HISTORY_FILE):
        return 0
    count = 0
    try:
        with open(_HISTORY_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("side") != side:
                    continue
                # Match either ``market`` (display string used as the key
                # by the executor) or ``condition_id`` for resilience.
                if rec.get("market") == market_key or rec.get("condition_id") == market_key:
                    count += 1
    except Exception as e:
        logger.warn(f"[trade-store] get_duplicate_count read failed: {e}")
        return 0
    return count


# ---------------------------------------------------------------------------
# Reaction latency tracking (rolling window, max 50 samples)
# ---------------------------------------------------------------------------

_latency_samples: list[float] = []


# ---------------------------------------------------------------------------
# Live poller liveness: the guard's stale-feed input
# ---------------------------------------------------------------------------

_last_poll_ok_ts: Optional[float] = None


def record_poll_ok(ts: Optional[float] = None) -> None:
    """The live source completed a poll and the watched wallets answered.

    A statement about OUR pipeline, never about the market: a quiet wallet
    still answers a poll, an outage does not. The guard's stale-feed trigger
    used to read the newest shadow-quote row instead, so every quiet hour
    (nothing to detect) looked like a dead feed and it flapped 255 times in
    20 days, messaging the owner each time, about nothing.
    """
    global _last_poll_ok_ts
    _last_poll_ok_ts = time.time() if ts is None else float(ts)


def last_poll_ok_ts() -> Optional[float]:
    """When the live poller last succeeded, or None before the first poll."""
    return _last_poll_ok_ts


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
