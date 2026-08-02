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
