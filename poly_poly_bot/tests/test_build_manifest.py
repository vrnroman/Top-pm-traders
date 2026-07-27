"""J2 build-manifest drift witness — the boot-side marker consume.

deploy.sh writes ~/app/data/claude-version-drift.json when the image's
claude-code CLI version changes between deploys; main._consume_claude_drift_marker
must surface it on Telegram exactly once and never take boot down.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import main as bot_main


def _patch_env(monkeypatch, tmp_path, marker: dict | None):
    sent = []
    monkeypatch.setattr(bot_main.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(bot_main.telegram_bot, "send_message",
                        lambda msg: sent.append(msg))
    if marker is not None:
        (tmp_path / "claude-version-drift.json").write_text(json.dumps(marker))
    return sent


def test_marker_surfaced_once_then_consumed(monkeypatch, tmp_path):
    sent = _patch_env(monkeypatch, tmp_path, {"old": "2.1.100", "new": "2.1.220"})
    bot_main._consume_claude_drift_marker()
    assert len(sent) == 1
    assert "2.1.100" in sent[0] and "2.1.220" in sent[0]
    assert not (tmp_path / "claude-version-drift.json").exists()
    # second boot: no marker, no re-alert
    bot_main._consume_claude_drift_marker()
    assert len(sent) == 1


def test_no_marker_is_noop(monkeypatch, tmp_path):
    sent = _patch_env(monkeypatch, tmp_path, None)
    bot_main._consume_claude_drift_marker()
    assert sent == []


def test_malformed_marker_never_raises(monkeypatch, tmp_path):
    sent = _patch_env(monkeypatch, tmp_path, None)
    (tmp_path / "claude-version-drift.json").write_text("{not json")
    bot_main._consume_claude_drift_marker()   # must swallow, boot continues
    assert sent == []
