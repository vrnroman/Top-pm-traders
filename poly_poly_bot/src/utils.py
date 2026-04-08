"""Shared utility functions."""

import asyncio
from datetime import datetime, timezone


async def async_sleep(seconds: float) -> None:
    """Async sleep wrapper."""
    await asyncio.sleep(seconds)


def short_address(addr: str) -> str:
    """Abbreviate address to 0x1234...5678."""
    return f"{addr[:6]}...{addr[-4:]}"


def round_cents(n: float) -> float:
    """Round to 2 decimal places."""
    return round(n * 100) / 100


def ceil_cents(n: float) -> float:
    """Ceil to 2 decimal places."""
    import math
    return math.ceil(n * 100) / 100


def today_utc() -> str:
    """Return today's date as YYYY-MM-DD in UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def error_message(err: BaseException | object) -> str:
    """Extract a human-readable message from an exception or unknown object."""
    if isinstance(err, Exception):
        return str(err)
    return str(err)
