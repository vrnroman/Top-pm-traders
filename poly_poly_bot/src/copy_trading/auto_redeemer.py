"""Automatic position redemption for resolved Polymarket markets.

Fetches redeemable positions from the Data API and calls CTF redeemPositions
on-chain. Skips neg-risk positions. Calculates P&L for reporting.
"""

from __future__ import annotations

from typing import Optional

import httpx
from web3 import Web3

from src.config import CONFIG
from src.constants import (
    CTF_CONTRACT,
    CTF_REDEEM_ABI,
    USDC_ADDRESS,
)
from src.logger import logger
from src.models import RedeemDetail, RedeemResult
from src.utils import error_message


# Bytes32 zero — parent collection ID for top-level positions
_ZERO_BYTES32 = b"\x00" * 32


def _build_index_sets(outcome_count: int) -> list[int]:
    """Build index sets for redemption (one bit per outcome)."""
    return [1 << i for i in range(outcome_count)]


async def _fetch_redeemable_positions(proxy_wallet: str) -> list[dict]:
    """Fetch positions eligible for redemption from the Data API.

    Returns a list of position dicts with at minimum:
      conditionId, tokenId, size, market/title, avgPrice, resolved, curPrice, negRisk
    """
    url = f"{CONFIG.data_api_url}/positions"
    params = {"user": proxy_wallet}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.error(f"[redeemer] Failed to fetch positions: {error_message(exc)}")
        return []

    if not isinstance(data, list):
        return []

    redeemable: list[dict] = []
    for entry in data:
        # Only resolved markets are redeemable
        resolved = entry.get("resolved", False)
        if not resolved:
            continue

        # Extract fields — handle nested market objects
        market_obj = entry.get("market", {})
        if isinstance(market_obj, dict):
            condition_id = market_obj.get("conditionId", "") or entry.get("conditionId", "")
            title = market_obj.get("question", "") or entry.get("title", "")
            neg_risk = market_obj.get("negRisk", False) or entry.get("negRisk", False)
            outcome_count = int(market_obj.get("outcomeCount", 2))
        else:
            condition_id = entry.get("conditionId", "")
            title = entry.get("title", "") or entry.get("market", "")
            neg_risk = entry.get("negRisk", False)
            outcome_count = int(entry.get("outcomeCount", 2))

        if not condition_id:
            continue

        token_id = entry.get("asset", {}).get("id", "") or entry.get("tokenId", "")
        shares = float(entry.get("size", 0) or entry.get("shares", 0))
        avg_price = float(entry.get("avgPrice", 0) or entry.get("avg_price", 0))
        cur_price = float(entry.get("curPrice", 0) or entry.get("price", 0))

        if shares <= 0:
            continue

        redeemable.append({
            "conditionId": condition_id,
            "tokenId": token_id,
            "shares": shares,
            "avgPrice": avg_price,
            "curPrice": cur_price,
            "title": title,
            "negRisk": neg_risk,
            "outcomeCount": outcome_count,
        })

    return redeemable


async def check_and_redeem_positions(private_key: str) -> RedeemResult:
    """Check for resolved positions and redeem them on-chain.

    Args:
        private_key: Hex private key (without 0x prefix).

    Returns:
        RedeemResult with count, market names, total shares, and per-position details.
    """
    proxy_wallet = CONFIG.proxy_wallet
    if not proxy_wallet:
        logger.warn("[redeemer] No proxy wallet configured, skipping redemption")
        return RedeemResult()

    positions = await _fetch_redeemable_positions(proxy_wallet)
    if not positions:
        return RedeemResult()

    w3 = Web3(Web3.HTTPProvider(CONFIG.rpc_url))
    account = w3.eth.account.from_key(f"0x{private_key}")
    ctf = w3.eth.contract(
        address=Web3.to_checksum_address(CTF_CONTRACT),
        abi=CTF_REDEEM_ABI,
    )

    # Gas overrides
    fee_history = w3.eth.fee_history(1, "latest")
    base_fee = fee_history["baseFeePerGas"][-1]
    max_fee = base_fee * 2
    max_priority_fee = Web3.to_wei(50, "gwei")

    redeemed_count = 0
    redeemed_markets: list[str] = []
    total_shares = 0.0
    details: list[RedeemDetail] = []

    for pos in positions:
        # Skip neg-risk positions — they use a different redemption mechanism
        if pos.get("negRisk", False):
            logger.info(f"[redeemer] Skipping neg-risk position: {pos['title'][:60]}")
            continue

        condition_id = pos["conditionId"]
        shares = pos["shares"]
        avg_price = pos["avgPrice"]
        cur_price = pos["curPrice"]
        title = pos["title"]
        outcome_count = pos.get("outcomeCount", 2)

        index_sets = _build_index_sets(outcome_count)

        try:
            logger.info(f"[redeemer] Redeeming {shares:.2f} shares of '{title[:60]}'...")

            tx = ctf.functions.redeemPositions(
                Web3.to_checksum_address(USDC_ADDRESS),
                _ZERO_BYTES32,
                Web3.to_bytes(hexstr=condition_id),
                index_sets,
            ).build_transaction({
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": max_priority_fee,
            })

            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash)

            if receipt["status"] == 1:
                redeemed_count += 1
                redeemed_markets.append(title)
                total_shares += shares

                # P&L calculation: curPrice > 0.5 means the outcome won
                cost_basis = shares * avg_price
                returned = shares if cur_price > 0.5 else 0.0

                details.append(RedeemDetail(
                    title=title,
                    shares=shares,
                    cost_basis=cost_basis,
                    returned=returned,
                ))

                # Update local inventory
                try:
                    from src.copy_trading.inventory import record_sell
                    record_sell(pos["tokenId"], shares)
                except Exception as inv_err:
                    logger.warn(f"[redeemer] Failed to update inventory: {error_message(inv_err)}")

                logger.info(f"[redeemer] Redeemed '{title[:60]}'. TX: {tx_hash.hex()}")
            else:
                logger.warn(f"[redeemer] Redemption tx reverted for '{title[:60]}'. TX: {tx_hash.hex()}")

        except Exception as exc:
            logger.error(f"[redeemer] Failed to redeem '{title[:60]}': {error_message(exc)}")
            continue

    return RedeemResult(
        count=redeemed_count,
        markets=redeemed_markets,
        total_shares=total_shares,
        details=details,
    )
