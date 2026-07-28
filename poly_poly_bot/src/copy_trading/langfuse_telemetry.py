"""Minimal Langfuse telemetry for the `claude -p` wallet gate.

Posts a trace + generation pair straight to the Langfuse ingestion API
(``/api/public/ingestion``, HTTP Basic auth) — no ``langfuse`` SDK dependency,
mirroring the echo-v0 project's approach (keeps the image lean and the deploy
simple). The ``claude -p`` JSON envelope already carries token usage, USD cost
and latency, so we forward it — with two corrections learned the hard way
(P2-6, 2026-07-28):

- ``usageDetails.input`` is the FULL prompt size (uncached + cache read +
  cache creation). Langfuse backfills its display ``usage`` from
  ``usageDetails``, and the 2026-07-11 CLI update moved the bulk of the prompt
  into the cache keys, so forwarding ``input_tokens`` raw dropped displayed
  input to 2-6 tokens/call (was ~3,000-5,000).
- When a call burned tokens but the envelope carries no ``total_cost_usd``,
  cost is COMPUTED from the token counts (price table below) instead of
  silently landing as $0.00 — the 07-11..14 CLI outage produced exactly such a
  window before anyone noticed.

Fully opt-in and defensive: a no-op unless ``LANGFUSE_PUBLIC_KEY`` and
``LANGFUSE_SECRET_KEY`` are set, and it NEVER raises — a telemetry outage must
not break a discovery sweep. Fire-and-forget with a short timeout.
"""

from __future__ import annotations

import base64
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger("poly_poly_bot")

_DEFAULT_HOST = "https://cloud.langfuse.com"
_TIMEOUT_S = 8


def _price_per_mtok(name: str, fallback: float) -> float:
    """USD per million tokens, env-overridable; read at call time (test-friendly)."""
    v = (os.getenv(name) or "").strip()
    return float(v) if v else fallback


def _computed_cost_usd(u: dict) -> float:
    """Fallback USD cost from token counts when the envelope reports none.

    claude-opus-4-8 list prices (the only model this bot gates with). Anthropic
    cache pricing: read = 0.1x input, 5m write = 1.25x, 1h write = 2x. Uses the
    envelope's ephemeral split when present; without it, cache writes are priced
    as 5m (the cheaper bucket — a fallback errs low, never invents spend).
    Calibrated against the live envelope 2026-07-28: $0.0504 computed vs
    $0.0510 reported on a 19.5k-token call."""
    in_base = int(u.get("input_tokens") or 0)
    cache_read = int(u.get("cache_read_input_tokens") or 0)
    cache_creation = int(u.get("cache_creation_input_tokens") or 0)
    out_tok = int(u.get("output_tokens") or 0)
    split = u.get("cache_creation") or {}
    w1h = int(split.get("ephemeral_1h_input_tokens") or 0)
    w5m = int(split.get("ephemeral_5m_input_tokens") or 0)
    if w1h + w5m <= 0:
        w5m, w1h = cache_creation, 0
    micros = (
        in_base * _price_per_mtok("LANGFUSE_PRICE_INPUT_PER_MTOK", 5.0)
        + out_tok * _price_per_mtok("LANGFUSE_PRICE_OUTPUT_PER_MTOK", 25.0)
        + cache_read * _price_per_mtok("LANGFUSE_PRICE_CACHE_READ_PER_MTOK", 0.5)
        + w5m * _price_per_mtok("LANGFUSE_PRICE_CACHE_WRITE_5M_PER_MTOK", 6.25)
        + w1h * _price_per_mtok("LANGFUSE_PRICE_CACHE_WRITE_1H_PER_MTOK", 10.0)
    )
    return micros / 1e6


def _config() -> tuple[str, str, str] | None:
    """(public_key, secret_key, host) or None when telemetry is unconfigured."""
    pub = (os.getenv("LANGFUSE_PUBLIC_KEY") or "").strip()
    sec = (os.getenv("LANGFUSE_SECRET_KEY") or "").strip()
    if not pub or not sec:
        return None
    host = (os.getenv("LANGFUSE_HOST") or os.getenv("LANGFUSE_BASE_URL")
            or _DEFAULT_HOST).strip().rstrip("/")
    return pub, sec, host


