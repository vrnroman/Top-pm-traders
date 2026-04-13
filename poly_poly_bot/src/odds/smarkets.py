"""Smarkets HTTP API v3 provider for sharp tennis odds.

Smarkets is a London-based betting exchange. Its public REST API serves the
same order book that backs the Smarkets website, with no special API key /
account / payment required for read-only quote access. Empirical testing
shows the unauthenticated quote endpoint returns essentially real-time
(sub-second tick) prices for in-play tennis matches; the "delayed" warning in
their OpenAPI spec applies only to historical/archived data, not live quotes.

Why we use this instead of The Odds API
---------------------------------------
The Odds API serves Pinnacle's *closing* prices, frozen the moment a match
goes in-play. That makes it useless for the tennis arb thesis ("Polymarket
lags sharp books during live matches"). Smarkets, being an exchange, has no
"close" — its price keeps updating tick-by-tick as long as the order book
exists. Comparing the live Smarkets mid against the live Polymarket price
during a match is the right shape of signal.

Why REST polling and not the legacy streaming SDK
-------------------------------------------------
Smarkets had a TCP+protobuf streaming SDK (`smk_python_sdk`) that was
archived in December 2019 and only supports Python ≤3.5. It is not
maintained and probably won't run cleanly on the bot's Python 3.12
container. The current public-facing protocol is the HTTP API v3 documented
at https://docs.smarkets.com/, which is REST polling (no WebSocket / SSE).
With batched market_ids (up to 200 per call) and the 20 req/min unauth rate
limit, we can refresh ~50 active tennis markets every 5 seconds — close
enough to "real-time" for the arb use case.

Architecture
------------
Each scan call does (at most) three HTTP requests:
  1. GET /v3/events/?type=tennis_match&state=upcoming + GET state=live
     → list of all upcoming and in-play tennis events (cached 5 min)
  2. GET /v3/events/{event_ids}/markets/ joined with commas
     → list of markets per event; we keep only "Match winner" markets
  3. GET /v3/markets/{market_ids}/quotes/ batched ≤200 IDs per call
     → current bid/ask ladder per contract, parsed into MatchOdds

Player names come from the event name (split on " vs "). Doubles markets
(player names containing "/") are skipped. Tour (ATP/WTA/Challenger/ITF) is
inferred from the event's full_slug.

Rate limits and self-throttling
-------------------------------
Smarkets returns three headers on every response:
  x-ratelimit-limit        → 20 (unauth) or 50 (API user)
  x-ratelimit-remaining    → calls left in the current window
  x-ratelimit-reset        → seconds until the window rolls

When `remaining` drops below the safety floor, the provider sleeps until
reset before the next call. On HTTP 429 it backs off using the reset header.
Rate limits are tracked per-host so a long backoff doesn't starve other
endpoints (none in this case — only Smarkets).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import requests

from src.odds.base import OddsProvider
from src.odds.models import MatchOdds

logger = logging.getLogger("odds.smarkets")

SMARKETS_API_BASE = os.getenv("SMARKETS_API_BASE", "https://api.smarkets.com")

# Smarkets API constants
_EVENTS_PATH = "/v3/events/"
_EVENT_MARKETS_PATH = "/v3/events/{event_ids}/markets/"
_MARKET_CONTRACTS_PATH = "/v3/markets/{market_ids}/contracts/"
_MARKET_QUOTES_PATH = "/v3/markets/{market_ids}/quotes/"

# Per-call ID-batch caps. The OpenAPI schema declares maxItems=200 for the
# market_ids array; we leave some headroom and use 100 to keep URLs short.
_MAX_MARKET_IDS_PER_CALL = 100
_MAX_EVENT_IDS_PER_CALL = 50

# Rate-limit safety: sleep when remaining drops below this.
_RATE_LIMIT_SAFETY_FLOOR = 3
_DEFAULT_REQUEST_TIMEOUT = 15.0

# Cache TTLs
_EVENTS_CACHE_TTL_S = 300.0  # rebuild event list every 5 minutes
_CONTRACTS_CACHE_TTL_S = 86400.0  # contract metadata never changes mid-match

# Tennis market types we care about. Smarkets uses "Match winner" for
# singles head-to-heads; everything else (set winner, total games, etc.) is
# noise for the arb strategy.
_MATCH_WINNER_MARKET_NAMES = {
    "match winner",
    "match odds",
    "to win match",
    "winner",
}


class SmarketsProvider(OddsProvider):
    """Fetch real-time tennis match-odds from Smarkets' public REST API.

    No authentication, no API key, no account required. The unauthenticated
    REST endpoints return the same tick-resolution data as authenticated
    callers receive (verified empirically against in-play matches).
    """

    def __init__(self, base_url: str | None = None, session: requests.Session | None = None):
        self._base = (base_url or SMARKETS_API_BASE).rstrip("/")
        self._session = session or requests.Session()
        # In-memory caches
        self._events_cache: list[dict] | None = None
        self._events_cache_at: float = 0.0
        # contract_id → {"name": "Player A", "contract_type": "PLAYER_A"}
        self._contracts_cache: dict[str, dict] = {}
        self._contracts_cache_at: float = 0.0
        # Track last rate-limit window seen so we can self-throttle pre-emptively
        self._last_remaining: int | None = None
        self._last_reset_at: float = 0.0

    # ------------------------------------------------------------------
    # OddsProvider interface
    # ------------------------------------------------------------------
    @property
    def name(self) -> str:
        return "smarkets"

    def fetch_tennis_odds(self, tours: list[str] | None = None) -> list[MatchOdds]:
        """Single scan cycle: discover events, batch-fetch quotes, assemble MatchOdds.

        `tours` filters the result to one or more of {"ATP", "WTA"}; markets
        on Challenger / ITF / lower-tier tours are dropped because they don't
        have meaningful Polymarket coverage anyway.
        """
        target_tours = {t.upper() for t in (tours or ["ATP", "WTA"])}

        # 1. discover upcoming + live tennis events
        events = self._fetch_tennis_events(use_cache=True)
        if not events:
            logger.info("Smarkets: no tennis events found")
            return []

        # 2. tour-filter and drop doubles
        usable_events = [
            ev for ev in events
            if self._infer_tour(ev) in target_tours
            and self._is_singles(ev)
        ]
        if not usable_events:
            logger.info(
                f"Smarkets: 0 usable singles events after tour filter "
                f"({len(events)} total fetched)"
            )
            return []

        # 3. fetch markets per event, keep only match-winner markets
        event_id_to_market = self._fetch_match_winner_markets([ev["id"] for ev in usable_events])
        if not event_id_to_market:
            logger.info("Smarkets: no match-winner markets discovered")
            return []

        # 4. fetch contract metadata for player names (cached aggressively)
        market_ids = list(event_id_to_market.values())
        self._refresh_contracts_cache(market_ids)

        # 5. batched quotes call
        quotes_by_contract = self._fetch_quotes(market_ids)
        if not quotes_by_contract:
            logger.info("Smarkets: quotes endpoint returned nothing")
            return []

        # 6. assemble MatchOdds objects
        results: list[MatchOdds] = []
        for ev in usable_events:
            mid = event_id_to_market.get(ev["id"])
            if not mid:
                continue
            mo = self._build_match_odds(ev, mid, quotes_by_contract)
            if mo is not None:
                results.append(mo)

        logger.info(
            f"Smarkets: {len(results)} match-odds pairs assembled "
            f"(rate budget: {self._last_remaining}/{self._last_reset_at - time.time():.0f}s remaining)"
        )
        return results

    # ------------------------------------------------------------------
    # Event discovery
    # ------------------------------------------------------------------
    def _fetch_tennis_events(self, use_cache: bool = True) -> list[dict]:
        """Fetch upcoming + live tennis events. Cached for `_EVENTS_CACHE_TTL_S`."""
        now = time.time()
        if (
            use_cache
            and self._events_cache is not None
            and now - self._events_cache_at < _EVENTS_CACHE_TTL_S
        ):
            return self._events_cache

        all_events: list[dict] = []
        for state in ("upcoming", "live"):
            try:
                evs = self._paginated_events(state=state)
                all_events.extend(evs)
            except Exception as exc:
                logger.warning(f"Smarkets event fetch failed (state={state}): {exc}")

        # Dedupe by id (an event briefly straddles upcoming → live)
        seen: set[str] = set()
        deduped: list[dict] = []
        for ev in all_events:
            eid = str(ev.get("id", ""))
            if not eid or eid in seen:
                continue
            seen.add(eid)
            deduped.append(ev)

        self._events_cache = deduped
        self._events_cache_at = now
        return deduped

    def _paginated_events(self, state: str) -> list[dict]:
        """Walk Smarkets' cursor-based pagination for tennis events in one state."""
        events: list[dict] = []
        params = {
            "type": "tennis_match",
            "state": state,
            "limit": 200,
            "include_hidden": "false",
        }
        for _ in range(20):  # safety cap: 4000 events max per state
            data = self._get_json(_EVENTS_PATH, params=params)
            if not isinstance(data, dict):
                break
            batch = data.get("events") or []
            if not batch:
                break
            events.extend(batch)
            pagination = data.get("pagination") or {}
            next_cursor = pagination.get("next_page")
            if not next_cursor:
                break
            # next_page is a query string fragment like "?state=upcoming&...&pagination_last_id=X"
            # Parse it minimally: extract pagination_last_id and reuse the same params.
            if "pagination_last_id=" in next_cursor:
                last_id = next_cursor.split("pagination_last_id=")[-1].split("&")[0]
                params["pagination_last_id"] = last_id
            else:
                break
        return events

    # ------------------------------------------------------------------
    # Markets per event
    # ------------------------------------------------------------------
    def _fetch_match_winner_markets(self, event_ids: list[str]) -> dict[str, str]:
        """For each event_id, return the Match Winner market_id (or skip).

        Smarkets allows joining multiple event_ids with commas in the path:
        `/v3/events/45007891,45007892,.../markets/`. We batch in chunks of
        `_MAX_EVENT_IDS_PER_CALL` to keep the URL under the server's path
        length cap (~8KB).
        """
        result: dict[str, str] = {}
        for chunk in _chunks(event_ids, _MAX_EVENT_IDS_PER_CALL):
            joined = ",".join(chunk)
            path = _EVENT_MARKETS_PATH.format(event_ids=joined)
            try:
                data = self._get_json(path)
            except Exception as exc:
                logger.debug(f"Smarkets markets fetch failed for {len(chunk)} events: {exc}")
                continue
            if not isinstance(data, dict):
                continue
            for m in data.get("markets") or []:
                name = (m.get("name") or "").strip().lower()
                if name not in _MATCH_WINNER_MARKET_NAMES:
                    continue
                state = (m.get("state") or "").lower()
                if state and state not in ("open", "live"):
                    continue
                event_id = str(m.get("event_id") or m.get("parent_id") or "")
                market_id = str(m.get("id") or "")
                if event_id and market_id and event_id not in result:
                    result[event_id] = market_id
        return result

    # ------------------------------------------------------------------
    # Contracts (player metadata)
    # ------------------------------------------------------------------
    def _refresh_contracts_cache(self, market_ids: list[str]) -> None:
        """Populate `_contracts_cache` with contract → player-name mapping.

        Uses the per-market `/contracts/` endpoint, batched. Cached for
        `_CONTRACTS_CACHE_TTL_S` because contract IDs and player names don't
        change mid-tournament (occasionally a player is replaced by a
        Lucky Loser, but that's rare enough to tolerate one stale scan).
        """
        now = time.time()
        if self._contracts_cache and now - self._contracts_cache_at < _CONTRACTS_CACHE_TTL_S:
            # Existing cache is fresh — only fetch contracts we don't have yet.
            cached_market_ids = {c.get("market_id") for c in self._contracts_cache.values()}
            missing = [m for m in market_ids if m not in cached_market_ids]
            if not missing:
                return
            target = missing
        else:
            self._contracts_cache.clear()
            target = market_ids
            self._contracts_cache_at = now

        for chunk in _chunks(target, _MAX_MARKET_IDS_PER_CALL):
            joined = ",".join(chunk)
            path = _MARKET_CONTRACTS_PATH.format(market_ids=joined)
            try:
                data = self._get_json(path)
            except Exception as exc:
                logger.debug(f"Smarkets contracts fetch failed: {exc}")
                continue
            if not isinstance(data, dict):
                continue
            for c in data.get("contracts") or []:
                cid = str(c.get("id") or "")
                if not cid:
                    continue
                self._contracts_cache[cid] = {
                    "name": (c.get("name") or "").strip(),
                    "contract_type": ((c.get("contract_type") or {}).get("name") or "").strip(),
                    "market_id": str(c.get("market_id") or ""),
                }

    # ------------------------------------------------------------------
    # Live quotes
    # ------------------------------------------------------------------
    def _fetch_quotes(self, market_ids: list[str]) -> dict[str, dict]:
        """Batched quote fetch. Returns flat `contract_id → {bids, offers}`."""
        all_quotes: dict[str, dict] = {}
        for chunk in _chunks(market_ids, _MAX_MARKET_IDS_PER_CALL):
            joined = ",".join(chunk)
            path = _MARKET_QUOTES_PATH.format(market_ids=joined)
            try:
                data = self._get_json(path)
            except Exception as exc:
                logger.debug(f"Smarkets quotes fetch failed: {exc}")
                continue
            if not isinstance(data, dict):
                continue
            for cid, q in data.items():
                if isinstance(q, dict):
                    all_quotes[str(cid)] = q
        return all_quotes

    # ------------------------------------------------------------------
    # Assemble MatchOdds for one event
    # ------------------------------------------------------------------
    def _build_match_odds(
        self,
        event: dict,
        market_id: str,
        quotes_by_contract: dict[str, dict],
    ) -> MatchOdds | None:
        """Combine event metadata + quotes + contract names into a MatchOdds."""
        # Find the two contracts (PLAYER_A / PLAYER_B) belonging to this market
        player_a_cid: str | None = None
        player_b_cid: str | None = None
        player_a_name: str = ""
        player_b_name: str = ""
        for cid, c in self._contracts_cache.items():
            if c.get("market_id") != market_id:
                continue
            ctype = c.get("contract_type", "")
            if ctype == "PLAYER_A":
                player_a_cid = cid
                player_a_name = c.get("name") or ""
            elif ctype == "PLAYER_B":
                player_b_cid = cid
                player_b_name = c.get("name") or ""
        if not (player_a_cid and player_b_cid):
            return None

        qa = quotes_by_contract.get(player_a_cid)
        qb = quotes_by_contract.get(player_b_cid)
        if not (qa and qb):
            return None

        odds_a = _quote_midpoint_to_decimal_odds(qa)
        odds_b = _quote_midpoint_to_decimal_odds(qb)
        if odds_a is None or odds_b is None:
            return None

        # Player names: prefer event-level "X vs Y" parse (matches Smarkets'
        # canonical singles format) and fall back to the contract names if
        # the event name parsing fails.
        ev_name = event.get("name") or ""
        a_from_event, b_from_event = _parse_event_name(ev_name)
        if a_from_event and b_from_event:
            player_a_name = a_from_event
            player_b_name = b_from_event

        if not (player_a_name and player_b_name):
            return None

        match_time = _parse_iso_datetime(event.get("start_datetime"))
        tour = self._infer_tour(event)
        tournament = self._infer_tournament_name(event) or f"{tour} Tennis"

        return MatchOdds.from_decimal_odds(
            source="smarkets",
            tournament=tournament,
            tour=tour,
            player_a=player_a_name,
            player_b=player_b_name,
            odds_a=odds_a,
            odds_b=odds_b,
            match_time=match_time,
        )

    # ------------------------------------------------------------------
    # Helpers: tour / tournament / singles inference
    # ------------------------------------------------------------------
    @staticmethod
    def _is_singles(event: dict) -> bool:
        # Smarkets formats doubles as "Player1A / Player1B vs Player2A / Player2B".
        # Singles never have "/" in the event name.
        name = event.get("name") or ""
        return "/" not in name

    @staticmethod
    def _infer_tour(event: dict) -> str:
        """Return 'ATP', 'WTA', or '' based on the event's full_slug."""
        slug = (event.get("full_slug") or "").lower()
        if "/wta" in slug or "wta-" in slug:
            return "WTA"
        if "/atp" in slug or "atp-" in slug:
            return "ATP"
        # Some Challenger / ITF events still ATP/WTA-tagged; fall through:
        if "/men" in slug or "men-" in slug:
            return "ATP"
        if "/women" in slug or "women-" in slug:
            return "WTA"
        return ""

    @staticmethod
    def _infer_tournament_name(event: dict) -> str:
        """Pull a human-readable tournament name from the slug.

        Smarkets slugs look like:
          /sport/tennis/atp/monte-carlo-2026/round-of-16/.../alcaraz-vs-sinner
        We extract the segment immediately after the tour level.
        """
        slug = (event.get("full_slug") or "").lower()
        if not slug:
            return ""
        parts = [p for p in slug.split("/") if p]
        for marker in ("atp", "wta", "challenger", "itf", "exhibition"):
            if marker in parts:
                idx = parts.index(marker)
                if idx + 1 < len(parts):
                    return parts[idx + 1].replace("-", " ").title()
        return ""

    # ------------------------------------------------------------------
    # HTTP plumbing (rate-limit aware)
    # ------------------------------------------------------------------
    def _get_json(self, path: str, params: dict | None = None) -> Any:
        """GET wrapper that respects Smarkets' rate-limit headers.

        Sleeps proactively if `_last_remaining` is at the safety floor and
        the reset window hasn't elapsed yet. Backs off on HTTP 429.
        """
        self._maybe_sleep_for_rate_limit()
        url = self._base + path
        try:
            resp = self._session.get(url, params=params, timeout=_DEFAULT_REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise RuntimeError(f"GET {path} failed: {exc}") from exc

        # Update rate-limit tracking from headers
        self._update_rate_limit(resp)

        if resp.status_code == 429:
            wait = self._compute_rate_limit_wait(resp) or 5.0
            logger.warning(f"Smarkets 429 — sleeping {wait:.1f}s")
            time.sleep(min(wait, 60.0))
            # Retry once after backoff
            resp = self._session.get(url, params=params, timeout=_DEFAULT_REQUEST_TIMEOUT)
            self._update_rate_limit(resp)

        if resp.status_code >= 400:
            raise RuntimeError(f"Smarkets HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            return resp.json()
        except ValueError as exc:
            raise RuntimeError(f"Smarkets non-JSON response: {exc}") from exc

    def _update_rate_limit(self, resp: requests.Response) -> None:
        try:
            limit = resp.headers.get("x-ratelimit-limit")
            remaining = resp.headers.get("x-ratelimit-remaining")
            reset = resp.headers.get("x-ratelimit-reset")
            if remaining is not None:
                self._last_remaining = int(remaining)
            if reset is not None:
                self._last_reset_at = time.time() + int(reset)
            if limit is not None:
                logger.debug(
                    f"Smarkets rate: {remaining}/{limit} reset_in={reset}s"
                )
        except (TypeError, ValueError):
            pass

    def _maybe_sleep_for_rate_limit(self) -> None:
        if self._last_remaining is None:
            return
        if self._last_remaining > _RATE_LIMIT_SAFETY_FLOOR:
            return
        wait = max(0.0, self._last_reset_at - time.time())
        if wait > 0:
            logger.debug(
                f"Smarkets self-throttle: {self._last_remaining} remaining, "
                f"sleeping {wait:.1f}s"
            )
            time.sleep(min(wait + 0.5, 60.0))
            # Reset our local view; the next response will refresh it.
            self._last_remaining = None

    @staticmethod
    def _compute_rate_limit_wait(resp: requests.Response) -> float | None:
        # Prefer Retry-After if present, otherwise fall back to the reset header
        ra = resp.headers.get("Retry-After")
        if ra:
            try:
                return float(ra)
            except ValueError:
                pass
        reset = resp.headers.get("x-ratelimit-reset")
        if reset:
            try:
                return float(reset)
            except ValueError:
                pass
        return None


# ---------------------------------------------------------------------------
# Module helpers (pure functions — easy to unit-test)
# ---------------------------------------------------------------------------

def _chunks(items: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        return [items]
    return [items[i:i + size] for i in range(0, len(items), size)]


def _quote_midpoint_to_decimal_odds(quote: dict) -> float | None:
    """Convert a Smarkets quote (best bid + best ask in BPS) to decimal odds.

    Smarkets returns prices as integers in basis points out of 10000:
      - bid 5000 = 50.00% implied probability = 2.0 decimal odds
      - bid 7500 = 75.00% implied probability = 1.333 decimal odds

    This function takes the midpoint of best bid and best ask in BPS, then
    converts the resulting probability into decimal odds. If only one side
    is populated (a one-sided book), we use that side directly. Returns None
    when the book is fully empty or the midpoint is at the edge of the tick
    range (price < 100 or > 9900) which would yield extreme odds.
    """
    bids = quote.get("bids") or []
    offers = quote.get("offers") or []
    best_bid = bids[0].get("price") if bids and isinstance(bids[0], dict) else None
    best_ask = offers[0].get("price") if offers and isinstance(offers[0], dict) else None

    if best_bid is None and best_ask is None:
        return None
    if best_bid is None:
        mid_bps = best_ask
    elif best_ask is None:
        mid_bps = best_bid
    else:
        mid_bps = (best_bid + best_ask) / 2.0

    try:
        mid_bps = float(mid_bps)
    except (TypeError, ValueError):
        return None

    if mid_bps < 100 or mid_bps > 9900:
        # Very extreme prices indicate dead / one-sided books — skip rather
        # than produce a 0.01 / 100.0 odds outlier.
        return None

    # decimal_odds = 1 / probability = 10000 / mid_bps
    return round(10000.0 / mid_bps, 4)


def _parse_event_name(name: str) -> tuple[str, str]:
    """Split a Smarkets event name like 'Player A vs Player B' into two names."""
    if not name:
        return "", ""
    parts = name.split(" vs ")
    if len(parts) != 2:
        return "", ""
    return parts[0].strip(), parts[1].strip()


def _parse_iso_datetime(s: Any) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
