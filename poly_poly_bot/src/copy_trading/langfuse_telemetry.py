"""Minimal Langfuse telemetry for the `claude -p` wallet gate.

Posts a trace + generation pair straight to the Langfuse ingestion API
(``/api/public/ingestion``, HTTP Basic auth) — no ``langfuse`` SDK dependency,
mirroring the echo-v0 project's approach (keeps the image lean and the deploy
simple). The ``claude -p`` JSON envelope already carries token usage, USD cost
and latency, so we just forward it.

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
    """
    cfg = _config()
    if cfg is None:
        return
    pub, sec, host = cfg
    try:
        u = usage or {}
        in_base = int(u.get("input_tokens") or 0)
        cache_read = int(u.get("cache_read_input_tokens") or 0)
        cache_creation = int(u.get("cache_creation_input_tokens") or 0)
        out_tok = int(u.get("output_tokens") or 0)
        prompt_tok = in_base + cache_read + cache_creation
        has_usage = prompt_tok > 0 or out_tok > 0

        meta = dict(metadata or {})
        if cost_usd is not None:
            meta["costUsd"] = cost_usd
        if duration_ms is not None:
            meta["durationMs"] = duration_ms

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
                "tags": tags or [],
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
            gen_body["usageDetails"] = {
                "input": in_base, "output": out_tok,
                "cache_read": cache_read, "cache_creation": cache_creation,
                "total": prompt_tok + out_tok,
            }
        if cost_usd:
            gen_body["costDetails"] = {"total": cost_usd}
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
