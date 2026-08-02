"""Ledger-integrity witness (s-log7q, 2026-08-02).

Every trust surface sums ledgers — realized-pnl.jsonl for System A, the two
copy-paper ledgers for the race. A duplicated settled row double-counts P&L
silently: on 2026-07-30 the preview resolver re-booked a 61-position legacy
batch and every all-time sum read −$575 low until the file was scrubbed. The
append path is now guarded (``pnl.append_realized`` dedups resolution exits),
but guards fail and tooling writes files directly (selective resets, manual
surgery) — this witness RE-READS the ledgers daily and says on the watched
channel whether the numbers being rendered came from a clean file.

Pure scanners + a thin formatter; the reporter loop calls it once per daily
snapshot.
"""

from __future__ import annotations

import json
import os
from collections import Counter


def _load_rows(path: str) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict):
                    rows.append(d)
    except OSError:
        pass
    return rows


def duplicate_resolution_keys(rows: list[dict]) -> Counter:
    """(condition_id, token_id) resolution exits booked more than once."""
    keys = Counter(
        (str(r.get("condition_id") or ""), str(r.get("token_id") or ""))
        for r in rows if r.get("exit") == "resolution")
    return Counter({k: n for k, n in keys.items() if n > 1})


def duplicate_copy_ids(rows: list[dict]) -> Counter:
    """copy_ids with more than one CLOSED row in a paper ledger.

    Open + closed is legitimate (a position updates as it closes); two closed
    rows for one copy_id means the close was booked twice and the race sums it
    twice.
    """
    closed = Counter(str(r.get("copy_id") or "")
                     for r in rows if r.get("closed"))
    return Counter({k: n for k, n in closed.items() if n > 1})


def scan(*, realized_path: str, a_ledger: str, b_ledger: str) -> dict:
    """Scan the three money ledgers. Returns per-file duplicate counts with a
    sample of offending keys (first 3) for the alert line."""
    out = {}
    for name, path, scanner in (
            ("realized-pnl", realized_path, duplicate_resolution_keys),
            ("ledger-A", a_ledger, duplicate_copy_ids),
            ("ledger-B", b_ledger, duplicate_copy_ids)):
        dups = scanner(_load_rows(path))
        if dups:
            sample = ["…".join((k[0][:8], k[1][:8])) if isinstance(k, tuple)
                      else k[:14] for k in list(dups)[:3]]
            out[name] = {"rows": sum(dups.values()), "keys": len(dups),
                         "sample": sample}
    return out


def format_findings(findings: dict) -> str:
    """One Telegram-safe line per dirty ledger ('' when all clean)."""
    if not findings:
        return ""
    bits = [f"{name}: {d['rows']} dup rows over {d['keys']} keys "
            f"({', '.join(d['sample'])})"
            for name, d in findings.items()]
    return "⚠ LEDGER INTEGRITY — duplicate settled rows: " + "; ".join(bits)


# --------------------------------------------------------------------------- #
# Daily data autopsy (s-log7q phase-2, P2) — one watched line for every file
# --------------------------------------------------------------------------- #
#
# The duplicate-row witness above covers the corruption class that already bit
# once. This generalizes it into a registry over every money/state ledger the
# bot keeps: duplicate settled keys, size trajectory, and timestamp inversion
# (a jsonl whose rows go backwards in time — the signature of tooling writes
# landing out of band). Scanners STREAM line-by-line (the e2-small has ~600MB
# of headroom; no whole-file loads) and cache dirs are excluded — they are
# regenerable, so rot there is self-healing, not an anomaly.
#
# Alerting dedups by anomaly fingerprint: a standing anomaly re-alerts only
# when its shape changes, so the watched channel never becomes noise.

import datetime as _dt
import hashlib
import os as _os
import time as _time

_GROWTH_FACTOR = 2.0        # flag when a file more than doubles ...
_GROWTH_MIN_BYTES = 5 * 1024 * 1024   # ... by at least this much, day-over-day
_INVERSION_TOL_S = 3600.0   # a row >1h older than its predecessor is an inversion


def _stream_lines(path: str):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                yield line
    except OSError:
        return


