"""s-r7m3qk (2026-08-02): the 08-22 §7 verdict memo must actually be
deliverable, /setkey must never echo a private key, and WARNING+ must reach
the operational log.

The verdict memo is the payload of a pre-registered falsification experiment
and it gates one-shot state on delivery: `main.py` only sets `verdict_sent`
when `_send_chunked` returns True. So a memo Telegram REFUSES is not a
cosmetic bug — it re-fires every morning forever, `/verdict` never arms, and
the kill criterion silently never lands.

Verified against the live Telegram API on 2026-08-02:
    text "  <$25  n= 12"  -> 400 "can't parse entities: Unsupported start tag
                                  \\"$25\\" at byte offset 2"
    the same inside <pre> -> 400, identical (pre does NOT protect it)
    a control plain line  -> 400 "chat not found"  (i.e. it parsed fine)
"""

from __future__ import annotations

import logging

from src import logger as bot_logger
from src import telegram_bot as tb
from src.copy_trading.strategy_compare import cost_slices, fmt_slices


# --------------------------------------------------------------------------- #
# The instance: no size-bucket label may contain a bare "<"
# --------------------------------------------------------------------------- #

def _rows(cat, spent, n):
    return [{"closed": True, "category": cat, "spent": spent, "pnl": 1.0,
             "ideal_pnl": 1.1, "ideal_cost_usd": 0.4, "won": True,
             "copy_id": f"{cat}-{spent}-{i}"} for i in range(n)]


def test_no_rendered_verdict_line_contains_an_html_tag_opener():
    """Every prod copy lands in a size bucket (COPY_PAPER_MAX_USD=50), so this
    is the line that would have killed the memo."""
    out = cost_slices(_rows("sports", 5.0, 12) + _rows("crypto", 60.0, 11))
    lines = (fmt_slices("by size:", out["by_size"])
             + fmt_slices("by category:", out["by_category"]))
    assert lines
    for ln in lines:
        assert "<" not in ln, f"HTML tag opener in a Telegram-bound line: {ln!r}"


# --------------------------------------------------------------------------- #
# The class: a parse failure must never cost us the message
# --------------------------------------------------------------------------- #

class _Resp:
    def __init__(self, ok, status_code=200, text=""):
        self.ok, self.status_code, self.text = ok, status_code, text


_PARSE_ERR = ('{"ok":false,"error_code":400,"description":"Bad Request: '
              'can\'t parse entities: Unsupported start tag \\"$25\\" at byte '
              'offset 2"}')


def _configure(monkeypatch):
    monkeypatch.setattr(tb, "is_configured", lambda: True)
    monkeypatch.setattr(tb.CONFIG, "telegram_bot_token", "123:abc", raising=False)
    monkeypatch.setattr(tb.CONFIG, "telegram_chat_id", "42", raising=False)


def test_parse_failure_retries_as_plain_text_and_reports_delivered(monkeypatch):
    _configure(monkeypatch)
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(json)
        if "parse_mode" in json:
            return _Resp(False, 400, _PARSE_ERR)
        return _Resp(True)

    monkeypatch.setattr(tb.requests, "post", fake_post)
    assert tb.send_message("  <$25  n=12") is True     # the memo still lands
    assert len(calls) == 2
    assert "parse_mode" not in calls[1]
    assert calls[1]["text"] == "  <$25  n=12"          # content preserved intact


def test_a_real_delivery_failure_is_not_retried(monkeypatch):
    """'chat not found' is not fixable by dropping markup — don't double-send."""
    _configure(monkeypatch)
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(json)
        return _Resp(False, 400, '{"description":"Bad Request: chat not found"}')

    monkeypatch.setattr(tb.requests, "post", fake_post)
    assert tb.send_message("hello") is False
    assert len(calls) == 1


