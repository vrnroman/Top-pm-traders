"""Token approval management for Polymarket exchanges."""

from web3 import Web3
from src.config import CONFIG
from src.logger import logger
from src.constants import (
    USDC_ADDRESS, CTF_EXCHANGE, NEG_RISK_CTF_EXCHANGE, CTF_CONTRACT,
    ERC20_APPROVE_ABI, ERC1155_APPROVAL_ABI,
)


def check_and_set_approvals(private_key: str) -> None:
    """Check and set token approvals for both exchanges.

    For each exchange (CTF_EXCHANGE and NEG_RISK_CTF_EXCHANGE):
      1. Check USDC (ERC20) allowance — approve unlimited if < 1M USDC.
      2. Check Conditional Tokens (ERC1155) approval — setApprovalForAll if not approved.

    Gas strategy: 2x current baseFee for maxFeePerGas, 50 gwei maxPriorityFeePerGas.
    """
    w3 = Web3(Web3.HTTPProvider(CONFIG.rpc_url))
    account = w3.eth.account.from_key(f"0x{private_key}")
    address = Web3.to_checksum_address(CONFIG.proxy_wallet)

    # Gas overrides
    fee_history = w3.eth.fee_history(1, "latest")
    base_fee = fee_history["baseFeePerGas"][-1]
    max_fee = base_fee * 2
    max_priority_fee = Web3.to_wei(50, "gwei")

    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_APPROVE_ABI)
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_CONTRACT), abi=ERC1155_APPROVAL_ABI)
    max_uint256 = 2**256 - 1
    threshold = 10**6 * 10**6  # 1M USDC in raw units (6 decimals)

    for exchange_name, exchange_addr in [("CTF Exchange", CTF_EXCHANGE), ("Neg Risk Exchange", NEG_RISK_CTF_EXCHANGE)]:
        exchange = Web3.to_checksum_address(exchange_addr)

        # Check USDC allowance
        allowance = usdc.functions.allowance(address, exchange).call()
        if allowance < threshold:
            logger.info(f"Setting USDC approval for {exchange_name}...")
            tx = usdc.functions.approve(exchange, max_uint256).build_transaction({
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": max_priority_fee,
            })
            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            w3.eth.wait_for_transaction_receipt(tx_hash)
            logger.info(f"USDC approved for {exchange_name}. TX: {tx_hash.hex()}")
        else:
            logger.info(f"USDC approval for {exchange_name}: OK")

        # Check ERC1155 approval
        is_approved = ctf.functions.isApprovedForAll(address, exchange).call()
        if not is_approved:
            logger.info(f"Setting Conditional Tokens approval for {exchange_name}...")
            tx = ctf.functions.setApprovalForAll(exchange, True).build_transaction({
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": max_priority_fee,
            })
            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            w3.eth.wait_for_transaction_receipt(tx_hash)
            logger.info(f"Conditional Tokens approved for {exchange_name}. TX: {tx_hash.hex()}")
        else:
            logger.info(f"Conditional Tokens approval for {exchange_name}: OK")
