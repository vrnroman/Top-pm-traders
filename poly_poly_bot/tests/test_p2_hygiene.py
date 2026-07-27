"""P2 hygiene pins — the purged tennis/weather strategies stay purged (I1).

The strategies were removed in 5f7a127 (2026-07-05); their prod data files were
archived + deleted from the VM in P2-3 (2026-07-28, manifest on the VM under
~/app/data/archive/). This pin fails if code ever re-references those data
files, so a resurrected strategy can't silently reappear against an archive.
"""

from __future__ import annotations

from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"

_DEAD_DATA_FILES = (
    "tennis_scan_metrics.jsonl",
    "tennis_trades.jsonl",
    "tennis_bet_state.json",
    "tennis_paper_book.json",
    "weather_trades.jsonl",
)


def test_src_tree_has_no_dead_strategy_data_refs():
    offenders = []
    for path in _SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for name in _DEAD_DATA_FILES:
            if name in text:
                offenders.append(f"{path.relative_to(_SRC)}: {name}")
    assert offenders == [], (
        "references to deleted P2-3 strategy data files found: " + "; ".join(offenders)
    )
