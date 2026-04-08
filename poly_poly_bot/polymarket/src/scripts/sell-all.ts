/**
 * Sell All Open Positions
 *
 * Fetches real positions from API, creates SELL orders for each.
 * Usage: npx tsx src/scripts/sell-all.ts
 */

import axios from "axios";
import { Side } from "@polymarket/clob-client";
import { CONFIG } from "../config";
import { logger } from "../logger";
import { ClobOrderResponse, DataApiPositionItem, errorMessage } from "../types";
import { createClobClient } from "../create-clob-client";

const DATA_API = CONFIG.dataApiUrl;

async function main(): Promise<void> {
  // Fetch real positions
  logger.info("Fetching positions...");
  const res = await axios.get(`${DATA_API}/positions`, {
    params: { user: CONFIG.proxyWallet },
    timeout: 15000,
  });

  const positions: DataApiPositionItem[] = Array.isArray(res.data)
    ? res.data.filter((p: DataApiPositionItem) => p.size > 0)
    : [];
  if (positions.length === 0) {
    logger.info("No open positions to sell.");
    return;
  }

  logger.info(`Found ${positions.length} open position(s):\n`);
  for (const p of positions) {
    logger.info(`  ${p.title} — ${p.outcome}: ${p.size} shares @ ${p.curPrice} (PnL: $${p.cashPnl?.toFixed(4)})`);
  }

  // Create CLOB client
  logger.info("\nAuthenticating...");
  const client = await createClobClient();

  // Sell each position
  for (const p of positions) {
    const tokenId = p.asset;
    const shares = p.size;
    const price = Math.max(0.01, p.curPrice * 0.98); // 2% below market for fast fill
    const roundedPrice = Math.round(price * 100) / 100;

    logger.info(`\nSelling ${shares} shares of "${p.title}" (${p.outcome}) @ ${roundedPrice}...`);

    try {
      const order = await client.createOrder({
        tokenID: tokenId,
        price: roundedPrice,
        size: shares,
        side: Side.SELL,
      });
      const result = await client.postOrder(order);
      const orderId = typeof result === "object" ? (result as ClobOrderResponse).orderID ?? "" : String(result);
      logger.info(`  Order placed: ${orderId}`);

      // Verify fill
      await new Promise((r) => setTimeout(r, 3000));
      try {
        const check = await client.getOrder(orderId);
        const matched = parseFloat(check.size_matched || "0");
        logger.info(`  Fill status: ${matched}/${shares} shares matched`);
      } catch {
        logger.warn("  Could not verify fill status");
      }
    } catch (err: unknown) {
      logger.error(`  Failed to sell: ${errorMessage(err)}`);
    }
  }

  logger.info("\nDone.");
}

main().catch((err: unknown) => {
  logger.error(`Fatal: ${errorMessage(err)}`);
  process.exit(1);
});
