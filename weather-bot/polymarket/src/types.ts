// Polymarket Data API response shapes (observed from actual API calls)

export interface DataApiActivityItem {
  id?: string;
  transactionHash?: string;
  conditionId?: string;
  asset?: string;
  assetId?: string;
  tokenId?: string;
  side?: string;
  size?: string;
  usdcSize?: string;
  amount?: string;
  price?: string;
  title?: string;
  market?: string;
  slug?: string;
  outcome?: string;
  outcomeName?: string;
  timestamp?: number | string;
  createdAt?: string;
}

export interface DataApiPositionItem {
  asset: string;
  size: number;
  avgPrice: number;
  curPrice: number;
  conditionId: string;
  title: string;
  outcome?: string;
  cashPnl?: number;
  redeemable?: boolean;
}

export interface ClobApiKeyResponse {
  apiKey?: string;
  key?: string;
  secret: string;
  passphrase: string;
}

export interface ClobOrderResponse {
  orderID?: string;
  [key: string]: unknown;
}

/** Extract a human-readable message from an unknown caught error. */
export function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
