"""Langfuse telemetry — wire format + opt-in/defensive behavior (no real network)."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from src.copy_trading import langfuse_telemetry as lt

_ENVELOPE_USAGE = {
    "input_tokens": 3000, "output_tokens": 40,
    "cache_read_input_tokens": 12000, "cache_creation_input_tokens": 500,
}


def _enable(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_HOST", "https://jp.cloud.langfuse.com")


def test_disabled_is_noop_when_keys_absent(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    posted = []
    monkeypatch.setattr(lt.requests, "post", lambda *a, **k: posted.append((a, k)))
    assert lt.enabled() is False
    lt.record_generation(name="wallet-gate", input="p", output="o", model="m",
                         start=1.0, end=2.0)
    assert posted == []                       # nothing sent


def test_record_generation_posts_trace_and_generation(monkeypatch):
    _enable(monkeypatch)
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, body=json, headers=headers, timeout=timeout)
        return SimpleNamespace(status_code=207, text="")

    monkeypatch.setattr(lt.requests, "post", fake_post)
    lt.record_generation(
        name="wallet-gate", input="the prompt", output='{"verdict":"skip"}',
        model="claude-opus-4-8", start=1000.0, end=1024.0,
        usage=_ENVELOPE_USAGE, cost_usd=0.037, duration_ms=4490,
        metadata={"wallet": "0xABC", "verdict": "skip"},
        tags=["wallet-gate"], error=None,
    )

    assert captured["url"].endswith("/api/public/ingestion")
    # Basic auth = base64(public:secret)
    assert captured["headers"]["Authorization"] == "Basic " + base64.b64encode(
        b"pk-test:sk-test").decode()

    batch = captured["body"]["batch"]
    types = [e["type"] for e in batch]
    assert types == ["trace-create", "generation-create"]

    gen = next(e for e in batch if e["type"] == "generation-create")["body"]
    assert gen["model"] == "claude-opus-4-8"
    assert gen["level"] == "DEFAULT"
    # usage maps Anthropic shape → prompt = input + cache_read + cache_creation
    assert gen["usage"] == {"input": 15500, "output": 40, "total": 15540, "unit": "TOKENS"}
    # P2-6: usageDetails.input is the FULL prompt size, not the uncached
    # remainder — Langfuse backfills its display `usage` from usageDetails.
    assert gen["usageDetails"] == {
        "input": 15500, "output": 40,
        "cache_read": 12000, "cache_creation": 500, "total": 15540,
    }
    assert gen["costDetails"]["total"] == 0.037
    assert gen["metadata"]["wallet"] == "0xABC"
    assert gen["metadata"]["costSource"] == "envelope"
    # trace + generation share a traceId
    trace = next(e for e in batch if e["type"] == "trace-create")["body"]
    assert gen["traceId"] == trace["id"]


# The shape the claude CLI actually returns since 2026-07-11 (2.1.x): the whole
# prompt lives in the cache keys, input_tokens is a 2-token remainder, and the
# cache_creation block splits ephemeral 5m/1h writes.
_CURRENT_ENVELOPE_USAGE = {
    "input_tokens": 2, "output_tokens": 738,
    "cache_read_input_tokens": 15240, "cache_creation_input_tokens": 5764,
    "cache_creation": {"ephemeral_1h_input_tokens": 5764,
                       "ephemeral_5m_input_tokens": 0},
}


def _posted_gen(monkeypatch, **kwargs):
    _enable(monkeypatch)
    captured = {}
    monkeypatch.setattr(lt.requests, "post", lambda url, json=None, **k: (
        captured.update(body=json) or SimpleNamespace(status_code=207, text="")))
    lt.record_generation(name="wallet-gate", input="p", output="o",
                         model="claude-opus-4-8", start=1.0, end=2.0, **kwargs)
    return next(e for e in captured["body"]["batch"]
                if e["type"] == "generation-create")["body"], \
        next(e for e in captured["body"]["batch"]
             if e["type"] == "trace-create")["body"]


def test_current_envelope_maps_full_prompt_and_ephemeral_split(monkeypatch):
    gen, _ = _posted_gen(monkeypatch, usage=_CURRENT_ENVELOPE_USAGE,
                         cost_usd=0.0854)
    assert gen["usage"]["input"] == 21006          # 2 + 15240 + 5764
    assert gen["usageDetails"]["input"] == 21006
    assert gen["usageDetails"]["cache_creation_1h"] == 5764
    assert gen["usageDetails"]["cache_creation_5m"] == 0
    assert gen["costDetails"]["total"] == 0.0854
    assert gen["metadata"]["costSource"] == "envelope"


def test_cost_falls_back_to_computed_when_envelope_reports_none(monkeypatch):
    gen, _ = _posted_gen(monkeypatch, usage=_CURRENT_ENVELOPE_USAGE,
                         cost_usd=None)
    # 2*5 + 738*25 + 15240*0.5 + 5764*10 (1h write) per Mtok = $0.08372
    assert gen["costDetails"]["total"] == pytest.approx(0.08372)
    assert gen["metadata"]["costSource"] == "computed"


def test_cost_fallback_treats_zero_envelope_cost_as_missing(monkeypatch):
    gen, _ = _posted_gen(monkeypatch, usage=_CURRENT_ENVELOPE_USAGE,
                         cost_usd=0.0)
    assert gen["costDetails"]["total"] > 0
    assert gen["metadata"]["costSource"] == "computed"


def test_cost_fallback_no_ephemeral_split_prices_5m_writes(monkeypatch):
    usage = {"input_tokens": 100, "output_tokens": 10,
             "cache_creation_input_tokens": 1000}
    gen, _ = _posted_gen(monkeypatch, usage=usage, cost_usd=None)
    # 100*5 + 10*25 + 1000*6.25 per Mtok = $0.007
    assert gen["costDetails"]["total"] == pytest.approx(0.007)


def test_computed_cost_honors_env_price_override(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PRICE_INPUT_PER_MTOK", "50")
    gen, _ = _posted_gen(monkeypatch,
                         usage={"input_tokens": 1000, "output_tokens": 0},
                         cost_usd=None)
    assert gen["costDetails"]["total"] == pytest.approx(0.05)


def test_watchdog_tags_success_with_zero_usage(monkeypatch, caplog):
    gen, trace = _posted_gen(monkeypatch, usage=None, cost_usd=None)
    assert "usage" not in gen
    assert "costDetails" not in gen
    assert gen["metadata"]["costSource"] == "none"
    assert "telemetry-suspect" in trace["tags"]
    assert any("zero usage" in r.message for r in caplog.records
               if r.levelname == "WARNING")


def test_watchdog_silent_on_failed_calls(monkeypatch, caplog):
    # failed calls truthfully carry no usage — that is not shape drift
    gen, trace = _posted_gen(monkeypatch, usage=None, cost_usd=None,
                             error="rate-limited")
    assert gen["level"] == "ERROR"
    assert "telemetry-suspect" not in trace["tags"]
    assert not any("zero usage" in r.message for r in caplog.records)


def test_record_generation_marks_error_level(monkeypatch):
    _enable(monkeypatch)
    captured = {}
    monkeypatch.setattr(lt.requests, "post", lambda url, json=None, **k: (
        captured.update(body=json) or SimpleNamespace(status_code=207, text="")))
    lt.record_generation(name="wallet-gate", input="p", output="", model="m",
                         start=1.0, end=2.0, error="unparseable verdict")
    gen = next(e for e in captured["body"]["batch"]
               if e["type"] == "generation-create")["body"]
    assert gen["level"] == "ERROR"
    assert gen["statusMessage"] == "unparseable verdict"
    assert "usage" not in gen                  # no usage block when none supplied


def test_record_generation_never_raises(monkeypatch):
    _enable(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(lt.requests, "post", boom)
    # must swallow the exception
    lt.record_generation(name="wallet-gate", input="p", output="o", model="m",
                         start=1.0, end=2.0, usage=_ENVELOPE_USAGE)
