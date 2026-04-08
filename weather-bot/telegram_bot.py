"""Telegram bot integration for weather betting bot.

Commands:
  /predict 11 Apr    — Run prediction for a specific date
  /predict           — Run prediction for default date (today + DAYS_IN_ADVANCE)
  /status            — Show bot status (separate for Strategy #1 and #2)
  /pnl               — Show P&L: realized + unrealized (separate for S1/S2)
  /takeprofit        — Close all positions with unrealized PnL > 30%
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

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    STRATEGY1_ENABLED, STRATEGY2_ENABLED,
    CITIES_TO_BET, DAYS_IN_ADVANCE, MIN_EDGE, BET_SIZE,
    MAX_BETS_PER_CITY, PREVIEW_MODE, DATA_DIR,
    CLOB_API_URL, POLYMARKET_FEE,
)

logger = logging.getLogger("telegram")

SGT = timezone(timedelta(hours=8))

# Take-profit threshold: close when unrealized PnL > this % of cost
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.30"))


def _esc(text: str) -> str:
    """Escape HTML special characters for Telegram."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


TELEGRAM_API = "https://api.telegram.org/bot{token}"

_poll_thread: threading.Thread | None = None
_stop_event = threading.Event()

# Callbacks set by main.py
on_predict_request = None   # Callable[[datetime], list[dict]]
on_sell_positions = None    # Callable[[list[dict]], list[dict]]


def is_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN) and bool(TELEGRAM_CHAT_ID)


def send_message(text: str, parse_mode: str = "HTML"):
    """Send a message to the configured Telegram chat."""
    if not is_configured():
        return
    try:
        url = f"{TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN)}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
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
            f"No signals above {MIN_EDGE:.0%} edge threshold."
        )
        return

    lines = [
        f"<b>Strategy #2 — Weather Prediction</b>",
        f"Target: <b>{target_date}</b>",
        f"Mode: {'PREVIEW' if PREVIEW_MODE else 'LIVE'}",
        f"Edge threshold: {MIN_EDGE:.0%} | Bet: ${BET_SIZE:.0f}",
        "",
    ]

    total_ev = 0
    for s in signals:
        deg = "°F" if s.get("unit") == "fahrenheit" else "°C"
        emoji = "🟢" if s["edge"] >= 0.10 else "🟡"
        bucket = _esc(s['bucket_label'])
        lines.append(
            f"{emoji} <b>{_esc(s['city_name'])}</b> {bucket}{deg}\n"
            f"   Model: {s['model_prob']:.1%}  Market: {s['market_price']:.1%}  "
            f"Edge: <b>{s['edge']:+.1%}</b>  EV: ${s.get('expected_pnl', 0):.2f}"
        )
        total_ev += s.get("expected_pnl", 0)

    lines.append(f"\nTotal signals: {len(signals)} | Total EV: ${total_ev:.2f}")
    send_message("\n".join(lines))


# ─── Live price fetching ──────────────────────────────────────────────

