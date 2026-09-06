"""J2 build-manifest drift witness — the boot-side marker consume.

deploy.sh writes ~/app/data/claude-version-drift.json when the image's
claude-code CLI version changes between deploys; main._consume_claude_drift_marker
must surface it on Telegram exactly once and never take boot down.
"""

from __future__ import annotations

import inspect
import json

import main as bot_main


def _patch_env(monkeypatch, tmp_path, marker: dict | None):
    sent = []
    monkeypatch.setattr(bot_main.CONFIG, "data_dir", str(tmp_path))
    # Accept the production call shape: text by keyword and a message class.
    monkeypatch.setattr(bot_main.telegram_bot, "send_message",
                        lambda msg=None, text=None, **kw: sent.append(msg if msg is not None else text))
    if marker is not None:
        (tmp_path / "claude-version-drift.json").write_text(json.dumps(marker))
    return sent


def test_main_calls_the_consumer_at_boot():
    # deleting the call site in main() must not regress green
    assert "_consume_claude_drift_marker()" in inspect.getsource(bot_main.main)


def test_marker_surfaced_once_then_consumed(monkeypatch, tmp_path):
    sent = _patch_env(monkeypatch, tmp_path, {"old": "2.1.100", "new": "2.1.220"})
    marker = tmp_path / "claude-version-drift.json"

    # Pin consume-BEFORE-send: a Telegram outage must not re-alert every boot.
    real_send = bot_main.telegram_bot.send_message

    def send_asserting_consumed(msg=None, text=None, **kw):
        # The production site passes the text by keyword and names its class;
        # a fake that only takes a positional made the suite lie about it.
        assert not marker.exists(), "marker still present at send time"
        real_send(msg if msg is not None else text, **kw)

    monkeypatch.setattr(bot_main.telegram_bot, "send_message",
                        send_asserting_consumed)
    bot_main._consume_claude_drift_marker()
    assert len(sent) == 1
    assert "2.1.100" in sent[0] and "2.1.220" in sent[0]
    assert not marker.exists()
    # second boot: no marker, no re-alert
    bot_main._consume_claude_drift_marker()
    assert len(sent) == 1


def test_no_marker_is_noop(monkeypatch, tmp_path):
    sent = _patch_env(monkeypatch, tmp_path, None)
    bot_main._consume_claude_drift_marker()
    assert sent == []


def test_malformed_marker_quarantined_never_raises(monkeypatch, tmp_path):
    sent = _patch_env(monkeypatch, tmp_path, None)
    marker = tmp_path / "claude-version-drift.json"
    marker.write_text("{not json")
    bot_main._consume_claude_drift_marker()   # must swallow, boot continues
    assert sent == []
    assert not marker.exists()
    # evidence quarantined, and a second boot does not re-raise on it
    assert (tmp_path / "claude-version-drift.json.bad").exists()
    bot_main._consume_claude_drift_marker()
