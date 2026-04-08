// Source abstraction for trade detection — data-api, onchain, or hybrid mode

export interface TradeSource {
  name: string;
  start(): Promise<void>;
  stop(): void;
}

import { DataApiSource } from "./data-api-source";
import { OnchainSource } from "./onchain-source";

/** Create trade sources based on TRADE_MONITOR_MODE config. */
export function createSources(mode: string): TradeSource[] {
  switch (mode) {
    case "onchain":
      return [new OnchainSource()];
    case "hybrid":
      return [new OnchainSource(), new DataApiSource()];
    default:
      return [new DataApiSource()];
  }
}