def _fetch_midpoint(token_id: str) -> float | None:
    """Fetch current midpoint price for a YES token from CLOB API."""
    try:
        resp = requests.get(
            f"{CLOB_API_URL}/midpoint",
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
    history_path = os.path.join(os.path.dirname(__file__), "data", "weather_trades.jsonl")
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
        cost = t.get("cost") or (t.get("bet_size", BET_SIZE) * (1 + POLYMARKET_FEE))
        bet_size = t.get("bet_size", BET_SIZE)

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


# ─── Command handlers ─────────────────────────────────────────────────

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
            (today + timedelta(days=DAYS_IN_ADVANCE)).year,
            (today + timedelta(days=DAYS_IN_ADVANCE)).month,
            (today + timedelta(days=DAYS_IN_ADVANCE)).day,
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
    """Handle /status command."""
    now = datetime.now(SGT)
    lines = [
        f"<b>Bot Status</b>",
        f"Time: {now.strftime('%Y-%m-%d %H:%M SGT')}",
        f"Mode: {'PREVIEW' if PREVIEW_MODE else 'LIVE'}",
        "",
    ]

    lines.append(f"<b>Strategy #1 — Copy Traders</b>")
    if STRATEGY1_ENABLED:
        lines.append("Status: 🟢 ENABLED")
        lock_path = os.path.join(DATA_DIR, "..", "polymarket", "data", "bot.lock")
        if os.path.isdir(lock_path):
            lines.append("Process: Running")
        else:
            lines.append("Process: Not running")
    else:
        lines.append("Status: ⚪ DISABLED")

    lines.append("")
    lines.append(f"<b>Strategy #2 — Weather Betting</b>")
    if STRATEGY2_ENABLED:
        lines.append("Status: 🟢 ENABLED")
        lines.append(f"Cities: {', '.join(CITIES_TO_BET)}")
        lines.append(f"Days ahead: {DAYS_IN_ADVANCE}")
        lines.append(f"Min edge: {MIN_EDGE:.0%} | Bet: ${BET_SIZE:.0f}")
    else:
        lines.append("Status: ⚪ DISABLED")

    signals_dir = os.path.join(os.path.dirname(__file__), "results")
    if os.path.isdir(signals_dir):
        signal_files = sorted(
            [f for f in os.listdir(signals_dir) if f.startswith("signals_")],
            reverse=True,
        )[:3]
        if signal_files:
            lines.append(f"\nRecent signals: {', '.join(signal_files)}")

    send_message("\n".join(lines))


def _handle_pnl():
    """Handle /pnl command — realized + unrealized P&L, separate per strategy."""
    lines = ["<b>P&amp;L Report</b>", ""]

    # ── Strategy #1 ──
    lines.append("<b>Strategy #1 — Copy Traders</b>")
    if STRATEGY1_ENABLED:
        s1_history = os.path.join(os.path.dirname(__file__),
                                   "polymarket", "data", "trade-history.jsonl")
        if os.path.exists(s1_history):
            trades = []
            with open(s1_history) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            trades.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            if trades:
                total_cost = sum((t.get("cost") or 0) for t in trades)
                total_pnl = sum((t.get("pnl") or 0) for t in trades)
                n_trades = len(trades)
                lines.append(f"Trades: {n_trades}")
                lines.append(f"Total cost: ${total_cost:.2f}")
                lines.append(f"Realized PnL: ${total_pnl:.2f}")
            else:
                lines.append("No trades yet.")
        else:
            lines.append("No trade history.")
    else:
        lines.append("DISABLED")

    lines.append("")

    # ── Strategy #2 ──
    lines.append("<b>Strategy #2 — Weather Betting</b>")
    trades = _load_s2_trades()

    if not trades:
        lines.append("No bets yet.")
        send_message("\n".join(lines))
        return

    send_message("\n".join(lines) + "\nFetching live prices...")

    # Enrich with live prices
    trades = _enrich_with_live_prices(trades)

    # Split into resolved and open
    resolved = [t for t in trades if t.get("resolved")]
    open_positions = [t for t in trades if not t.get("resolved")]

    lines2 = [f"<b>Strategy #2 — Weather Betting</b>", ""]

    # Realized P&L (resolved bets)
    realized_pnl = sum((t.get("pnl") or 0) for t in resolved)
    realized_cost = sum((t.get("cost") or 0) for t in resolved)
    n_wins = sum(1 for t in resolved if t.get("won"))
    lines2.append(f"<b>Realized</b> ({len(resolved)} resolved, {n_wins} wins)")
    if resolved:
        lines2.append(f"  PnL: ${realized_pnl:.2f}")
        if realized_cost > 0:
            lines2.append(f"  ROI: {100 * realized_pnl / realized_cost:.1f}%")
    else:
        lines2.append("  None yet")

    lines2.append("")

    # Unrealized P&L (open positions)
    lines2.append(f"<b>Unrealized</b> ({len(open_positions)} open)")
    total_unrealized = 0
    total_open_cost = 0
    tp_candidates = 0

    for t in sorted(open_positions, key=lambda x: x.get("target_date", "")):
        deg = "°F" if t.get("unit") == "fahrenheit" else "°C"
        bucket = _esc(t.get("bucket_label", "?"))
        city = _esc(t.get("city_name", t.get("city", "?")))
        entry = t.get("entry_price", 0)
        current = t.get("current_price")
        cost = t.get("cost") or 0
        unr_pnl = t.get("unrealized_pnl")
        unr_pct = t.get("unrealized_pct")
        target = t.get("target_date", "?")

        total_open_cost += cost

        if current is not None and unr_pnl is not None:
            total_unrealized += unr_pnl
            pct_str = f"{unr_pct:+.0%}" if unr_pct is not None else "?"
            pnl_emoji = "📈" if unr_pnl >= 0 else "📉"
            tp_flag = " 🎯" if unr_pct is not None and unr_pct >= TAKE_PROFIT_PCT else ""
            if unr_pct is not None and unr_pct >= TAKE_PROFIT_PCT:
                tp_candidates += 1
            lines2.append(
                f"{pnl_emoji} {city} {bucket}{deg} ({target})\n"
                f"   Entry: {entry:.1%} → Now: {current:.1%}  "
                f"PnL: ${unr_pnl:+.2f} ({pct_str}){tp_flag}"
            )
        else:
            lines2.append(
                f"❓ {city} {bucket}{deg} ({target})\n"
                f"   Entry: {entry:.1%} → Now: N/A"
            )

    lines2.append("")
    lines2.append(f"<b>Total unrealized: ${total_unrealized:+.2f}</b>")
    if total_open_cost > 0:
        lines2.append(f"Open cost: ${total_open_cost:.2f} | "
                       f"Unr. ROI: {100 * total_unrealized / total_open_cost:+.1f}%")

    lines2.append("")
    lines2.append(f"<b>Combined: ${realized_pnl + total_unrealized:+.2f}</b>")

    if tp_candidates > 0:
        lines2.append(f"\n🎯 {tp_candidates} position(s) above {TAKE_PROFIT_PCT:.0%} profit — "
                       f"use /takeprofit to close")

    if PREVIEW_MODE:
        lines2.append(f"\n<i>PREVIEW MODE — positions are simulated</i>")

    send_message("\n".join(lines2))


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
        f"<b>Take Profit — {len(candidates)} position(s)</b>",
        f"Threshold: {TAKE_PROFIT_PCT:.0%} of cost",
        "",
    ]

    total_revenue = 0
    sell_orders = []
    for t in candidates:
        deg = "°F" if t.get("unit") == "fahrenheit" else "°C"
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
            f"🎯 {city} {bucket}{deg} ({t.get('target_date', '?')})\n"
            f"   {shares:.1f} shares @ {t.get('entry_price', 0):.1%} → "
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

    if PREVIEW_MODE:
        lines.append(f"\n<i>PREVIEW MODE — orders NOT placed</i>")
        lines.append("Set PREVIEW_MODE=false to enable live selling.")
        # Still save the sell orders for reference
        orders_path = os.path.join(os.path.dirname(__file__), "data", "pending_sells.json")
        os.makedirs(os.path.dirname(orders_path), exist_ok=True)
        with open(orders_path, "w") as f:
            json.dump(sell_orders, f, indent=2)
        lines.append(f"Sell orders saved to pending_sells.json")
    else:
        # Live mode: trigger sell via callback
        if on_sell_positions and sell_orders:
            try:
                results = on_sell_positions(sell_orders)
                lines.append(f"\n✅ {len(results)} sell order(s) placed!")
            except Exception as e:
                lines.append(f"\n❌ Sell failed: <code>{_esc(str(e))}</code>")
        else:
            # Write sell orders for the TS bot to execute
            orders_path = os.path.join(os.path.dirname(__file__), "data", "pending_sells.json")
            os.makedirs(os.path.dirname(orders_path), exist_ok=True)
            with open(orders_path, "w") as f:
                json.dump(sell_orders, f, indent=2)
            lines.append(f"\n📝 {len(sell_orders)} sell order(s) written to pending_sells.json")
            lines.append("Execute via TS bot or manually.")

    send_message("\n".join(lines))


