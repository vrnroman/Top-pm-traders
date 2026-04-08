import { ClobClient, Side } from "@polymarket/clob-client";
import { logger } from "./logger";
import { DetectedTrade } from "./trade-monitor";
import { getPosition } from "./inventory";
import { ClobOrderResponse } from "./types";
import { shortAddress, roundCents } from "./utils";
import { MarketSnapshot } from "./market-price";

export interface OrderResult {
  orderId: string;
  shares: number;
  orderPrice: number;
}

/** Place a copy order on the CLOB with adaptive pricing (live spread) or fixed 2% fallback. */
export async function executeCopyOrder(
  clobClient: ClobClient,
  trade: DetectedTrade,
  copySize: number,
  snapshot?: MarketSnapshot | null
): Promise<OrderResult> {
  const addr = shortAddress(trade.traderAddress);

  if (!trade.tokenId) {
    throw new Error(`No tokenId for trade on "${trade.market}" — cannot create order`);
  }

  const PRICE_BUFFER_BUY = 1.02;  // fallback: 2% more aggressive for BUY
  const PRICE_BUFFER_SELL = 0.98; // fallback: 2% more aggressive for SELL
  const MIN_PRICE = 0.01;
  const MAX_PRICE = 0.99;

  let orderPrice: number;
  if (snapshot && trade.side === "BUY") {
    // Adaptive: use best ask, cap at trader+2% to avoid overpaying
    orderPrice = Math.min(snapshot.bestAsk, trade.price * PRICE_BUFFER_BUY);
    orderPrice = Math.min(MAX_PRICE, orderPrice);
    logger.debug(`Adaptive BUY: bestAsk=${snapshot.bestAsk}, cap=${roundCents(trade.price * PRICE_BUFFER_BUY)}, using=${roundCents(orderPrice)}`);
  } else if (snapshot && trade.side === "SELL") {
    // Adaptive: use best bid, floor at trader-2% to avoid underselling
    orderPrice = Math.max(snapshot.bestBid, trade.price * PRICE_BUFFER_SELL);
    orderPrice = Math.max(MIN_PRICE, orderPrice);
    logger.debug(`Adaptive SELL: bestBid=${snapshot.bestBid}, floor=${roundCents(trade.price * PRICE_BUFFER_SELL)}, using=${roundCents(orderPrice)}`);
  } else {
    // Fallback: fixed buffer (no snapshot available)
    if (trade.side === "BUY") {
      orderPrice = Math.min(MAX_PRICE, trade.price * PRICE_BUFFER_BUY);
    } else {
      orderPrice = Math.max(MIN_PRICE, trade.price * PRICE_BUFFER_SELL);
    }
    logger.debug(`Fixed buffer: ${trade.side} @ ${roundCents(orderPrice)} (no market snapshot)`);
  }
  orderPrice = roundCents(orderPrice);
  if (orderPrice <= 0) {
    throw new Error(`Order price rounded to $0 (raw: ${trade.price}) on "${trade.market}"`);
  }

  // Round shares UP for BUY to ensure USD value meets CLOB minimum ($1).
  // roundCents can round down: 1.00/0.33 = 3.0303 → 3.03 → 3.03×0.33 = $0.9999 < $1 → rejected.
  // Math.ceil: 3.0303 → 3.04 → 3.04×0.33 = $1.0032 ✓
  const ceilCents = (n: number) => Math.ceil(n * 100) / 100;

  let shares: number;
  if (trade.side === "SELL") {
    const pos = getPosition(trade.tokenId);
    if (!pos || pos.shares <= 0) {
      throw new Error("No shares to sell");
    }
    const sellShares = roundCents(copySize / orderPrice);
    shares = Math.min(sellShares, pos.shares);
  } else {
    shares = ceilCents(copySize / orderPrice);
  }

  logger.trade(
    `Copying ${addr}: ${trade.side} $${copySize} (${shares} shares) on "${trade.market}" @ ${orderPrice} (trader @ ${trade.price})`
  );

  const orderPayload = {
    tokenID: trade.tokenId,
    price: orderPrice,
    size: shares,
    side: trade.side === "BUY" ? Side.BUY : Side.SELL,
  };

  const signedOrder = await clobClient.createOrder(orderPayload);
  const result = await clobClient.postOrder(signedOrder);

  const orderId = typeof result === "object" ? (result as ClobOrderResponse).orderID ?? "" : String(result);
  if (!orderId) {
    logger.debug(`postOrder response: ${JSON.stringify(result)}`);
  }
  logger.trade(`Order placed (pending fill): ${orderId} — ${shares} shares @ ${orderPrice}`);

  return { orderId, shares, orderPrice };
}
