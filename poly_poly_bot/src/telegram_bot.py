"""Unified Telegram bot for all three strategies.

Commands:
  /predict 11 Apr    — Run prediction for a specific date (Strategy #2)
  /predict           — Run prediction for default date (today + days_in_advance)
  /status            — Show bot status for all strategies
  /pnl               — Show P&L: realized + unrealized (all strategies)
  /history           — Show last 10 copy trades (Strategy #1)
  /takeprofit        — Close all positions with unrealized PnL > 30%
  /tennis            — Show current tennis divergences (Strategy #3)
  /help              — Show available commands
"""

import os
import re
import json
import time
import logging
import threading
from datetime import datetime, timedelta, timezone

import requests

from src.config import CONFIG

logger = logging.getLogger("telegram")

SGT = timezone(timedelta(hours=8))

# Take-profit threshold: close when unrealized PnL > this % of cost
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.30"))

# Polymarket fee (2%)
POLYMARKET_FEE = float(os.getenv("POLYMARKET_FEE", "0.02"))


def _esc(text: str) -> str:
    """Escape HTML special characters for Telegram."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


TELEGRAM_API = "https://api.telegram.org/bot{token}"

_poll_thread: threading.Thread | None = None
_stop_event = threading.Event()

# Callbacks set by main.py
on_predict_request = None   # Callable[[datetime], list[dict]]
on_sell_positions = None    # Callable[[list[dict]], list[dict]]
on_tennis_scan_request = None  # Callable[[], list[dict]]


def is_configured() -> bool:
    """Check if Telegram bot is configured."""
    return bool(CONFIG.telegram_bot_token) and bool(CONFIG.telegram_chat_id)


def send_message(text: str, parse_mode: str = "HTML"):
    """Send a message to the configured Telegram chat."""
    if not is_configured():
        return
    try:
        url = f"{TELEGRAM_API.format(token=CONFIG.telegram_bot_token)}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": CONFIG.telegram_chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }, timeout=10)
        if not resp.ok:
            logger.warning(f"Telegram send failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"Telegram send error: {e}")


def send_strategy2_signals(signals: list[dict], target_date: str):
    """Send Strategy #2 prediction results to Telegram."""
    if not signals:
        send_message(
            f"<b>Strategy #2 — Weather</b>\n"
            f"Target: {target_date}\n"
            f"No signals above {CONFIG.min_edge:.0%} edge threshold."
        )
        return

    lines = [
        f"<b>Strategy #2 — Weather Prediction</b>",
        f"Target: <b>{target_date}</b>",
        f"Mode: {'PREVIEW' if CONFIG.preview_mode else 'LIVE'}",
        f"Edge threshold: {CONFIG.min_edge:.0%} | Bet: ${CONFIG.bet_size:.0f}",
        "",
    ]

    total_ev = 0
    for s in signals:
        deg = "\u00b0F" if s.get("unit") == "fahrenheit" else "\u00b0C"
        emoji = "\U0001f7e2" if s["edge"] >= 0.10 else "\U0001f7e1"
        bucket = _esc(s['bucket_label'])
        lines.append(
            f"{emoji} <b>{_esc(s['city_name'])}</b> {bucket}{deg}\n"
            f"   Model: {s['model_prob']:.1%}  Market: {s['market_price']:.1%}  "
            f"Edge: <b>{s['edge']:+.1%}</b>  EV: ${s.get('expected_pnl', 0):.2f}"
        )
        total_ev += s.get("expected_pnl", 0)

    lines.append(f"\nTotal signals: {len(signals)} | Total EV: ${total_ev:.2f}")
    send_message("\n".join(lines))


def send_tennis_signals(signals: list[dict]):
    """Send Strategy #3 tennis arb signals to Telegram."""
    if not signals:
        return

    preview = signals[0].get("preview", True) if signals else True
    lines = [
        f"<b>{'[PREVIEW] ' if preview else ''}Strategy #3 — Tennis Arb</b>",
        f"Signals: {len(signals)} | Threshold: {CONFIG.tennis_min_divergence:.0%}",
        "",
    ]

    for s in signals:
        lines.append(
            f"[TENNIS] {_esc(s.get('tournament', ''))}: "
            f"{_esc(s['player_a'])} vs {_esc(s['player_b'])} — "
            f"Sharp: {s['sharp_prob']:.0%} / PM: {s['polymarket_price']:.0%} — "
            f"Edge: <b>{s['divergence']:.0%}</b> — "
            f"{s['side']} @ ${s['bet_size']:.0f}"
        )

    send_message("\n".join(lines))


