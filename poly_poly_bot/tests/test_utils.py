"""Tests for shared utility functions."""

import math
from datetime import datetime, timezone

from src.utils import short_address, round_cents, ceil_cents, today_utc, error_message


class TestShortAddress:
    def test_normal_address(self):
        addr = "0x1234567890abcdef1234567890abcdef12345678"
        assert short_address(addr) == "0x1234...5678"

    def test_short_string(self):
        # Should still work even if address is short (no crash)
        result = short_address("0xABCD")
        assert "..." in result


class TestRoundCents:
    def test_rounds_down(self):
        assert round_cents(1.234) == 1.23

    def test_rounds_up(self):
        assert round_cents(1.235) == 1.24

    def test_exact(self):
        assert round_cents(1.50) == 1.50

    def test_zero(self):
        assert round_cents(0.0) == 0.0

    def test_negative(self):
        assert round_cents(-1.236) == -1.24


class TestCeilCents:
    def test_rounds_up(self):
        assert ceil_cents(1.231) == 1.24

    def test_exact_stays(self):
        assert ceil_cents(1.23) == 1.23

    def test_zero(self):
        assert ceil_cents(0.0) == 0.0

    def test_small_fraction(self):
        assert ceil_cents(0.001) == 0.01


class TestTodayUtc:
    def test_returns_date_string(self):
        result = today_utc()
        # Format YYYY-MM-DD
        assert len(result) == 10
        assert result[4] == "-"
        assert result[7] == "-"

    def test_matches_utc_date(self):
        expected = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert today_utc() == expected


class TestErrorMessage:
    def test_exception(self):
        exc = ValueError("something went wrong")
        assert error_message(exc) == "something went wrong"

    def test_runtime_error(self):
        exc = RuntimeError("timeout")
        assert error_message(exc) == "timeout"

    def test_non_exception_object(self):
        assert error_message(42) == "42"

    def test_string(self):
        assert error_message("raw error") == "raw error"

    def test_none(self):
        assert error_message(None) == "None"
