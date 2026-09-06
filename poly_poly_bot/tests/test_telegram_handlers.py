"""Telegram command-dispatch + handler tests.

These cover the safety-critical Telegram surface — the user's only
real-time control over the bot. Each test goes through the public
``_handle_command`` entry point so dispatch (the startswith chain),
argument parsing, the CONFIRM-token gate, and state mutation are all
exercised together.

What we mock:
  - ``send_message``: captured into a list so we can assert what the user
    would have seen.
  - The handler callbacks (``on_refresh_clob_client``): set to MagicMock so
    we can confirm they fire without standing up the real subsystems.

We do NOT mock the modules the handlers operate on (``set_private_key``) —
those are pure in-process state changes and testing the real implementation
is the point.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def captured_messages(monkeypatch):
    """Replace telegram_bot.send_message; return the message buffer."""
    from src import telegram_bot
    buf: list[str] = []
    monkeypatch.setattr(telegram_bot, "send_message", lambda text, **_kw: buf.append(text))
    return buf


@pytest.fixture
def mock_callbacks(monkeypatch):
    """Replace the on_* callbacks so handlers run without real subsystems."""
    from src import telegram_bot
    cbs = MagicMock()
    cbs.on_refresh_clob_client = MagicMock()
    monkeypatch.setattr(telegram_bot, "on_refresh_clob_client", cbs.on_refresh_clob_client)
    return cbs


@pytest.fixture(autouse=True)
def _reset_private_key(monkeypatch):
    """Snapshot/restore the in-memory private key around each test."""
    from src import config
    snap = config._private_key
    yield
    config._private_key = snap


# ------------------------------------------------------------------
# /setkey — second-line kill switch
# ------------------------------------------------------------------

def test_setkey_clear_wipes_key_and_calls_refresh(captured_messages, mock_callbacks):
    from src import config, telegram_bot
    config._private_key = "ab" * 32
    telegram_bot._handle_command("/setkey clear CONFIRM")
    assert config.get_private_key() == ""
    mock_callbacks.on_refresh_clob_client.assert_called_once()


def test_setkey_without_confirm_is_noop(captured_messages, mock_callbacks):
    from src import config, telegram_bot
    config._private_key = "ab" * 32
    telegram_bot._handle_command("/setkey clear")
    assert config.get_private_key() == "ab" * 32  # unchanged
    mock_callbacks.on_refresh_clob_client.assert_not_called()
    assert any("Usage" in m for m in captured_messages)


def test_setkey_invalid_hex_does_not_mutate(captured_messages, mock_callbacks):
    from src import config, telegram_bot
    config._private_key = "ab" * 32
    telegram_bot._handle_command("/setkey nothex CONFIRM")
    assert config.get_private_key() == "ab" * 32  # unchanged
    mock_callbacks.on_refresh_clob_client.assert_not_called()


def test_setkey_replaces_key_with_valid_hex(captured_messages, mock_callbacks):
    from src import config, telegram_bot
    config._private_key = "aa" * 32
    new_key = "bb" * 32
    telegram_bot._handle_command(f"/setkey {new_key} CONFIRM")
    assert config.get_private_key() == new_key
    mock_callbacks.on_refresh_clob_client.assert_called_once()
    # The success message should echo the derived EOA so user can sanity-check.
    assert any("EOA" in m for m in captured_messages)


def test_setkey_with_0x_prefix_normalized(captured_messages, mock_callbacks):
    from src import config, telegram_bot
    new_key = "cc" * 32
    telegram_bot._handle_command(f"/setkey 0x{new_key} CONFIRM")
    # Stored without the 0x prefix.
    assert config.get_private_key() == new_key


# ------------------------------------------------------------------
# /shutdown — last-resort kill switch
# ------------------------------------------------------------------

def test_shutdown_without_confirm_is_noop(captured_messages, mock_callbacks):
    """No CONFIRM token must NOT trigger a process exit."""
    from src import telegram_bot
    with patch("os._exit") as mexit:
        telegram_bot._handle_command("/shutdown")
        # Give the (non-existent) delayed-exit thread a tick to be sure it didn't fire.
        import time
        time.sleep(0.05)
        mexit.assert_not_called()
    assert any("Usage" in m for m in captured_messages)


def test_shutdown_with_confirm_schedules_exit(captured_messages, mock_callbacks):
    from src import telegram_bot
    with patch("os._exit") as mexit, patch("time.sleep"):
        telegram_bot._handle_command("/shutdown CONFIRM")
        # The delayed exit runs in a daemon thread; give it a moment to start.
        import time as _t
        _t.sleep(0.1)
        mexit.assert_called_once_with(0)
    assert any("Shutting down" in m for m in captured_messages)


# ==================================================================
# Full update-path tests (synthesized Telegram getUpdates payloads)
#
# These cover the layers between the Telegram-API and _handle_command:
#   - msg.chat.id extraction + chat-id filter (auth)
#   - msg.text extraction + "/" prefix filter
#   - the try/except wrapper around _handle_command (kill-switch
#     resilience: a handler crash must NOT kill the polling thread)
# A regression in any of these silently breaks the user's only
# real-time control over the bot, so they get their own tests.
# ==================================================================

CHAT_ID = "12345"
WRONG_CHAT_ID = 99999  # tg sends ints; bot stringifies before compare


@pytest.fixture
def configured_chat(monkeypatch):
    from src import telegram_bot
    monkeypatch.setattr(telegram_bot.CONFIG, "telegram_chat_id", CHAT_ID)


def _update(text: str, chat_id=CHAT_ID, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text},
    }


def test_update_from_wrong_chat_id_is_ignored(configured_chat, captured_messages, mock_callbacks):
    """A regression in the chat-id filter is the difference between a
    private bot and a public one. Pin it."""
    from src import telegram_bot
    telegram_bot._process_update(_update("/status", chat_id=WRONG_CHAT_ID))
    # No message sent — the command never reached a handler.
    assert captured_messages == []


def test_update_from_correct_chat_id_dispatches(configured_chat, captured_messages, mock_callbacks):
    from src import telegram_bot
    telegram_bot._process_update(_update("/status"))
    # /status always emits a status message.
    assert captured_messages != []


def test_update_with_non_command_text_ignored(configured_chat, captured_messages, mock_callbacks, monkeypatch):
    from src import telegram_bot
    spy = MagicMock()
    monkeypatch.setattr(telegram_bot, "_handle_command", spy)
    telegram_bot._process_update(_update("hello there, not a command"))
    spy.assert_not_called()


def test_update_with_no_message_object_ignored(configured_chat, captured_messages, mock_callbacks):
    from src import telegram_bot
    # Some update types (edited_message, callback_query) won't have ``message``.
    telegram_bot._process_update({"update_id": 5})
    assert captured_messages == []


def test_handler_exception_does_not_kill_polling(configured_chat, captured_messages, monkeypatch):
    """If a command handler raises, the wrapper must catch it and surface
    an error to the user — not let the exception bubble up and tear down
    the polling thread (which would silently break the kill switch)."""
    from src import telegram_bot

    def boom(_text):
        raise RuntimeError("handler exploded")

    monkeypatch.setattr(telegram_bot, "_handle_command", boom)
    # No exception escapes:
    telegram_bot._process_update(_update("/anything"))
    assert any("Error" in m and "handler exploded" in m for m in captured_messages)


def test_update_with_string_chat_id_also_works(configured_chat, captured_messages, mock_callbacks):
    """Telegram sometimes sends chat.id as int, sometimes as str (bot
    libraries vary). The bot stringifies before compare — pin both."""
    from src import telegram_bot
    telegram_bot._process_update(_update("/status", chat_id=int(CHAT_ID)))
    assert captured_messages != []


# ------------------------------------------------------------------
# End-to-end: real Telegram payload → state mutation
# ------------------------------------------------------------------

def test_e2e_setkey_clear(configured_chat, captured_messages, mock_callbacks):
    """{"text":"/setkey clear CONFIRM"} must wipe the in-memory key."""
    from src import config, telegram_bot
    config._private_key = "ab" * 32
    telegram_bot._process_update(_update("/setkey clear CONFIRM"))
    assert config.get_private_key() == ""
    mock_callbacks.on_refresh_clob_client.assert_called_once()


def test_e2e_setkey_clear_without_confirm_no_op(configured_chat, captured_messages, mock_callbacks):
    """The CONFIRM token must be required even when the payload comes
    through the full update path."""
    from src import config, telegram_bot
    config._private_key = "ab" * 32
    telegram_bot._process_update(_update("/setkey clear"))
    assert config.get_private_key() == "ab" * 32  # unchanged
    mock_callbacks.on_refresh_clob_client.assert_not_called()


# ------------------------------------------------------------------
# /reset — zero all P&L + risk/spend state
# ------------------------------------------------------------------

def test_reset_without_confirm_is_noop(captured_messages, monkeypatch):
    from src import telegram_bot
    called = {"n": 0}
    import src.copy_trading.reset_pnl as rp
    monkeypatch.setattr(rp, "reset_pnl", lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    telegram_bot._handle_command("/reset")
    assert called["n"] == 0
    assert "Usage:" in captured_messages[-1]


def test_reset_confirm_invokes_reset_pnl(captured_messages, monkeypatch):
    from src import telegram_bot
    import src.copy_trading.reset_pnl as rp

    class _Res:
        def summary(self):
            return "archived 3, cleared 3, skipped 6"

    calls = {"n": 0}

    def fake_reset(data_dir, **kw):
        calls["n"] += 1
        assert kw.get("confirm") is True
        return _Res()

    monkeypatch.setattr(rp, "reset_pnl", fake_reset)
    telegram_bot._handle_command("/reset CONFIRM")
    assert calls["n"] == 1
    assert "P&amp;L reset" in captured_messages[-1]


# ------------------------------------------------------------------
# /pnl + /wallets — unified per-strategy / per-wallet P&L
# ------------------------------------------------------------------

def _unified_fixture():
    """Synthetic unified P&L: one 1a executor wallet (realized win + open) and
    two paper wallets on theory 1b (one winner, one loser)."""
    from src.copy_trading import pnl_unified as u
    from src.copy_trading.copy_paper import PaperPosition
    from src.copy_trading.pnl import OpenPositionPnl

    a = u.aggregate_system_a(
        [{"trader_address": "0xaaa111", "tier": "1a", "pnl": 10.0, "cost_basis": 40.0, "won": True}],
        [OpenPositionPnl("tok", "M", 100, 0.5, 0.6, cost=50.0, value=60.0,
                         unrealized_pnl=10.0, unrealized_pct=0.2,
                         tier="1a", trader_address="0xaaa111")],
        tier_of=lambda _w: None,
    )

    def pp(target, pnl, copy_id):
        return PaperPosition(
            copy_id=copy_id, target=target, condition_id="c", token_id="t",
            outcome_index=0, category="x", their_price=0.5, entry_price=0.5,
            shares=100, spent=50.0, drag_bps=0, opened_ts=0.0,
            flagged_by=("1b",), closed=True, won=(pnl > 0), pnl=pnl,
        )

    positions = [pp("0xbbb222", 20.0, "c1"), pp("0xccc333", -15.0, "c2")]
    b = u.aggregate_system_b(positions)
    return u.build_unified(a, b), a, b, 0, {"near": positions, "b": []}


def test_pnl_unified_shows_total_and_per_strategy(captured_messages, monkeypatch):
    from src import telegram_bot

    monkeypatch.setattr(telegram_bot, "_compute_unified", _unified_fixture)
    monkeypatch.setattr(telegram_bot.CONFIG, "preview_mode", True)
    telegram_bot._handle_command("/pnl")
    out = captured_messages[-1]

    assert "<b>TOTAL</b>" in out
    assert "Realized:    <b>$+15.00</b>" in out   # A:10 + B:(20-15)=5
    assert "Unrealized:  <b>$+10.00</b>" in out   # A open mark
    assert "Net:         <b>$+25.00</b>" in out
    assert "A:1a" in out and "B:1b" in out
    assert "PREVIEW MODE" in out


def _unified_with_open_paper():
    """One near-term paper wallet with a marked-to-market open + an unmarked open."""
    from src.copy_trading import pnl_unified as u
    from src.copy_trading.copy_paper import PaperPosition

    def op(copy_id, mark):
        p = PaperPosition(
            copy_id=copy_id, target="0xopen", condition_id="c", token_id="t",
            outcome_index=0, category="x", their_price=0.5, entry_price=0.5,
            shares=100.0, spent=50.0, drag_bps=0, opened_ts=0.0,
            flagged_by=("1b",), closed=False,
        )
        if mark:
            p.mark(0.60)          # +$10 unrealized
        return p

    b = u.aggregate_system_b([op("c1", mark=True), op("c2", mark=False)])
    return u.build_unified([], b), [], b, 0, {"near": [], "b": []}


def test_pnl_footer_discloses_open_paper_exposure_and_unpriced(captured_messages, monkeypatch):
    from src import telegram_bot

    monkeypatch.setattr(telegram_bot, "_compute_unified", _unified_with_open_paper)
    monkeypatch.setattr(telegram_bot.CONFIG, "preview_mode", True)
    telegram_bot._handle_command("/pnl")
    out = captured_messages[-1]

    assert "Paper open:" in out
    assert "$100" in out and "2 position(s)" in out   # 2 opens x $50
    assert "1 unpriced" in out                         # the unmarked one
    assert "Unrealized:  <b>$+10.00</b>" in out        # the marked one contributes


def test_mark_open_paper_marks_only_open_nondust_positions():
    from src import telegram_bot
    from src.copy_trading.copy_paper import PaperPosition

    def pos(closed, entry, shares, their=0.5):
        return PaperPosition(
            copy_id="c", target="0xa", condition_id="c", token_id="tok",
            outcome_index=0, category="x", their_price=their, entry_price=entry,
            shares=shares, spent=50.0, drag_bps=0, opened_ts=0.0,
            flagged_by=("1b",), closed=closed,
        )

    open_pos = pos(closed=False, entry=0.50, shares=100.0)   # $50 -> mid 0.60 = +$10
    closed_pos = pos(closed=True, entry=0.50, shares=100.0)
    dust = pos(closed=False, entry=0.0005, shares=100000.0)  # implausible fill = dust

    fetched = []

    def fake_mid(token_id):
        fetched.append(token_id)
        return 0.60

    telegram_bot._mark_open_paper([open_pos, closed_pos, dust], fake_mid)

    assert open_pos.mark_price == 0.60
    assert open_pos.unrealized_pnl == pytest.approx(10.0)
    assert closed_pos.mark_price == 0.0        # closed never marked
    assert dust.mark_price == 0.0              # dust skipped
    assert fetched == ["tok"]                  # only the one open, non-dust position priced


def test_mark_open_paper_skips_when_no_quote():
    from src import telegram_bot
    from src.copy_trading.copy_paper import PaperPosition

    p = PaperPosition(copy_id="c", target="0xa", condition_id="c", token_id="tok",
                      outcome_index=0, category="x", their_price=0.5, entry_price=0.5,
                      shares=100.0, spent=50.0, drag_bps=0, opened_ts=0.0,
                      flagged_by=("1b",), closed=False)
    telegram_bot._mark_open_paper([p], lambda _t: None)   # dead book
    assert p.mark_price == 0.0                            # stays unpriced


def test_gate_command_shows_mix_and_per_theory(captured_messages, monkeypatch, tmp_path):
    from src import telegram_bot
    from src.copy_trading import gate_history

    p = str(tmp_path / "gate-history.jsonl")
    gate_history.append(p, {"wallet": "0xaaa111", "admitted": True, "theories": ["1b"]})
    gate_history.append(p, {"wallet": "0xbbb222", "admitted": False, "theories": ["1e"],
                            "reasoning": "variance artifact, no copyable edge", "confidence": 0.9})
    monkeypatch.setattr(telegram_bot, "_gate_history_path", lambda: p)

    telegram_bot._handle_command("/gate")
    out = captured_messages[-1]

    assert "LLM Gate" in out
    assert "admitted <b>1</b>" in out and "rejected <b>1</b>" in out
    assert "1e" in out and "1b" in out                 # per-theory breakdown
    assert "variance artifact" in out                  # recent rejection reason


def test_gate_command_handles_empty_history(captured_messages, monkeypatch, tmp_path):
    from src import telegram_bot
    monkeypatch.setattr(telegram_bot, "_gate_history_path", lambda: str(tmp_path / "none.jsonl"))
    telegram_bot._handle_command("/gate")
    assert "no gate decisions logged yet" in captured_messages[-1]


def test_wallets_lists_top_overall_then_per_strategy_deduped(captured_messages, monkeypatch):
    from src import telegram_bot

    monkeypatch.setattr(telegram_bot, "_compute_unified", _unified_fixture)
    telegram_bot._handle_command("/wallets")
    out = captured_messages[-1]

    assert "Wallet leaderboard" in out
    # Part 1: top wallets across all strategies (only profitable ones)
    assert "Top wallets: all strategies" in out
    assert "By strategy" in out
    assert "B:1b" in out
    # both paper wallets ranked under the strategy, each shown once (deduped)
    assert "0xbbb222" in out and "0xccc333" in out
    assert out.count("0xccc333") == 1   # losing wallet appears once, not 4×
    # winner (net +20) is profitable -> appears in BOTH the top section and 1b
    assert out.count("0xbbb222") == 2


# ------------------------------------------------------------------
# Bot menu (setMyCommands) parity with the dispatcher
# ------------------------------------------------------------------

def test_bot_menu_command_names_are_valid():
    """Telegram rejects the entire setMyCommands batch if any name fails
    ^[a-z0-9_]{1,32}$. A typo here silently leaves the menu in its prior
    state — which is exactly how /test-live broke things in 86f9d4d.
    """
    import re
    from src import telegram_bot

    pattern = re.compile(r"^[a-z0-9_]{1,32}$")
    for entry in telegram_bot.BOT_MENU_COMMANDS:
        assert pattern.match(entry["command"]), (
            f"invalid Telegram command name: {entry['command']!r}"
        )
        assert 1 <= len(entry["description"]) <= 256


def test_bot_menu_matches_dispatcher():
    """Every command in the popup menu must be handled by ``_handle_command``,
    and every dispatched command (besides aliases) must appear in the menu.
    Catches drift when someone adds a handler but forgets to register it
    (or vice versa)."""
    import inspect
    import re
    from src import telegram_bot

    source = inspect.getsource(telegram_bot._handle_command)
    dispatched = set(re.findall(r'text\.startswith\("/([a-z0-9_-]+)"\)', source))

    # Aliases that intentionally don't get their own menu entry — they
    # share a description with the canonical command. (Empty for now.)
    aliases: set[str] = set()
    dispatched -= aliases

    menu = {entry["command"] for entry in telegram_bot.BOT_MENU_COMMANDS}
    # /start is registered in the menu but dispatched via the /help branch.
    menu_dispatched_via_help = {"start"}

    missing_from_menu = dispatched - menu
    missing_from_dispatch = menu - dispatched - menu_dispatched_via_help

    assert not missing_from_menu, (
        f"commands handled but not in menu: {sorted(missing_from_menu)}"
    )
    assert not missing_from_dispatch, (
        f"commands in menu but not handled: {sorted(missing_from_dispatch)}"
    )
