"""Auto-redeemer realized-P&L persistence.

The redeemer is the only place a copy position is closed, so it must persist
the realized P&L (previously it computed P&L only for a one-off Telegram
notification and discarded it, leaving /pnl with no realized data to report).
We mock web3 + the positions fetch so the test runs without a chain or network.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def redeem_env(monkeypatch, tmp_path):
    from src.config import CONFIG
    monkeypatch.setattr(CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(CONFIG, "proxy_wallet", "0xproxy")
    return tmp_path


def _mock_web3():
    """A Web3 stand-in whose call chain yields a successful redemption.

    The mocked signer address is the SAME address as the configured proxy:
    `redeemPositions` redeems for msg.sender, so redemption is only possible
    at all when the signer is the holder, and the redeemer now refuses
    outright when it is not (run s-yr3unh).
    """
    w3 = MagicMock()
    w3.eth.fee_history.return_value = {"baseFeePerGas": [100]}
    w3.eth.get_transaction_count.return_value = 0
    w3.eth.send_raw_transaction.return_value = MagicMock()
    w3.eth.wait_for_transaction_receipt.return_value = {"status": 1}
    w3.eth.account.from_key.return_value.address = "0xproxy"

    Web3 = MagicMock()
    Web3.return_value = w3
    Web3.to_checksum_address.side_effect = lambda a: a
    Web3.to_bytes.return_value = b""
    Web3.to_wei.return_value = 1
    return Web3


def test_successful_redemption_persists_realized_pnl(redeem_env):
    from src.copy_trading import auto_redeemer
    from src.copy_trading import pnl as s1pnl

    winning_position = {
        "conditionId": "0xcond",
        "tokenId": "tok-1",
        "shares": 100.0,
        "avgPrice": 0.40,
        "curPrice": 1.0,   # > 0.5 -> won
        "title": "Will A happen?",
        "negRisk": False,
        "outcomeCount": 2,
    }

    with patch.object(auto_redeemer, "Web3", _mock_web3()), \
         patch.object(auto_redeemer, "_fetch_redeemable_positions",
                      AsyncMock(return_value=[winning_position])):
        result = _run(auto_redeemer.check_and_redeem_positions("aa" * 32))

    assert result.count == 1

    rows = s1pnl.load_realized()
    assert len(rows) == 1
    row = rows[0]
    assert row["condition_id"] == "0xcond"
    assert row["cost_basis"] == pytest.approx(40.0)
    assert row["returned"] == pytest.approx(100.0)
    assert row["pnl"] == pytest.approx(60.0)
    assert row["won"] is True


def test_losing_redemption_records_negative_pnl(redeem_env):
    from src.copy_trading import auto_redeemer
    from src.copy_trading import pnl as s1pnl

    losing_position = {
        "conditionId": "0xcond2",
        "tokenId": "tok-2",
        "shares": 50.0,
        "avgPrice": 0.60,
        "curPrice": 0.0,   # < 0.5 -> lost
        "title": "Will B happen?",
        "negRisk": False,
        "outcomeCount": 2,
    }

    with patch.object(auto_redeemer, "Web3", _mock_web3()), \
         patch.object(auto_redeemer, "_fetch_redeemable_positions",
                      AsyncMock(return_value=[losing_position])):
        result = _run(auto_redeemer.check_and_redeem_positions("aa" * 32))

    assert result.count == 1
    rows = s1pnl.load_realized()
    assert len(rows) == 1
    assert rows[0]["pnl"] == pytest.approx(-30.0)
    assert rows[0]["won"] is False


def test_negrisk_position_skipped_and_not_recorded(redeem_env):
    from src.copy_trading import auto_redeemer
    from src.copy_trading import pnl as s1pnl

    neg_risk = {
        "conditionId": "0xcond3",
        "tokenId": "tok-3",
        "shares": 10.0,
        "avgPrice": 0.5,
        "curPrice": 1.0,
        "title": "Neg risk market",
        "negRisk": True,
        "outcomeCount": 2,
    }

    with patch.object(auto_redeemer, "Web3", _mock_web3()), \
         patch.object(auto_redeemer, "_fetch_redeemable_positions",
                      AsyncMock(return_value=[neg_risk])):
        result = _run(auto_redeemer.check_and_redeem_positions("aa" * 32))

    assert result.count == 0
    assert s1pnl.load_realized() == []