def _ts_of(row: dict):
    """Best-effort row timestamp: ISO ``timestamp`` or epoch ``ts``."""
    iso = row.get("timestamp")
    if isinstance(iso, str) and iso:
        try:
            return _dt.datetime.fromisoformat(
                iso.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    try:
        v = float(row.get("ts"))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def scan_size_trajectory(name: str, path: str, prev: dict | None,
                         now: float) -> tuple[str | None, dict]:
    """Flag >_GROWTH_FACTOR growth with a >= _GROWTH_MIN_BYTES absolute jump
    since the previous sample. Returns (anomaly or None, new sample)."""
    try:
        size = _os.path.getsize(path)
    except OSError:
        return None, {"bytes": 0, "ts": now}
    sample = {"bytes": size, "ts": now}
    if prev and prev.get("bytes") and prev.get("ts"):
        delta = size - int(prev["bytes"])
        if (size > _GROWTH_FACTOR * int(prev["bytes"])
                and delta >= _GROWTH_MIN_BYTES):
            return (f"{name} grew {prev['bytes'] / 1e6:.0f}MB → "
                    f"{size / 1e6:.0f}MB (+{delta / 1e6:.0f}MB) since "
                    f"{_dt.datetime.fromtimestamp(float(prev['ts']), _dt.timezone.utc):%m-%d %H:%M}Z",
                    sample)
    return None, sample


def scan_ts_inversions(name: str, path: str, max_findings: int = 3) -> list[str]:
    """Count rows whose timestamp jumps >1h backwards from the previous row —
    the signature of out-of-band tooling writes."""
    findings, last_ts, count = [], None, 0
    for line in _stream_lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        ts = _ts_of(row)
        if ts is None:
            continue
        if last_ts is not None and ts < last_ts - _INVERSION_TOL_S:
            count += 1
            if len(findings) < max_findings:
                findings.append(
                    _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%m-%d %H:%M"))
        last_ts = ts
    if count:
        return [f"{name}: {count} timestamp inversion(s) "
                f"(e.g. {', '.join(findings)}) — out-of-band write?"]
    return []


def default_watches(data_dir: str) -> list[dict]:
    """The money/state ledgers under watch. Each: name, path, and which extra
    scanners run beyond the size trajectory."""
    return [
        {"name": "realized-pnl", "path": _os.path.join(data_dir, "realized-pnl.jsonl"),
         "dups": "resolution", "ts": True},
        {"name": "ledger-A", "path": _os.path.join(data_dir, "copy_paper_ledger.jsonl"),
         "dups": "copy_id", "ts": False},
        {"name": "ledger-B", "path": _os.path.join(data_dir, "copy_paper_ledger_b.jsonl"),
         "dups": "copy_id", "ts": False},
        {"name": "gate-history", "path": _os.path.join(data_dir, "gate-history.jsonl"),
         "dups": None, "ts": True},
        {"name": "trade-history", "path": _os.path.join(data_dir, "trade-history.jsonl"),
         "dups": None, "ts": True},
        {"name": "cull-histogram", "path": _os.path.join(data_dir, "cull-histogram.jsonl"),
         "dups": None, "ts": False},
    ]


def run_autopsy(data_dir: str, *, now: float | None = None,
                state_name: str = "autopsy-state.json") -> dict:
    """Run every watch, dedup findings by fingerprint, persist state.

    Returns ``{"findings": [...], "new": [...]}`` — ``new`` is the subset the
    owner has not been told about (alert-worthy); ``findings`` is everything
    currently anomalous (for the log line). Never raises: an autopsy that
    crashes would hide the rot it exists to catch.
    """
    now = now if now is not None else _time.time()
    state_path = _os.path.join(data_dir, state_name)
    state = _load_state(state_path)
    prev_files = state.get("files") or {}
    seen_fps = state.get("fingerprints") or {}
    findings, new_findings, files_state = [], [], {}
    try:
        for w in default_watches(data_dir):
            name, path = w["name"], w["path"]
            anomalies = []
            if w.get("dups") == "resolution":
                dups = duplicate_resolution_keys(_load_rows(path))
                if dups:
                    anomalies.append(
                        f"{name}: {sum(dups.values())} duplicate resolution "
                        f"row(s) over {len(dups)} key(s)")
            elif w.get("dups") == "copy_id":
                dups = duplicate_copy_ids(_load_rows(path))
                if dups:
                    anomalies.append(
                        f"{name}: {sum(dups.values())} duplicate closed "
                        f"copy_id row(s) over {len(dups)} id(s)")
            if w.get("ts"):
                anomalies.extend(scan_ts_inversions(name, path))
            size_anomaly, sample = scan_size_trajectory(
                name, path, prev_files.get(name), now)
            if size_anomaly:
                anomalies.append(size_anomaly)
            files_state[name] = sample
            for a in anomalies:
                findings.append(a)
                fp = hashlib.sha256(a.encode()).hexdigest()[:16]
                if fp not in seen_fps:
                    new_findings.append(a)
                    seen_fps[fp] = now
        _save_state(state_path, {"files": files_state,
                                 "fingerprints": seen_fps})
    except Exception:  # noqa: BLE001 — best effort by design
        pass
    return {"findings": findings, "new": new_findings}


def _load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    _os.replace(tmp, path)
