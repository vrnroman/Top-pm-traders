"""Singleton CLOB client factory."""

from __future__ import annotations
from typing import Optional
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds
from src.config import CONFIG, get_private_key
from src.logger import logger

_client: Optional[ClobClient] = None


def create_clob_client() -> ClobClient:
    """Create and cache a singleton authenticated CLOB client."""
    global _client
    if _client is not None:
        return _client

    private_key = get_private_key()
    logger.info("Deriving API credentials from wallet...")

    # Initial client for key derivation
    l1_client = ClobClient(
        CONFIG.clob_api_url,
        chain_id=CONFIG.chain_id,
        key=f"0x{private_key}",
        signature_type=CONFIG.signature_type,
        funder=CONFIG.proxy_wallet,
    )

    creds = l1_client.create_or_derive_api_creds()
    api_key = creds.get("apiKey") or creds.get("key", "")
    api_secret = creds["secret"]
    api_passphrase = creds["passphrase"]

    _client = ClobClient(
        CONFIG.clob_api_url,
        chain_id=CONFIG.chain_id,
        key=f"0x{private_key}",
        creds=ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        ),
        signature_type=CONFIG.signature_type,
        funder=CONFIG.proxy_wallet,
    )

    logger.info("CLOB client authenticated successfully")
    return _client