def _load_s3_trades() -> list[dict]:
    """Load Strategy #3 tennis trade history."""
    history_path = os.path.join(CONFIG.data_dir, "tennis_trades.jsonl")
    if not os.path.exists(history_path):
        return []
    trades = []
    with open(history_path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    trades.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return trades


# --- Live price fetching ---

def _fetch_midpoint(token_id: str) -> float | None:
    """Fetch current midpoint price for a YES token from CLOB API."""
    try:
        resp = requests.get(
            f"{CONFIG.clob_api_url}/midpoint",
            params={"token_id": token_id},
            timeout=5,
        )
        if resp.ok:
            data = resp.json()
            mid = data.get("mid")
            if mid is not None:
                return float(mid)
    except Exception as e:
        logger.debug(f"Midpoint fetch failed for {token_id[:20]}...: {e}")
    return None


def _load_s2_trades() -> list[dict]:
    """Load Strategy #2 trade history."""
    history_path = os.path.join(CONFIG.data_dir, "weather_trades.jsonl")
    if not os.path.exists(history_path):
        return []
    trades = []
    with open(history_path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    trades.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return trades


def _load_s1_trades() -> list[dict]:
    """Load Strategy #1 copy trading trade history."""
    history_path = os.path.join(CONFIG.data_dir, "trade-history.jsonl")
    if not os.path.exists(history_path):
        return []
    trades = []
    with open(history_path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    trades.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return trades


def _enrich_with_live_prices(trades: list[dict]) -> list[dict]:
    """Add current_price and unrealized PnL to each trade."""
    for t in trades:
        token_id = t.get("clob_token_yes")
        entry_price = t.get("market_price") or 0
        cost = t.get("cost") or (t.get("bet_size", CONFIG.bet_size) * (1 + POLYMARKET_FEE))
        bet_size = t.get("bet_size", CONFIG.bet_size)

        if entry_price > 0:
            shares = bet_size / entry_price
        else:
            shares = 0

        t["shares"] = round(shares, 2)
        t["entry_price"] = entry_price
        t["cost"] = cost

        # Fetch live price
        current_price = None
        if token_id:
            current_price = _fetch_midpoint(token_id)

        if current_price is not None:
            t["current_price"] = current_price
            # If we sold now: revenue = shares * current_price, minus sell fee
            sell_revenue = shares * current_price * (1 - POLYMARKET_FEE)
            t["unrealized_pnl"] = round(sell_revenue - cost, 2)
            t["unrealized_pct"] = round((sell_revenue - cost) / cost, 4) if cost > 0 else 0
        else:
            t["current_price"] = None
            t["unrealized_pnl"] = None
            t["unrealized_pct"] = None

    return trades


# --- Command handlers ---

def _parse_date_from_text(text: str) -> datetime | None:
    """Parse date from telegram command text like '11 Apr' or '2026-04-11'."""
    text = text.strip()

    month_map = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        "january": 1, "february": 2, "march": 3, "april": 4,
        "june": 6, "july": 7, "august": 8, "september": 9,
        "october": 10, "november": 11, "december": 12,
    }

    m = re.match(r'(\d{1,2})\s+([A-Za-z]+)', text)
    if m:
        day = int(m.group(1))
        month_str = m.group(2).lower()
        month = month_map.get(month_str)
        if month:
            now = datetime.now(SGT)
            year = now.year
            try:
                dt = datetime(year, month, day)
                if dt.date() < now.date():
                    dt = datetime(year + 1, month, day)
                return dt
            except ValueError:
                pass

    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    return None


def _handle_command(text: str):
    """Process a telegram command."""
    text = text.strip()

    if text.startswith("/predict"):
        _handle_predict(text)
    elif text.startswith("/tennis"):
        _handle_tennis()
    elif text.startswith("/history"):
        _handle_history()
    elif text.startswith("/status"):
        _handle_status()
    elif text.startswith("/pnl"):
        _handle_pnl()
    elif text.startswith("/takeprofit"):
        _handle_takeprofit()
    elif text.startswith("/help") or text.startswith("/start"):
        _handle_help()
    else:
        return


def _handle_predict(text: str):
    """Handle /predict command."""
    parts = text.split(maxsplit=1)
    if len(parts) > 1:
        target_date = _parse_date_from_text(parts[1])
        if not target_date:
            send_message(f"Could not parse date: <code>{parts[1]}</code>\nFormat: <code>/predict 11 Apr</code>")
            return
    else:
        today = datetime.now(SGT).date()
        target_date = datetime(
            (today + timedelta(days=CONFIG.days_in_advance)).year,
            (today + timedelta(days=CONFIG.days_in_advance)).month,
            (today + timedelta(days=CONFIG.days_in_advance)).day,
        )

    date_str = target_date.strftime("%Y-%m-%d")
    send_message(f"Running prediction for <b>{date_str}</b>...")

    if on_predict_request:
        try:
            signals = on_predict_request(target_date)
            send_strategy2_signals(signals, date_str)
        except Exception as e:
            logger.exception("Prediction failed")
            send_message(f"Prediction failed: <code>{_esc(str(e))}</code>")
    else:
        send_message("Prediction handler not configured.")


def _handle_status():
    """Handle /status command — show status for all three strategies."""
    now = datetime.now(SGT)
    cities_list = [c.strip() for c in CONFIG.cities_to_bet.split(",")]
    tennis_tournaments = [t.strip() for t in CONFIG.tennis_tournaments.split(",")]

    lines = [
        f"<b>Bot Status</b>",
        f"Time: {now.strftime('%Y-%m-%d %H:%M SGT')}",
        f"Mode: {'PREVIEW' if CONFIG.preview_mode else 'LIVE'}",
        "",
    ]

    # Strategy #1 — Copy Trading
    lines.append(f"<b>Strategy #1 — Copy Traders</b>")
    if CONFIG.strategy1_enabled:
        lines.append("Status: \U0001f7e2 ENABLED")
        lines.append(f"Wallets tracked: {len(CONFIG.user_addresses)}")
        lines.append(f"Copy strategy: {CONFIG.copy_strategy}")
        lines.append(f"Copy size: ${CONFIG.copy_size:.0f}")
        # Try to show balance
        s1_trades = _load_s1_trades()
        if s1_trades:
            total_cost = sum((t.get("cost") or 0) for t in s1_trades)
            lines.append(f"Total deployed: ${total_cost:.2f}")
            lines.append(f"Total trades: {len(s1_trades)}")
    else:
        lines.append("Status: \u26aa DISABLED")

    lines.append("")
    lines.append(f"<b>Strategy #2 — Weather Betting</b>")
    if CONFIG.strategy2_enabled:
        lines.append("Status: \U0001f7e2 ENABLED")
        lines.append(f"Cities: {', '.join(cities_list)}")
        lines.append(f"Days ahead: {CONFIG.days_in_advance}")
        lines.append(f"Min edge: {CONFIG.min_edge:.0%} | Bet: ${CONFIG.bet_size:.0f}")
    else:
        lines.append("Status: \u26aa DISABLED")

    lines.append("")
    lines.append(f"<b>Strategy #3 — Tennis Arb</b>")
    if CONFIG.strategy3_enabled:
        lines.append("Status: \U0001f7e2 ENABLED")
        lines.append(f"Tournaments: {', '.join(tennis_tournaments)}")
        lines.append(f"Min divergence: {CONFIG.tennis_min_divergence:.0%}")
        lines.append(f"Scan interval: {CONFIG.tennis_scan_interval}s")
        lines.append(f"Max bet: ${CONFIG.tennis_max_bet_size:.0f}")
        tennis_trades = _load_s3_trades()
        if tennis_trades:
            lines.append(f"Total signals: {len(tennis_trades)}")
    else:
        lines.append("Status: \u26aa DISABLED")

    signals_dir = CONFIG.results_dir
    if os.path.isdir(signals_dir):
        signal_files = sorted(
            [f for f in os.listdir(signals_dir) if f.startswith("signals_")],
            reverse=True,
        )[:3]
        if signal_files:
            lines.append(f"\nRecent signals: {', '.join(signal_files)}")

    send_message("\n".join(lines))


def _handle_pnl():
    """Handle /pnl command — unified P&L report across all strategies."""
    lines = ["\U0001f4ca <b>P&amp;L Report</b>", ""]

    # Track totals for the summary
    grand_realized = 0.0
    grand_unrealized = 0.0
    grand_open_bets = 0

    # -- Strategy #1 (Copy Traders) --
    lines.append("<b>Strategy #1 — Copy Traders</b>")
    if CONFIG.strategy1_enabled:
        s1_trades = _load_s1_trades()
        s1_realized = sum((t.get("pnl") or 0) for t in s1_trades) if s1_trades else 0.0
        grand_realized += s1_realized
        lines.append(f"  Realized:    ${s1_realized:+.2f}")
        lines.append(f"  Unrealized:  $0.00")
        lines.append(f"  Open bets:   0")
    else:
        lines.append("  [disabled]")

    lines.append("")

    # -- Strategy #2 (Weather Betting) --
    lines.append("<b>Strategy #2 — Weather Betting</b>")
    trades = _load_s2_trades()

    if CONFIG.strategy2_enabled:
        if trades:
            send_message("\n".join(lines) + "\nFetching live prices...")

            # Enrich with live prices
            trades = _enrich_with_live_prices(trades)

            resolved = [t for t in trades if t.get("resolved")]
            open_positions = [t for t in trades if not t.get("resolved")]

            s2_realized = sum((t.get("pnl") or 0) for t in resolved)
            s2_unrealized = sum((t.get("unrealized_pnl") or 0) for t in open_positions
                                if t.get("unrealized_pnl") is not None)
            s2_open = len(open_positions)

            grand_realized += s2_realized
            grand_unrealized += s2_unrealized
            grand_open_bets += s2_open

            # Rebuild lines after interim message
            lines = ["\U0001f4ca <b>P&amp;L Report</b>", ""]

            # Re-add Strategy #1
            lines.append("<b>Strategy #1 \u2014 Copy Traders</b>")
            if CONFIG.strategy1_enabled:
                s1_trades_re = _load_s1_trades()
                s1_re = sum((t.get("pnl") or 0) for t in s1_trades_re) if s1_trades_re else 0.0
                lines.append(f"  Realized:    ${s1_re:+.2f}")
                lines.append("  Unrealized:  $0.00")
                lines.append("  Open bets:   0")
            else:
                lines.append("  [disabled]")
            lines.append("")

            lines.append("<b>Strategy #2 \u2014 Weather Betting</b>")
            lines.append(f"  Realized:    ${s2_realized:+.2f}")
            lines.append(f"  Unrealized:  ${s2_unrealized:+.2f}")
            lines.append(f"  Open bets:   {s2_open}")

            tp_candidates = sum(1 for t in open_positions
                                if t.get("unrealized_pct") is not None
                                and t.get("unrealized_pct") >= TAKE_PROFIT_PCT)
            if tp_candidates > 0:
                lines.append(f"  \U0001f3af {tp_candidates} position(s) above {TAKE_PROFIT_PCT:.0%} \u2014 /takeprofit")
        else:
            lines.append("  Realized:    $0.00")
            lines.append("  Unrealized:  $0.00")
            lines.append("  Open bets:   0")
    else:
        lines.append("  [disabled]")

    lines.append("")

    # -- Strategy #3 (Tennis Arb) --
    lines.append("<b>Strategy #3 \u2014 Tennis Arb</b>")
    if CONFIG.strategy3_enabled:
        tennis_trades = _load_s3_trades()
        if tennis_trades:
            total_bet = sum(t.get("bet_size", 0) for t in tennis_trades)
            preview_count = sum(1 for t in tennis_trades if t.get("preview"))
            live_count = len(tennis_trades) - preview_count
            avg_edge = sum(t.get("divergence", 0) for t in tennis_trades) / len(tennis_trades)

            lines.append(f"  Realized:    $0.00")
            lines.append(f"  Unrealized:  $0.00")
            lines.append(f"  Open bets:   {len(tennis_trades)} ({live_count} live, {preview_count} preview)")
            lines.append(f"  Total bet:   ${total_bet:.2f}")
            lines.append(f"  Avg edge:    {avg_edge:.1%}")

            grand_open_bets += len(tennis_trades)
        else:
            lines.append("  Realized:    $0.00")
            lines.append("  Unrealized:  $0.00")
            lines.append("  Open bets:   0")
    else:
        lines.append("  [disabled]")

    # -- Grand Total --
    lines.append("")
    lines.append("\u2500" * 14)
    lines.append("<b>TOTAL:</b>")
    lines.append(f"  Realized:    ${grand_realized:+.2f}")
    lines.append(f"  Unrealized:  ${grand_unrealized:+.2f}")
    lines.append(f"  Open bets:   {grand_open_bets}")
    net_pnl = grand_realized + grand_unrealized
    lines.append(f"  Net P&amp;L:     ${net_pnl:+.2f}")

    if CONFIG.preview_mode:
        lines.append(f"\n<i>PREVIEW MODE \u2014 positions are simulated</i>")

    send_message("\n".join(lines))


def _handle_history():
    """Handle /history command — show last 10 copy trades (Strategy #1)."""
    if not CONFIG.strategy1_enabled:
        send_message("Strategy #1 (Copy Trading) is disabled.")
        return

    trades = _load_s1_trades()
    if not trades:
        send_message("<b>Strategy #1 — Copy Trading History</b>\nNo trades yet.")
        return

    recent = trades[-10:]
    lines = [
        f"<b>Strategy #1 — Last {len(recent)} Copy Trades</b>",
        "",
    ]

    for t in reversed(recent):
        ts = t.get("timestamp", "?")
        if isinstance(ts, str) and len(ts) > 16:
            ts = ts[:16]
        market = _esc(t.get("market_question", t.get("market", "?"))[:40])
        side = t.get("side", "?")
        size = t.get("size_usd", t.get("cost", 0))
        price = t.get("price", 0)
        status = t.get("status", "?")
        pnl = t.get("pnl")

        pnl_str = f" | PnL: ${pnl:+.2f}" if pnl is not None else ""
        lines.append(
            f"<code>{ts}</code> {side} ${size:.1f} @ {price:.1%}\n"
            f"  {market}\n"
            f"  Status: {status}{pnl_str}"
        )

    send_message("\n".join(lines))


def _handle_takeprofit():
    """Close all positions with unrealized PnL > TAKE_PROFIT_PCT of cost."""
    trades = _load_s2_trades()
    open_positions = [t for t in trades if not t.get("resolved")]

    if not open_positions:
        send_message("No open positions to close.")
        return

    send_message(f"Checking {len(open_positions)} open position(s) for take-profit...")

    open_positions = _enrich_with_live_prices(open_positions)

    # Find candidates
    candidates = []
    for t in open_positions:
        unr_pct = t.get("unrealized_pct")
        if unr_pct is not None and unr_pct >= TAKE_PROFIT_PCT:
            candidates.append(t)

    if not candidates:
        send_message(
            f"No positions above {TAKE_PROFIT_PCT:.0%} take-profit threshold.\n\n"
            + _format_position_summary(open_positions)
        )
        return

    # Report what we'd close
    lines = [
        f"<b>Take Profit \u2014 {len(candidates)} position(s)</b>",
        f"Threshold: {TAKE_PROFIT_PCT:.0%} of cost",
        "",
    ]

    total_revenue = 0
    sell_orders = []
    for t in candidates:
        deg = "\u00b0F" if t.get("unit") == "fahrenheit" else "\u00b0C"
        bucket = _esc(t.get("bucket_label", "?"))
        city = _esc(t.get("city_name", t.get("city", "?")))
        current = t.get("current_price", 0)
        shares = t.get("shares", 0)
        cost = t.get("cost", 0)
        unr_pnl = t.get("unrealized_pnl", 0)
        unr_pct = t.get("unrealized_pct", 0)
        revenue = shares * current * (1 - POLYMARKET_FEE)
        total_revenue += revenue

        lines.append(
            f"\U0001f3af {city} {bucket}{deg} ({t.get('target_date', '?')})\n"
            f"   {shares:.1f} shares @ {t.get('entry_price', 0):.1%} \u2192 "
            f"{current:.1%} | PnL: ${unr_pnl:+.2f} ({unr_pct:+.0%})"
        )

        sell_orders.append({
            "tokenId": t.get("clob_token_yes"),
            "price": current,
            "size": shares,
            "side": "SELL",
            "meta": {
                "city": t.get("city_name", t.get("city")),
                "date": t.get("target_date"),
                "bucket": t.get("bucket_label"),
                "entry_price": t.get("entry_price"),
                "unrealized_pct": unr_pct,
            },
        })

    lines.append(f"\nTotal revenue: ~${total_revenue:.2f}")

    if CONFIG.preview_mode:
        lines.append(f"\n<i>PREVIEW MODE \u2014 orders NOT placed</i>")
        lines.append("Set PREVIEW_MODE=false to enable live selling.")
        # Still save the sell orders for reference
        orders_path = os.path.join(CONFIG.data_dir, "pending_sells.json")
        os.makedirs(CONFIG.data_dir, exist_ok=True)
        with open(orders_path, "w") as f:
            json.dump(sell_orders, f, indent=2)
        lines.append(f"Sell orders saved to pending_sells.json")
    else:
        # Live mode: trigger sell via callback
        if on_sell_positions and sell_orders:
            try:
                results = on_sell_positions(sell_orders)
                lines.append(f"\n\u2705 {len(results)} sell order(s) placed!")
            except Exception as e:
                lines.append(f"\n\u274c Sell failed: <code>{_esc(str(e))}</code>")
        else:
            # Write sell orders for execution
            orders_path = os.path.join(CONFIG.data_dir, "pending_sells.json")
            os.makedirs(CONFIG.data_dir, exist_ok=True)
            with open(orders_path, "w") as f:
                json.dump(sell_orders, f, indent=2)
            lines.append(f"\n\U0001f4dd {len(sell_orders)} sell order(s) written to pending_sells.json")
            lines.append("Execute via bot or manually.")

    send_message("\n".join(lines))


def _format_position_summary(positions: list[dict]) -> str:
    """Format a brief summary of current positions."""
    lines = ["Current positions:"]
    for t in positions:
        deg = "\u00b0F" if t.get("unit") == "fahrenheit" else "\u00b0C"
        bucket = _esc(t.get("bucket_label", "?"))
        city = _esc(t.get("city_name", t.get("city", "?")))
        unr_pct = t.get("unrealized_pct")
        pct_str = f"{unr_pct:+.0%}" if unr_pct is not None else "?"
        lines.append(f"  {city} {bucket}{deg}: {pct_str}")
    return "\n".join(lines)


def _handle_tennis():
    """Handle /tennis command — trigger a tennis arb scan and show results."""
    if not CONFIG.strategy3_enabled:
        send_message("Strategy #3 (Tennis Arb) is disabled.")
        return

    send_message("Scanning tennis divergences...")

    if on_tennis_scan_request:
        try:
            signals = on_tennis_scan_request()
            if signals:
                send_tennis_signals(signals)
            else:
                tennis_tournaments = [t.strip() for t in CONFIG.tennis_tournaments.split(",")]
                send_message(
                    "<b>Strategy #3 — Tennis Arb</b>\n"
                    f"No divergences above {CONFIG.tennis_min_divergence:.0%} threshold.\n"
                    f"Tournaments: {', '.join(tennis_tournaments)}"
                )
        except Exception as e:
            logger.exception("Tennis scan failed")
            send_message(f"Tennis scan failed: <code>{_esc(str(e))}</code>")
    else:
        send_message("Tennis scan handler not configured.")


def _handle_help():
    """Handle /help command."""
    send_message(
        "<b>Polymarket Trading Bot \u2014 Commands</b>\n\n"
        "<b>Strategy #1 \u2014 Copy Trading</b>\n"
        "<code>/status</code> \u2014 Bot status, balance, positions\n"
        "<code>/pnl</code> \u2014 Unified P&amp;L across all strategies\n"
        "<code>/history</code> \u2014 Last 10 copy trades\n\n"
        "<b>Strategy #2 \u2014 Weather Betting</b>\n"
        "<code>/predict 11 Apr</code> \u2014 Run prediction for Apr 11\n"
        "<code>/predict</code> \u2014 Run prediction for default date\n"
        "<code>/takeprofit</code> \u2014 Close positions with &gt;30% profit\n\n"
        "<b>Strategy #3 \u2014 Tennis Arb</b>\n"
        "<code>/tennis</code> \u2014 Show current tennis divergences\n\n"
        "<code>/help</code> \u2014 Show this message\n\n"
        f"Strategy #1: {'ON' if CONFIG.strategy1_enabled else 'OFF'}\n"
        f"Strategy #2: {'ON' if CONFIG.strategy2_enabled else 'OFF'}\n"
        f"Strategy #3: {'ON' if CONFIG.strategy3_enabled else 'OFF'}\n"
        f"Take-profit threshold: {TAKE_PROFIT_PCT:.0%}"
    )


# --- Polling ---

def _poll_loop():
    """Poll Telegram for new messages."""
    last_update_id = 0

    # Flush stale updates
    try:
        url = f"{TELEGRAM_API.format(token=CONFIG.telegram_bot_token)}/getUpdates"
        resp = requests.get(url, params={"offset": -1, "timeout": 0}, timeout=10)
        if resp.ok:
            data = resp.json()
            results = data.get("result", [])
            if results:
                last_update_id = results[-1]["update_id"] + 1
    except Exception:
        pass

    logger.info("Telegram polling started")

    while not _stop_event.is_set():
        try:
            url = f"{TELEGRAM_API.format(token=CONFIG.telegram_bot_token)}/getUpdates"
            resp = requests.get(url, params={
                "offset": last_update_id,
                "timeout": 10,
                "allowed_updates": '["message"]',
            }, timeout=15)

            if not resp.ok:
                time.sleep(5)
                continue

            data = resp.json()
            for update in data.get("result", []):
                last_update_id = update["update_id"] + 1
                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "")

                if chat_id != CONFIG.telegram_chat_id:
                    continue

                if text.startswith("/"):
                    logger.info(f"Telegram command: {text}")
                    try:
                        _handle_command(text)
                    except Exception as e:
                        logger.exception(f"Command handler error: {e}")
                        send_message(f"Error: <code>{_esc(str(e))}</code>")

        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            logger.warning(f"Telegram poll error: {e}")
            time.sleep(5)


def _register_bot_menu():
    """Register all bot commands in the Telegram UI menu."""
    try:
        url = f"{TELEGRAM_API.format(token=CONFIG.telegram_bot_token)}/setMyCommands"
        requests.post(url, json={
            "commands": [
                {"command": "predict", "description": "Run weather prediction (e.g. /predict 11 Apr)"},
                {"command": "tennis", "description": "Show current tennis divergences"},
                {"command": "status", "description": "Balance, positions, daily limits"},
                {"command": "pnl", "description": "Unified P&L across all strategies"},
                {"command": "history", "description": "Last 10 copy trades"},
                {"command": "takeprofit", "description": "Close positions with >30% profit"},
                {"command": "help", "description": "Show all commands"},
            ],
        }, timeout=10)
    except Exception:
        pass  # non-critical -- menu just won't update


def start_polling():
    """Start telegram polling in a background thread."""
    global _poll_thread
    if not is_configured():
        logger.info("Telegram not configured, skipping poll")
        return
    _register_bot_menu()
    _stop_event.clear()
    _poll_thread = threading.Thread(target=_poll_loop, daemon=True, name="telegram-poll")
    _poll_thread.start()
    logger.info("Telegram polling thread started")


def stop_polling():
    """Stop telegram polling."""
    _stop_event.set()
    if _poll_thread:
        _poll_thread.join(timeout=15)
    logger.info("Telegram polling stopped")
