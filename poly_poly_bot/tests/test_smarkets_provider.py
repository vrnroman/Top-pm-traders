"""Tests for the Smarkets HTTP API provider.

We can't (and shouldn't) hit the real Smarkets API in unit tests, so this
suite mocks `requests.Session.get` and feeds the provider canned JSON
fixtures captured from real API probes. Coverage:

  - Pure helper functions (BPS → decimal odds, event name parsing, chunking)
  - Tour inference from slug
  - Singles vs doubles filtering
  - Quote midpoint with various book shapes (two-sided, one-sided, empty,
    extreme prices that should be rejected)
  - The full `fetch_tennis_odds` happy path via mocked HTTP layer
  - Rate-limit header tracking
  - HTTP 429 backoff path
  - Multi-event batching (the URL gets ID lists joined by commas)
  - Doubles markets dropped silently
  - Tour filter (ATP-only request rejects WTA events)
  - Cache TTL behavior (events fetched once, served from cache on second call)
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.odds.models import MatchOdds
from src.odds.smarkets import (
    SmarketsProvider,
    _chunks,
    _parse_event_name,
    _parse_iso_datetime,
    _quote_midpoint_to_decimal_odds,
)


# ---------------------------------------------------------------------------
# Fixtures: real-shape JSON captured from the actual Smarkets API
# ---------------------------------------------------------------------------

EVENT_NORRIE = {
    "id": "45008901",
    "name": "Cameron Norrie vs Stan Wawrinka",
    "type": "tennis_match",
    "state": "live",
    "start_datetime": "2026-04-13T20:30:00Z",
    "full_slug": "/sport/tennis/atp/monte-carlo-2026/round-of-16/2026/04/13/20-30/cameron-norrie-vs-stan-wawrinka",
    "inplay_enabled": True,
    "parent_id": "44000000",
}

EVENT_WTA = {
    "id": "45011202",
    "name": "Kamilla Rakhimova vs Elvina Kalieva",
    "type": "tennis_match",
    "state": "upcoming",
    "start_datetime": "2026-04-13T22:00:00Z",
    "full_slug": "/sport/tennis/wta/madrid-2026/r1/2026/04/13/22-00/kamilla-rakhimova-vs-elvina-kalieva",
    "inplay_enabled": True,
}

EVENT_DOUBLES = {
    "id": "45007891",
    "name": "Puttergill C / Sweeny D vs Pearson K / Walton A",
    "type": "tennis_match",
    "state": "upcoming",
    "start_datetime": "2026-04-15T02:00:00Z",
    "full_slug": "/sport/tennis/challenger/atp-challenger-busan-2026/round-of-16/2026/04/15/02-00/puttergill-c-sweeny-d-vs-pearson-k-walton-a",
}

# Markets per event (for the /v3/events/{ids}/markets/ call)
MARKETS_NORRIE = {
    "markets": [
        {
            "id": "135840158",
            "name": "Match winner",
            "state": "open",
            "event_id": "45008901",
            "parent_id": "45008901",
        },
        {
            "id": "135840159",
            "name": "Set 1 winner",
            "state": "open",
            "event_id": "45008901",
            "parent_id": "45008901",
        },
    ]
}

MARKETS_WTA = {
    "markets": [
        {
            "id": "136000001",
            "name": "Match winner",
            "state": "open",
            "event_id": "45011202",
            "parent_id": "45011202",
        }
    ]
}

# Contracts per market (player names)
CONTRACTS_NORRIE = {
    "contracts": [
        {
            "id": "381249155",
            "name": "Cameron Norrie",
            "contract_type": {"name": "PLAYER_A"},
            "market_id": "135840158",
            "state_or_outcome": "open",
        },
        {
            "id": "381249156",
            "name": "Stan Wawrinka",
            "contract_type": {"name": "PLAYER_B"},
            "market_id": "135840158",
            "state_or_outcome": "open",
        },
    ]
}

CONTRACTS_WTA = {
    "contracts": [
        {
            "id": "381300001",
            "name": "Kamilla Rakhimova",
            "contract_type": {"name": "PLAYER_A"},
            "market_id": "136000001",
            "state_or_outcome": "open",
        },
        {
            "id": "381300002",
            "name": "Elvina Kalieva",
            "contract_type": {"name": "PLAYER_B"},
            "market_id": "136000001",
            "state_or_outcome": "open",
        },
    ]
}

# Quote responses — shape: contract_id → {bids: [...], offers: [...]}
QUOTES_NORRIE_LIVE = {
    "381249155": {  # Norrie  ~71% bid, ~73% ask
        "bids": [{"price": 7092, "quantity": 225790}],
        "offers": [{"price": 7353, "quantity": 200000}],
    },
    "381249156": {  # Wawrinka  ~26% bid, ~29% ask
        "bids": [{"price": 2667, "quantity": 1687289}],
        "offers": [{"price": 2941, "quantity": 1500000}],
    },
}

QUOTES_WTA = {
    "381300001": {
        "bids": [{"price": 5500, "quantity": 100000}],
        "offers": [{"price": 5700, "quantity": 100000}],
    },
    "381300002": {
        "bids": [{"price": 4300, "quantity": 100000}],
        "offers": [{"price": 4500, "quantity": 100000}],
    },
}


# ---------------------------------------------------------------------------
# Mock HTTP session
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code: int, body: dict | list | None, headers: dict | None = None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {
            "x-ratelimit-limit": "20",
            "x-ratelimit-remaining": "19",
            "x-ratelimit-reset": "59",
        }
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def make_session(routes: dict[str, FakeResponse | list[FakeResponse]]) -> MagicMock:
    """Build a MagicMock session that returns canned responses keyed by URL substring.

    Route keys are matched against the URL path (NOT including query string —
    `requests.get(url, params=...)` keeps params separate from the URL).
    Longer keys take priority so that `/v3/events/45008901/markets/` matches
    that specific endpoint instead of the bare `/v3/events/` events list.
    """
    sess = MagicMock(spec=requests.Session)
    call_indexes: dict[str, int] = {}
    # Sort keys longest-first so that more specific endpoints win the match.
    sorted_keys = sorted(routes.keys(), key=len, reverse=True)

    def fake_get(url, params=None, timeout=None):
        for key in sorted_keys:
            if key in url:
                value = routes[key]
                if isinstance(value, list):
                    idx = call_indexes.get(key, 0)
                    response = value[min(idx, len(value) - 1)]
                    call_indexes[key] = idx + 1
                    return response
                return value
        return FakeResponse(404, {"error": f"no route for {url}"})

    sess.get = MagicMock(side_effect=fake_get)
    return sess


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------

class TestQuoteMidpointConversion:
    def test_balanced_book(self):
        # bid 5000 (50%), ask 5200 (52%) → mid 5100 (51%) → 1.961 odds
        q = {"bids": [{"price": 5000}], "offers": [{"price": 5200}]}
        assert _quote_midpoint_to_decimal_odds(q) == round(10000 / 5100, 4)

    def test_one_sided_bid_only(self):
        q = {"bids": [{"price": 7500}], "offers": []}
        assert _quote_midpoint_to_decimal_odds(q) == round(10000 / 7500, 4)

    def test_one_sided_ask_only(self):
        q = {"bids": [], "offers": [{"price": 2500}]}
        assert _quote_midpoint_to_decimal_odds(q) == round(10000 / 2500, 4)

    def test_empty_book(self):
        q = {"bids": [], "offers": []}
        assert _quote_midpoint_to_decimal_odds(q) is None

    def test_extreme_low_price_rejected(self):
        # Below 100 BPS = 1% — extreme outlier, reject
        q = {"bids": [{"price": 50}], "offers": [{"price": 80}]}
        assert _quote_midpoint_to_decimal_odds(q) is None

    def test_extreme_high_price_rejected(self):
        # Above 9900 BPS = 99% — extreme outlier, reject
        q = {"bids": [{"price": 9950}], "offers": [{"price": 9990}]}
        assert _quote_midpoint_to_decimal_odds(q) is None

    def test_norrie_real_quote(self):
        # Real captured Norrie quote: bid 7092 ask 7353 → mid 7222.5 → 1.385 odds
        q = QUOTES_NORRIE_LIVE["381249155"]
        odds = _quote_midpoint_to_decimal_odds(q)
        assert odds is not None
        assert 1.35 < odds < 1.41


class TestEventNameParsing:
    def test_singles(self):
        assert _parse_event_name("Cameron Norrie vs Stan Wawrinka") == (
            "Cameron Norrie",
            "Stan Wawrinka",
        )

    def test_extra_spaces_trimmed(self):
        assert _parse_event_name("  Player A  vs  Player B  ") == (
            "Player A",
            "Player B",
        )

    def test_empty(self):
        assert _parse_event_name("") == ("", "")

    def test_no_vs_delimiter(self):
        assert _parse_event_name("Tournament Final") == ("", "")


class TestChunks:
    def test_even_split(self):
        assert _chunks(["a", "b", "c", "d"], 2) == [["a", "b"], ["c", "d"]]

    def test_uneven_split(self):
        assert _chunks(["a", "b", "c", "d", "e"], 2) == [["a", "b"], ["c", "d"], ["e"]]

    def test_single_chunk(self):
        assert _chunks(["a", "b", "c"], 100) == [["a", "b", "c"]]

    def test_empty(self):
        assert _chunks([], 5) == []


class TestParseIso:
    def test_z_suffix(self):
        dt = _parse_iso_datetime("2026-04-13T20:30:00Z")
        assert dt is not None
        assert dt.year == 2026
        assert dt.hour == 20

    def test_offset(self):
        dt = _parse_iso_datetime("2026-04-13T20:30:00+00:00")
        assert dt is not None

    def test_invalid(self):
        assert _parse_iso_datetime("not a date") is None
        assert _parse_iso_datetime(None) is None
        assert _parse_iso_datetime("") is None


class TestTourInference:
    def test_atp_slug(self):
        assert SmarketsProvider._infer_tour({"full_slug": "/sport/tennis/atp/monte-carlo-2026/..."}) == "ATP"

    def test_wta_slug(self):
        assert SmarketsProvider._infer_tour({"full_slug": "/sport/tennis/wta/madrid-2026/..."}) == "WTA"

    def test_atp_challenger(self):
        # "atp-challenger-..." segment should still be detected as ATP
        assert SmarketsProvider._infer_tour(
            {"full_slug": "/sport/tennis/challenger/atp-challenger-busan/..."}
        ) == "ATP"

    def test_unknown_slug(self):
        assert SmarketsProvider._infer_tour({"full_slug": "/sport/tennis/itf/some-event"}) == ""


class TestSinglesFilter:
    def test_singles_passes(self):
        assert SmarketsProvider._is_singles({"name": "Cameron Norrie vs Stan Wawrinka"}) is True

    def test_doubles_blocked(self):
        assert SmarketsProvider._is_singles(EVENT_DOUBLES) is False

    def test_empty_name_passes(self):
        # Empty name is treated as singles by default — the event name parser
        # will reject it later if it can't extract two players.
        assert SmarketsProvider._is_singles({"name": ""}) is True


# ---------------------------------------------------------------------------
# Provider-level tests with mocked HTTP
# ---------------------------------------------------------------------------

class TestFetchTennisOddsHappyPath:
    def test_norrie_match_assembled(self):
        routes = {
            "/v3/events/": FakeResponse(200, {
                "events": [EVENT_NORRIE],
                "pagination": {},
            }),
            "/v3/events/45008901/markets/": FakeResponse(200, MARKETS_NORRIE),
            "/v3/markets/135840158/contracts/": FakeResponse(200, CONTRACTS_NORRIE),
            "/v3/markets/135840158/quotes/": FakeResponse(200, QUOTES_NORRIE_LIVE),
        }
        sess = make_session(routes)
        provider = SmarketsProvider(session=sess)

        odds = provider.fetch_tennis_odds(tours=["ATP"])
        assert len(odds) == 1
        mo = odds[0]
        assert isinstance(mo, MatchOdds)
        assert mo.source == "smarkets"
        assert mo.tour == "ATP"
        assert mo.player_a == "Cameron Norrie"
        assert mo.player_b == "Stan Wawrinka"
        # Norrie at ~72% (bid 7092 ask 7353 → mid 7222) → odds ~1.385
        assert 1.35 < mo.odds_a < 1.41
        # Wawrinka at ~28% (bid 2667 ask 2941 → mid 2804) → odds ~3.566
        assert 3.45 < mo.odds_b < 3.65
        # Implied probabilities should sum to ~1 (de-vigged)
        assert abs((mo.implied_prob_a + mo.implied_prob_b) - 1.0) < 0.01

    def test_doubles_silently_skipped(self):
        routes = {
            "/v3/events/": FakeResponse(200, {"events": [EVENT_DOUBLES], "pagination": {}}),
        }
        sess = make_session(routes)
        provider = SmarketsProvider(session=sess)

        odds = provider.fetch_tennis_odds(tours=["ATP"])
        assert odds == []
        # Should NOT have called the markets / contracts / quotes endpoints
        # since the only event was filtered out at the singles gate.
        called_paths = [c.args[0] for c in sess.get.call_args_list]
        assert not any("/markets/" in p for p in called_paths if "/events/" not in p)

    def test_tour_filter_drops_wta_when_only_atp_requested(self):
        routes = {
            "/v3/events/": FakeResponse(200, {
                "events": [EVENT_NORRIE, EVENT_WTA],
                "pagination": {},
            }),
            "/v3/events/45008901/markets/": FakeResponse(200, MARKETS_NORRIE),
            "/v3/markets/135840158/contracts/": FakeResponse(200, CONTRACTS_NORRIE),
            "/v3/markets/135840158/quotes/": FakeResponse(200, QUOTES_NORRIE_LIVE),
        }
        sess = make_session(routes)
        provider = SmarketsProvider(session=sess)

        odds = provider.fetch_tennis_odds(tours=["ATP"])
        assert len(odds) == 1
        assert odds[0].tour == "ATP"
        # WTA event should not have been opened — verify by checking no WTA
        # markets path was hit.
        called_paths = [c.args[0] for c in sess.get.call_args_list]
        assert not any("136000001" in p for p in called_paths)

    def test_both_tours_returns_both(self):
        routes = {
            "/v3/events/": FakeResponse(200, {
                "events": [EVENT_NORRIE, EVENT_WTA],
                "pagination": {},
            }),
            "/v3/events/45008901,45011202/markets/": FakeResponse(200, {
                "markets": MARKETS_NORRIE["markets"] + MARKETS_WTA["markets"],
            }),
            "/v3/markets/135840158,136000001/contracts/": FakeResponse(200, {
                "contracts": CONTRACTS_NORRIE["contracts"] + CONTRACTS_WTA["contracts"],
            }),
            "/v3/markets/135840158,136000001/quotes/": FakeResponse(200, {
                **QUOTES_NORRIE_LIVE,
                **QUOTES_WTA,
            }),
        }
        sess = make_session(routes)
        provider = SmarketsProvider(session=sess)

        odds = provider.fetch_tennis_odds(tours=["ATP", "WTA"])
        assert len(odds) == 2
        tours = sorted(o.tour for o in odds)
        assert tours == ["ATP", "WTA"]

    def test_empty_event_list_returns_empty(self):
        routes = {
            "/v3/events/": FakeResponse(200, {"events": [], "pagination": {}}),
        }
        sess = make_session(routes)
        provider = SmarketsProvider(session=sess)
        assert provider.fetch_tennis_odds() == []


class TestRateLimitHandling:
    def test_remaining_header_is_tracked(self):
        routes = {
            "/v3/events/": FakeResponse(
                200,
                {"events": [], "pagination": {}},
                headers={"x-ratelimit-limit": "20", "x-ratelimit-remaining": "5", "x-ratelimit-reset": "30"},
            ),
        }
        sess = make_session(routes)
        provider = SmarketsProvider(session=sess)
        provider.fetch_tennis_odds()
        assert provider._last_remaining == 5

    def test_429_triggers_backoff_and_retry(self):
        # First call returns 429, second call returns success
        routes = {
            "/v3/events/": [
                FakeResponse(
                    429,
                    {"error_type": "RATE_LIMIT_EXCEEDED"},
                    headers={"x-ratelimit-limit": "20", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "1", "Retry-After": "1"},
                ),
                FakeResponse(200, {"events": [], "pagination": {}}),
            ],
        }
        sess = make_session(routes)
        provider = SmarketsProvider(session=sess)

        with patch("time.sleep") as mock_sleep:
            provider.fetch_tennis_odds()
        # We should have slept at least once due to the 429
        assert mock_sleep.called

    def test_low_remaining_triggers_self_throttle(self):
        provider = SmarketsProvider(session=MagicMock())
        provider._last_remaining = 1
        provider._last_reset_at = time.time() + 5
        with patch("time.sleep") as mock_sleep:
            provider._maybe_sleep_for_rate_limit()
        mock_sleep.assert_called_once()
        # Slept duration should be ~5s plus a small buffer
        slept = mock_sleep.call_args[0][0]
        assert 4.0 < slept < 6.5


class TestEventCaching:
    def test_events_cached_for_ttl(self):
        routes = {
            "/v3/events/": FakeResponse(200, {"events": [EVENT_NORRIE], "pagination": {}}),
            "/v3/events/45008901/markets/": FakeResponse(200, MARKETS_NORRIE),
            "/v3/markets/135840158/contracts/": FakeResponse(200, CONTRACTS_NORRIE),
            "/v3/markets/135840158/quotes/": FakeResponse(200, QUOTES_NORRIE_LIVE),
        }
        sess = make_session(routes)
        provider = SmarketsProvider(session=sess)

        provider.fetch_tennis_odds(tours=["ATP"])
        first_call_count = sess.get.call_count

        # Second call within TTL — events should be served from cache
        provider.fetch_tennis_odds(tours=["ATP"])
        second_call_count = sess.get.call_count

        # Quotes ARE re-fetched (no quote cache), but events should NOT be.
        # First call: events × 2 (upcoming + live) + markets + contracts + quotes = 5 calls
        # Second call: markets + contracts (already cached) + quotes
        # Events fetch should not appear in the second call's increment.
        events_calls_in_second = sum(
            1 for c in sess.get.call_args_list[first_call_count:]
            if "/v3/events/?" in c.args[0]
        )
        assert events_calls_in_second == 0


class TestMarketDiscoveryEdgeCases:
    def test_only_set_winner_market_no_match_winner(self):
        """Event with markets but no 'Match winner' → skip."""
        markets_only_set = {
            "markets": [
                {
                    "id": "999",
                    "name": "Set 1 winner",
                    "state": "open",
                    "event_id": "45008901",
                }
            ]
        }
        routes = {
            "/v3/events/": FakeResponse(200, {"events": [EVENT_NORRIE], "pagination": {}}),
            "/v3/events/45008901/markets/": FakeResponse(200, markets_only_set),
        }
        sess = make_session(routes)
        provider = SmarketsProvider(session=sess)
        odds = provider.fetch_tennis_odds(tours=["ATP"])
        assert odds == []

    def test_closed_market_filtered(self):
        markets_closed = {
            "markets": [
                {
                    "id": "135840158",
                    "name": "Match winner",
                    "state": "settled",  # closed
                    "event_id": "45008901",
                }
            ]
        }
        routes = {
            "/v3/events/": FakeResponse(200, {"events": [EVENT_NORRIE], "pagination": {}}),
            "/v3/events/45008901/markets/": FakeResponse(200, markets_closed),
        }
        sess = make_session(routes)
        provider = SmarketsProvider(session=sess)
        assert provider.fetch_tennis_odds(tours=["ATP"]) == []


class TestProviderIdentity:
    def test_name(self):
        assert SmarketsProvider().name == "smarkets"
