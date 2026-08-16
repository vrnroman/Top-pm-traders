"""The CLOB client's real wire contract, pinned.

Every fake in this repo encoded the contract wrongly, so 1061 green tests
happily described a snapshot function that returned None on every single live
call. These tests use the shapes probed against the production CLOB on
2026-08-16 and nothing else:

    get_price(token, BUY)  -> {"price": "0.67"}   # the BUY side of the book = best BID
    get_price(token, SELL) -> {"price": "0.68"}   # the SELL side       = best ASK
    get_midpoint(token)    -> {"mid": "0.675"}
    get_order_book(token)  -> {"bids": [...ascending...], "asks": [...descending...]}

Observed book for token 594357064218…: bids 0.01 → 0.67, asks 0.99 → 0.68.
So index 0 is the WORST price on each side.
"""

import pytest

from py_clob_client_v2.order_builder.constants import BUY, SELL

from src.copy_trading import market_price
from src.copy_trading.trade_executor import _get_market_snapshot


# The real book, verbatim in shape: bids ascending, asks descending.
LIVE_BOOK = {
    "bids": [{"price": "0.01", "size": "500"},
             {"price": "0.55", "size": "120"},
             {"price": "0.67", "size": "80"}],
    "asks": [{"price": "0.99", "size": "400"},
             {"price": "0.80", "size": "150"},
             {"price": "0.68", "size": "90"}],
}


class LiveShapeClient:
    """Speaks exactly what the production CLOB speaks."""

    def __init__(self, book=None):
        self._book = book if book is not None else LIVE_BOOK

    def get_price(self, token_id, side):
        prices = [float(x["price"]) for x in self._book["bids"]] if side == BUY \
            else [float(x["price"]) for x in self._book["asks"]]
        best = max(prices) if side == BUY else min(prices)
        return {"price": f"{best:.2f}"}

    def get_midpoint(self, token_id):
        bid = max(float(x["price"]) for x in self._book["bids"])
        ask = min(float(x["price"]) for x in self._book["asks"])
        return {"mid": f"{(bid + ask) / 2:.4f}"}

    def get_order_book(self, token_id):
        return self._book


@pytest.fixture(autouse=True)
def _clear_cache():
    market_price._snapshot_cache.clear()
    yield
    market_price._snapshot_cache.clear()


# --------------------------------------------------------------------------- #
# get_price returns an object, and `side` names the side of the BOOK
# --------------------------------------------------------------------------- #

def test_price_is_unwrapped_from_the_json_object():
    assert market_price._price_of({"price": "0.68"}) == 0.68
    assert market_price._price_of({"mid": "0.675"}) == 0.675
    # a bare string still works, so a client change either way is survivable
    assert market_price._price_of("0.42") == 0.42
    assert market_price._price_of(None) is None
    assert market_price._price_of({"nope": 1}) is None


def test_snapshot_reads_the_live_shapes_and_the_right_sides():
    snap = market_price.fetch_market_snapshot(LiveShapeClient(), "tok")
    assert snap is not None, "this returned None on EVERY live call before the fix"
    # side=BUY is the buy side of the book, i.e. the best bid.
    assert snap.best_bid == 0.67
    assert snap.best_ask == 0.68
    assert snap.best_bid < snap.best_ask, "an inverted mapping crosses the book"
    assert snap.midpoint == pytest.approx(0.675)
    assert snap.spread_bps == pytest.approx(148, abs=2)


def test_a_bare_float_would_have_raised_before():
    # Guards the actual production failure: float({"price": "0.68"}).
    with pytest.raises(TypeError):
        float({"price": "0.68"})


# --------------------------------------------------------------------------- #
# The order book is not sorted the way index 0 assumes
# --------------------------------------------------------------------------- #

def test_index_zero_is_the_worst_price_on_each_side():
    """Pins the ordering fact the live path used to get backwards."""
    assert (float(LIVE_BOOK["bids"][0]["price"]),
            float(LIVE_BOOK["asks"][0]["price"])) == (0.01, 0.99)
    assert (max(float(b["price"]) for b in LIVE_BOOK["bids"]),
            min(float(a["price"]) for a in LIVE_BOOK["asks"])) == (0.67, 0.68)


@pytest.mark.asyncio
async def test_live_executor_snapshot_is_the_real_top_of_book():
    snap = await _get_market_snapshot(LiveShapeClient(), "tok")
    assert snap is not None
    assert snap["best_bid"] == 0.67
    assert snap["best_ask"] == 0.68
    assert snap["midpoint"] == pytest.approx(0.675)
    # 0.01/0.99 would have been a 19600bps spread and rejected every copy.
    assert snap["spread_bps"] < 200


@pytest.mark.asyncio
async def test_a_copy_order_is_priced_at_the_ask_not_the_worst_ask():
    from src.copy_trading.order_executor import quote_copy_order
    snap = await _get_market_snapshot(LiveShapeClient(), "tok")
    price = quote_copy_order("BUY", 0.68, snap)
    assert price == 0.68, "a 0.99 quote for a 0.68 token is a 46% overpay"


@pytest.mark.asyncio
async def test_empty_sides_do_not_invent_a_top_of_book():
    snap = await _get_market_snapshot(LiveShapeClient({"bids": [], "asks": []}), "tok")
    assert snap is None


def test_shadow_quote_records_against_the_live_shapes(tmp_path, monkeypatch):
    """End to end: the measurement produces a row from a real-shaped client."""
    from src.copy_trading import shadow_quote
    monkeypatch.setattr(shadow_quote.CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(shadow_quote, "SECOND_SAMPLE_DELAY_S", 0.0)

    row = shadow_quote.sample_trade(LiveShapeClient(), {
        "copy_id": "c1", "target": "0xw", "token_id": "tok",
        "their_price": 0.60, "their_ts": 100.0, "detected_at": 130.0,
    })
    assert row is not None, "the observer wrote nothing at all in production"
    assert row["our_price"] == 0.68           # the real ask
    assert row["penalty_bps"] == 1333         # 0.68 vs their 0.60
    assert row["notify_latency_s"] == 30.0