def enabled() -> bool:
    return _config() is not None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def record_generation(
    *,
    name: str,
    input: Any,
    output: Any,
    model: str,
    start: float,
    end: float,
    usage: dict | None = None,        # the claude -p envelope's `usage` block
    cost_usd: float | None = None,
    duration_ms: float | None = None,
    metadata: dict | None = None,
    tags: list[str] | None = None,
    error: str | None = None,
) -> None:
    """Emit one trace+generation to Langfuse. No-op if unconfigured; never raises.

    ``usage`` is forwarded in the Anthropic shape (input_tokens, output_tokens,
    cache_read_input_tokens, cache_creation_input_tokens) exactly as the
    ``claude -p`` envelope returns it; we map it to Langfuse usage/usageDetails.
    ``usageDetails.input`` is the full prompt size (see module docstring, P2-6);
    cost falls back to a computed value when the envelope carries none.
    """
    cfg = _config()
    if cfg is None:
        return
    pub, sec, host = cfg
    try:
        u = usage or {}
        try:
            in_base = int(u.get("input_tokens") or 0)
            cache_read = int(u.get("cache_read_input_tokens") or 0)
            cache_creation = int(u.get("cache_creation_input_tokens") or 0)
            out_tok = int(u.get("output_tokens") or 0)
            split = u.get("cache_creation") or {}
            w1h = int(split.get("ephemeral_1h_input_tokens") or 0)
            w5m = int(split.get("ephemeral_5m_input_tokens") or 0)
        except (TypeError, ValueError, AttributeError) as e:
            # Unparseable usage shape: drop the row, but LOUDLY — the
            # 2026-07-11 regression was a silent envelope shape change, and a
            # wholesale provider-side change SHOULD flood WARNINGs. (Still
            # never raises: telemetry must not break a sweep.)
            desc = ({k: type(v).__name__ for k, v in u.items()}
                    if isinstance(u, dict) else type(u).__name__)
            logger.warning(
                "[LANGFUSE] dropping %s generation: unparseable usage block "
                "(%r); keys/types: %s", name, e, desc)
            return
        prompt_tok = in_base + cache_read + cache_creation
        has_usage = prompt_tok > 0 or out_tok > 0

        # Cost: the envelope's own figure wins; when it's absent/0 but tokens
        # were burned, compute from the price table so cost never lands as a
        # silent $0.00 (P2-6). `costDetails` must ALWAYS ride a success row —
        # with usageDetails.input now the full prompt size, a missing
        # costDetails would let Langfuse's own calc price the cache tokens
        # twice.
        if cost_usd:
            cost, cost_source = cost_usd, "envelope"
        elif has_usage:
            cost, cost_source = _computed_cost_usd(u), "computed"
        else:
            cost, cost_source = None, "none"

        meta = dict(metadata or {})
        if cost_usd is not None:
            meta["costUsd"] = cost_usd
        meta["costSource"] = cost_source
        if duration_ms is not None:
            meta["durationMs"] = duration_ms

        # I4 watchdog: a successful generation with zero usage means the
        # envelope shape drifted under us (the 2026-07-11 regression class) —
        # make it loud instead of letting bad accounting sit invisible.
        tags_out = list(tags or [])
        if error is None and not has_usage:
            logger.warning(
                "[LANGFUSE] %s generation succeeded but carries zero usage — "
                "envelope shape drift?", name)
            tags_out.append("telemetry-suspect")

        trace_id = str(uuid.uuid4())
        obs_id = str(uuid.uuid4())
        now_iso = _iso(end)
        io = {"input": input, "output": output}
        level = "ERROR" if error else "DEFAULT"

        trace_event = {
            "id": str(uuid.uuid4()),
            "type": "trace-create",
            "timestamp": now_iso,
            "body": {
                "id": trace_id,
                "timestamp": _iso(start),
                "name": name,
                **io,
                # P1-4: the level must ride the TRACE, not only the generation —
                # Langfuse's error views/filter key on trace level, which is why
                # ~13-15% of gate calls could fail open with "zero error traces"
                # (§1.7a) while the generations quietly carried level=ERROR.
                "level": level,
                **({"statusMessage": error} if error else {}),
                "tags": tags_out,
                "metadata": meta,
            },
        }

        gen_body: dict = {
            "id": obs_id,
            "traceId": trace_id,
            "name": name,
            "startTime": _iso(start),
            "endTime": now_iso,
            "model": model,
            **io,
            "level": level,
            "metadata": meta,
        }
        if has_usage:
            gen_body["usage"] = {
                "input": prompt_tok, "output": out_tok,
                "total": prompt_tok + out_tok, "unit": "TOKENS",
            }
            details = {
                # input = FULL prompt tokens (uncached + cache read + cache
                # creation), not the envelope's uncached remainder — Langfuse
                # backfills `usage` from usageDetails, which is how displayed
                # input collapsed to 2-6 tokens on 2026-07-11 (P2-6).
                "input": prompt_tok, "output": out_tok,
                "cache_read": cache_read, "cache_creation": cache_creation,
                "total": prompt_tok + out_tok,
            }
            if w1h or w5m:
                details["cache_creation_5m"] = w5m
                details["cache_creation_1h"] = w1h
            gen_body["usageDetails"] = details
        if cost:
            gen_body["costDetails"] = {"total": cost}
        if error:
            gen_body["statusMessage"] = error

        gen_event = {
            "id": str(uuid.uuid4()),
            "type": "generation-create",
            "timestamp": now_iso,
            "body": gen_body,
        }

        auth = base64.b64encode(f"{pub}:{sec}".encode()).decode()
        resp = requests.post(
            f"{host}/api/public/ingestion",
            json={"batch": [trace_event, gen_event]},
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
            timeout=_TIMEOUT_S,
        )
        # 207 = partial success (per-event status); both are acceptable.
        if resp.status_code not in (200, 201, 207):
            logger.debug("[LANGFUSE] ingestion %s: %s", resp.status_code, resp.text[:200])
    except Exception:  # telemetry must never break the gate
        logger.debug("[LANGFUSE] telemetry post failed", exc_info=True)
