"""The counterfactual book and the per-wallet speed breakdown.

Fixture-driven on purpose: the real `shadow-quotes.jsonl` is empty until the
observer has been collecting on the VM for a while, and a surface that can
only be checked once production has data is a surface nobody checks.
"""

import json

from src.copy_trading import shadow_quote, virtual_ledger


def _write_ledger(tmp_path, rows):
    p = tmp_path / "ledger.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return str(p)


def _pos(copy_id, spent=50.0, their_price=0.50, entry_price=0.50, won=True,
         closed=True, **kw):
    shares = spent / entry_price
    payout = shares if won else 0.0
    row = {
        "copy_id": copy_id, "target": "0xw", "condition_id": "0xc",
        "token_id": "tok", "outcome_index": 0, "category": "sports",
        "their_price": their_price, "entry_price": entry_price,
        "shares": shares, "spent": spent, "drag_bps": 0,
        "opened_ts": 1000.0, "closed": closed, "won": won,
        "pnl": payout - spent, "ideal_pnl": payout - shares * their_price,
        "closed_ts": 2000.0, "exited_early": False, "cost_usd": 0.02,
        "ideal_cost_usd": 0.02,
    }
    row.update(kw)
    return row


# --------------------------------------------------------------------------- #
# The counterfactual
# --------------------------------------------------------------------------- #

def test_worse_real_price_means_fewer_shares_and_less_profit(tmp_path):
    # Paper filled at 0.50 (100 shares, wins $50). The real book was asking
    # 0.62, so the same $50 buys 80.6 shares and the same win pays less.
    path = _write_ledger(tmp_path, [_pos("c1", entry_price=0.50, won=True)])
    out = virtual_ledger.replay(path, quote_rows=[{"copy_id": "c1", "our_price": 0.62}])

    assert out["n_matched"] == 1
    assert out["paper_pnl"] == 50.0                  # 100 shares - $50
    assert round(out["real_pnl"], 2) == 30.65        # 80.65 shares - $50
    assert out["real_roi"] < out["paper_roi"]
    assert out["execution_drag_roi"] < 0             # execution cost us ROI
    assert out["counterfactual"] is True


def test_a_loss_is_a_loss_at_any_entry_price(tmp_path):
    # Losers lose the whole stake regardless of entry, so execution cannot
    # flatter them — a counterfactual that improved losers would be wrong.
    path = _write_ledger(tmp_path, [_pos("c1", won=False)])
    out = virtual_ledger.replay(path, quote_rows=[{"copy_id": "c1", "our_price": 0.62}])
    assert out["paper_pnl"] == -50.0
    assert out["real_pnl"] == -50.0


def test_a_better_real_price_helps(tmp_path):
    path = _write_ledger(tmp_path, [_pos("c1", entry_price=0.50, won=True)])
    out = virtual_ledger.replay(path, quote_rows=[{"copy_id": "c1", "our_price": 0.40}])
    assert out["real_pnl"] > out["paper_pnl"]
    assert out["execution_drag_roi"] > 0


def test_unquoted_and_unsettled_rows_are_excluded_and_counted(tmp_path):
    path = _write_ledger(tmp_path, [
        _pos("c1"),                       # settled + quoted -> counted
        _pos("c2"),                       # settled, no quote -> unmatched
        _pos("c3", closed=False),         # open -> not settled at all
    ])
    out = virtual_ledger.replay(path, quote_rows=[{"copy_id": "c1", "our_price": 0.55}])
    assert out["n_settled"] == 2          # c3 is open
    assert out["n_matched"] == 1
    assert out["n_unmatched"] == 1
    # Coverage is stated, so a partial join can never read as a whole book.
    assert out["coverage"] == 0.5


def test_early_exit_reprices_the_entry_only(tmp_path):
    # Book exited at 0.60 (100 shares -> $60 proceeds, +$10). At a real entry
    # of 0.55 the same $50 buys 90.9 shares, exited at the same 0.60.
    row = _pos("c1", entry_price=0.50, won=True)
    row["exited_early"] = True
    row["pnl"] = 10.0                      # proceeds 60 - spent 50
    path = _write_ledger(tmp_path, [row])
    out = virtual_ledger.replay(path, quote_rows=[{"copy_id": "c1", "our_price": 0.55}])
    assert round(out["real_pnl"], 2) == 4.55   # 90.91 * 0.60 - 50


def test_net_charges_the_books_own_cost_basis_and_not_the_spread_twice(tmp_path):
    path = _write_ledger(tmp_path, [_pos("c1", won=True)])
    out = virtual_ledger.replay(path, quote_rows=[{"copy_id": "c1", "our_price": 0.62}])
    # Only cost_usd (gas+fee) is charged: the spread is already inside the
    # real quote, and PaperPosition is explicit it is never re-charged.
    assert round(out["real_pnl"] - out["real_pnl_net"], 2) == 0.02


def test_empty_inputs_do_not_fabricate_a_number(tmp_path):
    path = _write_ledger(tmp_path, [])
    out = virtual_ledger.replay(path, quote_rows=[])
    assert out["n_matched"] == 0
    assert out["real_roi"] is None
    assert out["coverage"] is None


def test_era_floor_scopes_the_replay(tmp_path):
    path = _write_ledger(tmp_path, [
        _pos("old", opened_ts=100.0),
        _pos("new", opened_ts=5000.0),
    ])
    quotes = [{"copy_id": "old", "our_price": 0.6},
              {"copy_id": "new", "our_price": 0.6}]
    out = virtual_ledger.replay(path, quote_rows=quotes, min_opened_ts=1000.0)
    assert out["n_settled"] == 1
    assert out["n_matched"] == 1


# --------------------------------------------------------------------------- #
# Per-wallet breakdown
# --------------------------------------------------------------------------- #

def test_by_wallet_ranks_worst_penalty_first_and_flags_thin():
    rows = [
        {"target": "0xA", "penalty_bps": 500, "notify_latency_s": 30,
         "category": "sports", "detected_at": 10},
        {"target": "0xA", "penalty_bps": 700, "notify_latency_s": 50,
         "category": "sports", "detected_at": 20},
        {"target": "0xA", "penalty_bps": 600, "notify_latency_s": 40,
         "category": "crypto", "detected_at": 30},
        {"target": "0xB", "penalty_bps": 100, "notify_latency_s": 5,
         "category": "other", "detected_at": 40},
    ]
    out = shadow_quote.by_wallet(rows, min_n=3)
    assert [d["wallet"] for d in out] == ["0xa", "0xb"]   # worst first
    assert out[0]["penalty_p50_bps"] == 600
    assert out[0]["top_category"] == "sports"
    assert out[0]["thin"] is False
    # A one-sample wallet is still shown, but marked — hiding it would make a
    # sparse sample look complete.
    assert out[1]["thin"] is True
    assert out[1]["n"] == 1


def test_by_wallet_ignores_rows_with_no_target():
    assert shadow_quote.by_wallet([{"penalty_bps": 100}]) == []


def test_collecting_since_reports_the_oldest_sample():
    rows = [{"detected_at": 500}, {"detected_at": 200}, {"detected_at": 900}]
    assert shadow_quote.collecting_since(rows) == 200
    assert shadow_quote.collecting_since([]) is None
