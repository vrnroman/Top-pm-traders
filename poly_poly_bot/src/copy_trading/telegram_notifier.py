"""Telegram notification sender for copy-trading events."""

import httpx
from src.config import CONFIG
from src.logger import logger
from src.utils import error_message

BOT_TOKEN = CONFIG.telegram_bot_token
CHAT_ID = CONFIG.telegram_chat_id


def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _plain(text: str) -> str:
    """Drop the HTML tags and unescape entities, for the plain-text retry."""
    import re
    out = re.sub(r"<[^>]+>", "", text)
    return (out.replace("&lt;", "<").replace("&gt;", ">")
               .replace("&quot;", '"').replace("&amp;", "&"))


async def _send_message(text: str, kind: str | None = "deal") -> bool:
    """Send as HTML; on a rejected parse retry as plain text. Returns whether
    anything was delivered, and LOGS a rejection: a 400 that vanished unlogged
    is how a message that mattered never reached the owner.

    Everything the executor announces is about a real order, so the default
    class is DEAL; the process-level notices pass KIND_BOT explicitly. The
    class prefix and the research switch live in `telegram_bot.classify`, one
    rule for both senders."""
    if not BOT_TOKEN or not CHAT_ID:
        return False
    from src import telegram_bot as _tb
    text, deliver = _tb.classify(text, kind)
    if not deliver:
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"})
            if resp.status_code == 200:
                return True
            logger.warn(f"Telegram rejected an HTML send ({resp.status_code}); "
                        f"retrying as plain text")
            resp2 = await client.post(url, json={"chat_id": CHAT_ID, "text": _plain(text)})
            if resp2.status_code == 200:
                return True
            logger.warn(f"Telegram rejected the plain retry too ({resp2.status_code}): "
                        f"{resp2.text[:120]}")
            return False
    except Exception as err:
        logger.warn(f"Telegram send failed: {error_message(err)}")
        return False


class TelegramNotifier:
    async def trade_placed(self, market: str, side: str, size: float, price: float) -> None:
        prefix = "🔵 [PREVIEW]" if CONFIG.preview_mode else "🟢 [LIVE]"
        await _send_message(f'{prefix} <b>Order Placed</b>\n{side} ${size:.2f} on "{_escape_html(market)}" @ {price}')

    async def trade_filled(self, market: str, shares: float, price: float) -> None:
        prefix = "🔵 [PREVIEW]" if CONFIG.preview_mode else "✅ [LIVE]"
        await _send_message(f'{prefix} <b>Filled</b>\n{shares} shares (${shares * price:.2f}) on "{_escape_html(market)}" @ {price}')

    async def trade_unfilled(self, market: str) -> None:
        prefix = "🔵 [PREVIEW]" if CONFIG.preview_mode else "⚪ [LIVE]"
        await _send_message(f'{prefix} <b>Unfilled</b> — cancelled\n"{_escape_html(market)}"')

    async def trade_failed(self, market: str, reason: str) -> None:
        prefix = "🔵 [PREVIEW]" if CONFIG.preview_mode else "🔴 [LIVE]"
        await _send_message(f'{prefix} <b>Failed</b>\n"{_escape_html(market)}"\n{_escape_html(reason)}')

    async def _bot_kind(self, text: str) -> None:
        await _send_message(text, kind="bot")

    async def bot_started(self, traders: int, balance: float) -> None:
        mode = "[PREVIEW MODE]" if CONFIG.preview_mode else "[LIVE MODE]"
        await _send_message(f"🚀 <b>Bot Started {mode}</b>\n{traders} traders | ${balance:.2f} USDC", kind="bot")

    async def bot_error(self, error: str) -> None:
        await _send_message(f"⚠️ <b>Error</b>\n{_escape_html(error)}", kind="bot")

    async def positions_redeemed(self, count: int, details: list) -> None:
        lines = []
        for d in details:
            pnl = d.returned - d.cost_basis
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            icon = "✅" if d.returned > 0 else "❌"
            lines.append(f"• {icon} {_escape_html(d.title)} — {d.shares:.2f} sh → ${d.returned:.2f} ({pnl_str})")
        await _send_message(f"💰 <b>Redeemed {count} position(s)</b>\n" + "\n".join(lines))

    async def daily_summary(self, trades: int, pnl: str, balance: float) -> None:
        await _send_message(f"📊 <b>Daily Summary</b>\nTrades: {trades}\nP&L: {pnl}\nBalance: ${balance:.2f}")


telegram = TelegramNotifier()
