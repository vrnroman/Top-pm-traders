"""Tests for configuration validators."""

import pytest

from src.config_validators import parse_addresses, validate_private_key, validate_address


class TestParseAddresses:
    def test_comma_separated(self):
        result = parse_addresses("0xabc, 0xdef, 0x123")
        assert result == ["0xabc", "0xdef", "0x123"]

    def test_json_array(self):
        result = parse_addresses('["0xabc", "0xdef"]')
        assert result == ["0xabc", "0xdef"]

    def test_single_address(self):
        result = parse_addresses("0xabc")
        assert result == ["0xabc"]

    def test_empty_string(self):
        result = parse_addresses("")
        assert result == []

    def test_whitespace_trimming(self):
        result = parse_addresses("  0xabc ,  0xdef  ")
        assert result == ["0xabc", "0xdef"]

    def test_json_invalid_types_raises(self):
        with pytest.raises(ValueError, match="array of strings"):
            parse_addresses("[1, 2, 3]")

    def test_json_with_whitespace(self):
        result = parse_addresses('  ["0xabc"]  ')
        assert result == ["0xabc"]


class TestValidatePrivateKey:
    def test_valid_64_hex(self):
        key = "a" * 64
        assert validate_private_key(key) == key

    def test_strips_0x_prefix(self):
        key = "0x" + "b" * 64
        assert validate_private_key(key) == "b" * 64

    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="64 hex characters"):
            validate_private_key("abcd")

    def test_non_hex_raises(self):
        with pytest.raises(ValueError, match="64 hex characters"):
            validate_private_key("g" * 64)

    def test_mixed_case_valid(self):
        key = "aAbBcCdDeEfF" * 5 + "aAbB"
        assert len(key) == 64
        assert validate_private_key(key) == key


class TestValidateAddress:
    def test_valid_address(self):
        addr = "0x" + "a" * 40
        assert validate_address(addr, "TEST") == addr

    def test_invalid_no_prefix(self):
        with pytest.raises(ValueError, match="valid Ethereum address"):
            validate_address("a" * 40, "TEST")

    def test_invalid_too_short(self):
        with pytest.raises(ValueError, match="valid Ethereum address"):
            validate_address("0x" + "a" * 10, "TEST")

    def test_invalid_non_hex(self):
        with pytest.raises(ValueError, match="valid Ethereum address"):
            validate_address("0x" + "g" * 40, "TEST")

    def test_error_includes_field_name(self):
        with pytest.raises(ValueError, match="MY_FIELD"):
            validate_address("invalid", "MY_FIELD")
