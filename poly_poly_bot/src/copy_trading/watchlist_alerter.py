"""Monitor-only alerter for tracked 1a/1b wallet trades.

When the bot runs without a CLOB client (no `PRIVATE_KEY`), the normal
executor path — which is where Tier-1a/1b orders + their Telegram
notifications live — never starts. The pattern detector still runs for
Strategy 1c, but 1a/1b wallets get silently dropped in `_monitor_drain_loop`.

This module closes that gap: a thin Telegram-only alerter that fires one
notification per *material* tracked-wallet trade, with three noise gates:

  1. Near-cert BUY gate — skip BUYs priced ≥ `near_cert_buy_price` (no insider
     edge in paying near-$1 for a $1 share).
  2. Minimum cash — skip individual fills worth < `min_cash_usd`. Big convictions
     arrive as one or two material fills plus many tiny scale-in fills; the
     tiny ones carry no extra signal.
  3. Per-(wallet, market, side) dedup cooldown — first qualifying fill fires;
     subsequent fills on the same market+side from the same wallet are
     suppressed for `dedup_cooldown_s`. This is what turns a 100-fill scale-in
     into a single notification.

All three gates are tunable via the `WATCHLIST_ALERT_*` env vars (see
`strategy_config.WatchlistAlertConfig`). The cache is in-memory and LRU-bounded.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Optional

from src.copy_trading.strategy_config import WATCHLIST_ALERT, StrategyTier
from src.logger import logger
from src.models import DetectedTrade


# (wallet_lower, condition_id_lower, side) -> last-alert epoch seconds.
# OrderedDict for LRU eviction when we cross max_dedup_entries.
_dedup_cache: "OrderedDict[tuple[str, str, str], float]" = OrderedDict()


def _dedup_key(trade: DetectedTrade) -> tuple[str, str, str]:
    return (
        (trade.trader_address or "").lower(),
        (getattr(trade, "condition_id", "") or "").lower(),
        (trade.side or "").upper(),
    )


def _should_suppress_duplicate(trade: DetectedTrade, now: float) -> bool:
    """Return True if a recent alert already covered this (wallet, market, side).

    Also prunes the cache to at most `max_dedup_entries` entries (LRU).
    """
    key = _dedup_key(trade)
    last = _dedup_cache.get(key)
    if last is not None and (now - last) < WATCHLIST_ALERT.dedup_cooldown_s:
        # Touch the entry so the LRU treats it as fresh — otherwise a busy
        # key would get evicted while still inside its cooldown window.
        _dedup_cache.move_to_end(key)
        return True
    _dedup_cache[key] = now
    _dedup_cache.move_to_end(key)
    while len(_dedup_cache) > WATCHLIST_ALERT.max_dedup_entries:
        _dedup_cache.popitem(last=False)
    return False


def _is_near_cert_buy(trade: DetectedTrade) -> bool:
    """BUY into an outcome already priced as a near-lock — no insider edge."""
    return (
        (trade.side or "").upper() == "BUY"
        and trade.price > 0
        and trade.price >= WATCHLIST_ALERT.near_cert_buy_price
    )


def _cash_value(trade: DetectedTrade) -> float:
    """USDC value committed by the fill.

    On Polymarket the feed's `size` is the number of shares and `price` is
    per-share. The dollar exposure of the fill is `size * price`. We guard
    against a zero-price feed (which would yield $0 for every fill and
    short-circuit the min-size gate) by falling back to raw size.
    """
    if trade.price > 0:
        return trade.size * trade.price
    return trade.size


async def maybe_alert_watchlist_trade(
    trade: DetectedTrade,
    tier: StrategyTier,
) -> bool:
    """Fire a Telegram alert for a tracked-wallet trade, gated by noise filters.

    Returns True when an alert was sent, False when suppressed. Safe to call
    on every drained trade — this is the monitor-mode equivalent of the
    executor's copy path.
    """
    if _is_near_cert_buy(trade):
        return False

    cash = _cash_value(trade)
    if cash < WATCHLIST_ALERT.min_cash_usd:
        return False

    now = time.time()
    if _should_suppress_duplicate(trade, now):
        return False

    try:
        from src.copy_trading.telegram_notifier import _send_message, _escape_html
    except Exception as exc:
        logger.warn(f"[watchlist] telegram import failed: {exc}")
        return False

    # Resolve the PM event slug via the geo cache if the market happens to be
    # in there. Non-geo markets (sports, crypto, etc.) won't be cached, so we
    # fall back to omitting the event link rather than producing a dead URL.
    event_url = ""
    cid = (getattr(trade, "condition_id", "") or "").lower()
    if cid:
        try:
            from src.copy_trading.geo_market_scanner import get_geo_market
            gm = get_geo_market(cid)
            if gm is not None:
                slug = gm.event_slug or gm.slug
                if slug:
                    event_url = f"https://polymarket.com/event/{slug}"
        except Exception:
            pass

    wallet = trade.trader_address or ""
    profile_url = f"https://polymarket.com/profile/{wallet}" if wallet else ""
    outcome = (getattr(trade, "outcome", "") or "").strip()
    side_line = f"Side: {trade.side}"
    if outcome:
        side_line += f" {outcome}"
    if trade.price > 0:
        side_line += f" @ {trade.price:.3f}"
    side_line += f"  |  Size: ${cash:,.0f}"

    lines = [
        f"📡 <b>Watchlist [{tier.upper()}] — {_escape_html(trade.market)}</b>",
    ]
    if event_url:
        lines.append(f"🔗 {event_url}")
    lines.append(side_line)
    lines.append(f"Wallet: <code>{_escape_html(wallet)}</code>")
    if profile_url:
        lines.append(f"👤 {profile_url}")

    try:
        await _send_message("\n".join(lines))
    except Exception as exc:
        logger.warn(f"[watchlist] send failed: {exc}")
        return False

    logger.info(
        f"[watchlist] alert fired tier={tier} ${cash:,.0f} {trade.side} "
        f"'{trade.market[:40]}' wallet={wallet[:12]}"
    )
    return True


def _reset_watchlist_alerter() -> None:
    """Clear the dedup cache. Test-only."""
    _dedup_cache.clear()
