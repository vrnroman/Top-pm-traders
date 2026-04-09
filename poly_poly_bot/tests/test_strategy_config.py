"""Tests for tiered strategy configuration."""

import os
from unittest.mock import patch

import pytest

from src.copy_trading.strategy_config import (
    TierConfig,
    Strategy1cConfig,
    get_tier_config,
    get_wallet_tier,
    TIER_1A,
    TIER_1B,
    TIER_1C,
    TIERED_MODE,
    _wallet_tier_map,
)


class TestTierConfigFromEnv:
    def test_tier_1a_defaults(self):
        """Tier 1a loads with default values when env vars are unset."""
        assert TIER_1A.tier == "1a"
        assert TIER_1A.min_bet >= 0
        assert TIER_1A.max_bet > TIER_1A.min_bet

    def test_tier_1b_defaults(self):
        assert TIER_1B.tier == "1b"

    def test_tier_1c_is_strategy1c_config(self):
        assert isinstance(TIER_1C, Strategy1cConfig)
        assert TIER_1C.tier == "1c"
        assert TIER_1C.new_account_age_days > 0
        assert TIER_1C.dormant_days > 0


class TestCaseInsensitiveWalletLookup:
    def test_lookup_returns_none_for_unknown(self):
        result = get_wallet_tier("0x" + "f" * 40)
        # Unknown wallet should return None
        assert result is None

    def test_wallet_tier_map_is_lowercase(self):
        for key in _wallet_tier_map:
            assert key == key.lower()


class TestTieredModeDetection:
    def test_tiered_mode_is_bool(self):
        assert isinstance(TIERED_MODE, bool)

    def test_get_tier_config_1a(self):
        cfg = get_tier_config("1a")
        assert cfg.tier == "1a"

    def test_get_tier_config_1b(self):
        cfg = get_tier_config("1b")
        assert cfg.tier == "1b"

    def test_get_tier_config_1c(self):
        cfg = get_tier_config("1c")
        assert cfg.tier == "1c"

    def test_get_tier_config_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown tier"):
            get_tier_config("legacy")
