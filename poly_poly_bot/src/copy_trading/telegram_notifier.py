"""Telegram notification sender for copy-trading events."""

import httpx
from src.config import CONFIG
from src.logger import logger
from src.utils import error_message

BOT_TOKEN = CONFIG.telegram_bot_token
CHAT_ID = CONFIG.telegram_chat_id


def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


async def _send_message(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            )
    except Exception as err:
        logger.warn(f"Telegram send failed: {error_message(err)}")


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

    async def bot_started(self, traders: int, balance: float) -> None:
        mode = "[PREVIEW MODE]" if CONFIG.preview_mode else "[LIVE MODE]"
        await _send_message(f"🚀 <b>Bot Started {mode}</b>\n{traders} traders | ${balance:.2f} USDC")

    async def bot_error(self, error: str) -> None:
        await _send_message(f"⚠️ <b>Error</b>\n{_escape_html(error)}")

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
