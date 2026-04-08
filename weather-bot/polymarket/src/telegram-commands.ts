import axios from "axios";
import { ClobClient } from "@polymarket/clob-client";
import { CONFIG } from "./config";
import { logger } from "./logger";
import { errorMessage } from "./types";
import { getUsdcBalance } from "./get-balance";
import { getPositions } from "./inventory";
import { getRiskStatus } from "./risk-manager";

let _clobClient: ClobClient | null = null;

const BOT_TOKEN = CONFIG.telegramBotToken;
const CHAT_ID = CONFIG.telegramChatId;
const POLL_INTERVAL_MS = 3000;

let lastUpdateId = 0;
let pollTimer: ReturnType<typeof setInterval> | null = null;

interface TelegramUpdate {
  update_id: number;
  message?: {
    chat: { id: number };
    text?: string;
  };
}

async function sendReply(chatId: number, text: string): Promise<void> {
  try {
    await axios.post(`https://api.telegram.org/bot${BOT_TOKEN}/sendMessage`, {
      chat_id: chatId,
      text,
      parse_mode: "HTML",
    }, { timeout: 10000 });
  } catch (err: unknown) {
    logger.warn(`Telegram reply failed: ${errorMessage(err)}`);
  }
}

async function handleStatus(chatId: number): Promise<void> {
  const balance = await getUsdcBalance();
  const positions = getPositions();
  const risk = getRiskStatus();

  const lines = [
    `📊 <b>Bot Status</b>`,
    ``,
    `💰 Balance: $${balance >= 0 ? balance.toFixed(2) : "unknown"}`,
    `📈 ${risk}`,
  ];

  if (positions.length === 0) {
    lines.push(`📦 No open positions`);
  } else {
    lines.push(`📦 <b>Positions (${positions.length}):</b>`);
    for (const p of positions) {
      const value = (p.shares * p.avgPrice).toFixed(2);
      lines.push(`  • ${p.market}: ${p.shares.toFixed(2)} sh @ ${p.avgPrice.toFixed(2)} ($${value})`);
    }
  }

  await sendReply(chatId, lines.join("\n"));
}

interface ApiPosition {
  title: string;
  outcome: string;
  size: number;
  avgPrice: number;
  curPrice: number;
  cashPnl: number;
  redeemable: boolean;
}

