from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FlowAlert(BaseModel):
    """Maps to the 'Flow Alert' schema in the UW OpenAPI spec."""

    ticker: str
    expiry: date
    strike: Decimal
    type: Literal["call", "put"]
    total_premium: Decimal
    total_size: int
    volume: int
    open_interest: int
    alert_rule: str
    trade_count: int
    underlying_price: Decimal | None = None
    has_sweep: bool = False
    has_floor: bool = False
    created_at: datetime | None = None

    @property
    def is_call(self) -> bool:
        return self.type == "call"


class SpotGEXByStrike(BaseModel):
    """
    Maps to 'Spot greek exposures by strike'.
    All numeric fields arrive as strings from the API.
    net_gex = call_gamma_oi + put_gamma_oi (put OI is already negative per spec sign convention).
    """

    price: Decimal  # strike price
    call_gamma_oi: Decimal
    put_gamma_oi: Decimal
    call_gamma_vol: Decimal | None = None
    put_gamma_vol: Decimal | None = None
    call_delta_oi: Decimal | None = None
    put_delta_oi: Decimal | None = None
    time: datetime | None = None

    @property
    def net_gex(self) -> Decimal:
        return self.call_gamma_oi + self.put_gamma_oi

    @property
    def strike(self) -> Decimal:
        return self.price


class MarketTide(BaseModel):
    """Maps to 'Daily Market Tide' schema."""

    timestamp: datetime
    net_call_premium: Decimal
    net_put_premium: Decimal
    net_volume: int


class DarkpoolPrint(BaseModel):
    """Maps to 'Darkpool Trade' schema."""

    ticker: str
    price: Decimal
    size: int
    premium: Decimal
    executed_at: datetime
    market_center: str
    canceled: bool = False
    volume: int | None = None


class NetPremTick(BaseModel):
    """Net premium tick for a single ticker."""

    timestamp: datetime
    net_call_premium: Decimal
    net_put_premium: Decimal
    net_volume: int | None = None


class OptionContract(BaseModel):
    """Option contract with greeks. Sourced from /stock/{ticker}/option-contracts or greeks."""

    ticker: str
    expiry: date
    strike: Decimal
    type: Literal["call", "put"]
    bid: Decimal
    ask: Decimal
    open_interest: int
    volume: int
    implied_volatility: Decimal | None = None
    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    vega: Decimal | None = None

    @property
    def is_call(self) -> bool:
        return self.type == "call"

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread_pct(self) -> Decimal:
        if self.mid == 0:
            return Decimal("999")
        return (self.ask - self.bid) / self.mid


class InterpolatedIVEntry(BaseModel):
    """
    One row from /stock/{ticker}/interpolated-iv.
    Each row represents a DTE horizon (1, 5, 7, 14, 30 days).
    percentile is 0-100 (higher = more expensive relative to 1-year history).
    """

    model_config = ConfigDict(populate_by_name=True)

    days: int
    volatility: Decimal
    percentile: Decimal  # 0-100
    implied_move_perc: Decimal | None = None
    trade_date: date | None = Field(default=None, alias="date")


class TechnicalPoint(BaseModel):
    """
    One data point from /stock/{ticker}/technical-indicator/{function}.
    Field names vary by indicator; optional fields cover RSI, MACD, BBANDS.
    """

    timestamp: str  # kept as string — format varies (date vs datetime)
    value: Decimal | None = None       # RSI, SMA, EMA and other single-value indicators
    macd: Decimal | None = None        # MACD line
    signal: Decimal | None = None      # MACD signal line
    histogram: Decimal | None = None   # MACD histogram
    upper_band: Decimal | None = None  # BBANDS
    middle_band: Decimal | None = None
    lower_band: Decimal | None = None


class QuotaStatus(BaseModel):
    """Tracks API quota state derived from response headers."""

    daily_limit: int = 0
    daily_remaining: int = 0
    per_minute_limit: int = 0
    per_minute_remaining: int = 0
    reset_at: datetime | None = None
