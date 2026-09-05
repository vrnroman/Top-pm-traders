"""Polygon contract addresses and ABI fragments used by Polymarket.

**The addresses come from the CLOB client library, not from this file.**

Found 2026-09-05, and it would have stopped the first live session dead:
Polymarket migrated its collateral from bridged USDC.e
(`0x2791Bca1...`) to **pUSD** (`0xC011a7E1...`), and its exchanges to the v2
contracts. This file still hardcoded the old USDC.e address, so every
on-chain read the bot made about "our money" asked the wrong token and got
zero, on a funder wallet that really held 80.41 pUSD. Zero balance means the
bankroll governor computes an effective budget of zero, which refuses every
live copy: the bot would have armed, seen a funded wallet, and traded
nothing.

Order placement was never affected, because the order builder reads
`py_clob_client_v2.config.get_contract_config`. So the fix is not to paste
newer addresses here, which is how this file went stale in the first place:
it is to read the SAME source the order path already trusts, and let this
module be a thin alias over it. A hardcoded copy is a second way to know
one fact, and the second way is the one that rots.
"""

from py_clob_client_v2.config import get_contract_config as _clob_config

# Polygon PoS. The chain the bot trades and the only one this file describes.
CHAIN_ID = 137
_CFG = _clob_config(CHAIN_ID)

# The collateral Polymarket settles in TODAY (pUSD as of 2026-09-05). Every
# balance read, approval and redemption must use this, never a literal.
USDC_ADDRESS = _CFG.collateral

# The exchanges that actually match orders now. `exchange_v2` /
# `neg_risk_exchange_v2` are what the CLOB reports allowances for; the v1
# addresses are kept under their own names for anything reading history.
CTF_EXCHANGE = _CFG.exchange_v2
NEG_RISK_CTF_EXCHANGE = _CFG.neg_risk_exchange_v2
CTF_EXCHANGE_V1 = _CFG.exchange
NEG_RISK_CTF_EXCHANGE_V1 = _CFG.neg_risk_exchange
NEG_RISK_ADAPTER = _CFG.neg_risk_adapter
CTF_CONTRACT = _CFG.conditional_tokens

# ABI fragments for web3.py
ERC20_BALANCE_ABI = [{"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"}]

ERC20_APPROVE_ABI = [
    {"inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
]

ERC1155_APPROVAL_ABI = [
    {"inputs": [{"name": "account", "type": "address"}, {"name": "operator", "type": "address"}], "name": "isApprovedForAll", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "operator", "type": "address"}, {"name": "approved", "type": "bool"}], "name": "setApprovalForAll", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]

CTF_REDEEM_ABI = [
    {"inputs": [{"name": "collateralToken", "type": "address"}, {"name": "parentCollectionId", "type": "bytes32"}, {"name": "conditionId", "type": "bytes32"}, {"name": "indexSets", "type": "uint256[]"}], "name": "redeemPositions", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]

ORDER_FILLED_ABI = [
    {"anonymous": False, "inputs": [
        {"indexed": False, "name": "orderHash", "type": "bytes32"},
        {"indexed": True, "name": "maker", "type": "address"},
        {"indexed": True, "name": "taker", "type": "address"},
        {"indexed": False, "name": "makerAssetId", "type": "uint256"},
        {"indexed": False, "name": "takerAssetId", "type": "uint256"},
        {"indexed": False, "name": "makerAmountFilled", "type": "uint256"},
        {"indexed": False, "name": "takerAmountFilled", "type": "uint256"},
    ], "name": "OrderFilled", "type": "event"},
]

# Trading constants
FILL_CHECK_DELAY_S = 3.0
FILL_CHECK_RETRIES = 2
EXECUTION_LOOP_S = 0.1  # 100ms
POLYGON_CHAIN_ID = 137
