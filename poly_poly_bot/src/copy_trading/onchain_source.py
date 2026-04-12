"""On-chain trade source — polls OrderFilled events via web3.py."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Optional

from web3 import Web3
from web3.contract import Contract

from src.config import CONFIG
from src.constants import (
    CTF_EXCHANGE,
    NEG_RISK_CTF_EXCHANGE,
    ORDER_FILLED_ABI,
    USDC_ADDRESS,
)
from src.logger import logger
from src.models import DetectedTrade
from src.utils import error_message, short_address

MAX_BLOCK_RANGE = 9
MAX_BLOCKS_BEHIND = 10000
USDC_DECIMALS = 6
POLL_INTERVAL_S = 2.0

_CURSOR_PATH = Path(CONFIG.data_dir) / "onchain-cursor.json"


# ---------------------------------------------------------------------------
# Cursor persistence
# ---------------------------------------------------------------------------

def _load_cursor() -> int:
    """Load last-processed block from disk, or 0 if absent."""
    try:
        if _CURSOR_PATH.exists():
            data = json.loads(_CURSOR_PATH.read_text())
            return int(data.get("lastBlock", 0))
    except Exception:
        pass
    return 0


def _save_cursor(block: int) -> None:
    """Persist the last-processed block number."""
    try:
        _CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CURSOR_PATH.write_text(json.dumps({"lastBlock": block}))
    except Exception as exc:
        logger.warn(f"Failed to save onchain cursor: {error_message(exc)}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _canonical_trade_id(tx_hash: str, token_id: str, side: str) -> str:
    return f"{tx_hash}-{token_id}-{side}"


def _determine_side(maker_asset_id: int, taker_asset_id: int) -> str:
    """Determine BUY/SELL from maker/taker asset IDs.

    If the maker asset is USDC (id == 0), the taker is buying outcome tokens → BUY.
    Otherwise the maker is selling outcome tokens → SELL.
    """
    if maker_asset_id == 0:
        return "BUY"
    return "SELL"


def _usdc_to_float(amount: int) -> float:
    """Convert raw USDC amount (6 decimals) to float."""
    return amount / (10 ** USDC_DECIMALS)


class OnchainSource:
    """Polls OrderFilled events from Polymarket exchange contracts."""

    name = "onchain"

    def __init__(self) -> None:
        self._running = False
        self._w3: Optional[Web3] = None
        self._ctf_contract: Optional[Contract] = None
        self._neg_risk_contract: Optional[Contract] = None
        self._block_ts_cache: dict[int, int] = {}
        self._tracked_addresses: set[str] = set()

    def _init_web3(self) -> None:
        """Initialize web3 provider and contract objects."""
        self._w3 = Web3(Web3.HTTPProvider(CONFIG.rpc_url))
        self._ctf_contract = self._w3.eth.contract(
            address=Web3.to_checksum_address(CTF_EXCHANGE),
            abi=ORDER_FILLED_ABI,
        )
        self._neg_risk_contract = self._w3.eth.contract(
            address=Web3.to_checksum_address(NEG_RISK_CTF_EXCHANGE),
            abi=ORDER_FILLED_ABI,
        )
        # Build tracked address set (lowercase)
        for addr in CONFIG.user_addresses:
            self._tracked_addresses.add(addr.lower())

    def _get_block_timestamp(self, block_number: int) -> int:
        """Fetch block timestamp with caching."""
        if block_number in self._block_ts_cache:
            return self._block_ts_cache[block_number]
        assert self._w3 is not None
        try:
            block = self._w3.eth.get_block(block_number)
            ts = int(block["timestamp"])
            self._block_ts_cache[block_number] = ts
            # Prune cache if too large
            if len(self._block_ts_cache) > 500:
                oldest = sorted(self._block_ts_cache.keys())[:250]
                for k in oldest:
                    del self._block_ts_cache[k]
            return ts
        except Exception:
            return int(time.time())

    def _process_events(
        self,
        events: list,
        contract_name: str,
    ) -> list[DetectedTrade]:
        """Process OrderFilled events and return matching DetectedTrade objects."""
        trades: list[DetectedTrade] = []

        for event in events:
            args = event["args"]
            maker = args["maker"].lower()
            taker = args["taker"].lower()

            # Check if either maker or taker is a tracked address
            trader_address: Optional[str] = None
            if maker in self._tracked_addresses:
                trader_address = maker
            elif taker in self._tracked_addresses:
                trader_address = taker
            else:
                continue

            maker_asset_id = int(args["makerAssetId"])
            taker_asset_id = int(args["takerAssetId"])
            maker_amount = int(args["makerAmountFilled"])
            taker_amount = int(args["takerAmountFilled"])

            side = _determine_side(maker_asset_id, taker_asset_id)

            # Token ID is the non-USDC asset ID
            token_id = str(taker_asset_id) if side == "BUY" else str(maker_asset_id)

            # USDC size
            usdc_amount = maker_amount if side == "BUY" else taker_amount
            size = _usdc_to_float(usdc_amount)
            if size <= 0:
                continue

            # Price: USDC / outcome tokens
            outcome_amount = taker_amount if side == "BUY" else maker_amount
            price = _usdc_to_float(usdc_amount) / (_usdc_to_float(outcome_amount) or 1.0)

            tx_hash = event["transactionHash"].hex()
            block_number = event["blockNumber"]
            block_ts = self._get_block_timestamp(block_number)

            from datetime import datetime, timezone
            timestamp = datetime.fromtimestamp(block_ts, tz=timezone.utc).isoformat()

            trade_id = _canonical_trade_id(tx_hash, token_id, side)

            # Enrich with market metadata
            market = ""
            condition_id = ""
            outcome = ""
            try:
                from src.copy_trading.market_cache import get_market_meta
                meta = get_market_meta(token_id)
                if meta is not None:
                    market = meta.market
                    condition_id = meta.condition_id
                    outcome = meta.outcome
            except Exception:
                pass

            trades.append(DetectedTrade(
                id=trade_id,
                trader_address=trader_address,
                timestamp=timestamp,
                market=market,
                condition_id=condition_id,
                token_id=token_id,
                side=side,  # type: ignore[arg-type]
                size=size,
                price=round(price, 4),
                outcome=outcome,
            ))

        return trades

    def _fetch_events_range(
        self,
        from_block: int,
        to_block: int,
    ) -> list[DetectedTrade]:
        """Fetch OrderFilled events for a block range from both exchanges."""
        assert self._ctf_contract is not None
        assert self._neg_risk_contract is not None

        all_trades: list[DetectedTrade] = []

        for contract, name in [
            (self._ctf_contract, "CTF"),
            (self._neg_risk_contract, "NEG_RISK_CTF"),
        ]:
            try:
                event_filter = contract.events.OrderFilled.create_filter(
                    fromBlock=from_block,
                    toBlock=to_block,
                )
                events = event_filter.get_all_entries()
                trades = self._process_events(events, name)
                all_trades.extend(trades)
            except Exception as exc:
                logger.error(f"Error fetching {name} events [{from_block}-{to_block}]: {error_message(exc)}")

        return all_trades

    async def start(self) -> None:
        """Start polling for on-chain OrderFilled events."""
        self._running = True
        self._init_web3()
        assert self._w3 is not None

        cursor = _load_cursor()
        if cursor == 0:
            cursor = self._w3.eth.block_number
            logger.info(f"Onchain source: no cursor, starting at block {cursor}")

        logger.info(f"Onchain source started, cursor at block {cursor}")

        while self._running:
            try:
                latest = self._w3.eth.block_number

                # Skip if cursor is too far behind
                if latest - cursor > MAX_BLOCKS_BEHIND:
                    logger.warn(
                        f"Onchain cursor {cursor} is {latest - cursor} blocks behind, "
                        f"skipping ahead to {latest - 100}"
                    )
                    cursor = latest - 100
                    _save_cursor(cursor)

                if cursor >= latest:
                    await asyncio.sleep(POLL_INTERVAL_S)
                    continue

                # Process in chunks of MAX_BLOCK_RANGE
                from_block = cursor + 1
                to_block = min(from_block + MAX_BLOCK_RANGE - 1, latest)

                trades = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self._fetch_events_range,
                    from_block,
                    to_block,
                )

                if trades:
                    logger.info(f"Onchain: {len(trades)} trades in blocks {from_block}-{to_block}")
                    for trade in trades:
                        try:
                            from src.copy_trading.trade_store import is_seen_trade, is_max_retries
                            from src.copy_trading.trade_queue import enqueue_trade
                            from src.models import QueuedTrade
                            if not is_seen_trade(trade.id) and not is_max_retries(trade.id):
                                from datetime import datetime
                                ts_ms = datetime.fromisoformat(
                                    trade.timestamp.replace("Z", "+00:00")
                                ).timestamp() * 1000
                                enqueue_trade(QueuedTrade(
                                    trade=trade,
                                    enqueued_at=ts_ms,
                                    source_detected_at=ts_ms,
                                    source="onchain",
                                ))
                        except Exception as exc:
                            logger.error(f"Error enqueueing onchain trade: {error_message(exc)}")

                cursor = to_block
                _save_cursor(cursor)

            except Exception as exc:
                logger.error(f"Onchain poll error: {error_message(exc)}")
                await asyncio.sleep(5.0)

            await asyncio.sleep(POLL_INTERVAL_S)

    def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
        logger.info("Onchain source stopped")