def _format_position_summary(positions: list[dict]) -> str:
    """Format a brief summary of current positions."""
    lines = ["Current positions:"]
    for t in positions:
        deg = "°F" if t.get("unit") == "fahrenheit" else "°C"
        bucket = _esc(t.get("bucket_label", "?"))
        city = _esc(t.get("city_name", t.get("city", "?")))
        unr_pct = t.get("unrealized_pct")
        pct_str = f"{unr_pct:+.0%}" if unr_pct is not None else "?"
        lines.append(f"  {city} {bucket}{deg}: {pct_str}")
    return "\n".join(lines)


def _handle_help():
    """Handle /help command."""
    send_message(
        "<b>Weather Betting Bot — Commands</b>\n\n"
        "<code>/predict 11 Apr</code> — Run prediction for Apr 11\n"
        "<code>/predict</code> — Run prediction for default date\n"
        "<code>/status</code> — Show bot status\n"
        "<code>/pnl</code> — Realized + unrealized P&amp;L\n"
        "<code>/takeprofit</code> — Close positions with &gt;30% profit\n"
        "<code>/help</code> — Show this message\n\n"
        f"Strategy #1 (Copy): {'ON' if STRATEGY1_ENABLED else 'OFF'}\n"
        f"Strategy #2 (Weather): {'ON' if STRATEGY2_ENABLED else 'OFF'}\n"
        f"Take-profit threshold: {TAKE_PROFIT_PCT:.0%}"
    )


# ─── Polling ──────────────────────────────────────────────────────────

def _poll_loop():
    """Poll Telegram for new messages."""
    last_update_id = 0

    # Flush stale updates
    try:
        url = f"{TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN)}/getUpdates"
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
            url = f"{TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN)}/getUpdates"
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

                if chat_id != TELEGRAM_CHAT_ID:
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


def start_polling():
    """Start telegram polling in a background thread."""
    global _poll_thread
    if not is_configured():
        logger.info("Telegram not configured, skipping poll")
        return
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
