import axios from "axios";
import { logger } from "./logger";
import { CONFIG } from "./config";
import { errorMessage } from "./types";

const BOT_TOKEN = CONFIG.telegramBotToken;
const CHAT_ID = CONFIG.telegramChatId;

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

async function sendMessage(text: string): Promise<void> {
  if (!BOT_TOKEN || !CHAT_ID) return;
  try {
    await axios.post(`https://api.telegram.org/bot${BOT_TOKEN}/sendMessage`, {
      chat_id: CHAT_ID,
      text,
      parse_mode: "HTML",
    }, { timeout: 10000 });
  } catch (err: unknown) {
    logger.warn(`Telegram send failed: ${errorMessage(err)}`);
  }
}

export const telegram = {
  // Trade events
  tradePlaced: (market: string, side: string, size: number, price: number) =>
    sendMessage(`${CONFIG.previewMode ? "🔵 [PREVIEW]" : "🟢 [LIVE]"} <b>Order Placed</b>\n${side} $${size.toFixed(2)} on "${escapeHtml(market)}" @ ${price}`),

  tradeFilled: (market: string, shares: number, price: number) =>
    sendMessage(`${CONFIG.previewMode ? "🔵 [PREVIEW]" : "✅ [LIVE]"} <b>Filled</b>\n${shares} shares ($${(shares * price).toFixed(2)}) on "${escapeHtml(market)}" @ ${price}`),

  tradeUnfilled: (market: string) =>
    sendMessage(`${CONFIG.previewMode ? "🔵 [PREVIEW]" : "⚪ [LIVE]"} <b>Unfilled</b> — cancelled\n"${escapeHtml(market)}"`),

  tradeFailed: (market: string, reason: string) =>
    sendMessage(`${CONFIG.previewMode ? "🔵 [PREVIEW]" : "🔴 [LIVE]"} <b>Failed</b>\n"${escapeHtml(market)}"\n${escapeHtml(reason)}`),

  // Bot lifecycle
  botStarted: (traders: number, balance: number) => {
    const mode = CONFIG.previewMode ? "[PREVIEW MODE]" : "[LIVE MODE]";
    return sendMessage(`🚀 <b>Bot Started ${mode}</b>\n${traders} traders | $${balance.toFixed(2)} USDC`);
  },

  botError: (error: string) =>
    sendMessage(`⚠️ <b>Error</b>\n${escapeHtml(error)}`),

  // Redemptions — details include cost basis and return for P&L display
  positionsRedeemed: (count: number, details: { title: string; shares: number; costBasis: number; returned: number }[]) => {
    const lines = details.map(d => {
      const pnl = d.returned - d.costBasis;
      const pnlStr = pnl >= 0 ? `+$${pnl.toFixed(2)}` : `-$${Math.abs(pnl).toFixed(2)}`;
      const icon = d.returned > 0 ? "✅" : "❌";
      return `• ${icon} ${escapeHtml(d.title)} — ${d.shares.toFixed(2)} sh → $${d.returned.toFixed(2)} (${pnlStr})`;
    });
    return sendMessage(`💰 <b>Redeemed ${count} position(s)</b>\n${lines.join("\n")}`);
  },

  // Daily summary
  dailySummary: (trades: number, pnl: string, balance: number) =>
    sendMessage(`📊 <b>Daily Summary</b>\nTrades: ${trades}\nP&L: ${pnl}\nBalance: $${balance.toFixed(2)}`),
};
