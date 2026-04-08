"""Trade queues for copy-trading pipeline.

Two queues:
  1. pendingTrades: detection -> execution (in-memory only)
  2. pendingOrders: execution -> verification (in-memory + disk persistence)

Disk persistence for pending orders ensures recovery after restart.
File: data/pending-orders.json with atomic writes.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Optional

from src.config import CONFIG
from src.logger import logger
from src.models import PendingOrder, QueuedTrade


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
# Pending trades queue (detection -> execution, in-memory only)
# ---------------------------------------------------------------------------

_pending_trades: list[QueuedTrade] = []


def enqueue_trade(trade: QueuedTrade) -> None:
    """Add a detected trade to the execution queue."""
    _pending_trades.append(trade)
    logger.debug(
        f"[queue] Enqueued trade {trade.trade.id[:12]}... | "
        f"queue depth: {len(_pending_trades)}"
    )


def drain_trades() -> list[QueuedTrade]:
    """Drain all pending trades from the queue. Returns the drained list."""
    drained = _pending_trades[:]
    _pending_trades.clear()
    if drained:
        logger.debug(f"[queue] Drained {len(drained)} trades")
    return drained


# ---------------------------------------------------------------------------
# Pending orders queue (execution -> verification, memory + disk)
# ---------------------------------------------------------------------------

_ORDERS_FILE = os.path.join(CONFIG.data_dir, "pending-orders.json")
_pending_orders: list[PendingOrder] = []


def _save_pending_orders() -> None:
    """Persist pending orders to disk."""
    data = [order.model_dump() for order in _pending_orders]
    _atomic_write_json(_ORDERS_FILE, data)


def load_pending_orders_from_disk() -> int:
    """Load pending orders from disk on startup. Returns count loaded."""
    global _pending_orders
    try:
        with open(_ORDERS_FILE, "r") as f:
            raw = json.load(f)
        if isinstance(raw, list):
            _pending_orders = [PendingOrder(**entry) for entry in raw]
            logger.info(f"[queue] Loaded {len(_pending_orders)} pending orders from disk")
            return len(_pending_orders)
        return 0
    except (FileNotFoundError, json.JSONDecodeError):
        return 0
    except Exception as e:
        logger.error(f"[queue] Failed to load pending orders: {e}")
        return 0


def clear_pending_orders_on_disk() -> None:
    """Clear the pending orders file on disk."""
    try:
        if os.path.exists(_ORDERS_FILE):
            os.unlink(_ORDERS_FILE)
    except OSError as e:
        logger.error(f"[queue] Failed to clear pending orders file: {e}")


def enqueue_pending_order(order: PendingOrder) -> None:
    """Add a placed order to the verification queue. Persists to disk."""
    _pending_orders.append(order)
    _save_pending_orders()
    logger.debug(
        f"[queue] Enqueued pending order {order.order_id[:12]}... | "
        f"queue depth: {len(_pending_orders)}"
    )


def peek_pending_orders() -> list[PendingOrder]:
    """Get a copy of all pending orders (does not modify the queue)."""
    return _pending_orders[:]


def remove_pending_order(order_id: str) -> Optional[PendingOrder]:
    """Remove a pending order by order_id. Persists to disk. Returns removed order or None."""
    global _pending_orders
    for i, order in enumerate(_pending_orders):
        if order.order_id == order_id:
            removed = _pending_orders.pop(i)
            _save_pending_orders()
            logger.debug(f"[queue] Removed pending order {order_id[:12]}...")
            return removed
    return None


def update_pending_order(order_id: str, **kwargs: object) -> bool:
    """Update fields on a pending order by order_id. Persists to disk.

    Args:
        order_id: The order to update.
        **kwargs: Fields to update (must be valid PendingOrder fields).

    Returns:
        True if order was found and updated, False otherwise.
    """
    for order in _pending_orders:
        if order.order_id == order_id:
            for key, value in kwargs.items():
                if hasattr(order, key):
                    setattr(order, key, value)
            _save_pending_orders()
            return True
    return False


def replace_pending_orders(orders: list[PendingOrder]) -> None:
    """Replace the entire pending orders list. Persists to disk.

    Used for bulk updates (e.g. removing multiple filled orders at once).
    """
    global _pending_orders
    _pending_orders = orders[:]
    _save_pending_orders()
    logger.debug(f"[queue] Replaced pending orders list ({len(_pending_orders)} orders)")


def get_pending_order_count() -> int:
    """Get the number of pending orders."""
    return len(_pending_orders)
