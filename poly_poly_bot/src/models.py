"""Pydantic data models for trade data, API responses, and risk decisions."""

from __future__ import annotations
from typing import Optional, Literal
from pydantic import BaseModel


class DetectedTrade(BaseModel):
    """A trade detected from Data API or on-chain source."""
    id: str
    trader_address: str
    timestamp: str  # ISO 8601
    market: str
    condition_id: str = ""
    token_id: str = ""
    side: Literal["BUY", "SELL"] = "BUY"
    size: float = 0.0
    price: float = 0.0
    outcome: str = ""


class CopyDecision(BaseModel):
    """Result of risk evaluation for a detected trade."""
    should_copy: bool
    copy_size: float
    reason: Optional[str] = None


class TieredCopyDecision(CopyDecision):
    """Copy decision with tier information."""
    tier: str
    alert_only: bool = False


class OrderResult(BaseModel):
    """Result from placing an order on the CLOB."""
    order_id: str
    shares: float
    order_price: float


class FillResult(BaseModel):
    """Order fill verification result."""
    status: Literal["FILLED", "PARTIAL", "UNFILLED", "UNKNOWN"]
    filled_shares: float = 0.0
    filled_usd: float = 0.0
    fill_price: float = 0.0


class MarketSnapshot(BaseModel):
    """Live market bid/ask snapshot."""
    best_bid: float
    best_ask: float
    midpoint: float
    spread: float
    spread_bps: int
    fetched_at: float


class MarketMeta(BaseModel):
    """Market metadata cached for on-chain enrichment."""
    condition_id: str
    market: str
    outcome: str
    token_id: str


class QueuedTrade(BaseModel):
    """Trade in the detection → execution queue."""
    trade: DetectedTrade
    enqueued_at: float  # epoch ms
    source_detected_at: float  # epoch ms
    source: str = "data-api"


class PendingOrder(BaseModel):
    """Order in the execution → verification queue."""
    trade: DetectedTrade
    order_id: str
    order_price: float
    copy_size: float
    placed_at: float
    market_key: str
    side: Literal["BUY", "SELL"]
    source_detected_at: float
    enqueued_at: float
    order_submitted_at: float
    source: str = "data-api"
    tier: Optional[str] = None
    accounted_filled_shares: float = 0.0
    accounted_filled_usd: float = 0.0
    uncertain_cycles: int = 0


class TradeRecord(BaseModel):
    """Trade history record for JSONL audit trail."""
    timestamp: str
    trader_address: str = ""
    market: str = ""
    side: str = ""
    trader_size: float = 0.0
    copy_size: float = 0.0
    price: float = 0.0
    status: str = ""
    reason: Optional[str] = None
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    fill_shares: Optional[float] = None
    trader_price: Optional[float] = None
    source_detected_at: Optional[float] = None
    enqueued_at: Optional[float] = None
    order_submitted_at: Optional[float] = None
    first_fill_seen_at: Optional[float] = None
    source: Optional[str] = None
    drift_bps: Optional[int] = None
    spread_bps: Optional[int] = None
    condition_id: Optional[str] = None
    token_id: Optional[str] = None
    outcome: Optional[str] = None


class RedeemDetail(BaseModel):
    """Details for a redeemed position."""
    title: str
    shares: float
    cost_basis: float
    returned: float


class RedeemResult(BaseModel):
    """Result of position redemption."""
    count: int = 0
    markets: list[str] = []
    total_shares: float = 0.0
    details: list[RedeemDetail] = []
