"""Trade source factory -- data-api, onchain, or hybrid mode."""

from __future__ import annotations
from typing import Protocol


class TradeSource(Protocol):
    """Protocol for pluggable trade detection sources."""

    name: str

    async def start(self) -> None: ...
    def stop(self) -> None: ...


def create_sources(mode: str) -> list[TradeSource]:
    """Create trade source(s) based on the configured mode.

    Args:
        mode: One of 'data-api', 'onchain', or 'hybrid'.

    Returns:
        List of trade sources to run concurrently.
    """
    from src.copy_trading.data_api_source import DataApiSource
    from src.copy_trading.onchain_source import OnchainSource

    if mode == "onchain":
        return [OnchainSource()]
    elif mode == "hybrid":
        return [OnchainSource(), DataApiSource()]
    else:
        return [DataApiSource()]
