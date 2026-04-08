// Shared types for backtest-traders modules

export interface ActivityTrade {
  conditionId: string;
  timestamp: number;
  side: string;
  size: number;
  usdcSize: number;
  price: number;
  title: string;
  asset: string;
  outcome: string;
}

export interface MarketResolution {
  curPrice: number; // 1 = won, 0 = lost
  avgPrice: number;
}

export interface SimTrade {
  day: string;
  market: string;
  entryPrice: number;
  traderPrice: number;
  outcome: "WIN" | "LOSS" | "UNRESOLVED";
  pnl: number;
  traderPnl: number;
  slippage: number;
  filled: boolean;
}

export interface TraderBacktest {
  address: string;
  name: string;
  totalTrades: number;
  withinWindow: number;
  afterLimits: number;
  simulated: number;
  filled: number;
  simTrades: SimTrade[];
  totalPnl: number;
  traderTheoreticalPnl: number;
  winRate: number;
  avgSlippageCents: number;
  daysActive: number;
  skippedReasons: Record<string, number>;
}

// Bot simulation parameters
export const BT_CONFIG = {
  COPY_SIZE: 1.0,
  MAX_ORDER_SIZE: 1.0,
  MAX_MARKET_PER_DAY: 3.0,
  MAX_DAILY_VOLUME: 10.0,
  PRICE_BUFFER: 0.02,
  FILL_RATE: 0.75,
} as const;