async function fetchPositionsFromApi(): Promise<ApiPosition[]> {
  const res = await axios.get(`${CONFIG.dataApiUrl}/positions`, {
    params: { user: CONFIG.proxyWallet },
    timeout: 15000,
  });
  if (!Array.isArray(res.data)) return [];
  return res.data
    .filter((p: { size?: number }) => (p.size ?? 0) > 0)
    .map((p: Record<string, unknown>) => ({
      title: String(p.title || "?"),
      outcome: String(p.outcome || "?"),
      size: Number(p.size) || 0,
      avgPrice: Number(p.avgPrice) || 0,
      curPrice: Number(p.curPrice) || 0,
      cashPnl: Number(p.cashPnl) || 0,
      redeemable: Boolean(p.redeemable),
    }));
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

async function handlePnl(chatId: number): Promise<void> {
  try {
    let positions: ApiPosition[] = [];

    if (CONFIG.previewMode) {
      const localPositions = getPositions();
      if (localPositions.length > 0) {
        // Fetch current prices from CLOB API (midpoint for open markets, market data for resolved)
        const priceMap: Record<string, { price: number; resolved: boolean; winner: boolean }> = {};
        await Promise.all(
          localPositions.map(async (p) => {
            try {
              // Try midpoint first (works for open markets with active orderbooks)
              const res = await axios.get(`${CONFIG.clobApiUrl}/midpoint`, {
                params: { token_id: p.tokenId },
                timeout: 5000,
              });
              if (res.data?.mid) {
                priceMap[p.tokenId] = { price: parseFloat(res.data.mid), resolved: false, winner: false };
                return;
              }
            } catch { /* orderbook may not exist for resolved markets */ }
            try {
              // Fallback: fetch market data to check resolution status
              const mktRes = await axios.get(`${CONFIG.clobApiUrl}/markets/${p.marketKey}`, { timeout: 5000 });
              if (mktRes.data?.tokens) {
                const token = mktRes.data.tokens.find((t: Record<string, unknown>) => String(t.token_id) === p.tokenId);
                if (token) {
                  priceMap[p.tokenId] = {
                    price: parseFloat(String(token.price)) || 0,
                    resolved: !!mktRes.data.closed,
                    winner: !!token.winner,
                  };
                  return;
                }
              }
            } catch { /* ignore */ }
            priceMap[p.tokenId] = { price: 0, resolved: false, winner: false };
          })
        );

        positions = localPositions.map((p) => {
          const info = priceMap[p.tokenId] || { price: 0, resolved: false, winner: false };
          const cashPnl = (info.price - p.avgPrice) * p.shares;
          return {
            title: p.market,
            outcome: info.resolved ? (info.winner ? "WON" : "LOST") : "OPEN",
            size: p.shares * p.avgPrice,
            avgPrice: p.avgPrice,
            curPrice: info.price,
            cashPnl,
            redeemable: info.winner,
          };
        });
      }
    } else {
      positions = await fetchPositionsFromApi();
    }
    
    if (positions.length === 0) {
      await sendReply(chatId, `📊 <b>P&L</b>\n\nNo open positions${CONFIG.previewMode ? " (Preview)" : ""}.`);
      return;
    }

    let totalPnl = 0;
    let totalInvested = 0;
    const lines = [`📊 <b>P&L Report${CONFIG.previewMode ? " (Preview)" : ""}</b>\n`];

    for (const p of positions) {
      totalPnl += p.cashPnl;
      totalInvested += p.size;
      const pnlStr = p.cashPnl >= 0 ? `+$${p.cashPnl.toFixed(2)}` : `-$${Math.abs(p.cashPnl).toFixed(2)}`;
      const icon = p.curPrice < 0.01 ? "❌" : p.cashPnl >= 0 ? "✅" : "⚠️";
      lines.push(`${icon} <b>${escapeHtml(p.title)}</b>`);
      lines.push(`   ${p.outcome}: ${p.size.toFixed(2)} USD | $${p.avgPrice.toFixed(2)} → $${p.curPrice.toFixed(3)} | ${pnlStr}`);
    }

    const totalStr = totalPnl >= 0 ? `+$${totalPnl.toFixed(2)}` : `-$${Math.abs(totalPnl).toFixed(2)}`;
    lines.push(`\n<b>Total:</b> ${totalStr} internal ($${totalInvested.toFixed(2)} invested)`);

    await sendReply(chatId, lines.join("\n"));
  } catch (err: unknown) {
    await sendReply(chatId, `❌ Failed to generate P&L report: ${errorMessage(err)}`);
  }
}

async function handleHistory(chatId: number): Promise<void> {
  try {
    const fs = await import("fs");
    const path = await import("path");
    const historyFile = path.resolve(process.cwd(), "data", "trade-history.jsonl");
    if (!fs.existsSync(historyFile)) {
      await sendReply(chatId, "📜 No trade history yet.");
      return;
    }
    const allLines = fs.readFileSync(historyFile, "utf8").trim().split(/\r?\n/).filter(Boolean);
    const recent = allLines.slice(-10).reverse(); // last 10, newest first
    const lines = [`📜 <b>Recent Trades (${Math.min(10, allLines.length)}/${allLines.length})</b>\n`];

    for (const raw of recent) {
      try {
        const t = JSON.parse(raw);
        const icon = t.status === "filled" ? "✅" : t.status === "partial" ? "🟡" : t.status === "skipped" ? "⏭" : t.status === "preview" ? "👁" : "❌";
        const time = t.timestamp ? new Date(t.timestamp).toISOString().slice(11, 16) : "??:??";
        lines.push(`${icon} ${time} ${t.side || "?"} $${(t.copySize || 0).toFixed(2)} "${escapeHtml(t.market || "?")}" — ${t.status}${t.reason ? ` (${escapeHtml(t.reason.slice(0, 30))})` : ""}`);
      } catch { /* skip malformed */ }
    }

    await sendReply(chatId, lines.join("\n"));
  } catch (err: unknown) {
    await sendReply(chatId, `❌ Failed to read history: ${errorMessage(err)}`);
  }
}

async function pollUpdates(): Promise<void> {
  try {
    const res = await axios.get(`https://api.telegram.org/bot${BOT_TOKEN}/getUpdates`, {
      params: { offset: lastUpdateId + 1, timeout: 0 },
      timeout: 10000,
    });

    const updates: TelegramUpdate[] = res.data?.result ?? [];
    for (const update of updates) {
      lastUpdateId = update.update_id;
      const text = update.message?.text?.trim();
      const chatId = update.message?.chat?.id;
      if (!text || !chatId) continue;

      // Only respond to our configured chat
      if (String(chatId) !== CHAT_ID) continue;

      if (text === "/status") {
        await handleStatus(chatId);
      } else if (text === "/pnl") {
        await handlePnl(chatId);
      } else if (text === "/history") {
        await handleHistory(chatId);
      }
    }
  } catch {
    // Silently ignore poll errors — will retry next interval
  }
}

/** Register bot menu commands in Telegram UI. */
async function registerBotMenu(): Promise<void> {
  try {
    await axios.post(`https://api.telegram.org/bot${BOT_TOKEN}/setMyCommands`, {
      commands: [
        { command: "status", description: "Balance, positions, daily limits" },
        { command: "pnl", description: "Live P&L with current prices" },
        { command: "history", description: "Last 10 trades (filled/failed/skipped)" },
      ],
    }, { timeout: 10000 });
  } catch { /* non-critical — menu just won't update */ }
}

/** Flush stale updates so we don't process commands sent while bot was offline. */
async function flushPendingUpdates(): Promise<void> {
  try {
    const res = await axios.get(`https://api.telegram.org/bot${BOT_TOKEN}/getUpdates`, {
      params: { offset: -1, timeout: 0 },
      timeout: 10000,
    });
    const updates: TelegramUpdate[] = res.data?.result ?? [];
    if (updates.length > 0) {
      lastUpdateId = updates[updates.length - 1].update_id;
    }
  } catch { /* ignore — first real poll will handle it */ }
}

/** Start polling Telegram for bot commands. Call once after bot startup. */
export async function startTelegramCommands(clobClient?: ClobClient): Promise<void> {
  if (clobClient) _clobClient = clobClient;
  if (!BOT_TOKEN || !CHAT_ID) return;
  await registerBotMenu();
  await flushPendingUpdates();
  pollTimer = setInterval(pollUpdates, POLL_INTERVAL_MS);
  logger.info("Telegram commands active (/status, /pnl, /history)");
}

/** Stop polling. Call on shutdown. */
export function stopTelegramCommands(): void {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}
