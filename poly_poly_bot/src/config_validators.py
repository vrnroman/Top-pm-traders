"""Pure validation functions for configuration values."""

import json
import re


def parse_addresses(raw: str) -> list[str]:
    """Parse comma-separated or JSON array of Ethereum addresses."""
    trimmed = raw.strip()
    if trimmed.startswith("["):
        parsed = json.loads(trimmed)
        if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
            raise ValueError("USER_ADDRESSES JSON must be an array of strings")
        return parsed
    return [a.strip() for a in trimmed.split(",") if a.strip()]


def validate_private_key(key: str) -> str:
    """Validate and normalize private key (strip 0x, ensure 64 hex chars)."""
    clean = key[2:] if key.startswith("0x") else key
    if not re.match(r'^[0-9a-fA-F]{64}$', clean):
        raise ValueError("PRIVATE_KEY must be 64 hex characters (without 0x prefix)")
    return clean


def validate_address(addr: str, name: str) -> str:
    """Validate Ethereum address format."""
    if not re.match(r'^0x[0-9a-fA-F]{40}$', addr):
        raise ValueError(f"{name} must be a valid Ethereum address (0x + 40 hex chars)")
    return addr
