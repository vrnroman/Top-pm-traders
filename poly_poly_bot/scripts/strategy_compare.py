#!/usr/bin/env python3
"""CLI for the A-vs-B race comparison — run any time for the current standings.

    python scripts/strategy_compare.py [A_LEDGER] [B_LEDGER]

Defaults to the configured ledgers. Prints the full verdict-memo format:
headline dollars with witnesses, the per-wallet routing table, governance
state per book, day-by-day cumulative PnL, and the verdict-validity stamp.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.config import CONFIG                                    # noqa: E402
from src.copy_trading import era_state                          # noqa: E402
from src.copy_trading.strategy_compare import compare, format_verdict  # noqa: E402


def main() -> int:
    a_path = sys.argv[1] if len(sys.argv) > 1 else CONFIG.copy_paper_ledger
    b_path = sys.argv[2] if len(sys.argv) > 2 else CONFIG.copy_paper_b_ledger
    # Same era floor the reporter uses, so the CLI and the daily snapshot agree.
    state_path = os.path.join(CONFIG.data_dir, "ab_race_state.json")
    cmp_ = compare(a_path, b_path,
                   b_slippage_bps=CONFIG.copy_paper_b_slippage_bps,
                   era_floor=era_state.era_floor_ts(state_path))
    print(format_verdict(cmp_))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