def test_plain_text_retry_that_also_fails_reports_false(monkeypatch):
    """The memo gates one-shot state on this bool — it must not lie."""
    _configure(monkeypatch)
    monkeypatch.setattr(tb.requests, "post",
                        lambda url, json=None, timeout=None: _Resp(False, 400, _PARSE_ERR))
    assert tb.send_message("  <$25") is False


def test_is_parse_error_discriminates():
    assert tb._is_parse_error(_Resp(False, 400, _PARSE_ERR)) is True
    assert tb._is_parse_error(_Resp(False, 400, "chat not found")) is False
    assert tb._is_parse_error(_Resp(False, 500, "parse entities")) is False


# --------------------------------------------------------------------------- #
# /setkey must never echo the key, in ANY accepted form
# --------------------------------------------------------------------------- #

KEY = "0x" + "ab12" * 16          # 64 hex chars
BARE = "ab12" * 16                # config_validators accepts this too


def _echo_of(text: str, monkeypatch, caplog) -> str:
    # telegram_bot logs to the "telegram" logger, which propagates to the ROOT
    # handlers main.py installs — i.e. straight to bot-<date>.log on the data
    # volume and to the docker json log. src.logger.scrub_secrets only knows
    # the bot-token shape, so nothing downstream would catch a private key.
    _configure(monkeypatch)
    monkeypatch.setattr(tb, "_handle_command", lambda t: None)
    upd = {"message": {"chat": {"id": "42"}, "text": text}}
    with caplog.at_level(logging.INFO, logger="telegram"):
        tb._process_update(upd)
    return caplog.text


def test_setkey_never_echoes_the_key_in_any_accepted_form(monkeypatch, caplog):
    for text in (
        f"/setkey {KEY} CONFIRM",           # the one shape the old regex caught
        f"/setkey {BARE} CONFIRM",          # validator strips an absent 0x
        f"/setkey@mybot {KEY} CONFIRM",     # Telegram appends @botname
        f"/setkey {KEY} confirm",           # typo — echoed before dispatch
        f"/setkey {KEY}",                   # missing CONFIRM
        f"/setkey  {KEY}  CONFIRM  ",       # stray whitespace
    ):
        caplog.clear()
        out = _echo_of(text, monkeypatch, caplog)
        assert "ab12ab12" not in out, f"private key leaked for: {text!r}"
        assert "REDACTED" in out


def test_ordinary_commands_are_still_echoed_in_full(monkeypatch, caplog):
    out = _echo_of("/pnl", monkeypatch, caplog)
    assert "Telegram command: /pnl" in out


# --------------------------------------------------------------------------- #
# WARNING+ must reach the operational log, not only signals
# --------------------------------------------------------------------------- #

def _rec(level, msg="[disk-watch] TRIPPED: free 4.1G"):
    return logging.LogRecord("poly_poly_bot", level, __file__, 1, msg, (), None)


def test_operational_log_carries_warnings_and_errors():
    """bot-*.log must never be able to report a clean sheet while the disk
    alarm is firing — that false all-clear is what this fixes."""
    f = bot_logger._OperationalFilter()
    assert f.filter(_rec(logging.WARNING)) is True
    assert f.filter(_rec(logging.ERROR)) is True
    assert f.filter(_rec(logging.CRITICAL)) is True


def test_signals_log_still_gets_them_too():
    f = bot_logger._SignalFilter()
    assert f.filter(_rec(logging.WARNING)) is True
    assert f.filter(_rec(logging.ERROR)) is True


def test_trade_and_skip_stay_out_of_the_operational_log():
    f = bot_logger._OperationalFilter()
    assert f.filter(_rec(bot_logger.TRADE, "[exec] bought")) is False
    assert f.filter(_rec(bot_logger.SKIP, "[pattern] skipped")) is False


def test_signal_prefixed_info_still_routes_to_signals_only():
    op, sig = bot_logger._OperationalFilter(), bot_logger._SignalFilter()
    r = _rec(logging.INFO, "[watchlist] new wallet")
    assert op.filter(r) is False and sig.filter(r) is True
