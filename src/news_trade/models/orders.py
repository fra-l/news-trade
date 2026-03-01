"""Order and execution models."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class OrderStatus(StrEnum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class Order(BaseModel):
    """Represents an order placed via the Alpaca API.

    Created by the ExecutionAgent after the RiskManagerAgent approves
    a TradeSignal. Tracks the full order lifecycle.
    """

    order_id: str = Field(description="Internal order id")
    broker_order_id: str | None = Field(
        default=None, description="Alpaca-assigned order id"
    )
    signal_id: str = Field(description="Originating TradeSignal id")
    ticker: str
    side: OrderSide
    order_type: OrderType = Field(default=OrderType.MARKET)
    qty: int = Field(ge=1)
    limit_price: float | None = Field(default=None)
    stop_price: float | None = Field(default=None)
    status: OrderStatus = Field(default=OrderStatus.PENDING)
    filled_qty: int = Field(default=0, ge=0)
    filled_avg_price: float | None = Field(default=None)
    submitted_at: datetime | None = Field(default=None)
    filled_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
