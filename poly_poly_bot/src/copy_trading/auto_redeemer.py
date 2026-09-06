"""Automatic position redemption for resolved Polymarket markets.

Fetches redeemable positions from the Data API and calls CTF redeemPositions
on-chain. Skips neg-risk positions. Calculates P&L for reporting.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Callable, Optional

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


class RedeemFetchError(RuntimeError):
    """The positions read failed after every retry.

    Raised, not swallowed. An empty list means "nothing to redeem"; a failed
    read means "we do not know", and the live guard's unredeemed trigger
    treats those two differently. Returning [] here made 27 rate-limit
    failures in 20 days read as 27 clean passes.
    """


# The data API answered 429 to the redeemer 27 times in 20 days. Three retries
# with backoff cover a rate-limit window; a longer outage is raised, not hidden.
_FETCH_RETRY_DELAYS_S: tuple[float, ...] = (1.0, 3.0, 9.0)


async def _fetch_redeemable_positions(
    proxy_wallet: str,
    *,
    sleep: Callable = asyncio.sleep,
) -> list[dict]:
    """Fetch positions eligible for redemption from the Data API.

    Returns a list of position dicts with at minimum:
      conditionId, tokenId, size, market/title, avgPrice, resolved, curPrice, negRisk
    Raises ``RedeemFetchError`` when the read fails after retries.
    """
    url = f"{CONFIG.data_api_url}/positions"
    params = {"user": proxy_wallet}

    data = None
    attempts = (*_FETCH_RETRY_DELAYS_S, None)
    for attempt, delay in enumerate(attempts, start=1):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
            break
        except Exception as exc:
            if delay is None:
                logger.error(f"[redeemer] Failed to fetch positions after "
                             f"{attempt} attempts: {error_message(exc)}")
                raise RedeemFetchError(error_message(exc)) from exc
            logger.warn(f"[redeemer] positions fetch failed (attempt {attempt}): "
                        f"{error_message(exc)}; retrying in {delay:.0f}s")
            await sleep(delay)

    if not isinstance(data, list):
        return []
    n_rows = len(data)

    redeemable: list[dict] = []
    unknown_schema = 0
    for entry in data:
        # The row's own word for "this can be redeemed now". The data API
        # carries `redeemable`; this code read `resolved`, a key the row does
        # not have, so `.get("resolved", False)` was False for EVERY position
        # forever. 61 of 61 real rows were redeemable and 0 passed. Because
        # the READ succeeded, the live guard saw a confident zero rather than
        # an error, which left its unredeemed trigger structurally unable to
        # fire and (through the equity number) the bankroll floor with it.
        # A missing key is now counted, not defaulted.
        flag = entry.get("redeemable")
        if flag is None:
            flag = entry.get("resolved")
        if flag is None:
            unknown_schema += 1
            continue
        if not flag:
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

        # `asset` is the token id as a STRING on the live API, not a nested
        # object. This line assumed a dict and raised AttributeError on every
        # real row; it was unreachable until the `redeemable` filter above was
        # fixed, so the crash arrived the moment rows started passing. Accept
        # both shapes and never index into a string.
        raw_asset = entry.get("asset")
        if isinstance(raw_asset, dict):
            token_id = raw_asset.get("id", "")
        else:
            token_id = str(raw_asset or "")
        token_id = token_id or str(entry.get("tokenId") or "")
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
            # What the position is worth NOW. A resolved loser is worth zero,
            # and zero is not stuck capital: the guard uses this to tell "the
            # redeemer is broken" from "these are old worthless tickets".
            "currentValue": float(entry.get("currentValue") or 0.0),
        })

    # Every row missing BOTH keys means the schema moved under us. Returning a
    # confident empty list is what made two safety triggers inert for weeks, so
    # this raises into the same "unknown, not zero" branch a network failure
    # takes.
    if n_rows and unknown_schema == n_rows:
        raise RedeemFetchError(
            f"none of the {n_rows} position row(s) carry a `redeemable` or "
            f"`resolved` field; the data API schema has changed and this list "
            f"cannot be trusted to be empty")
    if unknown_schema:
        logger.warn(f"[redeemer] {unknown_schema} of {n_rows} position row(s) "
                    f"carry neither `redeemable` nor `resolved`; they are excluded")
    return redeemable


# Conditions already reported, so a permanent situation speaks once, not every
# 30 minutes forever.
_warned: set = set()


# A resolved position worth less than this has nothing to collect.
DUST_VALUE_USD = 1.0


def _position_value(p: dict) -> float:
    """What the position is worth now: the API's figure, else shares times
    the current price (the rows a fake or an older schema hands over)."""
    v = float(p.get("currentValue") or 0.0)
    if v > 0:
        return v
    try:
        return float(p.get("shares") or 0.0) * float(p.get("curPrice") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _warn_once(key: str) -> bool:
    if key in _warned:
        return False
    _warned.add(key)
    return True


async def check_and_redeem_positions(private_key: str,
                                     notify=None) -> RedeemResult:
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

    try:
        positions = await _fetch_redeemable_positions(proxy_wallet)
    except RedeemFetchError as exc:
        # Skip THIS pass and say so. The next pass (30 minutes) retries; the
        # guard sees the failed read as unknown, not as "nothing stuck".
        logger.warn(f"[redeemer] skipping this pass, positions unreadable: {exc}")
        return RedeemResult()
    if not positions:
        return RedeemResult()

    w3 = Web3(Web3.HTTPProvider(CONFIG.rpc_url))
    account = w3.eth.account.from_key(f"0x{private_key}")

    # WHO HOLDS THE TOKENS decides who may redeem them. `redeemPositions`
    # redeems for msg.sender, and these positions belong to the proxy while
    # this transaction would be signed by the EOA. The call does NOT revert on
    # a zero balance: it skips the burn, transfers nothing, and returns
    # status 1, which this function's success branch would then book as a
    # WINNING realized-P&L row that never happened, into the same ledger the
    # honest-metrics floor reads. So refuse before sending anything, say it
    # once, and leave the positions counted so the guard's unredeemed trigger
    # can still fire.
    if (CONFIG.proxy_wallet or "").lower() != account.address.lower():
        # Only positions with something to collect are worth a message. The
        # 61 April-era losers on this wallet are worth under $1 each; naming
        # them "worth $837 at cost" on every boot read as a loss six times in
        # one day. Their cost still counts, in the log, once.
        collectable = [p for p in positions if _position_value(p) >= DUST_VALUE_USD]
        if not collectable:
            if _warn_once("proxy-mismatch-dust"):
                logger.info(f"[redeemer] {len(positions)} resolved position(s) sit on "
                            f"the proxy wallet, each worth under ${DUST_VALUE_USD:.0f}: "
                            f"nothing to claim, nothing sent, no P&L recorded.")
            return RedeemResult()
        if _warn_once("proxy-mismatch"):
            value = sum(float(p.get("currentValue") or 0.0) for p in collectable)
            msg = (f"{len(collectable)} position(s) worth ${value:,.2f} are held by "
                   f"the proxy wallet, but this bot signs as a different address, "
                   f"so it cannot redeem them on chain. Nothing was sent and no "
                   f"P&L was recorded. Claim them by hand in the Polymarket "
                   f"interface.")
            logger.error(f"[redeemer] {msg}")
            if notify is not None:
                try:
                    notify("💤 <b>Cannot redeem automatically.</b> " + msg)
                except Exception as exc:
                    logger.warn(f"[redeemer] notify failed: {exc}")
        return RedeemResult()

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

                # P&L calculation: curPrice > 0.5 means the outcome won. A
                # winning binary share redeems for $1, a losing one for $0.
                cost_basis = shares * avg_price
                won = cur_price > 0.5
                returned = shares if won else 0.0

                details.append(RedeemDetail(
                    title=title,
                    shares=shares,
                    cost_basis=cost_basis,
                    returned=returned,
                ))

                # Persist realized P&L so /pnl can report it. This is the only
                # place a copy position is closed, so this ledger is the source
                # of truth for Strategy 1 realized P&L. We read the local
                # inventory position first to attribute the row to its strategy
                # tier and the followed wallet (stamped at buy time).
                try:
                    from src.copy_trading.inventory import get_position
                    inv_pos = get_position(pos.get("tokenId", "")) or {}
                except Exception:
                    inv_pos = {}
                try:
                    from src.copy_trading.pnl import append_realized
                    append_realized({
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "title": title,
                        "condition_id": condition_id,
                        "token_id": pos.get("tokenId", ""),
                        "shares": round(shares, 6),
                        "avg_price": round(avg_price, 6),
                        "cost_basis": round(cost_basis, 6),
                        "returned": round(returned, 6),
                        "pnl": round(returned - cost_basis, 6),
                        "won": won,
                        "tier": inv_pos.get("tier", ""),
                        "trader_address": inv_pos.get("trader_address", ""),
                        "exit": "resolution",
                        # Provenance: the preview resolver writes the same
                        # shape into the same file. The daily real-money
                        # line must never count paper as realized.
                        "source": "redeemer",
                    })
                except Exception as led_err:
                    logger.warn(f"[redeemer] Failed to record realized P&L: {error_message(led_err)}")

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
