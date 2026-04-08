import { ClobClient } from "@polymarket/clob-client";
import { FILL_CHECK_DELAY_MS, FILL_CHECK_RETRIES } from "./constants";
import { logger } from "./logger";
import { errorMessage } from "./types";

export interface FillResult {
  status: "FILLED" | "PARTIAL" | "UNFILLED" | "UNKNOWN";
  filledShares: number;
  filledUsd: number;
  fillPrice: number;
}

/** Poll CLOB for order fill status with retries. Returns FILLED, PARTIAL, UNFILLED, or UNKNOWN. */
export async function verifyOrderFill(clobClient: ClobClient, orderId: string): Promise<FillResult> {
  for (let attempt = 0; attempt <= FILL_CHECK_RETRIES; attempt++) {
    await new Promise((r) => setTimeout(r, FILL_CHECK_DELAY_MS));
    try {
      const order = await clobClient.getOrder(orderId);
      const originalSize = parseFloat(order.original_size || "0");
      const sizeMatched = parseFloat(order.size_matched || "0");
      const price = parseFloat(order.price || "0");

      // If API returns 0/empty original_size but has matched shares, treat as FILLED
      if (sizeMatched > 0 && originalSize <= 0) {
        return { status: "FILLED", filledShares: sizeMatched, filledUsd: sizeMatched * price, fillPrice: price };
      }
      if (sizeMatched >= originalSize && originalSize > 0) {
        return { status: "FILLED", filledShares: sizeMatched, filledUsd: sizeMatched * price, fillPrice: price };
      }
      if (sizeMatched > 0) {
        return { status: "PARTIAL", filledShares: sizeMatched, filledUsd: sizeMatched * price, fillPrice: price };
      }
      if (attempt < FILL_CHECK_RETRIES) continue;
      return { status: "UNFILLED", filledShares: 0, filledUsd: 0, fillPrice: 0 };
    } catch (err: unknown) {
      logger.warn(`Failed to check order ${orderId}: ${errorMessage(err)}`);
      if (attempt < FILL_CHECK_RETRIES) continue;
      return { status: "UNKNOWN", filledShares: 0, filledUsd: 0, fillPrice: 0 };
    }
  }
  return { status: "UNFILLED", filledShares: 0, filledUsd: 0, fillPrice: 0 };
}
