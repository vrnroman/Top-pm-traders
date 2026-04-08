"""USDC balance check with spurious-zero protection."""

from __future__ import annotations

from web3 import Web3

from src.config import CONFIG, get_private_key
from src.constants import ERC20_BALANCE_ABI, USDC_ADDRESS
from src.logger import logger
from src.utils import error_message

USDC_DECIMALS = 6

# Spurious-zero protection: cache last known balance and require
# multiple consecutive zero readings before reporting zero.
_ZERO_CONFIRM_THRESHOLD = 3
_last_known_balance: float | None = None
_consecutive_zeros: int = 0


def _get_wallet_address() -> str:
    """Derive wallet address from private key."""
    pk = get_private_key()
    w3 = Web3()
    account = w3.eth.account.from_key(f"0x{pk}")
    return account.address


def get_usdc_balance() -> float:
    """Read the USDC balance for the bot's proxy wallet (or main wallet).

    Returns:
        Balance in USDC (float). Returns cached value on RPC error.
        Returns -1.0 if no prior balance is available and RPC fails.

    Spurious-zero protection:
        If the RPC returns 0 but we previously had a non-zero balance,
        we require 3 consecutive zero readings before reporting zero.
    """
    global _last_known_balance, _consecutive_zeros

    wallet = CONFIG.proxy_wallet or _get_wallet_address()

    try:
        w3 = Web3(Web3.HTTPProvider(CONFIG.rpc_url))
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_ADDRESS),
            abi=ERC20_BALANCE_ABI,
        )
        raw_balance: int = contract.functions.balanceOf(
            Web3.to_checksum_address(wallet)
        ).call()
        balance = raw_balance / (10 ** USDC_DECIMALS)

    except Exception as exc:
        logger.warn(f"RPC error fetching USDC balance: {error_message(exc)}")
        if _last_known_balance is not None:
            return _last_known_balance
        return -1.0

    # Spurious-zero protection
    if balance == 0.0 and _last_known_balance is not None and _last_known_balance > 0:
        _consecutive_zeros += 1
        if _consecutive_zeros < _ZERO_CONFIRM_THRESHOLD:
            logger.warn(
                f"Spurious zero balance? ({_consecutive_zeros}/{_ZERO_CONFIRM_THRESHOLD}), "
                f"returning cached {_last_known_balance:.2f}"
            )
            return _last_known_balance
        # Confirmed zero after threshold
        logger.warn(f"Balance confirmed zero after {_ZERO_CONFIRM_THRESHOLD} readings")

    if balance > 0:
        _consecutive_zeros = 0

    _last_known_balance = balance
    return balance
