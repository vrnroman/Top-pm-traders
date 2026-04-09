"""Tests for order fill verification."""

import asyncio
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from src.copy_trading.order_verifier import _parse_fill_from_order, verify_order_fill


class TestParseFillFromOrder:
    def test_filled_when_size_matched_gte_original(self):
        order = {"original_size": "100", "size_matched": "100", "average_price": "0.50"}
        result = _parse_fill_from_order(order)
        assert result.status == "FILLED"
        assert result.filled_shares == 100.0
        assert result.fill_price == 0.50
        assert result.filled_usd == 50.0

    def test_partial_when_partially_filled(self):
        order = {"original_size": "100", "size_matched": "40", "average_price": "0.60"}
        result = _parse_fill_from_order(order)
        assert result.status == "PARTIAL"
        assert result.filled_shares == 40.0

    def test_unfilled_when_zero_matched(self):
        order = {"original_size": "100", "size_matched": "0", "average_price": "0.50"}
        result = _parse_fill_from_order(order)
        assert result.status == "UNFILLED"
        assert result.filled_shares == 0.0

    def test_camelcase_keys(self):
        order = {"originalSize": "200", "sizeMatched": "200", "averagePrice": "0.70"}
        result = _parse_fill_from_order(order)
        assert result.status == "FILLED"
        assert result.filled_shares == 200.0

    def test_missing_keys_gives_unfilled(self):
        order = {}
        result = _parse_fill_from_order(order)
        assert result.status == "UNFILLED"
        assert result.filled_shares == 0.0


class TestVerifyOrderFill:
    @pytest.mark.asyncio
    @patch("src.copy_trading.order_verifier.FILL_CHECK_DELAY_S", 0.0)
    @patch("src.copy_trading.order_verifier.FILL_CHECK_RETRIES", 2)
    async def test_filled_on_first_try(self):
        mock_client = MagicMock()
        mock_client.get_order.return_value = {
            "original_size": "50",
            "size_matched": "50",
            "average_price": "0.45",
        }
        result = await verify_order_fill(mock_client, "order-123")
        assert result.status == "FILLED"
        assert mock_client.get_order.call_count == 1

    @pytest.mark.asyncio
    @patch("src.copy_trading.order_verifier.FILL_CHECK_DELAY_S", 0.0)
    @patch("src.copy_trading.order_verifier.FILL_CHECK_RETRIES", 3)
    async def test_unfilled_after_all_retries(self):
        mock_client = MagicMock()
        mock_client.get_order.return_value = {
            "original_size": "50",
            "size_matched": "0",
            "average_price": "0.45",
        }
        result = await verify_order_fill(mock_client, "order-456")
        assert result.status == "UNFILLED"
        assert mock_client.get_order.call_count == 3

    @pytest.mark.asyncio
    @patch("src.copy_trading.order_verifier.FILL_CHECK_DELAY_S", 0.0)
    @patch("src.copy_trading.order_verifier.FILL_CHECK_RETRIES", 3)
    async def test_unknown_on_api_error(self):
        mock_client = MagicMock()
        mock_client.get_order.side_effect = Exception("Connection refused")
        result = await verify_order_fill(mock_client, "order-789")
        assert result.status == "UNKNOWN"

    @pytest.mark.asyncio
    @patch("src.copy_trading.order_verifier.FILL_CHECK_DELAY_S", 0.0)
    @patch("src.copy_trading.order_verifier.FILL_CHECK_RETRIES", 2)
    async def test_partial_fill(self):
        mock_client = MagicMock()
        mock_client.get_order.return_value = {
            "original_size": "100",
            "size_matched": "30",
            "average_price": "0.55",
        }
        result = await verify_order_fill(mock_client, "order-partial")
        assert result.status == "PARTIAL"
        assert result.filled_shares == 30.0

    @pytest.mark.asyncio
    async def test_empty_order_id_returns_unknown(self):
        mock_client = MagicMock()
        result = await verify_order_fill(mock_client, "")
        assert result.status == "UNKNOWN"
